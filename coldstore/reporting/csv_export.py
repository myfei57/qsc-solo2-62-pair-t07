"""CSV rendering for operator exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor
    from coldstore.console.telemetry import Telemetry

QUOTE_TRIGGERS = (",", '"', "\n")


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(trigger in text for trigger in QUOTE_TRIGGERS):
        return '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a header row and body rows as comma separated text."""

    lines = [",".join(_escape(column) for column in columns)]
    for row in rows:
        lines.append(",".join(_escape(value) for value in row))
    return "\n".join(lines) + "\n"


def audit_export(auditor: Auditor) -> tuple[list[str], list[list[str]]]:
    """Columns and rows for the runtime audit trail."""

    columns = ["time", "kind", "detail", "id"]
    rows = [[entry.time, entry.kind, entry.detail, entry.id] for entry in auditor.entries()]
    return columns, rows


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the telemetry counters."""

    return ["metric", "value"], [[key, value] for key, value in telemetry.as_dict().items()]


def accounting_export(buckets: Sequence[object]) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the itemised hourly load ledger."""

    columns = [
        "hour",
        "inbound_kwh",
        "envelope_kwh",
        "defrost_kwh",
        "total_kwh",
        "inbound_peak_kw",
        "envelope_peak_kw",
        "defrost_peak_kw",
        "peak_kw_upper_bound",
        "records",
        "zero_filled",
    ]
    rows: list[list[object]] = []
    for bucket in buckets:
        data = bucket.as_dict()  # type: ignore[attr-defined]
        rows.append([data[column] for column in columns])
    return columns, rows
