"""Facade that ties itemised terms, vouchers, ledger, limiter and audit together.

One object owns the accounting policy and is the only place a registration turns
into both a voucher and a ledger integral. Because both artifacts are derived
from the same returned figure in one call, they cannot drift apart.
"""

from __future__ import annotations

import json

from coldstore.audit import Auditor
from coldstore.store.store import Store

from .ledger import Ledger, VoucherBook
from .limiter import RateLimiter
from .report import (
    DEFAULT_AMBIENT,
    DEFAULT_DEFROST_POWER,
    DEFAULT_DISCHARGE_SECONDS,
    DEFAULT_MAX_SPAN_BUCKETS,
    DEFAULT_PRODUCT_CP,
    DEFAULT_RATE_BURST,
    DEFAULT_RATE_PER_HOUR,
    DEFAULT_ROOM_TEMP,
    DEFAULT_U_AREA,
    EPS,
    POLICY,
    AccountingParams,
    LoadBucket,
    Reconciliation,
    Voucher,
    validate_duration,
    validate_epoch,
    validate_mass,
    validate_non_negative,
    validate_positive,
    validate_temperature,
)
from .terms import (
    defrost_energy_kwh,
    envelope_power_kw,
    hour_of,
    inbound_energy,
    split_pulse,
    split_ramp,
)

PARAMS_KEY = "accounting:params"


class Registry:
    """Itemised load accounting for one persisted store."""

    def __init__(self, store: Store, auditor: Auditor) -> None:
        self._store = store
        self._auditor = auditor
        self.ledger = Ledger(store)
        self.vouchers = VoucherBook(store)

    # -- parameters -------------------------------------------------------

    def params(self) -> AccountingParams:
        defaults = _default_params().as_dict()
        raw, present = self._store.get(PARAMS_KEY)
        if present:
            try:
                loaded = json.loads(raw)
            except ValueError:
                loaded = {}
            if isinstance(loaded, dict):
                defaults.update({key: value for key, value in loaded.items() if key in defaults})
        return AccountingParams(**defaults)  # type: ignore[arg-type]

    def _save_params(self, params: AccountingParams) -> None:
        self._store.put(PARAMS_KEY, json.dumps(params.as_dict(), sort_keys=True))

    def set_params(
        self,
        *,
        product_cp: float | None = None,
        room_temp: float | None = None,
        u_area: float | None = None,
        ambient: float | None = None,
        defrost_power: float | None = None,
        discharge_seconds: int | None = None,
        rate_per_hour: float | None = None,
        rate_burst: float | None = None,
        max_span_buckets: int | None = None,
    ) -> AccountingParams:
        current = self.params()
        values = AccountingParams(
            product_cp=current.product_cp if product_cp is None else product_cp,
            room_temp=current.room_temp if room_temp is None else room_temp,
            u_area=current.u_area if u_area is None else u_area,
            ambient=current.ambient if ambient is None else ambient,
            defrost_power=current.defrost_power if defrost_power is None else defrost_power,
            discharge_seconds=current.discharge_seconds if discharge_seconds is None else discharge_seconds,
            rate_per_hour=current.rate_per_hour if rate_per_hour is None else rate_per_hour,
            rate_burst=current.rate_burst if rate_burst is None else rate_burst,
            max_span_buckets=current.max_span_buckets if max_span_buckets is None else max_span_buckets,
        )
        validate_positive(values.product_cp, "product_cp")
        validate_temperature(values.room_temp)
        validate_non_negative(values.u_area, "u_area")
        validate_temperature(values.ambient)
        validate_positive(values.defrost_power, "defrost_power")
        validate_duration(values.discharge_seconds, "discharge_seconds")
        validate_positive(values.rate_per_hour, "rate_per_hour")
        validate_positive(values.rate_burst, "rate_burst")
        if values.max_span_buckets < 1:
            raise ValueError("max_span_buckets must be positive")
        self._save_params(values)
        return values

    def _limiter(self, params: AccountingParams | None = None) -> RateLimiter:
        params = params or self.params()
        return RateLimiter(self._store, params.rate_per_hour, params.rate_burst)

    def limiter_state(self, at: int) -> dict[str, object]:
        params = self.params()
        limiter = self._limiter(params)
        return {
            "tokens": limiter.tokens(at),
            "rate_per_hour": params.rate_per_hour,
            "burst": params.rate_burst,
            "at": at,
        }

    # -- registrations ----------------------------------------------------

    def register_batch(
        self,
        batch_id: str,
        at: int,
        mass_kg: float,
        product_temp: float,
        *,
        discharge_seconds: int | None = None,
    ) -> tuple[Voucher, LoadBucket | None]:
        """Register one inbound batch through the limiter into the ledger.

        Returns the voucher and the hour bucket the energy landed in. A rejected
        batch yields a non-accepted voucher, no ledger change and no receipt.
        """

        if not batch_id:
            raise ValueError("batch_id is required")
        validate_epoch(at)
        validate_mass(mass_kg)
        validate_temperature(product_temp)
        params = self.params()
        duration = params.discharge_seconds if discharge_seconds is None else discharge_seconds
        validate_duration(duration, "discharge_seconds")

        if self.vouchers.contains(batch_id):
            raise ValueError(f"batch {batch_id} already registered")

        decision = self._limiter(params).admit(at)
        if not decision.accepted:
            voucher = Voucher(
                batch_id=batch_id,
                at=at,
                mass_kg=mass_kg,
                product_temp=product_temp,
                discharge_seconds=duration,
                energy_kwh=0.0,
                accepted=False,
                reason=decision.reason,
                policy=POLICY["version"],
            )
            self._auditor.record(
                "accounting-voucher",
                json.dumps({"batch_id": batch_id, "accepted": False, "reason": decision.reason}, sort_keys=True),
            )
            return voucher, None

        energy = inbound_energy(mass_kg, product_temp, params.room_temp, params.product_cp)
        peak_kw = energy * 3600.0 / duration
        voucher = Voucher(
            batch_id=batch_id,
            at=at,
            mass_kg=mass_kg,
            product_temp=product_temp,
            discharge_seconds=duration,
            energy_kwh=energy,
            accepted=True,
            reason="accepted",
            policy=POLICY["version"],
        )
        self.vouchers.add(voucher)
        hour = hour_of(at)
        segments = list(split_pulse(at, energy, peak_kw, params.max_span_buckets))
        self.ledger.fold_many("inbound", segments, count=1)

        bucket = next((item for item in self.ledger.range(hour, hour)), None)
        self._auditor.record(
            "accounting-voucher",
            json.dumps(
                {"batch_id": batch_id, "at": at, "energy_kwh": energy, "hour": hour},
                sort_keys=True,
            ),
        )
        self._auditor.record(
            "accounting-ledger",
            json.dumps(
                {"term": "inbound", "hour": hour, "energy_kwh": energy, "batch_id": batch_id},
                sort_keys=True,
            ),
        )
        return voucher, bucket

    # -- defrost ----------------------------------------------------------

    def record_defrost(self, room_id: str, start: int, end: int) -> tuple[float, list[LoadBucket]]:
        """Integrate one constant-power defrost interval across its hours."""

        if not room_id:
            raise ValueError("room_id is required")
        validate_epoch(start)
        if end < start:
            raise ValueError("defrost end must not precede its start")
        params = self.params()
        power = params.defrost_power
        energy = defrost_energy_kwh(power, end - start)
        segments = split_ramp(start, power, end, power, params.max_span_buckets)
        self.ledger.fold_many("defrost", segments, count=1)
        start_hour, end_hour = hour_of(start), hour_of(end)
        buckets = self.ledger.range(start_hour, end_hour)
        for hour, part_energy, _ in segments:
            self._auditor.record(
                "accounting-ledger",
                json.dumps(
                    {
                        "term": "defrost",
                        "room_id": room_id,
                        "hour": hour,
                        "energy_kwh": part_energy,
                        "start": start,
                        "end": end,
                    },
                    sort_keys=True,
                ),
            )
        return energy, buckets

    # -- envelope ---------------------------------------------------------

    def record_envelope(self, at: int, ambient: float | None = None) -> list[LoadBucket]:
        """Integrate envelope gain from the previous ambient sample to ``at``."""

        validate_epoch(at)
        params = self.params()
        ambient = params.ambient if ambient is None else ambient
        validate_temperature(ambient)
        power = envelope_power_kw(ambient, params.room_temp, params.u_area)
        previous = self.ledger.last_envelope_sample()
        hours: list[int] = [hour_of(at)]
        if previous is not None:
            last_at, last_power = previous
            if at < last_at:
                raise ValueError("ambient sample precedes the previous sample")
            segments = split_ramp(last_at, last_power, at, power, params.max_span_buckets)
            self.ledger.fold_many("envelope", segments, count=1)
            hours = [hour_of(last_at), hour_of(at)]
        self.ledger.save_envelope_sample(at, power)
        buckets = self.ledger.range(min(hours), max(hours))
        self._auditor.record(
            "accounting-ledger",
            json.dumps({"term": "envelope", "hour": hour_of(at), "ambient": ambient}, sort_keys=True)
        )
        return buckets

    # -- reads ------------------------------------------------------------

    def load_range(self, start: int, end: int) -> list[LoadBucket]:
        validate_epoch(start)
        if end < start:
            raise ValueError("range end must not precede its start")
        return self.ledger.range(hour_of(start), hour_of(end))

    def reconcile(self) -> Reconciliation:
        return self.ledger.reconcile(self.vouchers)

    def state(self, start: int | None = None, end: int | None = None) -> dict[str, object]:
        hours = self.ledger.hours()
        if hours:
            start_hour = hours[0] if start is None else hour_of(start)
            end_hour = hours[-1] if end is None else hour_of(end)
        else:
            anchor = start or end or 0
            start_hour = end_hour = hour_of(anchor) if anchor else 0
        if end_hour < start_hour:
            start_hour, end_hour = end_hour, start_hour
        buckets = self.ledger.range(start_hour, end_hour)
        totals = {
            "inbound_kwh": sum(item.inbound_kwh for item in buckets),
            "envelope_kwh": sum(item.envelope_kwh for item in buckets),
            "defrost_kwh": sum(item.defrost_kwh for item in buckets),
            "total_kwh": sum(item.total_kwh for item in buckets),
        }
        peak = max((item.peak_kw_upper_bound for item in buckets), default=0.0)
        return {
            "policy": POLICY,
            "params": self.params(),
            "vouchers": self.vouchers.all(),
            "range_start": start_hour,
            "range_end": end_hour,
            "buckets": buckets,
            "totals": totals,
            "peak_kw_upper_bound": peak,
            "limiter": self.limiter_state(max((end or 0), 1)),
        }

    def describe(self) -> str:
        reconciliation = self.reconcile()
        return (
            f"accounting basis={POLICY['basis']} vouchers={self.vouchers.count()} "
            f"buckets={len(self.ledger.hours())} matched={reconciliation.matches} "
            f"missing={POLICY['missing']}"
        )


def _default_params() -> AccountingParams:
    return AccountingParams(
        product_cp=DEFAULT_PRODUCT_CP,
        room_temp=DEFAULT_ROOM_TEMP,
        u_area=DEFAULT_U_AREA,
        ambient=DEFAULT_AMBIENT,
        defrost_power=DEFAULT_DEFROST_POWER,
        discharge_seconds=DEFAULT_DISCHARGE_SECONDS,
        rate_per_hour=DEFAULT_RATE_PER_HOUR,
        rate_burst=DEFAULT_RATE_BURST,
        max_span_buckets=DEFAULT_MAX_SPAN_BUCKETS,
    )
