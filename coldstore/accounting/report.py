"""Projections, shared policy constants and validation for itemised load accounting.

The accounting split keeps three heat terms separate and integrates every one of
them over time:

* inbound heat from batches registered at the dock (``inbound``),
* envelope heat leaking through the room skin (``envelope``),
* heat injected while a cooler defrosts (``defrost``).

The accounting basis is the hourly integral (energy, kilowatt hours). Peak power
(kilowatts) is reported only as a derived sizing signal; it is never converted
into the billable/load total. See ``POLICY`` for the fixed choices.
"""

from __future__ import annotations

from dataclasses import dataclass, field

INBOUND = "inbound"
ENVELOPE = "envelope"
DEFROST = "defrost"
COMPONENTS = (INBOUND, ENVELOPE, DEFROST)

#: Fixed policy choices called out in the itemised accounting design. Changing
#: any of these changes the accounting basis, hence the single version stamp.
POLICY = {
    "components": list(COMPONENTS),
    # Energy is the hourly time integral; peak power is only a derived signal.
    "basis": "hourly_integral_kwh",
    "peak_basis": "derived_peak_kw_not_billed",
    # A missing registration contributes nothing; gaps are zero filled at read
    # time and flagged, never silently carried forward from a prior record.
    "missing": "zero_fill",
    "bucket_seconds": 3600,
    "version": "accounting-v1",
}

# Physical defaults. Mass uses kilograms, temperatures degrees Celsius, power
# kilowatts, energy kilowatt hours.
DEFAULT_PRODUCT_CP = 3.0
DEFAULT_ROOM_TEMP = -18.0
DEFAULT_U_AREA = 0.05
DEFAULT_AMBIENT = 30.0
DEFAULT_DEFROST_POWER = 6.0
DEFAULT_DISCHARGE_SECONDS = 3600

# Registration flood control: a refillable token bucket in front of inbound
# registrations (the limiting/shaping decision).
DEFAULT_RATE_PER_HOUR = 200.0
DEFAULT_RATE_BURST = 250.0

# Integration performance budget: no fold may span more than this many hourly
# buckets, which bounds the work a single registration can trigger.
DEFAULT_MAX_SPAN_BUCKETS = 256

EPS = 1e-6


@dataclass(frozen=True)
class AccountingParams:
    """Coefficients shared by the three heat terms."""

    product_cp: float
    room_temp: float
    u_area: float
    ambient: float
    defrost_power: float
    discharge_seconds: int
    rate_per_hour: float
    rate_burst: float
    max_span_buckets: int

    def as_dict(self) -> dict[str, object]:
        return {
            "product_cp": self.product_cp,
            "room_temp": self.room_temp,
            "u_area": self.u_area,
            "ambient": self.ambient,
            "defrost_power": self.defrost_power,
            "discharge_seconds": self.discharge_seconds,
            "rate_per_hour": self.rate_per_hour,
            "rate_burst": self.rate_burst,
            "max_span_buckets": self.max_span_buckets,
        }


@dataclass(frozen=True)
class Voucher:
    """Registration receipt for one inbound batch.

    The heat energy stamped here is the single figure of record: the ledger
    inbound integral and every audit line reuse ``energy_kwh`` so the receipt and
    the accounting can only ever agree.
    """

    batch_id: str
    at: int
    mass_kg: float
    product_temp: float
    discharge_seconds: int
    energy_kwh: float
    accepted: bool
    reason: str
    policy: str

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "at": self.at,
            "mass_kg": self.mass_kg,
            "product_temp": self.product_temp,
            "discharge_seconds": self.discharge_seconds,
            "energy_kwh": self.energy_kwh,
            "accepted": self.accepted,
            "reason": self.reason,
            "policy": self.policy,
        }


@dataclass(frozen=True)
class LoadBucket:
    """One hour's integrated energy and per-term peak power.

    Missing buckets are synthesised on read with ``zero_filled`` set; persisted
    buckets carry the count of records that fed them so the zero-fill choice can
    always be distinguished from a genuinely quiet hour.
    """

    hour: int
    inbound_kwh: float
    envelope_kwh: float
    defrost_kwh: float
    total_kwh: float
    inbound_peak_kw: float
    envelope_peak_kw: float
    defrost_peak_kw: float
    peak_kw_upper_bound: float
    records: int
    zero_filled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "hour": self.hour,
            "epoch_start": self.hour * 3600,
            "inbound_kwh": self.inbound_kwh,
            "envelope_kwh": self.envelope_kwh,
            "defrost_kwh": self.defrost_kwh,
            "total_kwh": self.total_kwh,
            "inbound_peak_kw": self.inbound_peak_kw,
            "envelope_peak_kw": self.envelope_peak_kw,
            "defrost_peak_kw": self.defrost_peak_kw,
            "peak_kw_upper_bound": self.peak_kw_upper_bound,
            "records": self.records,
            "zero_filled": self.zero_filled,
        }


@dataclass(frozen=True)
class Reconciliation:
    """Result of checking vouchers against the ledger on the same basis."""

    voucher_kwh: float
    ledger_inbound_kwh: float
    difference_kwh: float
    matches: bool
    vouchers: int
    buckets: int

    def as_dict(self) -> dict[str, object]:
        return {
            "voucher_kwh": self.voucher_kwh,
            "ledger_inbound_kwh": self.ledger_inbound_kwh,
            "difference_kwh": self.difference_kwh,
            "matches": self.matches,
            "vouchers": self.vouchers,
            "buckets": self.buckets,
            "basis": POLICY["basis"],
            "policy": POLICY["version"],
        }


@dataclass(frozen=True)
class AccountingState:
    """Projection returned by the ``/accounting`` endpoint."""

    params: AccountingParams
    vouchers: list[Voucher]
    range_start: int
    range_end: int
    buckets: list[LoadBucket]
    totals: dict[str, float]
    peak_kw_upper_bound: float
    limiter: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": POLICY,
            "params": self.params.as_dict(),
            "vouchers": [voucher.as_dict() for voucher in self.vouchers],
            "range": {"start": self.range_start, "end": self.range_end},
            "buckets": [bucket.as_dict() for bucket in self.buckets],
            "totals": self.totals,
            "peak_kw_upper_bound": self.peak_kw_upper_bound,
            "limiter": self.limiter,
        }


def validate_mass(value: float) -> None:
    if value < 0:
        raise ValueError("mass must be non-negative")
    if value > 1_000_000.0:
        raise ValueError("mass is above the supported range")


def validate_temperature(value: float) -> None:
    if value < -100.0 or value > 100.0:
        raise ValueError("temperature is outside the supported range")


def validate_non_negative(value: float, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def validate_positive(value: float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def validate_duration(value: int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > 24 * 3600:
        raise ValueError(f"{name} must not exceed one day")


def validate_epoch(value: int, name: str = "time") -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive unix timestamp")
