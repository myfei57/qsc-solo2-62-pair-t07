"""Persisted hourly load ledger and inbound registration vouchers.

The ledger keeps one aggregate record per hour with separate energy and peak
counters for each heat term. Appending a registration, ambient sample or
defrost folds into the affected buckets only, so the write cost stays bounded by
the integration budget rather than the history length.

Vouchers are keyed by batch id: they make registrations idempotent and are the
receipt side reconciled against the ledger inbound integral.
"""

from __future__ import annotations

import json

from coldstore.store.store import Store

from .report import COMPONENTS, LoadBucket, Reconciliation, Voucher

LEDGER_KEY = "accounting:ledger"
VOUCHER_KEY = "accounting:vouchers"
ENVELOPE_SAMPLE_KEY = "accounting:envelope-last"

# Internal per-bucket counter names.
_E_KEY = {COMPONENTS[0]: "in_e", "envelope": "en_e", "defrost": "de_e"}
_P_KEY = {COMPONENTS[0]: "in_p", "envelope": "en_p", "defrost": "de_p"}


class VoucherBook:
    """Append-only registration receipts indexed by batch id."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _load(self) -> dict[str, dict[str, object]]:
        raw, present = self._store.get(VOUCHER_KEY)
        if not present:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def contains(self, batch_id: str) -> bool:
        return batch_id in self._load()

    def add(self, voucher: Voucher) -> None:
        if not voucher.accepted:
            raise ValueError("only accepted registrations carry a voucher")
        vouchers = self._load()
        if voucher.batch_id in vouchers:
            raise ValueError(f"batch {voucher.batch_id} already registered")
        vouchers[voucher.batch_id] = voucher.as_dict()
        self._store.put(VOUCHER_KEY, json.dumps(vouchers, ensure_ascii=False, sort_keys=True))

    def get(self, batch_id: str) -> Voucher | None:
        payload = self._load().get(batch_id)
        return voucher_from_dict(payload) if payload else None

    def all(self) -> list[Voucher]:
        return [voucher_from_dict(payload) for payload in self._load().values()]

    def count(self) -> int:
        return len(self._load())


def voucher_from_dict(payload: dict[str, object]) -> Voucher:
    return Voucher(
        batch_id=str(payload.get("batch_id", "")),
        at=int(payload.get("at", 0)),
        mass_kg=float(payload.get("mass_kg", 0.0)),
        product_temp=float(payload.get("product_temp", 0.0)),
        discharge_seconds=int(payload.get("discharge_seconds", 0)),
        energy_kwh=float(payload.get("energy_kwh", 0.0)),
        accepted=bool(payload.get("accepted", True)),
        reason=str(payload.get("reason", "accepted")),
        policy=str(payload.get("policy", "")),
    )


class Ledger:
    """Hourly integrated energy and per-term peak counters."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _load(self) -> dict[str, dict[str, float]]:
        raw, present = self._store.get(LEDGER_KEY)
        if not present:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def _save(self, buckets: dict[str, dict[str, float]]) -> None:
        self._store.put(LEDGER_KEY, json.dumps(buckets, ensure_ascii=False, sort_keys=True))

    def fold(self, component: str, hour: int, energy_kwh: float, peak_kw: float, count: int = 0) -> None:
        """Merge one term segment into a single hourly bucket."""

        buckets = self._load()
        key = str(hour)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"in_e": 0.0, "en_e": 0.0, "de_e": 0.0,
                      "in_p": 0.0, "en_p": 0.0, "de_p": 0.0, "n": 0}
        bucket[_E_KEY[component]] = bucket.get(_E_KEY[component], 0.0) + energy_kwh
        bucket[_P_KEY[component]] = max(bucket.get(_P_KEY[component], 0.0), peak_kw)
        bucket["n"] = bucket.get("n", 0) + count
        buckets[key] = bucket
        self._save(buckets)

    def fold_many(self, component: str, segments: list[tuple[int, float, float]], count: int = 0) -> None:
        """Merge several ``(hour, energy, peak)`` segments in one write."""

        if not segments:
            return
        buckets = self._load()
        first = True
        for hour, energy_kwh, peak_kw in segments:
            key = str(hour)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {"in_e": 0.0, "en_e": 0.0, "de_e": 0.0,
                          "in_p": 0.0, "en_p": 0.0, "de_p": 0.0, "n": 0}
            bucket[_E_KEY[component]] = bucket.get(_E_KEY[component], 0.0) + energy_kwh
            bucket[_P_KEY[component]] = max(bucket.get(_P_KEY[component], 0.0), peak_kw)
            if first:
                bucket["n"] = bucket.get("n", 0) + count
                first = False
            buckets[key] = bucket
        self._save(buckets)

    def hours(self) -> list[int]:
        return sorted(int(key) for key in self._load())

    def _bucket(self, hour: int, zero_filled: bool, stored: dict[str, float]) -> LoadBucket:
        inbound = stored.get("in_e", 0.0)
        envelope = stored.get("en_e", 0.0)
        defrost = stored.get("de_e", 0.0)
        in_peak = stored.get("in_p", 0.0)
        en_peak = stored.get("en_p", 0.0)
        de_peak = stored.get("de_p", 0.0)
        return LoadBucket(
            hour=hour,
            inbound_kwh=inbound,
            envelope_kwh=envelope,
            defrost_kwh=defrost,
            total_kwh=inbound + envelope + defrost,
            inbound_peak_kw=in_peak,
            envelope_peak_kw=en_peak,
            defrost_peak_kw=de_peak,
            peak_kw_upper_bound=in_peak + en_peak + de_peak,
            records=int(stored.get("n", 0)),
            zero_filled=zero_filled,
        )

    def range(self, start_hour: int, end_hour: int) -> list[LoadBucket]:
        """Dense hour list; hours with no records are synthesised zero filled."""

        if end_hour < start_hour:
            raise ValueError("range end must not precede its start")
        stored = self._load()
        result: list[LoadBucket] = []
        for hour in range(start_hour, end_hour + 1):
            key = str(hour)
            if key in stored:
                result.append(self._bucket(hour, False, stored[key]))
            else:
                result.append(self._bucket(hour, True, {}))
        return result

    def all_buckets(self) -> list[LoadBucket]:
        stored = self._load()
        return [self._bucket(hour, False, stored[str(hour)]) for hour in self.hours()]

    def reconcile(self, book: VoucherBook) -> Reconciliation:
        """Compare accepted voucher energy with the ledger inbound integral."""

        vouchers = [voucher for voucher in book.all() if voucher.accepted]
        voucher_kwh = sum(voucher.energy_kwh for voucher in vouchers)
        ledger_kwh = sum(bucket.inbound_kwh for bucket in self.all_buckets())
        difference = abs(voucher_kwh - ledger_kwh)
        scale = max(1.0, voucher_kwh, ledger_kwh)
        return Reconciliation(
            voucher_kwh=voucher_kwh,
            ledger_inbound_kwh=ledger_kwh,
            difference_kwh=difference,
            matches=difference <= 1e-6 * scale,
            vouchers=len(vouchers),
            buckets=len(self._load()),
        )

    def last_envelope_sample(self) -> tuple[int, float] | None:
        raw, present = self._store.get(ENVELOPE_SAMPLE_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return int(payload.get("at", 0)), float(payload.get("power", 0.0))

    def save_envelope_sample(self, at: int, power: float) -> None:
        self._store.put(
            ENVELOPE_SAMPLE_KEY, json.dumps({"at": at, "power": power}, sort_keys=True)
        )
