import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import browser_reader as br  # noqa: E402
import jobs  # noqa: E402

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
CFG = jobs.load_config()


def run(c):
    return asyncio.run(c)


class ParserTests(unittest.TestCase):
    def test_meta_cards_become_jobs_with_locations_and_a_direct_link_when_there_is_one(self):
        cards = [{"text": "Software Engineer, Systems ML\nNew York, NY\n⋅\nMenlo Park, CA", "href": "/profile/job_details/1791619365408270"},
                 {"text": "Machine Learning Engineer\nRemote, US", "href": "#"},
                 {"text": "Careers", "href": "/jobs"}, {"text": "Learn more\nabout life at Meta", "href": "/blog/"}]
        jobs_ = br.parse_meta_cards(cards)
        self.assertEqual([j["title"] for j in jobs_], ["Software Engineer, Systems ML", "Machine Learning Engineer"])
        self.assertEqual(jobs_[0]["url"], "https://www.metacareers.com/profile/job_details/1791619365408270")
        self.assertEqual(jobs_[0]["location"], "New York, NY; Menlo Park, CA")
        self.assertIn("jobs?q=Machine%20Learning%20Engineer", jobs_[1]["url"])      # no direct link: search for its title

    def test_eightfold_anchors_use_the_first_line_as_the_title_and_drop_duplicates(self):
        anchors = [{"text": "Software Engineer II\nUnited States, New York, New York + 2 more\nPosted 3 days ago", "href": "/careers/job/123?query=x"},
                   {"text": "Software Engineer II\nUnited States, New York, New York", "href": "/careers/job/123"},
                   {"text": "Sign in", "href": "/login"}]
        found = br.parse_eightfold_anchors(anchors, "https://apply.careers.microsoft.com")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url"], "https://apply.careers.microsoft.com/careers/job/123")
        self.assertEqual((found[0]["title"], found[0]["posted"]), ("Software Engineer II", "Posted 3 days ago"))

    def test_netflix_cards_link_to_a_search_for_the_exact_title(self):
        found = br.parse_netflix_cards([{"text": "Software Engineer 5 - Ads\nNew York, New York, United States and 1 more\nEngineering"}],
                                       "https://x/careers?query=__Q__&location=New%20York")
        self.assertEqual(found[0]["url"], "https://x/careers?query=Software%20Engineer%205%20-%20Ads&location=New%20York")


class BrowserFileTests(unittest.TestCase):
    def write(self, data):
        path = os.path.join(tempfile.mkdtemp(), "browser_jobs.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def company(self, name="Meta"):
        return {"name": name, "provider": "browser", "source": name, "tier": 1}

    def data(self, fetched, jobs_=None, error=None):
        return {"fetched_at": fetched.isoformat(), "sources": {"Meta": {"jobs": jobs_ if jobs_ is not None else
                [{"title": "Software Engineer, ML", "location": "New York, NY", "url": "https://m/1", "posted": ""}], "error": error}}}

    def test_a_fresh_file_gives_jobs_and_no_error(self):
        found, err = jobs.browser_jobs(self.company(), CFG, NOW, self.write(self.data(NOW - timedelta(hours=5))))
        self.assertEqual((len(found), err), (1, None))
        self.assertFalse(found[0]["details_checked"])

    def test_an_old_file_still_works_but_says_how_old_it_is(self):
        found, err = jobs.browser_jobs(self.company(), CFG, NOW, self.write(self.data(NOW - timedelta(days=5))))
        self.assertEqual(len(found), 1)
        self.assertIn("120 hours old", err)

    def test_a_missing_file_or_source_is_reported_not_fatal(self):
        found, err = jobs.browser_jobs(self.company(), CFG, NOW, "/nonexistent/file.json")
        self.assertEqual(found, [])
        self.assertIn("no browser data", err)
        found, err = jobs.browser_jobs(self.company("Microsoft"), CFG, NOW, self.write(self.data(NOW)))
        self.assertEqual(found, [])

    def test_a_source_error_from_the_reader_is_passed_on(self):
        _, err = jobs.browser_jobs(self.company(), CFG, NOW, self.write(self.data(NOW, [], "HTTP 403")))
        self.assertIn("HTTP 403", err)

    def test_scan_uses_the_file_and_marks_details_unread(self):
        path = self.write(self.data(NOW))
        cfg = dict(CFG, companies=[dict(self.company(), file=path)])

        async def no_net(url, body=None):
            raise AssertionError("the browser provider must not use the network")
        text, n, errors = run(jobs.scan(cfg, no_net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(n, 1)
        self.assertIn("Meta  [tier 1]: Software Engineer, ML", text)
        self.assertIn("posting details not read", text)


def hn_comment(i, text):
    return {"id": i, "created_at": "2026-10-02T15:00:00.000Z", "text": text}


class HackerNewsTests(unittest.TestCase):
    def test_a_posting_is_read_from_the_first_line(self):
        j = jobs.hn_job(hn_comment(1, 'Acme AI | Software Engineer, Backend | New York, NY | ONSITE | VISA<p>We build things. <a href="https://acme.ai/jobs">apply</a>'), "HN")
        self.assertEqual((j["company"], j["title"]), ("Acme AI", "Software Engineer, Backend"))
        self.assertIn("New York, NY", j["location"])
        self.assertEqual(j["url"], "https://acme.ai/jobs")
        self.assertNotIn("VISA", j["location"])                         # the visa tag is a note, not a place
        self.assertIn("Visa sponsorship is available", j["text"])
        self.assertIn("✅ mentions sponsorship", jobs.visa_flags(j["text"]))

    def test_no_visa_remote_and_internships_are_understood(self):
        j = jobs.hn_job(hn_comment(2, "Beta | ML Engineer | REMOTE (US) | No visa"), "HN")
        self.assertTrue(j["remote"])
        self.assertIn("⚠️ says no sponsorship", jobs.visa_flags(j["text"]))
        self.assertEqual(jobs.hn_job(hn_comment(3, "Gamma | Software Engineering Intern | NYC | Summer"), "HN")["employment"], "intern")

    def test_comments_that_are_not_job_posts_are_skipped(self):
        for text in ["Thanks for posting!", "Delta | | ", "Epsilon | Head of Sales | New York | ONSITE", ""]:
            self.assertIsNone(jobs.hn_job(hn_comment(9, text), "HN"), text)

    def test_the_link_falls_back_to_the_hacker_news_item(self):
        j = jobs.hn_job(hn_comment(77, "Zeta | Software Engineer | NYC | ONSITE"), "HN")
        self.assertEqual(j["url"], "https://news.ycombinator.com/item?id=77")

    def test_fetch_reads_the_latest_hiring_thread(self):
        calls = []

        async def get_json(url, body=None):
            calls.append(url)
            if "search_by_date" in url:
                return 200, {"hits": [{"title": "Ask HN: Who wants to be hired? (October 2026)", "objectID": "5"},
                                      {"title": "Ask HN: Who is hiring? (October 2026)", "objectID": "7"}]}
            return 200, {"children": [hn_comment(1, "Acme | Software Engineer | New York, NY | ONSITE"), hn_comment(2, "just chatting")]}
        found, err = run(jobs.fetch_hn({"name": "HN"}, get_json, NOW))
        self.assertIsNone(err)
        self.assertEqual([j["company"] for j in found], ["Acme"])
        self.assertTrue(calls[1].endswith("/items/7"))                 # the hiring thread, not "who wants to be hired"

    def test_scan_filters_hn_roles_like_any_other(self):
        async def get_json(url, body=None):
            if "search_by_date" in url:
                return 200, {"hits": [{"title": "Ask HN: Who is hiring? (October 2026)", "objectID": "7"}]}
            return 200, {"children": [hn_comment(1, "Acme | Software Engineer | New York, NY | ONSITE | VISA"),
                                      hn_comment(2, "Berlin Co | Software Engineer | Berlin, Germany | ONSITE"),
                                      hn_comment(3, "Old Co | Senior Staff Engineer | New York, NY | ONSITE")]}
        cfg = dict(CFG, companies=[{"name": "HN", "provider": "hn", "tier": 3}])
        text, n, _ = run(jobs.scan(cfg, get_json, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(n, 1)
        self.assertIn("Acme", text)
        self.assertNotIn("Berlin", text)
        self.assertNotIn("Old Co", text)


if __name__ == "__main__":
    unittest.main()
