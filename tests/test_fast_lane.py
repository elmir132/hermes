import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402


class FastLaneSelectionTests(unittest.TestCase):
    def test_only_top_tiers_on_light_readers_plus_named_extras(self):
        cfg = {"companies": [
            {"name": "A", "tier": 1, "provider": "greenhouse"},
            {"name": "B", "tier": 2, "provider": "google"},       # heavy reader: daily only
            {"name": "C", "tier": 3, "provider": "lever"},        # lower tier: daily only
            {"name": "Two Sigma", "tier": 1, "provider": "avature"},
        ]}
        self.assertEqual([c["name"] for c in jobs.fast_lane_companies(cfg)], ["A", "Two Sigma"])
        cfg["fast_lane"] = {"tiers": [1, 2, 3], "include": []}
        self.assertEqual([c["name"] for c in jobs.fast_lane_companies(cfg)], ["A", "C"])

    def test_the_real_config_has_a_sensible_lane(self):
        lane = jobs.fast_lane_companies(jobs.load_config())
        self.assertTrue(20 <= len(lane) <= 120, len(lane))
        self.assertTrue(all(c.get("provider") != "browser" for c in lane))

    def test_quiet_hours_are_new_york_night(self):
        at = lambda h: datetime(2026, 10, 21, h, 30, tzinfo=timezone.utc)   # EDT = UTC-4
        self.assertTrue(jobs.fast_lane_quiet(at(4)))       # 00:30 New York
        self.assertTrue(jobs.fast_lane_quiet(at(10)))      # 06:30
        self.assertFalse(jobs.fast_lane_quiet(at(11)))     # 07:30
        self.assertFalse(jobs.fast_lane_quiet(at(2)))      # 22:30
        self.assertTrue(jobs.fast_lane_quiet(at(3)))       # 23:30


class FastLaneScanTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "h.db")

    def test_known_memory_starts_empty_and_learns_a_baseline(self):
        self.assertIsNone(jobs.fast_lane_known(self.db))
        jobs.fast_lane_remember(self.db, [])
        self.assertEqual(jobs.fast_lane_known(self.db), {"__baseline__"})
        jobs.fast_lane_remember(self.db, ["k1", "k2"])
        self.assertEqual(jobs.fast_lane_known(self.db), {"__baseline__", "k1", "k2"})

    def test_only_roles_not_met_before_are_sent(self):
        from test_jobs import FakeFeeds, gh_job, cfg_with, NOW
        feeds = FakeFeeds({"boards-api": {"jobs": [gh_job(1, "Software Engineer, New Grad"),
                                                    gh_job(2, "Machine Learning Engineer (Junior)")]}})
        cfg = cfg_with([{"name": "Acme", "provider": "greenhouse", "slug": "acme"}])
        met = []
        asyncio.run(jobs.scan(cfg, feeds, self.db, now=NOW, mark=False, candidate_keys=met))
        self.assertEqual(len(met), 2)                                    # first run only learns the backlog
        old = next(k for k in met if k.endswith("1"))
        met2 = []
        text, n, _ = asyncio.run(jobs.scan(cfg, feeds, self.db, now=NOW, mark=False,
                                           exclude_keys={old}, candidate_keys=met2))
        self.assertEqual(n, 1)
        self.assertIn("Machine Learning Engineer (Junior)", text)
        self.assertNotIn("New Grad", text)
        self.assertEqual(sorted(met2), sorted(met))                     # both are still remembered


if __name__ == "__main__":
    unittest.main()
