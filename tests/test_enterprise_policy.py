"""Regression checks for the enterprise routes that remain unconfigured."""
import json
from pathlib import Path
import unittest

import jobs
from public_jobs import PublicJobsClient

class EnterprisePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_hubspot_style_graphql_error_is_not_a_job_feed(self):
        calls = []
        async def raw(url, body=None):
            calls.append(url)
            if url.endswith('/robots.txt'):
                return 403, {}, 'Forbidden'
            return 200, {}, json.dumps({'errors': [{'message': '404: Not Found'}], 'data': {'jobs': None}})
        public = PublicJobsClient(raw=raw, pause=0)
        status, data = await public.json('https://graphql.example.org/careers/graphql', {'query': 'query Jobs { jobs { id title } }'})
        self.assertEqual(status, 200)
        self.assertTrue(data['errors'])
        self.assertIsNone(data['data']['jobs'])
        self.assertEqual(len(calls), 2)

    async def test_ibm_style_redirected_policy_does_not_hide_validation_error(self):
        calls = []
        async def raw(url, body=None):
            calls.append(url)
            if url.endswith('/robots.txt'):
                return 301, {'Location': 'https://policy.example.org/plain'}, ''
            if url.endswith('/plain'):
                return 200, {}, '<html>No robots directives here</html>'
            return 400, {}, '{"errors":[{"param":"query","msg":"Invalid value"}]}'
        public = PublicJobsClient(raw=raw, pause=0)
        status, _ = await public.json('https://search.example.org/search/api/v2', {'appId': 'careers', 'query': 'unknown schema'})
        self.assertEqual(status, 400)
        self.assertEqual(len(calls), 3)

    async def test_eightfold_style_disallow_prevents_careers_request(self):
        calls = []
        async def raw(url):
            calls.append(url)
            return 200, {}, 'User-agent: *\nDisallow: /careers\n'
        public = PublicJobsClient(raw=raw, pause=0)
        self.assertEqual(await public.text('https://board.example.org/careers'), (0, ''))
        self.assertEqual(calls, ['https://board.example.org/robots.txt'])

    def test_yahoo_country_only_sample_is_not_a_city_or_remote_eligibility(self):
        info = json.loads((Path(__file__).parent / 'fixtures/yahoo_location.json').read_text())
        self.assertEqual(info['location'], 'United States of America')
        self.assertFalse(jobs.location_ok(info['location'], False, jobs.load_config()))
        self.assertNotIn('addressLocality', info['jobRequisitionLocation'])
