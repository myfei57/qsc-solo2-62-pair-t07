"""Itemized load settlement: hourly integration with a work budget.

Every settle call integrates at most ``budget`` hourly slices so a long
backlog cannot stall the console. Each slice folds the three components of
the hour into one energy figure:

* inbound heat, summed from the vouchers registered for that hour,
* envelope heat, ambient to room gap integrated over the hour per room,
* defrost heat, one nominal energy dose per recorded defrost event.

The cursor only moves forward and a slice is written exactly once, which is
what keeps the settlement and the vouchers on the same caliber.
"""

from __future__ import annotations

import json

from coldstore.defrost.schedule import now_unix
from coldstore.load import LoadCalculator
from coldstore.room import RoomRegistry
from coldstore.store.commands import append_command, load_commands
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .report import CALIBER, GAP_POLICY, HOUR_SECONDS, Settlement, Slice, hour_floor
from .vouchers import VoucherBook

SLICE_KEY_PREFIX = "ledger:slices:"
DEFROST_KEY_PREFIX = "ledger:defrost:"
CURSOR_KEY = "ledger:cursor"
ENVELOPE_UA_KEY = "ledger:config:envelope-ua"
AMBIENT_KEY = "ledger:config:ambient"
DEFROST_KW_KEY = "ledger:config:defrost-kw"
DEFROST_HOURS_KEY = "ledger:config:defrost-hours"
BUDGET_KEY = "ledger:config:budget"

DEFAULT_ENVELOPE_UA = 0.8  # kW per kelvin and room
DEFAULT_AMBIENT = 25.0
DEFAULT_DEFROST_KW = 12.0
DEFAULT_DEFROST_HOURS = 0.5
DEFAULT_BUDGET = 24.0


def slice_hour(key: str) -> int | None:
    """Parse the hour out of a slice key, or None when foreign."""

    if not key.startswith(SLICE_KEY_PREFIX):
        return None
    try:
        return int(key[len(SLICE_KEY_PREFIX) :])
    except ValueError:
        return None


class Settler:
    """Integrates the three load components hour by hour."""

    def __init__(self, store: Store, rooms: RoomRegistry, load: LoadCalculator) -> None:
        self._store = store
        self._rooms = rooms
        self._load = load
        self._book = VoucherBook(store)

    def record_defrost(self, room_id: str, at: int | None = None) -> None:
        """Note one defrost event in its hour bucket.

        Wired as a listener on the defrost controller so every start,
        replayed or direct, lands in the same bucket structure.
        """

        stamped = now_unix() if at is None else at
        hour = hour_floor(stamped)
        event = {"room": room_id, "at": stamped}
        append_command(
            self._store, f"{DEFROST_KEY_PREFIX}{hour}", json.dumps(event, ensure_ascii=False)
        )

    def defrost_events(self, hour: int) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for item in load_commands(self._store, f"{DEFROST_KEY_PREFIX}{hour}"):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def cursor(self) -> int | None:
        """The last settled hour, or None before the first settlement."""

        value, present = load_float(self._store, CURSOR_KEY)
        return int(value) if present else None

    def slice_for(self, hour: int) -> Slice | None:
        """The settled slice for one hour, or None when still pending."""

        raw, present = self._store.get(f"{SLICE_KEY_PREFIX}{hour}")
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return Slice.from_dict(payload)

    def settled_hours(self) -> list[int]:
        hours: list[int] = []
        for key in self._store.keys():
            hour = slice_hour(key)
            if hour is not None:
                hours.append(hour)
        return sorted(hours)

    def pending_count(self) -> int:
        """Closed hours still waiting for settlement."""

        last_closed = hour_floor(now_unix()) - HOUR_SECONDS
        cursor = self.cursor()
        if cursor is None:
            first = self._first_data_hour()
            if first is None or first > last_closed:
                return 0
            return (last_closed - first) // HOUR_SECONDS + 1
        return max(0, (last_closed - cursor) // HOUR_SECONDS)

    def settle(self, to_hour: int | None = None) -> Settlement:
        """Integrate every closed hour up to ``to_hour``, budget permitting."""

        last_closed = hour_floor(now_unix()) - HOUR_SECONDS
        target = last_closed if to_hour is None else to_hour
        if target > last_closed:
            raise ValueError("cannot settle an hour that has not closed")
        cursor = self.cursor()
        if cursor is None:
            first = self._first_data_hour()
            start = first if first is not None else target
        else:
            start = cursor + HOUR_SECONDS
        pending = max(0, (target - start) // HOUR_SECONDS + 1) if start <= target else 0
        budget = int(self.budget())
        settled: list[Slice] = []
        hour = start
        while hour <= target and len(settled) < budget:
            item = self._integrate(hour)
            self._store.put(
                f"{SLICE_KEY_PREFIX}{hour}", json.dumps(item.as_dict(), ensure_ascii=False)
            )
            save_float(self._store, CURSOR_KEY, float(hour))
            settled.append(item)
            hour += HOUR_SECONDS
        backlog = pending - len(settled)
        new_cursor = settled[-1].hour if settled else (cursor if cursor is not None else 0)
        return Settlement(settled=settled, backlog=backlog, cursor=new_cursor)

    def envelope_ua(self) -> float:
        value, present = load_float(self._store, ENVELOPE_UA_KEY)
        return value if present else DEFAULT_ENVELOPE_UA

    def ambient(self) -> float:
        value, present = load_float(self._store, AMBIENT_KEY)
        return value if present else DEFAULT_AMBIENT

    def defrost_kw(self) -> float:
        value, present = load_float(self._store, DEFROST_KW_KEY)
        return value if present else DEFAULT_DEFROST_KW

    def defrost_hours(self) -> float:
        value, present = load_float(self._store, DEFROST_HOURS_KEY)
        return value if present else DEFAULT_DEFROST_HOURS

    def budget(self) -> float:
        value, present = load_float(self._store, BUDGET_KEY)
        return value if present else DEFAULT_BUDGET

    def set_envelope_ua(self, value: float) -> None:
        save_float(self._store, ENVELOPE_UA_KEY, value)

    def set_ambient(self, value: float) -> None:
        save_float(self._store, AMBIENT_KEY, value)

    def set_defrost_kw(self, value: float) -> None:
        save_float(self._store, DEFROST_KW_KEY, value)

    def set_defrost_hours(self, value: float) -> None:
        save_float(self._store, DEFROST_HOURS_KEY, value)

    def set_budget(self, value: float) -> None:
        save_float(self._store, BUDGET_KEY, value)

    def _integrate(self, hour: int) -> Slice:
        vouchers = self._book.vouchers_for(hour)
        inbound = sum(voucher.heat_kwh for voucher in vouchers)
        rooms = self._rooms.rooms()
        ambient = self.ambient()
        envelope = sum(
            self.envelope_ua() * max(ambient - room.temp, 0.0) for room in rooms
        )
        events = self.defrost_events(hour)
        defrost = len(events) * self.defrost_kw() * self.defrost_hours()
        return Slice(
            hour=hour,
            inbound_kwh=inbound,
            envelope_kwh=envelope,
            defrost_kwh=defrost,
            total_kwh=inbound + envelope + defrost,
            vouchers=len(vouchers),
            defrost_events=len(events),
            rooms=len(rooms),
            gap_policy=GAP_POLICY,
            caliber=CALIBER,
            settled_at=now_unix(),
        )

    def _first_data_hour(self) -> int | None:
        """Oldest hour holding vouchers or defrost events, if any."""

        hours = self._book.voucher_hours()
        for key in self._store.keys():
            if not key.startswith(DEFROST_KEY_PREFIX):
                continue
            try:
                hours.append(int(key[len(DEFROST_KEY_PREFIX) :]))
            except ValueError:
                continue
        return min(hours) if hours else None
