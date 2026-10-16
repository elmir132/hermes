import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import browser_reader as br  # noqa: E402
import luma  # noqa: E402
from test_luma_campus import FakeLuma, entry, page  # noqa: E402

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def write_file(events, fetched=NOW, error=None):
    path = os.path.join(tempfile.mkdtemp(), "browser_jobs.json")
    with open(path, "w") as f:
        json.dump({"fetched_at": fetched.isoformat(), "sources": {}, "events": {"Cerebral Valley": {"events": events, "error": error}}}, f)
    return path


class CardParserTests(unittest.TestCase):
    def test_cards_become_events_without_tracking_parameters(self):
        cards = [{"href": "https://luma.com/hacksprint-nyc?utm_source=cv-events&utm_medium=x", "card": "Oct\n16\nDaytona HackSprint\nFri, Oct 16 · 9:00 AM EDT"},
                 {"href": "https://innodata.com/events/h?utm_source=cv", "card": "Nov\n14\nInnodata Hackathon 2026\nSat, Nov 14 · 9:00 AM EDT"},
                 {"href": "https://luma.com/x", "card": "just some text"}, {"href": "/relative", "card": "Oct\n1\nT\nFri, Oct 1 · 9:00 AM EDT"}]
        found = br.parse_cerebral_cards(cards)
        self.assertEqual([e["name"] for e in found], ["Daytona HackSprint", "Innodata Hackathon 2026"])
        self.assertEqual(found[0]["url"], "https://luma.com/hacksprint-nyc")

    def test_duplicates_are_dropped(self):
        card = {"href": "https://luma.com/a?utm=1", "card": "Oct\n16\nA Hackathon\nFri, Oct 16 · 9:00 AM EDT"}
        self.assertEqual(len(br.parse_cerebral_cards([card, dict(card, href="https://luma.com/a?utm=2")])), 1)


class PartialRunTests(unittest.TestCase):
    def test_a_partial_run_keeps_the_rest_of_the_file(self):
        path = os.path.join(tempfile.mkdtemp(), "b.json")
        with open(path, "w") as f:
            json.dump({"fetched_at": "2026-10-14T07:00:00+00:00", "sources": {"Meta": {"jobs": [{"title": "T"}], "error": None}}, "events": {}}, f)
        import browser_reader
        calls = []

        class FakePage:
            def close(self): pass

        class FakeContext:
            def new_page(self): return FakePage()

        class FakeBrowser:
            def new_context(self, **k): return FakeContext()
            def close(self): pass

        class FakePW:
            chromium = type("C", (), {"launch": staticmethod(lambda **k: FakeBrowser())})()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import types
        fake_module = types.SimpleNamespace(sync_playwright=lambda: FakePW())
        sys.modules["playwright"] = types.SimpleNamespace(sync_api=fake_module)
        sys.modules["playwright.sync_api"] = fake_module
        original = browser_reader.read_events
        browser_reader.read_events = lambda page, name: ([{"name": "E", "when": "x", "url": "https://luma.com/e"}], None)
        try:
            result = browser_reader.run(only="Cerebral Valley", out=path)
        finally:
            browser_reader.read_events = original
        self.assertEqual(result["sources"]["Meta"]["jobs"][0]["title"], "T")               # kept
        self.assertEqual(result["events"]["Cerebral Valley"]["events"][0]["name"], "E")    # updated
        self.assertEqual(result["fetched_at"], "2026-10-14T07:00:00+00:00")


class WhenTests(unittest.TestCase):
    def test_dates_are_read_in_new_york_time(self):
        w = luma.parse_when("Fri, Oct 16 · 9:00 AM EDT", NOW)
        self.assertEqual((w.month, w.day, w.hour, w.minute, w.year), (10, 16, 9, 0, 2026))
        self.assertEqual(luma.parse_when("Thu, Oct 22 · 6:00 PM EDT", NOW).hour, 18)
        self.assertEqual(luma.parse_when("Sat, Jan 09 · 12:30 PM EST", NOW).year, 2027)         # the nearest future date
        self.assertIsNone(luma.parse_when("sometime soon", NOW))
        self.assertIsNone(luma.parse_when("", NOW))


class BrowserEventTests(unittest.TestCase):
    EVENTS = [{"name": "Daytona HackSprint", "when": "Fri, Oct 16 · 9:00 AM EDT", "url": "https://luma.com/hacksprint-nyc"},
              {"name": "Innodata Hackathon 2026", "when": "Sat, Nov 14 · 9:00 AM EDT", "url": "https://innodata.com/events/h"},
              {"name": "Bad", "when": "someday", "url": "https://x"}]

    def test_events_are_read_and_luma_links_get_a_slug(self):
        events, notes = luma.browser_events({}, NOW, write_file(self.EVENTS))
        self.assertEqual([e["name"] for e in events], ["Daytona HackSprint", "Innodata Hackathon 2026"])
        self.assertEqual((events[0]["slug"], events[1]["slug"]), ("hacksprint-nyc", ""))
        self.assertTrue(events[0]["end_guessed"])

    def test_a_missing_or_old_file_is_reported_not_fatal(self):
        events, notes = luma.browser_events({}, NOW, "/nonexistent.json")
        self.assertEqual(events, [])
        self.assertIn("no Cerebral Valley list", notes[0])
        old = write_file(self.EVENTS, fetched=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertIn("days old", luma.browser_events({}, NOW, old)[1][0])
        self.assertIn("HTTP 403", luma.browser_events({}, NOW, write_file([], error="HTTP 403"))[1][0])


class CalendarTests(unittest.TestCase):
    def test_calendars_are_paged_and_only_new_york_events_kept(self):
        class Net(FakeLuma):
            async def json(self, url):
                self.calls.append(url)
                if "calendar/get-items" in url:
                    if "pagination_cursor=c1" in url:
                        return 200, {"entries": [entry(3, "Berlin Hackathon", "2026-11-05T09:00:00.000Z", "2026-11-05T17:00:00.000Z")], "has_more": False}
                    return 200, {"entries": [entry(1, "NYC Agents Hackathon", "2026-11-02T14:00:00.000Z", "2026-11-02T22:00:00.000Z", slug="h1"),
                                             entry(2, "London Hackathon", "2026-11-03T14:00:00.000Z", "2026-11-03T22:00:00.000Z", slug="h2")], "has_more": True, "next_cursor": "c1"}
                return 200, {"entries": [], "has_more": False}
        net = Net([], {"h1": page()})
        # make the fake's second and third events look like other cities
        orig = luma.entry_to_event

        def tagged(e):
            ev = orig(e)
            ev["place"] = "New York, NY" if ev["slug"] == "h1" else "London, UK" if ev["slug"] == "h2" else "Berlin, Germany"
            return ev
        luma.entry_to_event = tagged
        try:
            cfg = {"calendars": [{"name": "Test", "calendar_api_id": "cal-x"}], "sponsors": []}
            ranked, notes = run(luma.find_hackathons(cfg, net.json, net.html, NOW))
        finally:
            luma.entry_to_event = orig
        self.assertEqual([r["slug"] for r in ranked], ["h1"])
        self.assertTrue(any("pagination_cursor=c1" in c for c in net.calls))


class CerebralEndToEndTests(unittest.TestCase):
    def test_luma_links_use_the_real_times_and_other_sites_stay_labeled(self):
        events = [{"name": "Daytona HackSprint", "when": "Fri, Oct 16 · 9:00 AM EDT", "url": "https://luma.com/hacksprint-nyc"},
                  {"name": "Innodata Hackathon 2026", "when": "Sat, Nov 14 · 9:00 AM EDT", "url": "https://innodata.com/events/h"}]
        net = FakeLuma([], {"hacksprint-nyc": page("Build AI agents. $20,000 prizes.", start="2026-10-16T13:00:00.000Z", end="2026-10-16T19:30:00.000Z")})
        cfg = {"browser_file": write_file(events), "sponsors": []}
        ranked, notes = run(luma.find_hackathons(cfg, net.json, net.html, NOW))
        names = {r["name"]: r for r in ranked}
        self.assertEqual(set(names), {"Daytona HackSprint", "Innodata Hackathon 2026"})
        self.assertFalse(names["Daytona HackSprint"].get("end_guessed"))                       # the Luma page set the real times
        self.assertEqual(names["Daytona HackSprint"]["end"], "2026-10-16T19:30:00.000Z")
        self.assertTrue(names["Innodata Hackathon 2026"]["end_guessed"])
        text = luma.format_hackathons(ranked, notes)
        self.assertIn("https://innodata.com/events/h", text)
        self.assertIn("details not read (not a Luma page)", text)

    def test_a_guessed_end_time_earns_no_length_bonus_and_clashes_use_three_hours(self):
        ev = {"name": "X Hackathon", "start": "2026-10-23T13:00:00+00:00", "end": "2026-10-23T21:00:00+00:00", "end_guessed": True, "online": False, "host": "", "waitlist": False}
        cfg = {"busy_dates": [{"name": "late thing", "from": "2026-10-23T14:00", "to": "2026-10-23T15:00"}]}
        score, reasons, warnings = luma.score_event(ev, {}, cfg, NOW)
        self.assertFalse(warnings)                                 # 9 AM to noon only: no clash with a 2 PM block
        longer = dict(ev, end_guessed=False)
        self.assertGreater(luma.score_event(longer, {}, {}, NOW)[0], luma.score_event(ev, {}, {}, NOW)[0])


if __name__ == "__main__":
    unittest.main()
