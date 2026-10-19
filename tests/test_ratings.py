import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402


class RatingTests(unittest.TestCase):
    def test_tier_follows_the_sum_of_the_four_scores(self):
        r = lambda p, s, w, e: {"pay": p, "prestige": s, "wlb": w, "exits": e}
        self.assertEqual(jobs.tier_from_ratings(r(5, 5, 4, 5)), 1)
        self.assertEqual(jobs.tier_from_ratings(r(4, 4, 4, 3)), 2)
        self.assertEqual(jobs.tier_from_ratings(r(3, 3, 3, 3)), 3)
        self.assertEqual(jobs.tier_from_ratings(r(2, 2, 3, 2)), 4)

    def test_label_and_sponsor_hint_are_marked_as_estimates(self):
        r = {"pay": 5, "prestige": 5, "wlb": 3, "exits": 5, "sponsor": True}
        self.assertEqual(jobs.rating_label(r), " · pay 5 · prestige 5 · WLB 3 · exits 5")
        self.assertEqual(jobs.rating_label(None), "")
        self.assertEqual(jobs.visa_flags("", r), ["visa: not stated (the company usually sponsors; estimate)"])
        self.assertEqual(jobs.visa_flags("", dict(r, sponsor=False)), ["visa: not stated (the company rarely sponsors; estimate)"])
        self.assertEqual(jobs.visa_flags(""), ["visa: not stated"])

    def test_the_posting_text_always_beats_the_estimate(self):
        r = {"pay": 3, "prestige": 3, "wlb": 3, "exits": 3, "sponsor": True}
        self.assertEqual(jobs.visa_flags("We are unable to sponsor visas for this role.", r), ["⚠️ says no sponsorship"])

    def test_every_rated_company_has_valid_scores_and_a_matching_tier(self):
        for c in jobs.load_config()["companies"]:
            r = c.get("ratings")
            if not r:
                continue
            for k in ("pay", "prestige", "wlb", "exits"):
                self.assertIn(r[k], (1, 2, 3, 4, 5), (c["name"], k))
            self.assertIn(r.get("sponsor"), (True, False, None))
            self.assertEqual(c["tier"], jobs.tier_from_ratings(r), c["name"])


if __name__ == "__main__":
    unittest.main()
