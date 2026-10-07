"""Pure heat-term arithmetic and hourly time integration.

Everything in this module is side effect free so the accounting rules can be
unit tested without a store. Energy is always kilowatt hours, power kilowatts,
time unix seconds. Each integrator splits its contribution at hour boundaries;
that is what lets every term be accumulated per whole hour regardless of when a
registration, ambient sample or defrost started.
"""

from __future__ import annotations

from collections.abc import Iterator

BUCKET_SECONDS = 3600
SECONDS_PER_HOUR = 3600.0


def hour_of(at: int) -> int:
    """Index of the hourly bucket that contains ``at``."""

    return at // BUCKET_SECONDS


def inbound_energy(mass_kg: float, product_temp: float, room_temp: float, cp: float) -> float:
    """Sensible heat released while a batch cools to room temperature.

    ``cp`` is kilojoules per kilogram kelvin; the result is floored at zero so a
    batch already colder than the room can never credit negative heat.
    """

    delta = product_temp - room_temp
    if delta <= 0 or mass_kg <= 0:
        return 0.0
    kilojoules = mass_kg * cp * delta
    return kilojoules / 3600.0


def envelope_power_kw(ambient: float, room_temp: float, u_area: float) -> float:
    """Instantaneous fabric heat gain, ``U*A`` in kilowatts per kelvin.

    A cold ambient cannot push heat into the room, so the value is floored.
    """

    delta = ambient - room_temp
    if delta <= 0:
        return 0.0
    return u_area * delta


def defrost_energy_kwh(power_kw: float, seconds: int) -> float:
    """Constant-power defrost energy over its duration."""

    if seconds <= 0 or power_kw <= 0:
        return 0.0
    return power_kw * seconds / SECONDS_PER_HOUR


def split_pulse(at: int, energy_kwh: float, peak_kw: float, max_buckets: int) -> Iterator[tuple[int, float, float]]:
    """Place an instantaneous impulse that then draws ``peak_kw`` briefly.

    All of the energy lands in the bucket of ``at`` (a registration cools inside
    one hour window), and the bucket also receives that constant power as a peak
    candidate. Yields ``(hour, energy_kwh, peak_kw)``.
    """

    if max_buckets < 1:
        raise ValueError("integration span exceeds the performance budget")
    yield hour_of(at), energy_kwh, peak_kw if energy_kwh > 0 else 0.0


def split_ramp(
    start: int,
    start_power: float,
    end: int,
    end_power: float,
    max_buckets: int,
) -> list[tuple[int, float, float]]:
    """Integrate a linear power ramp, splitting trapezoids at hour boundaries.

    Returns one ``(hour, energy_kwh, peak_kw)`` segment per bucket spanned. The
    work is proportional to the number of buckets crossed; callers bound it with
    ``max_buckets`` so a wild timestamp cannot inflate the integration budget.
    """

    if end < start:
        raise ValueError("segment end must not precede its start")
    if end == start:
        return [(hour_of(start), 0.0, max(start_power, end_power))]
    span = hour_of(end) - hour_of(start) + 1
    if span > max_buckets:
        raise ValueError("integration span exceeds the performance budget")

    duration = float(end - start)
    segments: list[tuple[int, float, float]] = []
    current = start
    current_power = start_power
    while current < end:
        boundary = (hour_of(current) + 1) * BUCKET_SECONDS
        nxt = min(boundary, end)
        fraction = (nxt - start) / duration
        nxt_power = start_power + (end_power - start_power) * fraction
        energy = (current_power + nxt_power) / 2.0 * (nxt - current) / SECONDS_PER_HOUR
        segments.append((hour_of(current), energy, max(current_power, nxt_power)))
        current = nxt
        current_power = nxt_power
    return segments
