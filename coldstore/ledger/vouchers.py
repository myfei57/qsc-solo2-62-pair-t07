"""Inbound registration book behind a token bucket gate.

Vouchers are stored in per hour buckets so a peak season flood only rewrites
the bucket of the current hour instead of one ever growing list. The token
bucket shaves the flood: bursts up to the bucket capacity pass, anything
beyond is rejected and counted so the operator can see the shaved peak.
"""

from __future__ import annotations

import json

from coldstore.defrost.schedule import now_unix
from coldstore.store.commands import append_command, load_commands
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .report import Voucher, hour_floor

VOUCHER_KEY_PREFIX = "ledger:vouchers:"
LIMITER_KEY = "ledger:limiter"
REJECTED_KEY = "ledger:rejected"
RATE_KEY = "ledger:config:rate-per-hour"
BURST_KEY = "ledger:config:burst"
GOODS_C_KEY = "ledger:config:goods-c"

DEFAULT_RATE_PER_HOUR = 300.0
DEFAULT_BURST = 60.0
DEFAULT_GOODS_C = 3.6  # kJ per kg and kelvin, typical frozen goods


class RateLimited(Exception):
    """Raised when the token bucket has no token for a registration."""


def voucher_hour(key: str) -> int | None:
    """Parse the hour out of a voucher bucket key, or None when foreign."""

    if not key.startswith(VOUCHER_KEY_PREFIX):
        return None
    try:
        return int(key[len(VOUCHER_KEY_PREFIX) :])
    except ValueError:
        return None


class VoucherBook:
    """Accepts, throttles and persists inbound registrations."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def register(
        self,
        voucher_id: str,
        room_id: str,
        mass: float,
        temp: float,
        target: float,
        at: int | None = None,
    ) -> Voucher:
        """Open a voucher for one batch, consuming one limiter token."""

        stamped = now_unix() if at is None else at
        hour = hour_floor(stamped)
        bucket = self.vouchers_for(hour)
        if any(voucher.id == voucher_id for voucher in bucket):
            raise ValueError(f"duplicate voucher {voucher_id} for hour {hour}")
        self._consume(stamped)
        heat = mass * self.goods_c() * max(temp - target, 0.0) / 3600.0
        voucher = Voucher(
            id=voucher_id,
            room=room_id,
            mass=mass,
            temp=temp,
            target=target,
            hour=hour,
            at=stamped,
            heat_kwh=heat,
        )
        append_command(
            self._store,
            f"{VOUCHER_KEY_PREFIX}{hour}",
            json.dumps(voucher.as_dict(), ensure_ascii=False),
        )
        return voucher

    def vouchers_for(self, hour: int) -> list[Voucher]:
        """Every well formed voucher registered for one hour bucket."""

        vouchers: list[Voucher] = []
        for item in load_commands(self._store, f"{VOUCHER_KEY_PREFIX}{hour}"):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            vouchers.append(Voucher.from_dict(payload))
        return vouchers

    def voucher_hours(self) -> list[int]:
        """Every hour that holds at least one voucher bucket."""

        hours: list[int] = []
        for key in self._store.keys():
            hour = voucher_hour(key)
            if hour is not None:
                hours.append(hour)
        return sorted(hours)

    def rejected(self) -> int:
        """How many registrations the limiter has turned away."""

        value, present = load_float(self._store, REJECTED_KEY)
        return int(value) if present else 0

    def tokens(self, at: int | None = None) -> float:
        """Tokens available right now, without mutating the bucket."""

        stamped = now_unix() if at is None else at
        tokens, marked = self._limiter()
        return min(self.burst(), tokens + max(0, stamped - marked) * self.rate_per_second())

    def goods_c(self) -> float:
        value, present = load_float(self._store, GOODS_C_KEY)
        return value if present else DEFAULT_GOODS_C

    def rate_per_hour(self) -> float:
        value, present = load_float(self._store, RATE_KEY)
        return value if present else DEFAULT_RATE_PER_HOUR

    def burst(self) -> float:
        value, present = load_float(self._store, BURST_KEY)
        return value if present else DEFAULT_BURST

    def set_goods_c(self, value: float) -> None:
        save_float(self._store, GOODS_C_KEY, value)

    def set_rate_per_hour(self, value: float) -> None:
        save_float(self._store, RATE_KEY, value)

    def set_burst(self, value: float) -> None:
        save_float(self._store, BURST_KEY, value)

    def rate_per_second(self) -> float:
        return self.rate_per_hour() / 3600.0

    def _consume(self, at: int) -> None:
        tokens, marked = self._limiter()
        tokens = min(self.burst(), tokens + max(0, at - marked) * self.rate_per_second())
        if tokens < 1.0:
            save_float(self._store, REJECTED_KEY, self.rejected() + 1.0)
            raise RateLimited("registration rate limited, retry later")
        self._save_limiter(tokens - 1.0, at)

    def _limiter(self) -> tuple[float, int]:
        raw, present = self._store.get(LIMITER_KEY)
        if present:
            try:
                payload = json.loads(raw)
                return float(payload["tokens"]), int(payload["at"])
            except (ValueError, KeyError, TypeError):
                pass
        return self.burst(), now_unix()

    def _save_limiter(self, tokens: float, at: int) -> None:
        payload = {"tokens": tokens, "at": at}
        self._store.put(LIMITER_KEY, json.dumps(payload, ensure_ascii=False))
