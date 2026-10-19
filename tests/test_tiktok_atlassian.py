import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402


def post(pid, title="Software Engineer, Ads", city="New York", kind="Regular", desc="Base salary $150,000 to $250,000 per year."):
    return {"id": pid, "title": title, "description": desc, "requirement": "",
            "city_info": {"code": "CT_114", "en_name": city}, "recruit_type": {"id": "101", "en_name": kind}}


class TikTokTests(unittest.TestCase):
    def test_posts_are_mapped_with_url_location_pay_and_kind(self):
        job = jobs.tiktok_jobs([post("7272850627067480375")])[0]
        self.assertEqual(job["key"], "tiktok:7272850627067480375")
        self.assertEqual(job["url"], "https://lifeattiktok.com/search/7272850627067480375")
        self.assertEqual(job["location"], "New York")
        self.assertEqual(job["employment"], "Regular")
        self.assertIs(job["details_checked"], True)
        self.assertIsNotNone(job["pay"])

    def test_bad_ids_are_skipped_and_interns_are_dropped_by_the_existing_rule(self):
        self.assertEqual(jobs.tiktok_jobs([post("../x"), post("12", title="")]), [])
        intern = jobs.tiktok_jobs([post("13", title="Software Engineer Intern", kind="Intern")])[0]
        self.assertIsNone(jobs.score_job(dict(intern, tier=2), jobs.load_config()))

    def test_fetch_sends_the_site_header_new_york_code_and_pages(self):
        calls = []

        async def get_json(url, body=None, headers=None):
            calls.append((url, body, headers))
            batch = [post(str(1000 + body["offset"] + i)) for i in range(100 if body["offset"] == 0 else 5)]
            return 200, {"code": 0, "data": {"count": 105, "job_post_list": batch}}

        found, err = asyncio.run(jobs.fetch_tiktok({"name": "TikTok", "queries": ["engineer"]}, get_json))
        self.assertIsNone(err)
        self.assertEqual(len(found), 105)
        self.assertEqual(len(calls), 2)
        url, body, headers = calls[0]
        self.assertEqual(url, jobs.TIKTOK_SEARCH)
        self.assertEqual(headers, {"website-path": "tiktok"})
        self.assertEqual(body["location_code_list"], ["CT_114"])

    def test_an_error_response_is_reported(self):
        async def get_json(url, body=None, headers=None):
            return 400, {"message": "invalid request"}

        found, err = asyncio.run(jobs.fetch_tiktok({"name": "TikTok"}, get_json))
        self.assertEqual((found, err), ([], "HTTP 400"))


class AtlassianTests(unittest.TestCase):
    LISTING = {"id": "25994", "title": "Software Engineer, Jira", "locations": ["New York - United States -   New York, New York 10003 United States", "Remote - Remote"],
               "overview": "<p>Build Jira.</p>", "responsibilities": "", "qualifications": "", "payRanges": "<p>$140,000 - $190,000 per year</p>"}

    def test_listing_is_mapped_with_clean_locations_url_and_pay(self):
        job = jobs.atlassian_jobs([self.LISTING])[0]
        self.assertEqual(job["url"], "https://www.atlassian.com/company/careers/details/25994")
        self.assertEqual(job["location"], "New York - United States - New York, New York 10003 United States; Remote - Remote")
        self.assertTrue(job["remote"])
        self.assertIsNotNone(job["pay"])
        self.assertTrue(jobs.location_ok(job["location"], job["remote"], jobs.load_config()))

    def test_bad_records_and_bad_responses(self):
        self.assertEqual(jobs.atlassian_jobs([{"id": "x", "title": "A"}, {"id": "1", "title": ""}]), [])

        async def get_json(url, body=None, headers=None):
            return 200, {"not": "a list"}

        self.assertEqual(asyncio.run(jobs.fetch_atlassian({"name": "Atlassian"}, get_json)), ([], "HTTP 200"))


if __name__ == "__main__":
    unittest.main()
