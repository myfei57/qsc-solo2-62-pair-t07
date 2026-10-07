"""End to end tests for the HTTP control console."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.console.routes import route_table
from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.store import Store


class ConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def add_room(self, room_id: str, zone: int, temp: float) -> None:
        status, _ = self.call("POST", "/room/add", {"id": room_id, "zone": zone, "temp": temp})
        self.assertEqual(status, 200)

    def test_health_snapshot_and_reports(self) -> None:
        status, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertIn("temp:rate-window", health["keys"])
        self.assertTrue(health["path"].endswith("state.json"))

        status, checks = self.call("GET", "/health/checks")
        self.assertEqual(status, 200)
        self.assertEqual(
            {check["name"] for check in checks["checks"]},
            {
                "store",
                "interlock",
                "ammonia",
                "temp",
                "compressors",
                "fans",
                "evap",
                "rooms",
                "accounting",
            },
        )

        status, snapshot = self.call("GET", "/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(snapshot),
            {
                "pipeline",
                "store",
                "rooms",
                "compressors",
                "supply",
                "temp",
                "ammonia",
                "interlock",
                "cold_fans",
                "evap",
                "defrost",
                "load",
                "quota",
                "audit",
                "schedule",
                "accounting",
            },
        )

        status, describe = self.call("GET", "/describe")
        self.assertEqual(status, 200)
        self.assertIn("pipeline", describe)

        status, report = self.call("GET", "/report")
        self.assertEqual(status, 200)
        self.assertIn("coldstore control report", report)

    def test_metadata_routes(self) -> None:
        status, pipeline = self.call("GET", "/pipeline")
        self.assertEqual(status, 200)
        self.assertTrue(pipeline["ordered"])
        self.assertEqual(pipeline["last_stage"], "audit")

        status, catalog = self.call("GET", "/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["routes"]), len(route_table()))
        self.assertGreater(len(catalog["paths"]), 0)

        status, ops = self.call("GET", "/ops")
        self.assertEqual(status, 200)
        self.assertEqual(ops["rooms"], 0)

        status, telemetry = self.call("GET", "/telemetry")
        self.assertEqual(status, 200)
        self.assertEqual(telemetry["compressors"], 0)
        self.assertTrue(telemetry["temp_within"])

        status, version = self.call("GET", "/version")
        self.assertEqual(status, 200)
        self.assertEqual(version["name"], "coldstore-control")

        status, system = self.call("GET", "/system")
        self.assertEqual(status, 200)
        self.assertIn("python", system)

    def test_history_round_trip(self) -> None:
        status, _ = self.call("POST", "/history", {"kind": "manual", "value": "start"})
        self.assertEqual(status, 200)
        status, listing = self.call("GET", "/history")
        self.assertEqual(status, 200)
        self.assertTrue(listing["has_events"])
        self.assertEqual(listing["count"], 2)
        status, _ = self.call("POST", "/history/clear")
        self.assertEqual(status, 200)
        _, listing = self.call("GET", "/history")
        self.assertEqual(listing["count"], 0)
        status, error = self.call("POST", "/history", {})
        self.assertEqual(status, 400)
        self.assertIn("kind", error["error"])

    def test_room_routes(self) -> None:
        self.add_room("r1", 1, -20.0)
        self.add_room("r2", 2, -5.0)

        status, rooms = self.call("GET", "/rooms")
        self.assertEqual(status, 200)
        self.assertEqual(rooms["count"], 2)
        self.assertEqual(rooms["hottest"], "r2")
        self.assertEqual(rooms["coldest"], "r1")
        self.assertEqual(rooms["ids"], ["r1", "r2"])
        self.assertEqual(rooms["unassigned"], ["r1", "r2"])

        status, body = self.call("POST", "/room/assign", {"id": "r1", "compressor": "c1"})
        self.assertEqual(body["compressor"], "c1")
        status, body = self.call("POST", "/room/sensor", {"id": "r1", "sensor": "a1"})
        self.assertEqual(body["sensor"], "a1")
        status, body = self.call("GET", "/room/compressor", {"compressor": "c1"})
        self.assertEqual(body["rooms"], ["r1"])

        status, body = self.call("POST", "/room/lookup", {"id": "r1"})
        self.assertEqual(body["zone"], 1)
        self.assertEqual(body["compressor"], "c1")
        self.assertEqual(body["sensor"], "a1")
        self.assertEqual(body["room"]["temp"], -20.0)

        status, body = self.call("POST", "/room/temp", {"id": "r2", "temp": -12.0})
        self.assertEqual(body["temp"], -12.0)
        status, body = self.call("GET", "/room/temp", {"id": "r2"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["temp"], -12.0)

        status, body = self.call("POST", "/room/renumber", {"id": "r2", "zone": 7})
        self.assertEqual(body["zone"], 7)
        status, _ = self.call("POST", "/room/reset", {})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/room/remove", {"id": "r1"})
        self.assertEqual(body["removed"], "r1")

    def test_temp_routes(self) -> None:
        status, body = self.call("GET", "/temp")
        self.assertEqual(status, 200)
        self.assertEqual(body["window"], 0.5)

        status, body = self.call("POST", "/temp/read", {"value": -18.0})
        self.assertEqual(body["value"], -18.0)
        status, body = self.call("POST", "/temp/read", {"value": -17.5})
        self.assertEqual(body["value"], -17.5)

        status, body = self.call("GET", "/temp/readings")
        self.assertEqual(body["samples"], 2)
        self.assertEqual(body["readings"][0]["value"], -18.0)

        status, body = self.call("GET", "/temp/verdict")
        self.assertEqual(body["current"], -17.5)
        self.assertTrue(body["within"])
        self.assertIn(body["direction"], {"steady", "rising"})

        status, body = self.call("POST", "/temp/window", {"window": 1.5})
        self.assertEqual(body["window"], 1.5)
        status, _ = self.call("POST", "/temp/reset", {})
        self.assertEqual(status, 200)
        _, body = self.call("GET", "/temp/readings")
        self.assertEqual(body["samples"], 0)

    def test_compressor_and_valve_routes(self) -> None:
        status, body = self.call("POST", "/comp/add", {"id": "c1", "stages": 4})
        self.assertEqual(body["stages"], 4)
        status, body = self.call("POST", "/comp/stage", {"id": "c1", "stage": 2})
        self.assertEqual(body["stage"], 2)
        status, body = self.call("POST", "/comp/lookup", {"id": "c1"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["compressor"]["stage"], 2)
        status, body = self.call("POST", "/comp/reserve", {"id": "c1", "room": "r1"})
        self.assertEqual(body["room"], "r1")
        status, body = self.call("GET", "/comp/reservations")
        self.assertEqual(body["reservations"], {"c1": "r1"})
        status, body = self.call("POST", "/comp/release", {"id": "c1", "room": "r1"})
        self.assertEqual(body["room"], "")
        status, body = self.call("POST", "/comp/stop", {"id": "c1"})
        self.assertEqual(body["stopped"], "c1")
        status, body = self.call("POST", "/comp/start", {"id": "c1"})
        self.assertEqual(body["stage"], 1)
        status, body = self.call("GET", "/comp/order")
        self.assertEqual(body["order"], ["c1"])
        self.assertEqual(body["running"], ["c1"])

        status, body = self.call("POST", "/valve/start", {"id": "c1"})
        self.assertTrue(body["open"])
        status, body = self.call("POST", "/valve/position", {"id": "c1", "position": "closed"})
        self.assertFalse(body["open"])
        status, body = self.call("POST", "/valve/open", {"id": "c1"})
        self.assertEqual(body["position"], "open")
        status, body = self.call("POST", "/valve/close", {"id": "c1"})
        self.assertEqual(body["position"], "closed")
        status, body = self.call("GET", "/valve")
        self.assertEqual(body["opened"], [])

        status, body = self.call("POST", "/comp/remove", {"id": "c1"})
        self.assertEqual(body["removed"], "c1")

    def test_fan_and_evap_routes(self) -> None:
        self.call("POST", "/fan/add", {"id": "f1", "role": "primary"})
        self.call("POST", "/fan/add", {"id": "f2", "role": "standby"})
        status, body = self.call("GET", "/fan")
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["primary"], "f1")
        self.assertEqual(body["healthy_standby"], "f2")

        status, body = self.call("POST", "/fan/start", {"id": "f1"})
        self.assertEqual(body["started"], "f1")
        status, body = self.call("POST", "/fan/lookup", {"id": "f1"})
        self.assertTrue(body["fan"]["running"])

        status, body = self.call("POST", "/fan/switch", {"id": "f2"})
        self.assertEqual(body["primary"], "f2")
        status, body = self.call("POST", "/fan/fail", {"id": "f2"})
        self.assertEqual(body["failed"], "f2")
        status, body = self.call("GET", "/fan/faults")
        self.assertEqual(body["faulted"], ["f2"])
        status, body = self.call("POST", "/fan/repair", {"id": "f2"})
        self.assertEqual(body["repaired"], "f2")
        status, body = self.call("POST", "/fan/stop", {"id": "f1"})
        self.assertEqual(body["stopped"], "f1")

        self.call("POST", "/fan/add", {"id": "e1", "role": "primary", "group": "evap"})
        self.call("POST", "/fan/add", {"id": "e2", "role": "standby", "group": "evap"})
        status, body = self.call("POST", "/fan/start", {"id": "e2", "group": "evap"})
        self.assertEqual(body["started"], "e2")
        status, body = self.call("POST", "/evap/baseline", {"baseline": 30.0})
        self.assertEqual(body["baseline"], 30.0)
        status, body = self.call("POST", "/evap/temp", {"temperature": 35.0})
        self.assertTrue(body["above_baseline"])
        status, body = self.call("GET", "/evap")
        self.assertTrue(body["above_baseline"])
        status, body = self.call("POST", "/evap/switch", {"id": "e1"})
        self.assertEqual(body["duty"], "e1")

    def test_defrost_routes(self) -> None:
        self.add_room("r1", 1, -10.0)
        self.add_room("r2", 2, -12.0)
        self.call("POST", "/fan/add", {"id": "f1", "role": "primary"})
        self.call("POST", "/fan/start", {"id": "f1"})

        status, body = self.call("POST", "/defrost/start", {"id": "r1"})
        self.assertEqual(body["started"], "r1")
        self.assertFalse(body["spilled"])
        status, body = self.call("GET", "/defrost")
        self.assertEqual(body["defrosting"], ["r1"])
        self.assertEqual(body["warmups"], ["r1"])

        status, body = self.call("POST", "/defrost/finish", {"id": "r1"})
        self.assertEqual(body["finished"], "r1")
        status, body = self.call("POST", "/defrost/enqueue", {"id": "r2"})
        self.assertEqual(body["pending"], 1)
        status, body = self.call("POST", "/defrost/replay")
        self.assertEqual(body["replayed"], ["r2"])
        status, body = self.call("POST", "/defrost/select", {"zone": 2})
        self.assertEqual(body["room"], "r2")
        status, body = self.call("POST", "/defrost/recover")
        self.assertTrue(body["recovered"])
        self.assertEqual(body["last_warmup"], "r2")

    def test_ammonia_and_interlock_routes(self) -> None:
        status, body = self.call("GET", "/ammonia")
        self.assertEqual(body["threshold"], 25.0)
        self.assertFalse(body["latched"])

        status, body = self.call("POST", "/ammonia/read", {"ppm": 5.0})
        self.assertEqual(body["level"], "ok")
        status, body = self.call("POST", "/ammonia/calibrate", {"factor": 2.0})
        self.assertEqual(body["calibration"], 2.0)
        status, body = self.call("POST", "/ammonia/read", {"ppm": 20.0})
        self.assertEqual(body["level"], "trip")
        self.assertTrue(body["trip"])
        status, body = self.call("GET", "/ammonia/verdict")
        self.assertTrue(body["latched"])
        status, body = self.call("POST", "/ammonia/threshold", {"threshold": 60.0})
        self.assertEqual(body["threshold"], 60.0)
        status, body = self.call("POST", "/ammonia/reset", {})
        self.assertFalse(body["latched"])
        status, body = self.call("GET", "/ammonia")
        self.assertEqual(body["calibration"], 2.0)

        status, body = self.call("POST", "/interlock/trip", {"reason": "ammonia high"})
        self.assertTrue(body["tripped"])
        status, body = self.call("GET", "/interlock")
        self.assertTrue(body["tripped"])
        self.assertEqual(body["reason"], "ammonia high")
        self.assertEqual(body["last"]["reason"], "ammonia high")
        status, body = self.call("GET", "/interlock/trips")
        self.assertEqual(body["count"], 1)
        status, body = self.call("POST", "/interlock/reset", {})
        self.assertFalse(body["tripped"])
        status, body = self.call("POST", "/interlock/clear", {})
        self.assertEqual(body["count"], 0)

    def test_load_and_quota_routes(self) -> None:
        self.add_room("r1", 1, -10.0)
        status, body = self.call("GET", "/load")
        self.assertEqual(body["baseline"], -18.0)
        self.assertEqual(body["total"], 12.0)
        status, body = self.call("POST", "/load/baseline", {"baseline": -20.0})
        self.assertEqual(body["baseline"], -20.0)
        status, body = self.call("POST", "/load/coefficient", {"coefficient": 2.0})
        self.assertEqual(body["coefficient"], 2.0)
        status, body = self.call("POST", "/load/estimate", {"temp": -10.0})
        self.assertEqual(body["load"], 20.0)
        self.assertTrue(body["high"])

        status, body = self.call("POST", "/quota/add", {"amount": 40.0})
        self.assertEqual(body["value"], 40.0)
        status, body = self.call("GET", "/quota")
        self.assertEqual(body["value"], 40.0)
        status, body = self.call("POST", "/quota/remaining", {"limit": 100.0})
        self.assertEqual(body["remaining"], 60.0)
        status, body = self.call("POST", "/quota/check", {"resource": "runtime", "used": 40.0, "limit": 100.0})
        self.assertEqual(body["remaining"], 60.0)
        self.assertTrue(body["ok"])
        status, body = self.call("POST", "/quota/reset", {})
        self.assertEqual(body["value"], 0.0)

    def test_audit_and_schedule_routes(self) -> None:
        self.call("POST", "/temp/read", {"value": -18.0})
        self.call("POST", "/ammonia/read", {"ppm": 3.0})
        status, body = self.call("GET", "/audit")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["entries"], list)

        self.call("POST", "/cycle", {"room_id": "", "comp_id": "", "temp": -5.0, "ppm": 1.0, "amount": 2.0})
        status, body = self.call("GET", "/audit/summary")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["by_kind"], {"cycle": 1})
        self.assertEqual(body["last"]["kind"], "cycle")
        status, body = self.call("POST", "/audit/filter", {"kind": "cycle"})
        self.assertEqual(len(body["entries"]), 1)
        status, body = self.call("GET", "/audit/totals")
        self.assertEqual(body["totals"]["cycle"], -5.0)

        self.add_room("r1", 1, -20.0)
        self.add_room("r2", 2, -8.0)
        status, body = self.call("GET", "/schedule")
        self.assertEqual(body["threshold"], -12.0)
        self.assertEqual(body["due"], ["r2"])
        status, body = self.call("POST", "/schedule/plan")
        self.assertEqual([entry["room_id"] for entry in body["entries"]], ["r2", "r1"])
        self.assertEqual(body["entries"][0]["priority"], 1)
        status, body = self.call("POST", "/schedule/threshold", {"threshold": -25.0})
        self.assertEqual(body["threshold"], -25.0)
        _, body = self.call("GET", "/schedule")
        self.assertEqual(body["due"], ["r2", "r1"])

    def test_cycle_simulation_and_exports(self) -> None:
        self.add_room("r1", 1, -5.0)
        self.call("POST", "/comp/add", {"id": "c1", "stages": 4})
        status, body = self.call(
            "POST",
            "/cycle",
            {"room_id": "r1", "comp_id": "c1", "temp": -5.0, "ppm": 1.0, "amount": 2.0},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["stage"], 1)
        self.assertTrue(body["supply_open"])
        self.assertFalse(body["tripped"])
        self.assertEqual(body["quota"], 2.0)
        self.assertEqual(body["cooling_load"], 19.5)
        self.assertEqual(body["audit_count"], 1)

        status, body = self.call(
            "POST",
            "/cycle",
            {"room_id": "r1", "comp_id": "c1", "temp": -5.0, "ppm": 120.0, "amount": 1.0},
        )
        self.assertTrue(body["tripped"])
        self.assertEqual(body["ammonia_level"], "trip")
        self.assertFalse(body["supply_open"])

        status, body = self.call("POST", "/simulate", {"ticks": 3})
        self.assertEqual(len(body["ticks"]), 3)
        self.assertEqual(body["last_stage"], "audit")
        status, body = self.call("POST", "/simulate", {})
        self.assertEqual(len(body["ticks"]), 5)

        status, body = self.call("GET", "/export/audit")
        self.assertEqual(status, 200)
        self.assertIn("time,kind,detail,id", body)
        status, body = self.call("GET", "/export/telemetry")
        self.assertIn("metric,value", body)
        self.assertIn("store_keys", body)

    def test_metrics_and_errors(self) -> None:
        self.call("GET", "/health")
        status, body = self.call("GET", "/metrics")
        self.assertGreater(body["requests"], 0)
        self.assertIn("GET /health", body["by_route"])
        status, body = self.call("POST", "/metrics/reset", {})
        self.assertTrue(body["reset"])
        _, body = self.call("GET", "/metrics")
        self.assertEqual(body["requests"], 1)

        status, body = self.call("GET", "/missing")
        self.assertEqual(status, 404)
        self.assertIn("no route", body["error"])
        status, body = self.call("DELETE", "/health")
        self.assertEqual(status, 405)
        self.assertIn("not allowed", body["error"])
        status, body = self.call("POST", "/temp/read", {"value": -500})
        self.assertEqual(status, 400)
        self.assertIn("below", body["error"])
        status, body = self.call("POST", "/temp/read", {"value": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("must be a number", body["error"])
        status, body = self.call("POST", "/room/remove", {"id": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])
        status, body = self.call("POST", "/ops/reset-store", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["keys"], 0)

    def test_fan_validation_errors(self) -> None:
        status, error = self.call("POST", "/fan/add", {"id": "f1", "role": "sideways"})
        self.assertEqual(status, 400)
        self.assertIn("role", error["error"])
        status, error = self.call("POST", "/valve/position", {"id": "c1", "position": "half"})
        self.assertEqual(status, 400)
        self.assertIn("valve position", error["error"])
        status, error = self.call("POST", "/room/add", {"id": "r1", "zone": -1})
        self.assertEqual(status, 400)
        self.assertIn("non-negative", error["error"])


class AccountingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)
        self.hour = 100 * 3600

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_register_voucher_ledger_and_policy(self) -> None:
        status, body = self.call(
            "POST",
            "/accounting/register",
            {"batch_id": "b1", "at": self.hour, "mass_kg": 1000.0, "product_temp": -10.0},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        energy = body["voucher"]["energy_kwh"]
        self.assertAlmostEqual(energy, 6.6666667, places=5)
        self.assertEqual(body["bucket"]["inbound_kwh"], energy)
        self.assertEqual(body["voucher"]["policy"], "accounting-v1")

        status, body = self.call("POST", "/accounting/voucher", {"batch_id": "b1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["voucher"]["energy_kwh"], energy)

        status, body = self.call("GET", "/accounting/reconcile")
        self.assertEqual(status, 200)
        self.assertTrue(body["matches"])
        self.assertEqual(body["difference_kwh"], 0.0)
        self.assertEqual(body["basis"], "hourly_integral_kwh")

        status, audit = self.call("GET", "/audit")
        kinds = {entry["kind"] for entry in audit["entries"]}
        self.assertIn("accounting-voucher", kinds)
        self.assertIn("accounting-ledger", kinds)

    def test_duplicate_conflict_and_missing_404(self) -> None:
        payload = {"batch_id": "b1", "at": self.hour, "mass_kg": 100.0, "product_temp": -10.0}
        self.call("POST", "/accounting/register", payload)
        status, body = self.call("POST", "/accounting/register", payload)
        self.assertEqual(status, 409)
        self.assertIn("already registered", body["error"])
        status, body = self.call("POST", "/accounting/voucher", {"batch_id": "ghost"})
        self.assertEqual(status, 404)

    def test_limiter_shapes_flood_and_resets_with_params(self) -> None:
        status, _ = self.call(
            "POST",
            "/accounting/params",
            {"rate_per_hour": 1.0, "rate_burst": 2.0},
        )
        self.assertEqual(status, 200)
        accepted = 0
        rejected = 0
        for index in range(4):
            status, body = self.call(
                "POST",
                "/accounting/register",
                {"batch_id": f"b{index}", "at": self.hour, "mass_kg": 10.0, "product_temp": -10.0},
            )
            if status == 429:
                rejected += 1
                self.assertFalse(body["accepted"])
            else:
                accepted += 1
        self.assertEqual((accepted, rejected), (2, 2))
        # After an hour the single per-hour token has refilled.
        status, body = self.call(
            "POST",
            "/accounting/register",
            {"batch_id": "b9", "at": self.hour + 3600, "mass_kg": 10.0, "product_temp": -10.0},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])

    def test_load_composition_zero_fill_and_export(self) -> None:
        self.call(
            "POST",
            "/accounting/register",
            {"batch_id": "b1", "at": self.hour, "mass_kg": 1000.0, "product_temp": -10.0},
        )
        self.call(
            "POST",
            "/accounting/defrost",
            {"room_id": "r1", "start": self.hour, "end": self.hour + 1800},
        )
        status, body = self.call(
            "POST",
            "/accounting/load",
            {"start": self.hour, "end": self.hour + 2 * 3600},
        )
        self.assertEqual(status, 200)
        buckets = body["buckets"]
        self.assertEqual(len(buckets), 3)
        self.assertFalse(buckets[0]["zero_filled"])
        self.assertTrue(buckets[1]["zero_filled"])
        self.assertAlmostEqual(buckets[0]["defrost_kwh"], 3.0)
        self.assertEqual(buckets[0]["envelope_kwh"], 0.0)
        self.assertGreater(buckets[0]["inbound_kwh"], 0.0)

        status, csv_body = self.call(
            "POST",
            "/export/accounting",
            {"start": self.hour, "end": self.hour + 3600},
        )
        self.assertEqual(status, 200)
        self.assertIn("inbound_kwh,envelope_kwh,defrost_kwh", csv_body)

    def test_state_carries_policy_and_budget(self) -> None:
        status, body = self.call("GET", "/accounting")
        self.assertEqual(status, 200)
        self.assertEqual(body["policy"]["basis"], "hourly_integral_kwh")
        self.assertEqual(body["policy"]["missing"], "zero_fill")
        self.assertIn("max_span_buckets", body["params"])
        self.assertIn("tokens", body["limiter"])

    def test_budget_rejects_long_defrost(self) -> None:
        self.call("POST", "/accounting/params", {"max_span_buckets": 2})
        status, body = self.call(
            "POST",
            "/accounting/defrost",
            {"room_id": "r1", "start": self.hour, "end": self.hour + 5 * 3600},
        )
        self.assertEqual(status, 400)
        self.assertIn("performance budget", body["error"])

    def test_validation_errors(self) -> None:
        status, body = self.call(
            "POST",
            "/accounting/register",
            {"batch_id": "", "at": self.hour, "mass_kg": 10.0, "product_temp": -10.0},
        )
        self.assertEqual(status, 400)
        status, body = self.call(
            "POST",
            "/accounting/register",
            {"batch_id": "b1", "at": self.hour, "mass_kg": -1.0, "product_temp": -10.0},
        )
        self.assertEqual(status, 400)
        self.assertIn("mass", body["error"])


class WsgiCase(unittest.TestCase):
    def test_wsgi_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            seed_defaults(store)
            server = Server(store)
            captured: dict[str, object] = {}

            def start_response(status, headers):
                captured["status"] = status
                captured["headers"] = headers

            payload = json.dumps({"value": -12.0}).encode("utf-8")

            class Stream:
                def read(self, size: int) -> bytes:
                    return payload

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/temp/read",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(payload)),
                "wsgi.input": Stream(),
            }
            body = b"".join(server(environ, start_response))
            self.assertEqual(captured["status"], "200 OK")
            self.assertEqual(json.loads(body)["value"], -12.0)
            store.close()


if __name__ == "__main__":
    unittest.main()
