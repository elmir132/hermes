import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import luma  # noqa: E402
from test_luma_campus import FakeLuma, entry, page  # noqa: E402

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
CFG = {"browser_file": "/nonexistent/b.json", "sponsors": [],
       "busy_weekly": [{"name": "evening class", "days": [0, 2], "from": "19:30", "to": "20:45"}],
       "registered": [{"name": "AI For NYC Hackathon", "start": "2026-10-19T18:30", "end": "2026-10-19T22:00", "place": "Queens", "status": "going"},
                      {"name": "Big Weekend Hackathon", "start": "2026-10-24T08:30", "end": "2026-10-25T20:00", "place": "Venue A", "status": "going"},
                      {"name": "Weekend VC Hackathon", "start": "2026-10-24T09:00", "end": "2026-10-24T18:00", "place": "Venue B", "status": "invited"}]}


def run(c):
    return asyncio.run(c)


class RegisteredTests(unittest.TestCase):
    def test_signed_up_events_become_busy_time_but_invited_ones_do_not(self):
        start, end = luma.local("2026-10-24T14:00:00.000Z"), luma.local("2026-10-24T18:00:00.000Z")       # Saturday 10 AM to 2 PM
        self.assertEqual(luma.clashes(start, end, CFG), ["Big Weekend Hackathon (signed up)"])
        self.assertEqual(luma.clashes(start, end, CFG, skip="Big Weekend Hackathon"), [])

    def test_name_matching_is_forgiving_but_not_too_loose(self):
        cfg = CFG
        self.assertTrue(luma.is_registered({"name": "AI For NYC Hackathon!", "slug": "x"}, cfg))
        self.assertTrue(luma.is_registered({"name": "Big Weekend Hackathon 2026", "slug": "y"}, cfg))
        self.assertFalse(luma.is_registered({"name": "AI Agents Build Day", "slug": "z"}, cfg))
        self.assertTrue(luma.is_registered({"name": "Totally different", "slug": "known"}, dict(cfg, registered=[{"name": "N", "slug": "known", "start": "2026-10-19T18:30", "end": "2026-10-19T22:00"}])))

    def test_events_you_already_signed_up_for_are_hidden_from_recommendations(self):
        entries = [entry(1, "AI For NYC Hackathon", "2026-10-19T22:30:00.000Z", "2026-10-20T02:00:00.000Z", slug="mine"),
                   entry(2, "Agent Build Day", "2026-10-21T22:00:00.000Z", "2026-10-22T01:00:00.000Z", slug="new")]
        net = FakeLuma(entries, {"mine": page(), "new": page()})
        ranked, notes = run(luma.find_hackathons(CFG, net.json, net.html, NOW))
        self.assertEqual([r["slug"] for r in ranked], ["new"])
        self.assertTrue(any("already signed up" in n and "mine" in n for n in notes))

    def test_a_recommendation_that_overlaps_a_signed_up_event_is_warned(self):
        entries = [entry(2, "Saturday Agent Hackathon", "2026-10-24T14:00:00.000Z", "2026-10-24T22:00:00.000Z", slug="new")]
        net = FakeLuma(entries, {"new": page()})
        ranked, _ = run(luma.find_hackathons(CFG, net.json, net.html, NOW))
        self.assertIn("Big Weekend Hackathon (signed up)", " ".join(ranked[0]["warnings"]))


class MineViewTests(unittest.TestCase):
    def test_the_list_shows_times_places_and_every_clash(self):
        text = luma.my_hackathons(CFG, NOW)
        self.assertIn("Your upcoming hackathons (3)", text)
        self.assertLess(text.index("AI For NYC"), text.index("Big Weekend"))                  # soonest first
        self.assertIn("Mon Oct 19, 6:30 PM to 10:00 PM · Queens", text)
        self.assertIn("clashes with evening class", text)                                    # 7:30 PM class during the hackathon
        self.assertIn("Weekend VC Hackathon (invited, not registered)", text)               # overlaps the big weekend event
        self.assertIn("invited, not registered", text)

    def test_past_events_drop_off_and_an_empty_list_is_explained(self):
        late = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.assertIn("don't have any upcoming", luma.my_hackathons(CFG, late))
        self.assertIn("don't have any upcoming", luma.my_hackathons({}, NOW))


if __name__ == "__main__":
    unittest.main()
