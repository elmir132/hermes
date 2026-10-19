import asyncio
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

CO = {"name": "GitHub", "provider": "jibe", "host": "www.github.careers"}


def item(slug, title="Software Engineer II", loc="United States", kind="ANY", desc="Salary $120,000 - $180,000 per year"):
    return {"data": {"slug": slug, "req_id": slug, "title": title, "full_location": loc, "location_type": kind,
                     "description": desc, "posted_date": "2026-10-10T00:00:00+0000", "employment_type": "FULL_TIME"}}


class JibeTests(unittest.TestCase):
    def test_mapping_url_remote_and_pay(self):
        job = jobs.jibe_jobs(CO, [item("5434")])[0]
        self.assertEqual(job["url"], "https://www.github.careers/jobs/5434")
        self.assertTrue(job["remote"])
        self.assertTrue(jobs.location_ok(job["location"], job["remote"], jobs.load_config()))
        self.assertIsNotNone(job["pay"])
        self.assertEqual(job["employment"], "FULL TIME")

    def test_unsafe_slugs_and_foreign_places(self):
        self.assertEqual(jobs.jibe_jobs(CO, [item("../x"), {"data": None}, "junk"]), [])
        india = jobs.jibe_jobs(CO, [item("1", loc="Hyderabad, India", kind="LAT_LNG")])[0]
        self.assertFalse(jobs.location_ok(india["location"], india["remote"], jobs.load_config()))

    def test_pages_until_total_and_errors(self):
        urls = []

        async def get_json(url, body=None, headers=None):
            urls.append(url)
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return 200, {"totalCount": 130, "jobs": [item(str(page * 1000 + i)) for i in range(100 if page == 1 else 30)]}

        found, err = asyncio.run(jobs.fetch_jibe(dict(CO, queries=["software engineer"]), get_json))
        self.assertEqual((len(found), err, len(urls)), (130, None, 2))

        async def bad(url, body=None, headers=None):
            return 500, None
        self.assertEqual(asyncio.run(jobs.fetch_jibe(CO, bad)), ([], "HTTP 500"))


if __name__ == "__main__":
    unittest.main()
