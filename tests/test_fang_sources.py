import asyncio
import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402


class SearchPageTests(unittest.TestCase):
    def test_config_enables_google_and_apple_and_reads_netflix_through_the_browser(self):
        companies = jobs.load_config()["companies"]
        providers = {company["name"]: company["provider"] for company in companies}
        self.assertEqual(providers["Google"], "google")
        self.assertEqual(providers["Apple"], "apple")
        self.assertEqual(providers.get("Netflix"), "browser")       # Netflix's own API is switched off for us; it comes from the browser reader

    def test_google_resolves_relative_links_prefers_accessible_title_and_deduplicates_urls(self):
        page = '''
        <a href="jobs/results/1234567890-software-engineer-cloud?query=first"
           aria-label="Learn more about Software Engineer, AI/Machine Learning, PhD Early Career 2027 Start"></a>
        <a href="/about/careers/applications/jobs/results/1234567890-software-engineer-cloud?query=second">
           Learn more about Software Engineer at Google
        </a>
        '''
        found = jobs.parse_google_html(page)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "Software Engineer, AI/Machine Learning, PhD Early Career 2027 Start")
        self.assertEqual(found[0]["url"], "https://www.google.com/about/careers/applications/jobs/results/1234567890-software-engineer-cloud")
        self.assertFalse(urlsplit(found[0]["url"]).query)

    def test_google_fetch_requests_only_the_first_results_page(self):
        seen = []

        async def get_html(url):
            seen.append(url)
            return 200, '<a href="jobs/results/12-software-engineer">role</a>'

        found, error = asyncio.run(jobs.fetch_google(
            {"name": "Google", "queries": ["software engineer"]}, get_html))
        self.assertIsNone(error)
        self.assertEqual(len(found), 1)
        query = parse_qs(urlsplit(seen[0]).query)
        self.assertNotIn("page", query)
        self.assertEqual(query["location"], ["New York, NY, USA"])

    def test_apple_discards_generic_seasonal_and_part_time_links(self):
        page = '''
        <a href="/en-us/details/100-software-engineer-maps"><h2>Software Engineer, Maps</h2></a>
        <a href="/en-us/details/101-see-role"><h2>See full role description</h2></a>
        <a href="/en-us/details/102-seasonal"><h2>Seasonal Software Engineer</h2></a>
        <a href="/en-us/details/103-part-time"><h2>Part-Time Software Engineer</h2></a>
        <a href="/en-us/details/104-hiring"><h2>Where we're hiring</h2></a>
        '''
        found = jobs.parse_apple_html(page, cfg=jobs.load_config())
        self.assertEqual([job["title"] for job in found], ["Software Engineer, Maps"])
        self.assertFalse(found[0]["details_checked"])

    def test_search_listing_output_says_posting_details_were_not_read(self):
        cfg = jobs.load_config()
        cfg["companies"] = [{"name": "Apple", "provider": "apple", "queries": ["software engineer"], "tier": 1}]
        page = '<a href="/en-us/details/100-software-engineer-maps"><h2>Software Engineer, Maps</h2></a>'

        async def get_html(_url):
            return 200, page

        async def unused_json(_url):
            raise AssertionError("HTML listing readers must not send JSON requests")

        with tempfile.TemporaryDirectory() as tmp:
            text, count, _ = asyncio.run(jobs.scan(cfg, unused_json, os.path.join(tmp, "jobs.db"),
                                                  mark=False, get_html=get_html))
        self.assertEqual(count, 1)
        self.assertIn("posting details not read; location, pay, visa, eligibility, experience unchecked", text)
        self.assertIn("search filter; listing location unverified", text)
        self.assertNotIn("pay: not stated", text)
        self.assertNotIn("visa: not stated", text)


if __name__ == "__main__":
    unittest.main()
