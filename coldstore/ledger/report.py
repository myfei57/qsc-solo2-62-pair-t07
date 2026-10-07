"""Itemized load projections, shared constants and validation rules."""

from __future__ import annotations

from dataclasses import dataclass

HOUR_SECONDS = 3600

# Missing registration hours contribute zero inbound heat. Registrations are
# discrete arrival events, not a state signal, so carrying the last voucher
# forward would integrate the same batch heat twice.
GAP_POLICY = "zero"

# The accounting caliber: every slice is one hour of energy per component.
# Peak figures in range queries are derived from these slices, never the
# other way around.
CALIBER = "hourly_energy"

# Tolerance used when reconciling settled slices against their vouchers.
RECONCILE_TOLERANCE = 1e-6

# Longest time range a monitoring query may span, in hours.
MAX_RANGE_HOURS = 24 * 31


def hour_floor(at: int) -> int:
    """The unix timestamp of the hour bucket containing ``at``."""

    return at - (at % HOUR_SECONDS)


@dataclass(frozen=True)
class Voucher:
    """One accepted inbound registration, the evidence for inbound heat."""

    id: str
    room: str
    mass: float
    temp: float
    target: float
    hour: int
    at: int
    heat_kwh: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "room": self.room,
            "mass": self.mass,
            "temp": self.temp,
            "target": self.target,
            "hour": self.hour,
            "at": self.at,
            "heat_kwh": self.heat_kwh,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Voucher":
        return cls(
            id=str(payload.get("id", "")),
            room=str(payload.get("room", "")),
            mass=float(payload.get("mass", 0.0)),
            temp=float(payload.get("temp", 0.0)),
            target=float(payload.get("target", 0.0)),
            hour=int(payload.get("hour", 0)),
            at=int(payload.get("at", 0)),
            heat_kwh=float(payload.get("heat_kwh", 0.0)),
        )


@dataclass(frozen=True)
class Slice:
    """One settled hour of itemized load, written exactly once."""

    hour: int
    inbound_kwh: float
    envelope_kwh: float
    defrost_kwh: float
    total_kwh: float
    vouchers: int
    defrost_events: int
    rooms: int
    gap_policy: str
    caliber: str
    settled_at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "hour": self.hour,
            "inbound_kwh": self.inbound_kwh,
            "envelope_kwh": self.envelope_kwh,
            "defrost_kwh": self.defrost_kwh,
            "total_kwh": self.total_kwh,
            "vouchers": self.vouchers,
            "defrost_events": self.defrost_events,
            "rooms": self.rooms,
            "gap_policy": self.gap_policy,
            "caliber": self.caliber,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Slice":
        return cls(
            hour=int(payload.get("hour", 0)),
            inbound_kwh=float(payload.get("inbound_kwh", 0.0)),
            envelope_kwh=float(payload.get("envelope_kwh", 0.0)),
            defrost_kwh=float(payload.get("defrost_kwh", 0.0)),
            total_kwh=float(payload.get("total_kwh", 0.0)),
            vouchers=int(payload.get("vouchers", 0)),
            defrost_events=int(payload.get("defrost_events", 0)),
            rooms=int(payload.get("rooms", 0)),
            gap_policy=str(payload.get("gap_policy", GAP_POLICY)),
            caliber=str(payload.get("caliber", CALIBER)),
            settled_at=int(payload.get("settled_at", 0)),
        )


@dataclass(frozen=True)
class Settlement:
    """The outcome of one bounded settle call."""

    settled: list[Slice]
    backlog: int
    cursor: int

    def as_dict(self) -> dict[str, object]:
        return {
            "settled": [item.as_dict() for item in self.settled],
            "backlog": self.backlog,
            "cursor": self.cursor,
        }


@dataclass(frozen=True)
class LedgerState:
    """Cursor, limiter and policy projection for the console."""

    cursor: int | None
    settled: int
    pending: int
    rejected: int
    tokens: float
    gap_policy: str
    caliber: str
    config: dict[str, float]

    def as_dict(self) -> dict[str, object]:
        return {
            "cursor": self.cursor,
            "settled": self.settled,
            "pending": self.pending,
            "rejected": self.rejected,
            "tokens": self.tokens,
            "gap_policy": self.gap_policy,
            "caliber": self.caliber,
            "config": dict(self.config),
        }


def validate_mass(value: float) -> None:
    """Reject implausible batch masses."""

    if value <= 0:
        raise ValueError("mass must be positive")
    if value > 1_000_000:
        raise ValueError("mass exceeds the supported range")


def validate_goods_temp(value: float) -> None:
    """Reject goods temperatures outside the supported range."""

    if value < -100:
        raise ValueError("goods temp is below the supported range")
    if value > 100:
        raise ValueError("goods temp exceeds the supported range")
