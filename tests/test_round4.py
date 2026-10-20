import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
import jobs
from public_readers import teamtailor_jobs, fetch_teamtailor, fetch_guarded_rippling

FIXTURES = Path(__file__).parent / "fixtures"
COMPANY = {"name": "Lindy", "provider": "teamtailor_rss", "feed_url": "https://careers.lindy.ai/jobs.rss"}

class TeamtailorTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_feed(self):
        found = teamtailor_jobs((FIXTURES / "teamtailor.xml").read_text(), COMPANY)
        self.assertEqual(len(found), 1)
        self.assertIn("Early Career", found[0]["title"])
        self.assertEqual(found[0]["location"], "San Francisco, United States")
        self.assertIn("We do sponsor visas", found[0]["text"])
        self.assertTrue(found[0]["details_checked"])
        self.assertTrue(found[0]["published"])

    async def test_empty_error_and_malformed(self):
        for status, page, error in [(200, "<rss><channel/></rss>", False), (403, "blocked", True), (200, "not XML", True), (200, "<html/>", True)]:
            public = type("Public", (), {"text": AsyncMock(return_value=(status, page))})()
            found, issue = await fetch_teamtailor(COMPANY, public)
            self.assertEqual(found, [])
            self.assertEqual(bool(issue), error)

    def test_reject_foreign_links_and_unverified_locations(self):
        page = "<rss><channel><item><title>Engineer</title><description>Remote</description><link>https://evil.example/jobs/1</link></item></channel></rss>"
        self.assertEqual(teamtailor_jobs(page, COMPANY), [])
        page = page.replace("evil.example", "careers.lindy.ai")
        job = teamtailor_jobs(page, COMPANY)[0]
        self.assertEqual(job["location"], "")
        self.assertFalse(job["remote"])

class PublicAshbyTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_posting_address(self):
        raw = {"id": "abc", "title": "Software Engineer", "location": "SF", "jobUrl": "https://jobs.ashbyhq.com/example/abc", "descriptionPlain": "Software", "address": {"postalAddress": {"addressLocality": "San Francisco", "addressRegion": "California", "addressCountry": "United States"}}}
        public = type("Public", (), {"json": AsyncMock(return_value=(200, {"jobs": [raw]}))})()
        found, error = await jobs.fetch_public_feed({"name": "Example", "provider": "ashby", "slug": "example"}, public)
        self.assertIsNone(error)
        self.assertEqual(found[0]["location"], "SF; San Francisco, California, United States")
        self.assertTrue(jobs.location_ok(found[0]["location"], False, jobs.load_config()))
        public.json.return_value = (404, {})
        self.assertTrue((await jobs.fetch_public_feed({"name": "Example", "provider": "ashby", "slug": "example"}, public))[1])

class GuardedRipplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_and_error(self):
        public = type("Public", (), {"json": AsyncMock(return_value=(200, []))})()
        company = {"name": "Example", "slug": "example", "tier": 3}
        self.assertEqual(await fetch_guarded_rippling(company, jobs.load_config(), public), ([], None))
        public.json.return_value = (429, None)
        self.assertTrue((await fetch_guarded_rippling(company, jobs.load_config(), public))[1])


class GuardedWorkdayTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_sample_and_request_bound(self):
        from public_readers import fetch_guarded_workday
        sample = json.loads((FIXTURES / "workday_public.json").read_text())
        sample["jobPostings"][0]["title"] = "Software Engineer"
        public = type("Public", (), {"json": AsyncMock()})()
        async def get(url, body=None):
            if url.endswith("/jobs"):
                return 200, sample
            return 200, {"jobPostingInfo": {"jobDescription": "Build software. 0–2 years.", "timeType": "Full time"}}
        public.json.side_effect = get
        company = {"name": "LendingClub", "host": "lendingclub.wd1.myworkdayjobs.com", "tenant": "lendingclub", "site": "External", "tier": 3}
        found, error = await fetch_guarded_workday(company, jobs.load_config(), public)
        self.assertIsNone(error)
        self.assertTrue(found)
        self.assertLessEqual(public.json.await_count, 6)
        for call in public.json.call_args_list:
            if call.args[0].endswith("/jobs"):
                self.assertIn(call.args[1]["offset"], (0,))

    async def test_listing_failure_and_no_target_facets(self):
        from public_readers import fetch_guarded_workday
        company = {"name": "Example", "host": "example.wd1.myworkdayjobs.com", "tenant": "example", "site": "Careers"}
        public = type("Public", (), {"json": AsyncMock(return_value=(403, {}))})()
        self.assertTrue((await fetch_guarded_workday(company, jobs.load_config(), public))[1])
        self.assertEqual(public.json.await_count, 1)
        public.json.return_value = (200, {"jobPostings": [], "facets": []})
        self.assertIn("filter", (await fetch_guarded_workday(company, jobs.load_config(), public))[1])

class GuardantTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_public_widget_payload(self):
        from public_readers import guardant_jobs
        data = json.loads((FIXTURES / 'guardant.json').read_text())
        rows = guardant_jobs(data, {'name': 'Guardant Health'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['location'], 'Palo Alto, CA')
        self.assertIn('Bioinformatics', rows[0]['title'])
        self.assertTrue(rows[0]['text'])
        self.assertTrue(rows[0]['details_checked'])
        self.assertIn('Full', rows[0]['employment'])
        data['data']['data'][0]['url'] = 'https://gh.wd1.myworkdayjobs.com/gh/private_posting/hidden'
        self.assertEqual(guardant_jobs(data, {'name': 'Guardant Health'}), [])

    async def test_empty_error_and_public_nonce_scope(self):
        from public_readers import fetch_guardant
        company = {'name': 'Guardant Health'}
        for status, page in [(403, ''), (200, '<html/>'), (200, 'var workdayApi = {"ajax_url":"https://evil.example/","nonce":"123"};')]:
            public = type('Public', (), {'text': AsyncMock(return_value=(status, page)), 'json': AsyncMock()})()
            self.assertTrue((await fetch_guardant(company, public))[1])
            public.json.assert_not_awaited()
        config = 'var workdayApi = {"ajax_url":"https://www.guardanthealth.com/wp-admin/admin-ajax.php","nonce":"PUBLIC123"};'
        public = type('Public', (), {'text': AsyncMock(return_value=(200, config)), 'json': AsyncMock(return_value=(200, {'success': True, 'data': {'data': []}}))})()
        self.assertEqual(await fetch_guardant(company, public), ([], None))
        self.assertIn('action=get_workday_data', public.json.call_args.args[0])
        public.json.return_value = (200, {'success': False})
        self.assertTrue((await fetch_guardant(company, public))[1])

class RecruiteeTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_offers(self):
        from public_readers import recruitee_jobs
        data = json.loads((FIXTURES / 'recruitee.json').read_text())
        rows = recruitee_jobs(data, {'name': 'Aikido Security', 'slug': 'aikidosecurity'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Developer Relations')
        self.assertIn('San Francisco', rows[0]['location'])
        self.assertTrue(rows[0]['details_checked'])
        data['offers'][0]['careers_url'] = 'https://evil.example/jobs/1'
        self.assertEqual(recruitee_jobs(data, {'name': 'Aikido Security', 'slug': 'aikidosecurity'}), [])

    async def test_empty_error_and_invalid_board(self):
        from public_readers import fetch_recruitee
        public = type('Public', (), {'json': AsyncMock(return_value=(200, {'offers': []}))})()
        company = {'name': 'Example', 'slug': 'example'}
        self.assertEqual(await fetch_recruitee(company, public), ([], None))
        public.json.return_value = (403, {})
        self.assertTrue((await fetch_recruitee(company, public))[1])
        public.json.return_value = (200, {})
        self.assertTrue((await fetch_recruitee(company, public))[1])
        public.json.reset_mock()
        self.assertTrue((await fetch_recruitee(dict(company, slug='../invalid'), public))[1])
        public.json.assert_not_awaited()

class PublicLeverTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_qualifications_are_not_omitted(self):
        data = json.loads((FIXTURES / 'lever_public.json').read_text())
        public = type('Public', (), {'json': AsyncMock(return_value=(200, data))})()
        rows, issue = await jobs.fetch_public_feed({'name': 'Finch', 'provider': 'lever', 'slug': 'finch'}, public)
        self.assertIsNone(issue)
        expected = jobs._clean(jobs.html_lib.unescape(data[0]['lists'][0]['content']))
        self.assertIn(expected, rows[0]['text'])
        data[0]['lists'] = [{'text': 'Requirements', 'content': '<li>5+ years of professional software engineering experience</li>'}]
        rows, _ = await jobs.fetch_public_feed({'name': 'Finch', 'provider': 'lever', 'slug': 'finch'}, public)
        self.assertGreaterEqual(jobs.required_years(rows[0]['text']), 5)
        public.json.return_value = (200, [])
        self.assertEqual(await jobs.fetch_public_feed({'name': 'Finch', 'provider': 'lever', 'slug': 'finch'}, public), ([], None))
