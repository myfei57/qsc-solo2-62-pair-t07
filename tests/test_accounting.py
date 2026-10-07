"""Behavioural tests for itemised, time integrated load accounting."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.accounting import (
    POLICY,
    Ledger,
    RateLimiter,
    Registry,
    VoucherBook,
    defrost_energy_kwh,
    envelope_power_kw,
    hour_of,
    inbound_energy,
    split_ramp,
)
from coldstore.accounting.registry import PARAMS_KEY
from coldstore.audit import Auditor
from coldstore.store import Store

HOUR = 3600
# Fixed anchor: 100 whole hours after the unix epoch so buckets are exact.
T0 = 100 * HOUR


class TermsCase(unittest.TestCase):
    def test_inbound_energy(self) -> None:
        # 1000 kg warming 8 K at cp 3 kJ/kgK = 24000 kJ = 6.6667 kWh.
        self.assertAlmostEqual(inbound_energy(1000.0, -10.0, -18.0, 3.0), 6.6666667, places=5)
        self.assertEqual(inbound_energy(1000.0, -20.0, -18.0, 3.0), 0.0)
        self.assertEqual(inbound_energy(0.0, -10.0, -18.0, 3.0), 0.0)

    def test_envelope_power_floors_heat_inflow(self) -> None:
        # 0.05 kW/K * 48 K = 2.4 kW.
        self.assertAlmostEqual(envelope_power_kw(30.0, -18.0, 0.05), 2.4)
        self.assertEqual(envelope_power_kw(-20.0, -18.0, 0.05), 0.0)

    def test_defrost_energy(self) -> None:
        self.assertAlmostEqual(defrost_energy_kwh(6.0, 1800), 3.0)
        self.assertEqual(defrost_energy_kwh(6.0, 0), 0.0)

    def test_hour_of(self) -> None:
        self.assertEqual(hour_of(T0), 100)
        self.assertEqual(hour_of(T0 + HOUR - 1), 100)
        self.assertEqual(hour_of(T0 + HOUR), 101)

    def test_split_ramp_trapezoid_within_hour(self) -> None:
        segments = split_ramp(T0, 1.0, T0 + HOUR, 3.0, 4)
        self.assertEqual([hour for hour, _, _ in segments], [100])
        # Average power 2 kW over one hour = 2 kWh, peak 3 kW.
        self.assertAlmostEqual(segments[0][1], 2.0)
        self.assertEqual(segments[0][2], 3.0)

    def test_split_ramp_splits_at_hour_boundary(self) -> None:
        segments = split_ramp(T0 + HOUR // 2, 0.0, T0 + 3 * HOUR // 2, 2.0, 4)
        self.assertEqual([hour for hour, _, _ in segments], [100, 101])
        self.assertAlmostEqual(sum(energy for _, energy, _ in segments), 1.0)
        self.assertEqual(max(peak for _, _, peak in segments), 2.0)

    def test_split_ramp_enforces_budget(self) -> None:
        with self.assertRaises(ValueError):
            split_ramp(T0, 1.0, T0 + 10 * HOUR, 1.0, max_buckets=4)


class LimiterCase(unittest.TestCase):
    def test_burst_then_refill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            limiter = RateLimiter(store, rate_per_hour=60.0, burst=2.0)
            self.assertTrue(limiter.admit(T0).accepted)
            self.assertTrue(limiter.admit(T0).accepted)
            denied = limiter.admit(T0)
            self.assertFalse(denied.accepted)
            self.assertEqual(denied.reason, "rate limited")
            # One hour later one token has refilled.
            self.assertTrue(limiter.admit(T0 + HOUR).accepted)

    def test_event_before_clock_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            limiter = RateLimiter(store, rate_per_hour=60.0, burst=5.0)
            limiter.admit(T0 + HOUR)
            self.assertFalse(limiter.admit(T0).accepted)


class LedgerCase(unittest.TestCase):
    def test_fold_and_zero_filled_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Store.open(f"{tmp}/state.json"))
            ledger.fold("inbound", 100, 5.0, 2.0, count=1)
            ledger.fold("inbound", 100, 3.0, 4.0, count=1)
            ledger.fold("defrost", 102, 1.0, 6.0, count=1)
            buckets = ledger.range(99, 103)
            self.assertEqual([bucket.hour for bucket in buckets], [99, 100, 101, 102, 103])
            self.assertTrue(buckets[0].zero_filled)
            self.assertTrue(buckets[2].zero_filled)
            self.assertFalse(buckets[1].zero_filled)
            self.assertAlmostEqual(buckets[1].inbound_kwh, 8.0)
            self.assertEqual(buckets[1].inbound_peak_kw, 4.0)
            self.assertEqual(buckets[1].records, 2)
            self.assertAlmostEqual(buckets[3].defrost_kwh, 1.0)
            self.assertAlmostEqual(buckets[3].total_kwh, 1.0)

class RegistryCase(unittest.TestCase):
    def _open(self) -> tuple[str, Registry, Auditor, Store]:
        context = tempfile.TemporaryDirectory()
        tmp = context.name
        store = Store.open(f"{tmp}/state.json")
        auditor = Auditor(store)
        registry = Registry(store, auditor)
        registry.set_params(rate_per_hour=200.0, rate_burst=3.0)
        self.addCleanup(context.cleanup)
        return tmp, registry, auditor, store

    def test_register_writes_voucher_and_ledger_on_one_figure(self) -> None:
        _, registry, auditor, store = self._open()
        voucher, bucket = registry.register_batch("b1", T0, 1000.0, -10.0)
        self.assertTrue(voucher.accepted)
        self.assertEqual(voucher.policy, POLICY["version"])
        self.assertAlmostEqual(voucher.energy_kwh, 6.6666667, places=5)
        self.assertIsNotNone(bucket)
        self.assertAlmostEqual(bucket.inbound_kwh, voucher.energy_kwh)
        # Same figure is retrievable from the receipt and reconciles to zero.
        self.assertAlmostEqual(registry.vouchers.get("b1").energy_kwh, voucher.energy_kwh)
        result = registry.reconcile()
        self.assertTrue(result.matches)
        self.assertEqual(result.difference_kwh, 0.0)
        kinds = auditor.count_by_kind()
        self.assertEqual(kinds["accounting-voucher"], 1)
        self.assertEqual(kinds["accounting-ledger"], 1)
        store  # persistence exercised through the registry

    def test_duplicate_batch_conflicts(self) -> None:
        _, registry, _, _ = self._open()
        registry.register_batch("b1", T0, 100.0, -10.0)
        with self.assertRaises(ValueError):
            registry.register_batch("b1", T0 + 10, 100.0, -10.0)

    def test_limiter_shapes_the_flood_without_fabricating_load(self) -> None:
        _, registry, _, _ = self._open()
        accepted = 0
        rejected = 0
        for index in range(6):
            voucher, bucket = registry.register_batch(f"b{index}", T0, 100.0, -10.0)
            if voucher.accepted:
                accepted += 1
                self.assertIsNotNone(bucket)
            else:
                rejected += 1
                self.assertIsNone(bucket)
        self.assertEqual(accepted, 3)
        self.assertEqual(rejected, 3)
        # Rejected batches must not exist as receipts and must not reconcile.
        self.assertEqual(registry.vouchers.count(), 3)
        self.assertTrue(registry.reconcile().matches)

    def test_missing_hours_are_zero_filled_not_carried_forward(self) -> None:
        _, registry, _, _ = self._open()
        registry.register_batch("b1", T0, 1000.0, -10.0)
        buckets = registry.load_range(T0, T0 + 2 * HOUR)
        self.assertEqual([bucket.hour for bucket in buckets], [100, 101, 102])
        self.assertFalse(buckets[0].zero_filled)
        self.assertTrue(buckets[1].zero_filled)
        self.assertTrue(buckets[2].zero_filled)
        self.assertEqual(buckets[1].inbound_kwh, 0.0)
        self.assertEqual(POLICY["missing"], "zero_fill")

    def test_defrost_spans_hours_and_is_itemised_separately(self) -> None:
        _, registry, auditor, _ = self._open()
        energy, buckets = registry.record_defrost("r1", T0 + HOUR // 2, T0 + 3 * HOUR // 2)
        self.assertAlmostEqual(energy, 6.0)  # 6 kW over one hour
        self.assertEqual([bucket.hour for bucket in buckets], [100, 101])
        self.assertAlmostEqual(sum(bucket.defrost_kwh for bucket in buckets), 6.0)
        # Defrost never lands in the inbound term.
        self.assertEqual(sum(bucket.inbound_kwh for bucket in buckets), 0.0)
        self.assertTrue(all(entry.kind != "accounting-voucher" for entry in auditor.entries()))

    def test_envelope_integrates_between_samples(self) -> None:
        _, registry, _, _ = self._open()
        registry.record_envelope(T0, 30.0)
        buckets = registry.record_envelope(T0 + HOUR, 30.0)
        # Constant 2.4 kW for one hour = 2.4 kWh in hour 100.
        self.assertAlmostEqual(buckets[0].envelope_kwh, 2.4)

    def test_integration_budget_rejects_long_span(self) -> None:
        _, registry, _, _ = self._open()
        registry.set_params(max_span_buckets=2)
        with self.assertRaises(ValueError):
            registry.record_defrost("r1", T0, T0 + 5 * HOUR)

    def test_state_totals_and_peak_are_derived(self) -> None:
        _, registry, _, _ = self._open()
        registry.register_batch("b1", T0, 1000.0, -10.0)
        registry.record_defrost("r1", T0, T0 + 1800)
        state = registry.state(T0, T0)
        self.assertEqual(state["policy"]["basis"], "hourly_integral_kwh")
        self.assertGreater(state["totals"]["total_kwh"], 0.0)
        self.assertGreater(state["peak_kw_upper_bound"], 0.0)


if __name__ == "__main__":
    unittest.main()
