"""Token bucket that throttles the inbound registration flood.

Registrations during the intake rush arrive far faster than the cold chain can
absorb them. The limiter admits at most ``rate_per_hour`` batches per hour with
a short burst allowance, shaping the flood instead of dropping accounting. The
bucket is refilled deterministically from event time so the limiter is testable
and restart safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from coldstore.store.store import Store

STATE_KEY = "accounting:limiter"
SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class LimiterDecision:
    """Whether one registration consumed a token."""

    accepted: bool
    reason: str
    tokens: float
    rate_per_hour: float
    burst: float

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "tokens": self.tokens,
            "rate_per_hour": self.rate_per_hour,
            "burst": self.burst,
        }


class RateLimiter:
    """Persistent refillable token bucket keyed on event time."""

    def __init__(self, store: Store, rate_per_hour: float, burst: float) -> None:
        self._store = store
        self._rate = rate_per_hour
        self._burst = burst

    def _load(self) -> tuple[float, int]:
        raw, present = self._store.get(STATE_KEY)
        if not present:
            return self._burst, 0
        try:
            payload = json.loads(raw)
        except ValueError:
            return self._burst, 0
        if not isinstance(payload, dict):
            return self._burst, 0
        return float(payload.get("tokens", self._burst)), int(payload.get("at", 0))

    def _save(self, tokens: float, at: int) -> None:
        self._store.put(STATE_KEY, json.dumps({"tokens": tokens, "at": at}, sort_keys=True))

    def tokens(self, at: int) -> float:
        tokens, last = self._load()
        if last and at >= last:
            refill = self._rate * (at - last) / SECONDS_PER_HOUR
            tokens = min(self._burst, tokens + refill)
        return tokens

    def admit(self, at: int, cost: float = 1.0) -> LimiterDecision:
        tokens, last = self._load()
        if last and at >= last:
            tokens = min(self._burst, tokens + self._rate * (at - last) / SECONDS_PER_HOUR)
        elif at < last:
            return LimiterDecision(False, "event before limiter clock", tokens, self._rate, self._burst)
        if tokens + 1e-9 < cost:
            self._save(tokens, at)
            return LimiterDecision(False, "rate limited", tokens, self._rate, self._burst)
        tokens -= cost
        self._save(tokens, at)
        return LimiterDecision(True, "accepted", tokens, self._rate, self._burst)

    def reset(self) -> None:
        self._store.delete(STATE_KEY)
