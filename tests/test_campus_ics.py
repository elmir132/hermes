import asyncio
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import campus  # noqa: E402

TODAY = date(2026, 10, 15)
CFG = {"busy_dates": [{"name": "football game", "from": "2026-10-22T16:00", "to": "2026-10-22T18:00"}], "busy_weekly": []}
TIERS = {"Palantir": 2, "Seagate": 3}


def vevent(summary, start, end="", loc="", desc="", extra=""):
    lines = ["BEGIN:VEVENT", f"SUMMARY:{summary}", f"DTSTART{start}"]
    if end:
        lines.append(f"DTEND{end}")
    if loc:
        lines.append(f"LOCATION:{loc}")
    if desc:
        lines.append(f"DESCRIPTION:{desc}")
    if extra:
        lines.append(extra)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def cal(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "\r\n".join(events) + "\r\nEND:VCALENDAR\r\n"


PALANTIR = vevent("Palantir On-Campus Info Session", ";TZID=America/New_York:20261022T173000", ";TZID=America/New_York:20261022T183000", "BLM 161X",
                  "Learn about Palantir. Sponsorship is available. Attendance is mandatory for all CTCM events once registered. We enforce a three-strike policy.")


class ParseTests(unittest.TestCase):
    def test_time_zones_are_converted_to_new_york(self):
        ev = campus.parse_ics(cal(vevent("A", ":20261022T213000Z", ":20261022T223000Z"), vevent("B", ";TZID=America/New_York:20261022T173000", ":20261022T230000Z")))
        self.assertEqual(ev[0]["start"].hour, 17)                                  # 21:30 UTC is 5:30 PM in New York (daylight time)
        self.assertEqual(ev[1]["start"].hour, 17)

    def test_all_day_events_missing_end_and_cancelled_events(self):
        ev = campus.parse_ics(cal(vevent("All day", ";VALUE=DATE:20261030"), vevent("No end", ":20261022T213000Z"), vevent("Gone", ":20261022T213000Z", extra="STATUS:CANCELLED")))
        self.assertEqual([e["title"] for e in ev], ["All day", "No end"])
        self.assertEqual(ev[1]["end"] - ev[1]["start"], campus.timedelta(hours=1))

    def test_folded_lines_and_escapes(self):
        text = "BEGIN:VEVENT\r\nSUMMARY:Info Session\\, Part\r\n  Two\r\nDTSTART:20261022T213000Z\r\nDTEND:20261022T223000Z\r\nEND:VEVENT"
        self.assertEqual(campus.parse_ics(text)[0]["title"], "Info Session, Part Two")

    def test_garbage_and_broken_events_do_not_crash(self):
        for bad in ["", "not a calendar", "BEGIN:VEVENT\r\nSUMMARY:x\r\nEND:VEVENT", "BEGIN:VEVENT\r\nDTSTART:garbage\r\nSUMMARY:x\r\nEND:VEVENT"]:
            self.assertEqual(campus.parse_ics(bad), [], bad)


class SessionTests(unittest.TestCase):
    ALL = cal(PALANTIR,
              vevent("Seagate Virtual Info Session", ":20261022T203000Z", ":20261022T211000Z", "Virtual", "Open to all students, including international students on CPT for STEM roles at the Master's level or above."),
              vevent("Yoga on the lawn", ":20261023T210000Z", ":20261023T220000Z", "Lawn"),
              vevent("Random Co Recruiting Night", ":20261024T230000Z", ":20261025T010000Z", "Zoom"),
              vevent("Old Co Info Session", ":20261001T210000Z", ":20261001T220000Z"),
              vevent("Far Away Co Info Session", ":20271201T210000Z", ":20271201T220000Z"))

    def test_only_future_career_events_in_the_window_are_kept(self):
        got = campus.ics_sessions(self.ALL, "", CFG, TIERS, TODAY)
        self.assertEqual([s["company"] for s in got], ["Seagate Virtual Info Session", "Palantir On-Campus Info Session", "Random Co Recruiting Night"])

    def test_mode_eligibility_clashes_and_tier(self):
        by = {s["company"]: s for s in campus.ics_sessions(self.ALL, "", CFG, TIERS, TODAY)}
        pal, sea = by["Palantir On-Campus Info Session"], by["Seagate Virtual Info Session"]
        self.assertEqual(pal["mode"], "on campus")
        self.assertEqual(pal["tier"], 2)
        self.assertEqual(pal["clashes"], ["football game"])                           # 5:30 PM starts inside the 4 to 6 PM game
        self.assertEqual(sea["mode"], "Zoom or virtual")
        self.assertIn("CPT", sea["who"])
        self.assertEqual(by["Random Co Recruiting Night"]["mode"], "Zoom or virtual")

    def test_registered_events_are_flagged_from_the_personal_feed(self):
        got = {s["company"]: s for s in campus.ics_sessions(self.ALL, cal(PALANTIR), CFG, TIERS, TODAY)}
        self.assertTrue(got["Palantir On-Campus Info Session"]["registered"])
        self.assertFalse(got["Seagate Virtual Info Session"]["registered"])

    def test_the_career_management_rule_shows_only_for_a_registered_career_event(self):
        with_reg = campus.format_ics_sessions(campus.ics_sessions(self.ALL, cal(PALANTIR), CFG, TIERS, TODAY))
        without = campus.format_ics_sessions(campus.ics_sessions(self.ALL, "", CFG, TIERS, TODAY))
        self.assertIn("three strikes", with_reg)
        self.assertIn("you are registered", with_reg)
        self.assertNotIn("three strikes", without)
        self.assertIn("clashes with football game", with_reg)

    def test_descriptions_cannot_add_links_or_markup(self):
        evil = cal(vevent("Evil Co Info Session", ":20261022T213000Z", ":20261022T223000Z", "Room", "Open to all. Visit https://evil.example.com now <b>click</b> [here](https://evil.example.com)"))
        text = campus.format_ics_sessions(campus.ics_sessions(evil, "", CFG, TIERS, TODAY))
        self.assertNotIn("evil.example.com", text)
        self.assertNotIn("<b>", text)

    def test_an_empty_calendar_is_explained(self):
        self.assertIn("no company sessions", campus.format_ics_sessions(campus.ics_sessions(cal(), "", CFG, TIERS, TODAY)))


class FeedTests(unittest.TestCase):
    def run_build(self, responses, mine=""):
        seen = []

        async def get_text(url):
            seen.append(url)
            return responses.get(url, (404, ""))
        text = asyncio.run(campus.build_campus_ics("https://x/all.ics", mine, get_text, CFG, TIERS, TODAY))
        return text, seen

    def test_it_reads_the_feed(self):
        text, seen = self.run_build({"https://x/all.ics": (200, cal(PALANTIR))})
        self.assertIn("Palantir On-Campus Info Session", text)
        self.assertEqual(seen, ["https://x/all.ics"])

    def test_a_bad_feed_is_reported_without_repeating_the_link(self):
        text, _ = self.run_build({})
        self.assertIn("couldn't read the Campus Groups calendar", text)
        self.assertNotIn("x/all.ics", text)
        text2, _ = self.run_build({"https://x/all.ics": (200, "<html>login</html>")})
        self.assertIn("couldn't read", text2)

    def test_a_failing_personal_feed_does_not_break_the_list(self):
        text, seen = self.run_build({"https://x/all.ics": (200, cal(PALANTIR))}, mine="https://x/mine.ics")
        self.assertIn("Palantir", text)
        self.assertNotIn("you are registered", text)
        self.assertEqual(seen, ["https://x/all.ics", "https://x/mine.ics"])


if __name__ == "__main__":
    unittest.main()
