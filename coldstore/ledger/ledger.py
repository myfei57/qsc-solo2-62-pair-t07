"""Facade that keeps registrations, settlement and audit on one caliber.

The ledger owns the two invariants the operator relies on:

* a voucher is never accepted for an hour that is already settled, so a
  settled slice can never drift away from its evidence,
* every accepted voucher and every settled slice is mirrored into the audit
  trail under the same hour key, so the monitoring queries and the audit
  export always tell the same story.
"""

from __future__ import annotations

import json

from coldstore.audit import Auditor
from coldstore.defrost.schedule import now_unix
from coldstore.load import LoadCalculator
from coldstore.room import RoomRegistry
from coldstore.store.store import Store

from .report import (
    CALIBER,
    GAP_POLICY,
    HOUR_SECONDS,
    MAX_RANGE_HOURS,
    RECONCILE_TOLERANCE,
    LedgerState,
    Settlement,
    Slice,
    Voucher,
    hour_floor,
    validate_goods_temp,
    validate_mass,
)
from .settle import Settler
from .vouchers import VoucherBook

AUDIT_VOUCHER_KIND = "ledger.voucher"
AUDIT_SLICE_KIND = "ledger.settle"

CONFIG_FIELDS = (
    "envelope_ua",
    "ambient",
    "goods_c",
    "defrost_kw",
    "defrost_hours",
    "rate_per_hour",
    "burst",
    "budget",
)


class Ledger:
    """Itemized cooling load accounting over vouchers and hourly slices."""

    def __init__(
        self, store: Store, rooms: RoomRegistry, load: LoadCalculator, auditor: Auditor
    ) -> None:
        self._store = store
        self._rooms = rooms
        self._load = load
        self._auditor = auditor
        self._book = VoucherBook(store)
        self._settler = Settler(store, rooms, load)

    def register(
        self, voucher_id: str, room_id: str, mass: float, temp: float, at: int | None = None
    ) -> Voucher:
        """Accept one inbound batch and mirror the voucher into the audit."""

        if not voucher_id:
            raise ValueError("voucher id is required")
        if self._rooms.room(room_id) is None:
            raise ValueError(f"cold room {room_id} not found")
        validate_mass(mass)
        validate_goods_temp(temp)
        cursor = self._settler.cursor()
        if at is not None and cursor is not None and hour_floor(at) <= cursor:
            raise ValueError(f"hour {hour_floor(at)} is already settled")
        voucher = self._book.register(
            voucher_id, room_id, mass, temp, self._load.baseline(), at=at
        )
        self._auditor.record(AUDIT_VOUCHER_KIND, json.dumps(voucher.as_dict(), ensure_ascii=False))
        return voucher

    def record_defrost(self, room_id: str, at: int | None = None) -> None:
        """Listener bound to the defrost controller."""

        self._settler.record_defrost(room_id, at=at)

    def settle(self, to_hour: int | None = None) -> Settlement:
        """Integrate pending hours and mirror every slice into the audit."""

        result = self._settler.settle(to_hour)
        for item in result.settled:
            self._auditor.record(AUDIT_SLICE_KIND, json.dumps(item.as_dict(), ensure_ascii=False))
        return result

    def vouchers_for(self, hour: int) -> list[Voucher]:
        return self._book.vouchers_for(hour)

    def load_range(self, from_hour: int, to_hour: int) -> dict[str, object]:
        """Load composition for a time range, the monitoring projection."""

        from_hour, to_hour = self._checked_range(from_hour, to_hour)
        slices: list[Slice] = []
        pending: list[int] = []
        last_closed = hour_floor(now_unix()) - HOUR_SECONDS
        hour = from_hour
        while hour <= to_hour:
            item = self._settler.slice_for(hour)
            if item is not None:
                slices.append(item)
            elif hour <= last_closed:
                pending.append(hour)
            hour += HOUR_SECONDS
        totals = {
            "inbound_kwh": sum(item.inbound_kwh for item in slices),
            "envelope_kwh": sum(item.envelope_kwh for item in slices),
            "defrost_kwh": sum(item.defrost_kwh for item in slices),
            "total_kwh": sum(item.total_kwh for item in slices),
        }
        peak = max(slices, key=lambda item: (item.total_kwh, item.hour), default=None)
        return {
            "from": from_hour,
            "to": to_hour,
            "caliber": CALIBER,
            "gap_policy": GAP_POLICY,
            "hours": [item.as_dict() for item in slices],
            "pending": pending,
            "totals": totals,
            "peak": {"hour": peak.hour, "total_kwh": peak.total_kwh} if peak else None,
        }

    def reconcile(self, from_hour: int, to_hour: int) -> dict[str, object]:
        """Recompute settled slices from their vouchers and report drift."""

        from_hour, to_hour = self._checked_range(from_hour, to_hour)
        mismatches: list[dict[str, object]] = []
        checked = 0
        hour = from_hour
        while hour <= to_hour:
            item = self._settler.slice_for(hour)
            if item is None:
                hour += HOUR_SECONDS
                continue
            checked += 1
            vouchers = self._book.vouchers_for(hour)
            inbound = sum(voucher.heat_kwh for voucher in vouchers)
            if abs(inbound - item.inbound_kwh) > RECONCILE_TOLERANCE:
                mismatches.append(
                    {
                        "hour": hour,
                        "field": "inbound_kwh",
                        "slice": item.inbound_kwh,
                        "vouchers": inbound,
                    }
                )
            if len(vouchers) != item.vouchers:
                mismatches.append(
                    {
                        "hour": hour,
                        "field": "vouchers",
                        "slice": item.vouchers,
                        "vouchers": len(vouchers),
                    }
                )
            hour += HOUR_SECONDS
        cursor = self._settler.cursor()
        settled = set(self._settler.settled_hours())
        orphans = [
            hour
            for hour in self._book.voucher_hours()
            if cursor is not None and hour <= cursor and hour not in settled
        ]
        awaiting = [
            hour for hour in self._book.voucher_hours() if cursor is None or hour > cursor
        ][:100]
        return {
            "from": from_hour,
            "to": to_hour,
            "hours_checked": checked,
            "consistent": not mismatches and not orphans,
            "mismatches": mismatches,
            "orphan_voucher_hours": orphans,
            "pending_voucher_hours": awaiting,
        }

    def config(self) -> dict[str, float]:
        return {
            "envelope_ua": self._settler.envelope_ua(),
            "ambient": self._settler.ambient(),
            "goods_c": self._book.goods_c(),
            "defrost_kw": self._settler.defrost_kw(),
            "defrost_hours": self._settler.defrost_hours(),
            "rate_per_hour": self._book.rate_per_hour(),
            "burst": self._book.burst(),
            "budget": self._settler.budget(),
        }

    def update_config(self, changes: dict[str, float]) -> dict[str, float]:
        """Apply a partial config update, validating every field first."""

        unknown = sorted(name for name in changes if name not in CONFIG_FIELDS)
        if unknown:
            raise ValueError("unknown config field " + ", ".join(unknown))
        for name, value in changes.items():
            self._validate_config(name, value)
        for name, value in changes.items():
            self._apply_config(name, value)
        return self.config()

    def state(self) -> LedgerState:
        return LedgerState(
            cursor=self._settler.cursor(),
            settled=len(self._settler.settled_hours()),
            pending=self._settler.pending_count(),
            rejected=self._book.rejected(),
            tokens=self._book.tokens(),
            gap_policy=GAP_POLICY,
            caliber=CALIBER,
            config=self.config(),
        )

    def describe(self) -> str:
        state = self.state()
        cursor = state.cursor if state.cursor is not None else "none"
        return (
            f"ledger cursor={cursor} slices={state.settled} pending={state.pending} "
            f"rejected={state.rejected}"
        )

    def _checked_range(self, from_hour: int, to_hour: int) -> tuple[int, int]:
        if from_hour <= 0:
            raise ValueError("from hour is required")
        if to_hour < from_hour:
            raise ValueError("to hour must not be before from hour")
        if (to_hour - from_hour) // HOUR_SECONDS + 1 > MAX_RANGE_HOURS:
            raise ValueError(f"range exceeds {MAX_RANGE_HOURS} hours")
        return from_hour, to_hour

    def _validate_config(self, name: str, value: float) -> None:
        if name in ("envelope_ua", "goods_c", "defrost_hours", "rate_per_hour") and value <= 0:
            raise ValueError(f"{name} must be positive")
        if name == "defrost_kw" and value < 0:
            raise ValueError("defrost_kw must not be negative")
        if name in ("burst", "budget") and value < 1:
            raise ValueError(f"{name} must be at least 1")
        if name == "ambient" and not -80.0 <= value <= 80.0:
            raise ValueError("ambient is outside the supported range")

    def _apply_config(self, name: str, value: float) -> None:
        setters = {
            "envelope_ua": self._settler.set_envelope_ua,
            "ambient": self._settler.set_ambient,
            "goods_c": self._book.set_goods_c,
            "defrost_kw": self._settler.set_defrost_kw,
            "defrost_hours": self._settler.set_defrost_hours,
            "rate_per_hour": self._book.set_rate_per_hour,
            "burst": self._book.set_burst,
            "budget": self._settler.set_budget,
        }
        setters[name](value)
