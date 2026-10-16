import asyncio
import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import apply  # noqa: E402
import jobs  # noqa: E402

GOOGLE_PAGE = ('<a href="jobs/results/111-software-engineer-cloud?q=x" aria-label="Learn more about Software Engineer, Cloud"></a>'
               '<a href="jobs/results/222-software-engineer-ads?q=x" aria-label="Learn more about Software Engineer, Ads"></a>')


def posting(years, place="Mountain View, CA, USA; New York, NY, USA"):
    return ("<html><body><h1>Role</h1><p>Note: your application goes to these locations: " + place + ". " + "Filler text. " * 80 +
            f"Minimum qualifications: Bachelor's degree. {years} years of experience with software development.</p>"
            "<script>var junk = 'ignore me';</script></body></html>")


def run(c):
    return asyncio.run(c)


class AppleTests(unittest.TestCase):
    def test_apple_search_uses_the_search_parameter(self):
        seen = []

        async def get_html(url):
            seen.append(url)
            return 200, '<a href="/en-us/details/1-software-engineer"><h2>Software Engineer, Maps</h2></a>'
        found, err = run(jobs.fetch_apple({"name": "Apple", "queries": ["software engineer"]}, get_html, jobs.load_config()))
        q = parse_qs(urlsplit(seen[0]).query)
        self.assertEqual(q.get("search"), ["software engineer"])
        self.assertNotIn("query", q)
        self.assertEqual(len(found), 1)


class GooglePostingTests(unittest.TestCase):
    def scan(self, pages):
        async def get_html(url):
            if "/jobs/results/" in url and url.endswith(tuple(pages)):
                return 200, pages[[k for k in pages if url.endswith(k)][0]]
            return 200, GOOGLE_PAGE

        async def no_json(url, body=None):
            raise AssertionError("no JSON requests expected")
        cfg = dict(jobs.load_config(), companies=[{"name": "Google", "provider": "google", "tier": 1, "queries": ["software engineer"]}])
        got = []
        text, n, _ = run(jobs.scan(cfg, no_json, os.path.join(tempfile.mkdtemp(), "t.db"), get_html=get_html, collect=got))
        return text, n, got

    def test_posting_pages_are_read_and_a_role_asking_for_many_years_is_dropped(self):
        text, n, got = self.scan({"111-software-engineer-cloud": posting(8), "222-software-engineer-ads": posting(2)})
        self.assertEqual(n, 1)
        self.assertIn("Software Engineer, Ads", text)
        self.assertNotIn("Software Engineer, Cloud", text)

    def test_a_read_posting_shows_real_checks_and_a_verified_location(self):
        text, n, got = self.scan({"111-software-engineer-cloud": posting(2), "222-software-engineer-ads": posting(2)})
        self.assertNotIn("details not read", text)
        self.assertIn("pay: not stated", text)
        self.assertNotIn("listing location unverified", text)
        self.assertIn("Minimum qualifications", got[0]["text"])
        self.assertNotIn("ignore me", got[0]["text"])               # scripts are not text

    def test_a_posting_that_does_not_mention_new_york_stays_marked_unverified(self):
        text, n, _ = self.scan({"111-software-engineer-cloud": posting(2, "Austin, TX, USA"), "222-software-engineer-ads": posting(2, "Austin, TX, USA")})
        self.assertIn("listing location unverified", text)

    def test_if_a_posting_page_cannot_be_read_the_listing_is_still_shown_as_unread(self):
        async def get_html(url):
            return (200, GOOGLE_PAGE) if "results?" in url else (503, "")

        async def no_json(url, body=None):
            return 200, {}
        cfg = dict(jobs.load_config(), companies=[{"name": "Google", "provider": "google", "tier": 1, "queries": ["software engineer"]}])
        text, n, _ = run(jobs.scan(cfg, no_json, os.path.join(tempfile.mkdtemp(), "t.db"), get_html=get_html))
        self.assertEqual(n, 2)
        self.assertIn("posting details not read", text)

    def test_page_text_drops_scripts_and_styles(self):
        self.assertEqual(jobs.page_text("<style>p{}</style><p>Hello <b>there</b></p><script>x()</script>"), "Hello there")


class FitNoteTests(unittest.TestCase):
    PROFILE = {"resumes": {"backend-swe": {"headline": "SWE", "summary": "x"}}}

    def test_a_title_only_role_gets_an_honest_note(self):
        async def ok(prompt):
            return "FIT: 3 of 5\nWHY: ok.\nGAPS: unknown.\nRESUME: backend-swe"
        _, text, _ = run(apply.fit_check(self.PROFILE, {"company": "A", "title": "SWE", "text": ""}, ok))
        self.assertIn("only had the title and location", text)

    def test_a_full_posting_gets_no_note(self):
        async def ok(prompt):
            return "FIT: 3 of 5\nWHY: ok.\nGAPS: unknown.\nRESUME: backend-swe"
        _, text, _ = run(apply.fit_check(self.PROFILE, {"company": "A", "title": "SWE", "text": "x" * 500}, ok))
        self.assertNotIn("only had the title", text)


if __name__ == "__main__":
    unittest.main()
