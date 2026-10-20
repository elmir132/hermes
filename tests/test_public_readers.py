import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jobs
import public_readers as readers
from public_jobs import PublicJobsClient

FIXTURES = Path(__file__).parent / 'fixtures'
CFG = jobs.load_config()
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


class PageReaderTests(unittest.TestCase):
    def test_saved_schema_pages_and_encoded_script_type(self):
        for name, host in [('blizzard', 'careers.blizzard.com'), ('servicenow', 'careers.servicenow.com')]:
            page = (FIXTURES / (name + '-job.html')).read_text()
            job = readers.parse_schema_job(page, 'https://' + host + '/job/123', name)
            self.assertTrue(job['title'])
            self.assertTrue(job['location'])
            self.assertTrue(job['text'])
            self.assertTrue(job['details_checked'])
        self.assertIsNone(readers.parse_schema_job('<script>not JSON</script>', 'https://example.org/job/1', 'Example'))

    def test_saved_boozallen_page(self):
        page = (FIXTURES / 'boozallen-job.html').read_text()
        job, err = readers.parse_boozallen_job(page, 'https://careers.boozallen.com/careers/JobDetail/example/123')
        self.assertIsNone(err)
        self.assertIn('Software Engineer', job['title'])
        self.assertIn('USA', job['location'])
        self.assertFalse(job['remote'])
        self.assertEqual(readers.parse_boozallen_job('', 'https://example.org')[0], None)

    def test_saved_intuit_cards(self):
        rows = readers.intuit_jobs((FIXTURES / 'intuit-search.html').read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Software Engineer 1')
        self.assertEqual(rows[0]['location'], 'Multiple Locations')
        self.assertTrue(rows[0]['url'].startswith('https://jobs.intuit.com/job/'))
        self.assertEqual(readers.intuit_jobs(''), [])

    def test_sitemap_empty_error_same_host_and_hard_cap(self):
        company = {'name':'Example', 'provider':'sitemap_jobs', 'sitemap_url':'https://example.org/sitemap.xml', 'job_path':'/job/'}
        urls = ['https://example.org/job/software-engineer-' + str(n) for n in range(8)]
        sitemap = '<urlset>' + ''.join('<url><loc>' + u + '</loc></url>' for u in urls + ['https://other.org/job/software-engineer']) + '</urlset>'
        schema = '<script type="application/ld+json">' + json.dumps({'@type':'JobPosting','title':'Software Engineer','description':'Build software','jobLocation':{'address':{'addressLocality':'New York','addressCountry':'US'}}}) + '</script>'
        calls = []
        class Client:
            async def text(self, url):
                calls.append(url)
                return 200, sitemap if url.endswith('sitemap.xml') else schema
        found, err = asyncio.run(readers.fetch_sitemap(company, CFG, Client()))
        self.assertIsNone(err)
        self.assertEqual(len(found), 2)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any('other.org' in u for u in calls))
        class Error:
            async def text(self, url):return status, body
        for status, body, expected in [(403, '', True), (200, 'not XML', True), (200, '<urlset/>', False)]:
            found, err = asyncio.run(readers.fetch_sitemap(company, CFG, Error()))
            self.assertEqual(found, [])
            self.assertEqual(bool(err), expected)

    def test_intuit_errors_and_complete_detail(self):
        company = {'name':'Intuit','provider':'intuit','tier':2}
        class Client:
            async def text(self, url):
                if 'search-jobs?' in url:return 200, (FIXTURES / 'intuit-search.html').read_text()
                return 200, (FIXTURES / 'intuit-job.html').read_text()
        found, err = asyncio.run(readers.fetch_intuit(company, CFG, Client()))
        self.assertIsNone(err)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]['text'])
        class Error:
            async def text(self, url):return status, body
        for status, body in [(403, ''), (200, '')]:
            found, err = asyncio.run(readers.fetch_intuit(company, CFG, Error()))
            self.assertEqual(found, [])
            self.assertTrue(err)

    def test_morgan_saved_full_time_and_internship(self):
        data = json.loads((FIXTURES / 'morgan-graduate.json').read_text())
        jobs_ = readers.morgan_jobs(data['resultSet'])
        self.assertEqual(len(jobs_), 2)
        self.assertEqual({j['employment'] for j in jobs_}, {'Full Time', 'Internship'})
        self.assertIn('2027', jobs_[0]['title'])
        class Client:
            async def json(self, url):return status, payload
        for status, payload, expected in [(200, {'resultSet':[]}, False), (200, {}, True), (403, {}, True)]:
            found, err = asyncio.run(readers.fetch_morgan({'name':'Morgan Stanley'}, Client()))
            self.assertEqual(found, [])
            self.assertEqual(bool(err), expected)

    def test_workable_saved_list_details_empty_partial_and_error(self):
        company = {'name':'Hugging Face','slug':'huggingface','provider':'workable','tier':2}
        payload = json.loads((FIXTURES / 'workable-jobs.json').read_text())
        rows = readers.workable_jobs(payload, company)
        self.assertEqual(len(rows), 6)
        self.assertTrue(any('New York' in j['location'] for j in rows))
        self.assertTrue(all(j['url'].startswith('https://apply.workable.com/huggingface/j/') for j in rows))
        calls = []
        class Client:
            async def json(self, url, body=None):
                calls.append((url, body))
                return (200, payload) if '/v3/' in url else (200, {'description':'Build open-source ML tools','requirements':'Python'})
        found, err = asyncio.run(readers.fetch_workable(company, CFG, Client()))
        self.assertIsNone(err)
        self.assertEqual(len(found), 1)
        self.assertEqual(calls[0][1], {})
        self.assertTrue(found[0]['text'])
        class Error:
            async def json(self, url, body=None):return status, data
        for status, data, expected in [(200, {'results':[], 'total':0}, False), (200, {'results':[], 'total':1}, True), (403, {}, True), (200, {}, True)]:
            found, err = asyncio.run(readers.fetch_workable(company, CFG, Error()))
            self.assertEqual(found, [])
            self.assertEqual(bool(err), expected)

    def test_custom_dispatch_uses_guarded_transport_and_remembers_rejected_details(self):
        company = {'name':'Blizzard','provider':'sitemap_jobs','sitemap_url':'https://careers.blizzard.com/global/en/sitemap.xml','job_path':'/global/en/job/','tier':3}
        url = 'https://careers.blizzard.com/global/en/job/123/software-engineer'
        class Client:
            def __init__(self, **kwargs):pass
            async def text(self, u):
                if u.endswith('sitemap.xml'):return 200, '<urlset><url><loc>' + url + '</loc></url></urlset>'
                return 200, (FIXTURES / 'blizzard-job.html').read_text()
        async def unused(*args):self.fail('unguarded callback called')
        with tempfile.TemporaryDirectory() as directory, patch('public_jobs.PublicJobsClient', Client):
            db = Path(directory) / 'seen.db'
            _, _, errors = asyncio.run(jobs.scan(dict(CFG, companies=[company]), unused, db, mark=True, now=NOW))
            self.assertEqual(errors, [])
            self.assertIn('page:' + url, jobs.seen_keys(db, ['page:' + url]))

    def test_sig_guarded_dispatch_and_page_cap(self):
        company = {'name':'SIG','provider':'jibe','host':'careers.sig.com','queries':['software engineer'],'pages':1,'guarded':True,'tier':2}
        calls = []
        class Client:
            def __init__(self, **kwargs):pass
            async def json(self, url):
                calls.append(url)
                return 200, {'jobs':[{'data':{'slug':'101','title':'Software Engineer','full_location':'New York, New York','description':'Build software','employment_type':'FULL_TIME'}}], 'totalCount':1000}
        async def unused(*args):self.fail('unguarded callback called')
        with tempfile.TemporaryDirectory() as directory, patch('public_jobs.PublicJobsClient', Client):
            _, count, errors = asyncio.run(jobs.scan(dict(CFG,companies=[company]), unused, Path(directory)/'seen.db', mark=False, now=NOW))
        self.assertEqual(errors, [])
        self.assertEqual(count, 1)
        self.assertEqual(len(calls), 1)

    def test_ashby_sparse_posting_does_not_index_empty_locations_or_pay(self):
        for name in ('Aurora', 'Superhuman'):
            row = {'id':'example','title':'Software Engineer','location':'','secondaryLocations':[],
                   'jobUrl':'https://example.org/job/1','compensation':{}}
            job = jobs.normalize('ashby', row, name)
            self.assertEqual(job['location'], '')
            self.assertIsNone(job['pay'])
            self.assertEqual(job['url'], row['jobUrl'])

    def test_custom_config_estimates_and_tiers(self):
        for company in CFG['companies']:
            if company['provider'] not in ('intuit','morganstanley','sitemap_jobs','workable') and not company.get('guarded'):
                continue
            self.assertEqual(company['tier'], jobs.tier_from_ratings(company['ratings']))
            self.assertIn('Estimates', company['ratings_note'])
            self.assertIn(company['ratings']['sponsor'], (True,False,None))


class PublicPostTests(unittest.TestCase):
    def test_post_robots_cache_body_identity_and_stop(self):
        calls = []
        async def raw(url, body=None):
            calls.append((url, body))
            if url.endswith('robots.txt'):return 200, {}, 'User-agent: *\nDisallow: /private\n'
            return (403, {}, '') if body == {'blocked':True} else (200, {}, json.dumps(body))
        client = PublicJobsClient(raw, pause=0)
        async def run():
            self.assertEqual(await client.json('https://example.org/private', {}), (0, {}))
            self.assertEqual(await client.json('https://example.org/jobs', {}), (200, {}))
            self.assertEqual(await client.json('https://example.org/jobs', {}), (200, {}))
            self.assertEqual(await client.json('https://example.org/jobs', {'page':2}), (200, {'page':2}))
            self.assertEqual((await client.json('https://example.org/jobs', {'blocked':True}))[0], 403)
            self.assertEqual((await client.json('https://example.org/other', {}))[0], 0)
        asyncio.run(run())
        self.assertEqual(len(calls), 4)
        self.assertIsNone(calls[0][1])

    def test_post_redirect_checks_destination_robots(self):
        calls = []
        async def raw(url, body=None):
            calls.append((url, body))
            if url.endswith('robots.txt'):return 200, {}, 'User-agent: *\nDisallow: /blocked\n'
            return 307, {'Location':'/blocked'}, ''
        client = PublicJobsClient(raw, pause=0)
        asyncio.run(client.json('https://example.org/jobs', {}))
        self.assertEqual(calls, [('https://example.org/robots.txt', None), ('https://example.org/jobs', {})])


class StaleLinkAndEmptyPageTests(unittest.TestCase):
    """One removed posting or one empty search page is normal and must not fail the whole source."""

    def test_a_removed_posting_in_the_sitemap_is_skipped(self):
        company = {'name':'Example', 'provider':'sitemap_jobs', 'sitemap_url':'https://example.org/sitemap.xml', 'job_path':'/job/'}
        urls = ['https://example.org/job/software-engineer-gone', 'https://example.org/job/software-engineer-live']
        sitemap = '<urlset>' + ''.join('<url><loc>' + u + '</loc></url>' for u in urls) + '</urlset>'
        schema = '<script type="application/ld+json">' + json.dumps({'@type':'JobPosting','title':'Software Engineer','description':'Build software','jobLocation':{'address':{'addressLocality':'New York','addressCountry':'US'}}}) + '</script>'
        class Client:
            def __init__(self, dead_status): self.dead_status = dead_status
            async def text(self, url):
                if url.endswith('sitemap.xml'): return 200, sitemap
                return (self.dead_status, '') if url.endswith('-gone') else (200, schema)
        for dead in (404, 410):
            found, err = asyncio.run(readers.fetch_sitemap(company, CFG, Client(dead)))
            self.assertIsNone(err)
            self.assertEqual([j['title'] for j in found], ['Software Engineer'])
        found, err = asyncio.run(readers.fetch_sitemap(company, CFG, Client(500)))
        self.assertEqual(err, 'posting HTTP 500')                    # a server error is still reported

    def test_apple_reports_only_when_every_results_page_is_empty(self):
        company = {'name':'Apple', 'locations':{'new-york-city-NYC':'New York'}, 'queries':['software engineer', 'machine learning']}
        async def get_html(url): return 200, '<html></html>'
        role = {'url':'https://jobs.apple.com/en-us/details/1', 'title':'Software Engineer'}
        with patch.object(jobs, 'parse_apple_html', side_effect=[[], [role]]):
            found, err = asyncio.run(jobs.fetch_apple(company, get_html, CFG))
        self.assertEqual((len(found), err), (1, None))
        with patch.object(jobs, 'parse_apple_html', return_value=[]):
            found, err = asyncio.run(jobs.fetch_apple(company, get_html, CFG))
        self.assertEqual(found, [])
        self.assertIn('layout may have changed', err)
