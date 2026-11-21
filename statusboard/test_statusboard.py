import io
import ipaddress
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import probe
import statusboard as board
from check_public import check


class StatusboardTest(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.db = board.connect(self.root / "readings.sqlite")
        self.addCleanup(self.db.close)
        self.cfg = {"series": "control-v1", "source": "control_plane", "locations": {"NL": "Netherlands"}}
        self.now = 1_800_000_000

    def record(self, slots_ago, state, offset=20):
        board.record(self.db, self.cfg, self.now - slots_ago * board.INTERVAL + offset, {"NL": state})

    def test_incident_reproduces_four_gaps_and_one_consistent_percentage(self):
        for index in range(1, 97):
            if index not in {46, 47, 48, 49}:
                self.record(index, "up")
        self.record(97, "down")  # previous window
        self.record(0, "up")  # current status, outside the closed history
        payload = board.snapshot(self.db, self.cfg, self.now + 30)
        self.assertEqual(payload["uptime"], 100)
        self.assertEqual(payload["coverage"], {"measured": 92, "maintenance": 0, "unknown": 4, "total": 96})
        self.assertEqual(payload["uptimeChecks"], 92)
        self.assertEqual(payload["locations"], [["Netherlands", "working"]])
        self.assertEqual(len(payload["history"]), 96)

    def test_duplicate_same_slot_does_not_replace_the_first_observation(self):
        self.record(1, "up")
        self.record(1, "down", offset=40)
        self.assertEqual(board.snapshot(self.db, self.cfg, self.now)["uptimeChecks"], 1)
        self.assertEqual(board.snapshot(self.db, self.cfg, self.now)["uptime"], 100)

    def test_retry_can_fill_an_entirely_unknown_slot(self):
        self.record(1, "unknown")
        self.record(1, "down", offset=40)
        self.record(1, "up", offset=60)
        payload = board.snapshot(self.db, self.cfg, self.now)
        self.assertEqual(payload["history"][-1]["state"], "down")
        self.assertEqual(payload["uptime"], 0)

    def test_unknown_disk_states_never_turn_green(self):
        self.record(1, "up")
        self.record(0, "up")
        with self.db:
            self.db.execute("UPDATE readings SET states = ?", ('{"NL":"new-unrecognized-status"}',))
        payload = board.snapshot(self.db, self.cfg, self.now + 50)
        self.assertEqual(payload["history"][-1]["state"], "unknown")
        self.assertEqual(payload["locations"], [["Netherlands", "unknown"]])
        self.assertIsNone(payload["uptime"])

    def test_retention_limits_one_series_and_does_not_prune_history_by_wall_clock(self):
        with patch("statusboard.RETAIN_SLOTS", 2):
            self.record(3, "up")
            board.record(self.db, {**self.cfg, "series": "other"}, self.now - 5 * board.INTERVAL, {"NL": "up"})
            self.record(2, "up")
            self.record(-1000, "up")
        retained = self.db.execute("SELECT series, slot FROM readings ORDER BY slot").fetchall()
        self.assertEqual(retained, [
            ("other", self.now - 5 * board.INTERVAL),
            ("control-v1", self.now - 2 * board.INTERVAL),
            ("control-v1", self.now + 1000 * board.INTERVAL),
        ])

    def test_unknown_and_maintenance_are_excluded_degraded_counts_as_failure(self):
        self.cfg["maintenance"] = [{"location": "NL", "start": self.now - 2 * board.INTERVAL,
                                    "end": self.now - board.INTERVAL}]
        for index, state in enumerate(("unknown", "down", "up", "degraded", "unknown"), 1):
            self.record(index, state)
        snap = board.snapshot(self.db, self.cfg, self.now)
        self.assertEqual(snap["uptime"], 50)
        self.assertEqual(snap["coverage"], {"measured": 2, "maintenance": 1, "unknown": 93, "total": 96})

    def test_rounding_agrees_with_frontend(self):
        for index in range(1, 33):
            self.record(index, "up" if index == 1 else "down")
        self.assertEqual(board.snapshot(self.db, self.cfg, self.now)["uptime"], 3.13)

    def test_stale_and_future_observations_and_incompatible_series(self):
        self.record(2, "up")
        self.record(0, "up", offset=60)
        snap = board.snapshot(self.db, self.cfg, self.now + 20)
        self.assertEqual(snap["locations"], [["Netherlands", "unknown"]])
        self.assertEqual(snap["uptime"], 100)
        new = board.snapshot(self.db, {**self.cfg, "series": "vpn-v1", "source": "vpn_e2e"}, self.now + 20)
        self.assertIsNone(new["uptime"])
        self.assertIsNone(new["observedAt"])
        self.assertEqual(new["coverage"]["unknown"], 96)

    def test_panel_handles_duplicates_partial_and_disabled_without_false_green(self):
        def node(country, connected, disabled=False):
            return {"countryCode": country, "isConnected": connected, "isDisabled": disabled}

        records = [node("NL", True), node("NL", False), node("DE", True, True), node("FI", "false"),
                   node("PL", False)]
        expected = {"NL": "degraded", "DE": "unknown", "SE": "unknown", "FI": "unknown", "PL": "down"}
        for ordered in (records, list(reversed(records))):
            self.assertEqual(board.panel_observation(ordered, expected), expected)

    def test_panel_timeout_is_unknown_and_independent_of_beszel(self):
        legacy = SimpleNamespace(http_json=Mock(side_effect=TimeoutError), panel_token=lambda: "test-token")
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            states = board.collect({**self.cfg, "panel_url": "http://127.0.0.1/mock"}, legacy)
        self.assertEqual(states, {"NL": "unknown"})
        self.assertEqual(stderr.getvalue().strip(), "panel observation unavailable: TimeoutError")

    def test_probe_cache_distinguishes_failed_stale_future_and_invalid(self):
        cache = self.root / "probe.json"
        routes = {"NL": ["a", "b"], "FI": ["c"], "PL": ["d"]}
        entries = {
            "a": {"kind": "vpn_e2e", "state": "ok", "checked_at": self.now - 2},
            "b": {"kind": "vpn_e2e", "state": "failed", "checked_at": self.now - 3},
            "c": {"kind": "vpn_e2e", "state": "failed", "checked_at": self.now - 181},
            "d": {"kind": "vpn_e2e", "state": "ok", "checked_at": self.now + 1},
        }
        for version, expected in ((1, {"NL": "degraded", "FI": "unknown", "PL": "unknown"}),
                                  (True, dict.fromkeys(routes, "unknown"))):
            cache.write_text(json.dumps({"version": version, "routes": entries}))
            self.assertEqual(board.probe_observation(cache, routes, self.now), expected)

    def test_publish_failure_leaves_the_previous_snapshot_intact(self):
        public = self.root / "status.json"
        board.write_json(public, {"old": True}, 0o644)
        with patch("statusboard.os.replace", side_effect=PermissionError), self.assertRaises(PermissionError):
            board.write_json(public, {"new": True}, 0o644)
        self.assertEqual(json.loads(public.read_bytes()), {"old": True})
        self.assertEqual(list(self.root.glob(".status.json.*")), [])

    @unittest.skipUnless(os.name == "posix", "systemd production locks use flock")
    def test_process_lock_prevents_duplicate_collectors(self):
        lock = self.root / "collect.lock"
        with board.process_lock(lock):
            with self.assertRaises(BlockingIOError), board.process_lock(lock):
                self.fail("second collector acquired the lock")
            self.record(0, "up")
        with board.process_lock(lock):
            self.assertEqual(board.snapshot(self.db, self.cfg, self.now + 30)["locations"][0][1], "working")

    def test_probe_requires_independent_control_and_expected_exit_ip(self):
        direct = ipaddress.ip_address("192.0.2.1")
        wanted = ipaddress.ip_address("192.0.2.2")
        different = ipaddress.ip_address("192.0.2.3")
        scenarios = (
            (direct, [wanted], "ok"),
            (direct, [different, different], "needs_control"),
            (direct, [different, wanted], "ok"),
            (direct, [different, None], "unknown"),
            (direct, [None, None], "needs_control"),
            (wanted, [], "unknown"),
        )
        for control, answers, expected in scenarios:
            with self.subTest(expected=expected), patch("probe.socket.create_connection"), patch(
                "probe.request", side_effect=answers
            ):
                self.assertEqual(probe.route_state("https://example.invalid", 10881, {wanted}, control), expected)

    def test_failed_routes_share_one_control_recheck(self):
        address = ipaddress.ip_address("192.0.2.1")
        settings = {"verifier": "https://example.invalid", "routes": {
            "exampleprobe001": {"port": 10881, "expected_egress": ["192.0.2.10"]},
            "exampleprobe002": {"port": 10882, "expected_egress": ["192.0.2.20"]},
        }}
        for confirmation, expected in ((address, "failed"), (None, "unknown")):
            with self.subTest(confirmation=confirmation), patch("probe.route_state", return_value="needs_control"), patch(
                "probe.request", side_effect=[None, address, confirmation]
            ) as request:
                result = probe.run(settings)
            self.assertEqual([r["state"] for r in result["routes"].values()], [expected, expected])
            self.assertEqual(request.call_count, 3)

    def test_socks_url_explicitly_overrides_environment_proxy_and_resolves_remotely(self):
        response = SimpleNamespace(returncode=0, stdout=b"ip=192.0.2.26\n")
        with patch("probe.subprocess.run", return_value=response) as run:
            self.assertEqual(str(probe.request("https://example.invalid", 10881)), "192.0.2.26")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--proxy") + 1], "socks5h://127.0.0.1:10881")
        self.assertEqual(command[command.index("--noproxy") + 1], "")

    def test_external_check_distinguishes_publisher_collector_and_missing_source(self):
        self.record(0, "up")
        snap = board.snapshot(self.db, self.cfg, self.now + 30)
        self.assertEqual(check(snap, self.now + 30), "ok")
        self.assertEqual(check(snap, self.now + 1300), "publisher_stale")
        updated = board.snapshot(self.db, self.cfg, self.now + 1300)
        self.assertEqual(check(updated, self.now + 1300), "collector_stale")
        snap["locations"][0][1] = "unknown"
        self.assertEqual(check(snap, self.now + 30), "missing_observations")


if __name__ == "__main__":
    unittest.main()
