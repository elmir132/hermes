import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jobs
from public_jobs import PublicJobsClient, USER_AGENT

SAMPLE = json.loads((Path(__file__).parent / 'fixtures/uber-search.json').read_text())


class UberTests(unittest.TestCase):
    def test_saved_public_response(self):
        found = jobs.uber_jobs(SAMPLE, 'Uber')
        self.assertEqual(len(found), 2)
        first = SAMPLE['jobs'][0]
        self.assertEqual(found[0]['title'], first['Title'])
        self.assertEqual(found[0]['key'], 'uber:' + first['Id'])
        self.assertIn('Basic Qualifications', found[0]['text'])
        self.assertNotIn('<p>', found[0]['text'])
        self.assertTrue(found[0]['location'])
        self.assertEqual(found[0]['employment'], 'Full time')
        self.assertTrue(found[0]['url'].startswith('https://jobs.uber.com/en/jobs/'))

    def test_empty_and_malformed(self):
        for payload in (None, [], {}, {'jobs': None}, {'jobs': [None, {}, {'Id':'x'}]}):
            self.assertEqual(jobs.uber_jobs(payload, 'Uber'), [])

    def test_no_unverified_text_or_external_urls(self):
        for changes in ({'Description':'$2a'}, {'Description':''}, {'Locations':[]},
                        {'Urls':[{'Culture':'en-us','Url':'https://bad.example/job'}]}):
            raw = dict(SAMPLE['jobs'][0], **changes)
            self.assertEqual(jobs.uber_jobs({'jobs':[raw]}, 'Uber'), [])

    def test_error_stops_all_queries(self):
        calls = []
        async def get(url):
            calls.append(url)
            return 403, {}
        found, err = asyncio.run(jobs.fetch_uber({'name':'Uber'}, get))
        self.assertEqual(found, [])
        self.assertIn('403', err)
        self.assertEqual(len(calls), 1)

    def test_empty_success(self):
        async def get(url):
            return 200, {'jobs':[], 'totalPages':1}
        self.assertEqual(asyncio.run(jobs.fetch_uber({'name':'Uber'}, get)), ([], None))

    def test_pagination_cap_and_dedup(self):
        calls = []
        async def get(url):
            calls.append(url)
            return 200, dict(SAMPLE, totalPages=100)
        found, err = asyncio.run(jobs.fetch_uber({'name':'Uber'}, get))
        self.assertIsNone(err)
        self.assertEqual(len(found),2)
        self.assertEqual(len(calls), jobs.UBER_PAGES * len(jobs.UBER_QUERIES))
        self.assertEqual(len(calls),len(set(calls)))

    def test_scan_dispatch_with_unchanged_filters(self):
        # A synthetic new-grad variant exercises scan; the parser test above uses
        # the unmodified live response. This is not evidence of live new-grad fit.
        raw = copy.deepcopy(SAMPLE['jobs'][0])
        raw.update(Title='Software Engineer, New Grad', Description='Build Python services.', DisplayDate='',
                   Locations=[{'Address':'New York, NY, USA'}], ContractType='Full time')
        async def get(url):
            return 200, {'jobs':[raw], 'totalPages':1}
        class Client:
            json = staticmethod(get)
        cfg = jobs.load_config()
        cfg['companies'] = [c for c in cfg['companies'] if c['provider']=='uber']
        async def unused(*args):
            self.fail('unguarded transport called')
        with tempfile.TemporaryDirectory() as temporary, patch('public_jobs.PublicJobsClient', Client):
            _, count, errors = asyncio.run(jobs.scan(cfg, unused, Path(temporary)/'seen.db',mark=False))
        self.assertEqual(count,1)
        self.assertEqual(errors,[])


class PublicTransportTests(unittest.TestCase):
    def client(self, responses):
        calls=[]
        async def raw(url):
            calls.append(url)
            return responses[url]
        return PublicJobsClient(raw,pause=0), calls

    def test_disallowed_exact_path_is_never_requested(self):
        c,calls=self.client({'https://example.com/robots.txt':(200,{},'User-agent: *\nDisallow: /api/')})
        self.assertEqual(asyncio.run(c.json('https://example.com/api/jobs?q=a')),(0,{}))
        self.assertEqual(calls,['https://example.com/robots.txt'])

    def test_redirect_target_robots_checked_before_request(self):
        c,calls=self.client({'https://example.com/robots.txt':(200,{},''),
            'https://example.com/jobs':(302,{'location':'https://other.example/private'},''),
            'https://other.example/robots.txt':(200,{},'User-agent: *\nDisallow: /private')})
        self.assertEqual(asyncio.run(c.text('https://example.com/jobs')),(0,''))
        self.assertEqual(calls[-1],'https://other.example/robots.txt')

    def test_robots_error_fails_closed(self):
        for status in (0,202,301,403,429,503):
            c,calls=self.client({'https://example.com/robots.txt':(status,{},'')})
            self.assertEqual(asyncio.run(c.text('https://example.com/jobs')),(0,''))
            self.assertEqual(len(calls),1)

    def test_missing_robots_allows(self):
        for status in (404,410):
            c,calls=self.client({'https://example.com/robots.txt':(status,{},''),
                'https://example.com/jobs':(200,{},'{"jobs":[]}')})
            self.assertEqual(asyncio.run(c.json('https://example.com/jobs')),(200,{'jobs':[]}))

    def test_repeated_wildcard_groups_fail_closed(self):
        c,calls=self.client({'https://example.com/robots.txt':(200,{},
            'User-agent: *\nAllow: /roles/\n\nUser-agent: *\nDisallow: /')})
        self.assertEqual(asyncio.run(c.text('https://example.com/results')),(0,''))
        self.assertEqual(len(calls),1)

    def test_allow_order_and_blank_line_do_not_override_prohibitions(self):
        for policy in ('User-agent: *\nAllow: /\nDisallow: /api',
                       'User-agent: *\n\nDisallow: /api'):
            c,calls=self.client({'https://example.com/robots.txt':(200,{},policy)})
            self.assertEqual(asyncio.run(c.text('https://example.com/api/jobs')),(0,''))
            self.assertEqual(len(calls),1)

    def test_no_requests_after_403_or_429(self):
        for status in (403,429):
            c,calls=self.client({'https://example.com/robots.txt':(200,{},''),
                'https://example.com/jobs':(status,{},'')})
            async def run():
                self.assertEqual(await c.text('https://example.com/jobs'),(status,''))
                self.assertEqual(await c.text('https://example.com/jobs?page=2'),(0,''))
            asyncio.run(run())
            self.assertEqual(len(calls),2)

    def test_cache_one_request_per_url(self):
        c,calls=self.client({'https://example.com/robots.txt':(200,{},''),
            'https://example.com/jobs':(200,{},'{}')})
        async def run():
            await c.json('https://example.com/jobs')
            await c.json('https://example.com/jobs')
        asyncio.run(run())
        self.assertEqual(len(calls),2)

    def test_pace_and_crawl_delay(self):
        c,calls=self.client({'https://example.com/robots.txt':(200,{},'User-agent: *\nCrawl-delay: 10'),
            'https://example.com/jobs':(200,{},'{}')})
        async def pause(delay):
            delays.append(delay)
        delays=[]
        with patch('public_jobs.asyncio.sleep',pause):
            asyncio.run(c.json('https://example.com/jobs'))
        self.assertGreater(delays[-1],9)

    def test_invalid_json_is_not_an_empty_success(self):
        c,_=self.client({'https://example.com/robots.txt':(200,{},''),
            'https://example.com/jobs':(200,{},'<html>unavailable</html>')})
        self.assertEqual(asyncio.run(c.json('https://example.com/jobs')),(200,None))

    def test_credentials_and_plain_http_rejected(self):
        c,calls=self.client({})
        for url in ('http://example.com/jobs','https://user:password@example.com/jobs'):
            with self.assertRaises(ValueError):asyncio.run(c.text(url))
        self.assertEqual(calls,[])
