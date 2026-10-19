import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402


def post(pid, title="Software Engineer", loc="San Francisco, United States", url=None, text="Base pay $170,000 - $210,000 per year."):
    return {"id": pid, "title": title, "location": {"name": loc}, "absolute_url": url or f"https://jobs.gem.com/retool/{pid}",
            "content_plain": text, "employment_type": "full_time", "first_published_at": "2026-10-10T00:00:00Z"}


class GemTests(unittest.TestCase):
    def test_posts_are_mapped_with_text_pay_and_their_own_key(self):
        job = jobs.gem_jobs([post("4003629005")], "Retool")[0]
        self.assertEqual(job["key"], "gem:Retool:4003629005")
        self.assertEqual(job["url"], "https://jobs.gem.com/retool/4003629005")
        self.assertIs(job["details_checked"], True)
        self.assertIsNotNone(job["pay"])
        self.assertNotIn("gh_id", job)                    # never sent to the Greenhouse detail reader
        self.assertTrue(jobs.location_ok(job["location"], job["remote"], jobs.load_config()))

    def test_links_off_gem_and_broken_records_are_dropped(self):
        found = jobs.gem_jobs([post("1", url="https://evil.example.com/x"), {"title": "No id"}, "junk", post("2", title="")], "Retool")
        self.assertEqual(found, [])

    def test_fetch_errors_are_reported(self):
        async def bad(url, body=None, headers=None):
            return 403, {"message": "Forbidden"}
        self.assertEqual(asyncio.run(jobs.fetch_gem({"name": "Retool", "slug": "retool"}, bad)), ([], "HTTP 403"))


if __name__ == "__main__":
    unittest.main()
