import asyncio
import os
import sys
import unittest
from urllib.parse import unquote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

COMPANY = {"name": "American Express", "provider": "oracle", "host": "egug.fa.us2.oraclecloud.com", "site": "CX_1", "tier": 3}


def req(rid, title="Senior Software Engineer I", primary="New York, NY, United States", secondary=(), **extra):
    return dict({"Id": rid, "Title": title, "PrimaryLocation": primary, "PostedDate": "2026-10-15", "WorkplaceType": "Hybrid",
                 "JobSchedule": "Full time", "WorkerType": "Employee",
                 "secondaryLocations": [{"Name": n} for n in secondary]}, **extra)


class OracleMappingTests(unittest.TestCase):
    def test_fields_url_and_locations_are_mapped(self):
        job = jobs.oracle_jobs(COMPANY, [req("26015071", secondary=["Phoenix, AZ, United States", "New York, NY, United States"])])[0]
        self.assertEqual(job["key"], "orc:American Express:26015071")
        self.assertEqual(job["url"], "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26015071")
        self.assertEqual(job["location"], "New York, NY, United States; Phoenix, AZ, United States")
        self.assertEqual(job["published"], "2026-10-15")
        self.assertFalse(job["remote"])
        self.assertIs(job["details_checked"], False)                      # the list gives no description
        self.assertIn("Full time", job["employment"])

    def test_description_text_marks_details_checked_and_reads_pay(self):
        job = jobs.oracle_jobs(COMPANY, [req("1", ShortDescriptionStr="Salary range: $150,000 to $200,000 per year.")])[0]
        self.assertIs(job["details_checked"], True)
        self.assertIsNotNone(job["pay"])

    def test_bad_ids_and_empty_titles_are_skipped(self):
        found = jobs.oracle_jobs(COMPANY, [req("../x"), req("2", title=""), req("3")])
        self.assertEqual([j["key"] for j in found], ["orc:American Express:3"])
        self.assertEqual(jobs.oracle_jobs(COMPANY, None), [])

    def test_a_part_time_role_is_dropped_by_the_existing_employment_rule(self):
        job = jobs.oracle_jobs(COMPANY, [req("4", JobSchedule="Part time")])[0]
        self.assertIsNone(jobs.score_job(dict(job, tier=3), jobs.load_config()))


class OracleFetchTests(unittest.TestCase):
    def test_pages_until_the_total_is_reached_and_encodes_the_keyword(self):
        urls = []

        async def get_json(url):
            urls.append(url)
            offset = int(unquote(url).split("offset=")[1].split(",")[0])
            batch = [req(str(offset + i)) for i in range(jobs.ORACLE_PAGE if offset == 0 else 30)]
            return 200, {"items": [{"TotalJobsCount": jobs.ORACLE_PAGE + 30, "requisitionList": batch}]}

        found, err = asyncio.run(jobs.fetch_oracle(dict(COMPANY, queries=["software engineer"]), get_json))
        self.assertIsNone(err)
        self.assertEqual(len(found), jobs.ORACLE_PAGE + 30)
        self.assertEqual(len(urls), 2)
        self.assertIn('keyword="software engineer"', unquote(urls[0]))
        self.assertNotIn(" ", urls[0])
        self.assertTrue(urls[0].startswith("https://egug.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?"))

    def test_the_same_role_from_two_queries_is_kept_once(self):
        async def get_json(url):
            return 200, {"items": [{"TotalJobsCount": 1, "requisitionList": [req("9")]}]}

        found, err = asyncio.run(jobs.fetch_oracle(COMPANY, get_json))
        self.assertEqual(len(found), 1)

    def test_an_http_error_is_reported(self):
        async def get_json(url):
            return 503, None

        found, err = asyncio.run(jobs.fetch_oracle(COMPANY, get_json))
        self.assertEqual(found, [])
        self.assertEqual(err, "HTTP 503")


if __name__ == "__main__":
    unittest.main()
