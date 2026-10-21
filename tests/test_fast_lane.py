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


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "h.db")

    def test_roles_left_over_beyond_the_cap_stay_new(self):
        self.assertEqual(jobs.fast_lane_to_remember(None, ["a", "b"], []), ["a", "b"])       # baseline
        self.assertEqual(jobs.fast_lane_to_remember(set(), ["a", "b", "c"], ["a", "c"]), ["a", "c"])

    def test_numbers_can_continue_after_an_earlier_list(self):
        from test_jobs import FakeFeeds, gh_job, cfg_with, NOW
        feeds = FakeFeeds({"boards-api": {"jobs": [gh_job(1, "Software Engineer, New Grad")]}})
        cfg = cfg_with([{"name": "Acme", "provider": "greenhouse", "slug": "acme"}])
        text, n, _ = asyncio.run(jobs.scan(cfg, feeds, self.db, now=NOW, mark=False, number_from=13))
        self.assertEqual(n, 1)
        self.assertIn("#13", text)
        self.assertNotIn("#1\n", text)

    def test_a_failed_greenhouse_posting_read_is_shown_as_unchecked(self):
        from test_jobs import FakeFeeds, gh_job, cfg_with, NOW
        feeds = FakeFeeds({"boards-api": {"jobs": [gh_job(1, "Software Engineer, New Grad")]}}, failing=("/jobs/1?",))
        cfg = cfg_with([{"name": "Acme", "provider": "greenhouse", "slug": "acme"}])
        text, n, _ = asyncio.run(jobs.scan(cfg, feeds, self.db, now=NOW, mark=False))
        self.assertEqual(n, 1)
        self.assertIn("posting details not read", text)

    def test_a_company_missing_from_the_browser_file_is_an_error_not_zero_jobs(self):
        import json
        path = os.path.join(tempfile.mkdtemp(), "b.json")
        with open(path, "w") as f:
            json.dump({"fetched_at": "2026-10-20T12:00:00Z", "sources": {"Meta": {"jobs": []}}}, f)
        found, err = jobs.browser_jobs({"name": "Netflix"}, {}, path=path)
        self.assertEqual(found, [])
        self.assertIn("no Netflix section", err)
        found, err = jobs.browser_jobs({"name": "Meta"}, {}, path=path,
                                       now=datetime(2026, 10, 20, 13, tzinfo=timezone.utc))
        self.assertIsNone(err)


class MetaCardTests(unittest.TestCase):
    def test_two_vacancies_with_one_title_are_both_kept_when_they_have_links(self):
        import browser_reader as br
        cards = [{"text": "Software Engineer, Product\nNew York, NY", "href": "/profile/job_details/111"},
                 {"text": "Software Engineer, Product\nMenlo Park, CA", "href": "/profile/job_details/222"},
                 {"text": "Software Engineer, Product\nNew York, NY", "href": "/profile/job_details/111"},
                 {"text": "Data Engineer\nNew York, NY", "href": ""},
                 {"text": "Data Engineer\nNew York, NY", "href": ""}]
        self.assertEqual(len(br.parse_meta_cards(cards)), 3)


if __name__ == "__main__":
    unittest.main()
