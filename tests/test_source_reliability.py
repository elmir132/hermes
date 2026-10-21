"""Bounded, polite source reads and honest failures under transient load."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

import jobs
import public_readers as readers
from public_jobs import PublicJobsClient

FIXTURES = Path(__file__).parent / 'fixtures'
CFG = jobs.load_config()
SF = next(c for c in CFG['companies'] if c['name'] == 'Salesforce')
INTUIT = next(c for c in CFG['companies'] if c['name'] == 'Intuit')
SAMPLE = json.loads((FIXTURES / 'workday_public.json').read_text())
POST = dict(SAMPLE['jobPostings'][0], title='Software Engineer, New Grad', locationsText='New York, NY', postedOn='Posted Today')


class WorkdayReliabilityTests(unittest.TestCase):
    def test_salesforce_page_caps_keep_queries_and_offsets_separate(self):
        calls = []
        async def net(url, body):
            calls.append(body)
            if body['limit'] == 1:
                return 200, SAMPLE
            return 200, {'total': 500, 'jobPostings': [dict(POST, externalPath='/job/' + str(body['appliedFacets']) + str(body['offset'] + i)) for i in range(body['limit'])]}
        found, err = asyncio.run(jobs.fetch_workday(SF, net))
        self.assertIsNone(err)
        self.assertEqual(len(found), 80)  # Bounded to 40 NY + 40 Bay/remote.
        self.assertEqual([(c['limit'], c['offset']) for c in calls], [(1, 0), (20, 0), (20, 20), (20, 0), (20, 20)])
        self.assertEqual(calls[1]['appliedFacets'], {'locations': ['0e0bece1e96e01cc0d5d99c50f0c865d']})
        self.assertNotIn('0e0bece1e96e01cc0d5d99c50f0c865d', calls[-1]['appliedFacets']['locations'])

    def test_later_page_failure_retains_partial_rows_and_stops_other_groups(self):
        calls = []
        async def net(url, body):
            calls.append(body)
            if body['limit'] == 1:
                return 200, SAMPLE
            if body['offset']:
                return 429, {}
            return 200, {'total': 100, 'jobPostings': [dict(POST, externalPath='/job/' + str(i)) for i in range(body['limit'])]}
        found, err = asyncio.run(jobs.fetch_workday(SF, net))
        self.assertEqual(len(found), 20)
        self.assertEqual(err, 'listing HTTP 429')
        self.assertEqual(len(calls), 3)

    def test_429_with_or_without_retry_after_is_not_retried(self):
        for headers in ({}, {'Retry-After': '10'}, {'Retry-After': 'Wed, 21 Oct 2026 12:00:00 GMT'}):
            calls = []
            async def raw(url, body=None):
                calls.append(url)
                if url.endswith('/robots.txt'):
                    return 200, {}, ''
                if body and body['limit'] == 1:
                    return 200, {}, json.dumps(SAMPLE)
                return 429, headers, ''
            c = PublicJobsClient(raw, pause=0)
            found, err = asyncio.run(readers.fetch_paced_workday(SF, CFG, c))
            self.assertEqual(found, [])
            self.assertEqual(err, 'listing HTTP 429')
            self.assertEqual(len(calls), 3)  # robots, metadata, failed list; no details.

    def test_detail_cap_and_detail_failure_are_recorded_by_scan(self):
        for failure in (None, 429):
            calls = []
            async def raw(url, body=None):
                calls.append((url, body))
                if url.endswith('/robots.txt'):
                    return 200, {}, ''
                if body and body['limit'] == 1:
                    return 200, {}, json.dumps(SAMPLE)
                if body:
                    return 200, {}, json.dumps({'total': 5, 'jobPostings': [dict(POST, externalPath='/job/' + str(i)) for i in range(5)]})
                if failure:
                    return failure, {}, ''
                return 200, {}, json.dumps({'jobPostingInfo': {'jobDescription': 'Build software. Visa sponsorship available.', 'timeType': 'Full time'}})
            options = []
            def factory(**kw):
                options.append(kw)
                return PublicJobsClient(raw, pause=0)
            async def forbidden(*args, **kwargs):
                self.fail('paced Salesforce must not use the legacy transport or detail phase')
            with tempfile.TemporaryDirectory() as tmp, patch('public_jobs.PublicJobsClient', factory):
                db = Path(tmp) / 'jobs.db'
                text, count, errors = asyncio.run(jobs.scan(dict(CFG, companies=[SF]), forbidden, db, mark=False))
                with closing(sqlite3.connect(db)) as con:
                    status = con.execute('select last_result_count,last_error from jobs_source_status').fetchone()
            details = [u for u, b in calls if b is None and not u.endswith('/robots.txt')]
            self.assertEqual(len(details), 1 if failure else 3)
            self.assertIn({'timeout': 25, 'pause': 2}, options)
            self.assertEqual(bool(errors), bool(failure))
            self.assertEqual(bool(status[1]), bool(failure))
            self.assertIn('Sources: 0/1' if failure else 'Sources: 1/1', text)
            self.assertEqual(count, 0 if failure else 3)

    def test_completed_employment_rejections_do_not_spend_tomorrows_detail_cap(self):
        details = []
        async def raw(url, body=None):
            if url.endswith('/robots.txt'):
                return 200, {}, ''
            if body and body['limit'] == 1:
                return 200, {}, json.dumps(SAMPLE)
            if body:
                return 200, {}, json.dumps({'total': 1, 'jobPostings': [POST]})
            details.append(url)
            return 200, {}, json.dumps({'jobPostingInfo': {'jobDescription': 'Build software.', 'timeType': 'Part time'}})
        def factory(**kw):
            return PublicJobsClient(raw, pause=0)
        async def forbidden(*args):
            self.fail('no legacy reads')
        with tempfile.TemporaryDirectory() as tmp, patch('public_jobs.PublicJobsClient', factory):
            db = Path(tmp) / 'jobs.db'
            for _ in range(2):
                _, count, errors = asyncio.run(jobs.scan(dict(CFG, companies=[SF]), forbidden, db))
                self.assertEqual(count, 0)
                self.assertEqual(errors, [])
        self.assertEqual(len(details), 1)

    def test_empty_list_and_malformed_metadata(self):
        class Client:
            async def json(self, url, body):
                if body['limit'] == 1:
                    return 200, SAMPLE
                return 200, {'total': 0, 'jobPostings': []}
        self.assertEqual(asyncio.run(readers.fetch_paced_workday(SF, CFG, Client())), ([], None))
        async def broken(url, body):
            return 200, None
        self.assertEqual(asyncio.run(jobs.fetch_workday(SF, broken)), ([], 'HTTP 200'))

    def test_paced_source_deadline_is_reported_and_not_swallowed(self):
        real_wait = asyncio.wait_for
        async def wait(coro, timeout):
            if timeout == 60:
                coro.close()
                raise TimeoutError
            return await real_wait(coro, timeout)
        async def forbidden(*args):
            self.fail('no legacy reads')
        with tempfile.TemporaryDirectory() as tmp, patch('jobs.asyncio.wait_for', wait):
            text, _, errors = asyncio.run(jobs.scan(dict(CFG, companies=[SF]), forbidden, Path(tmp) / 'jobs.db', mark=False))
        self.assertEqual(errors, ['Salesforce: paced Workday exceeded 60-second budget'])
        self.assertIn('Sources: 0/1', text)

    def test_other_workday_sources_keep_twenty_item_pages(self):
        calls = []
        async def net(url, body):
            calls.append(body)
            if body['limit'] == 1:
                return 200, SAMPLE
            return 200, {'total': 0, 'jobPostings': []}
        company = dict(SF)
        company.pop('paced')
        company.pop('page_cap')
        asyncio.run(jobs.fetch_workday(company, net))
        self.assertEqual([b['limit'] for b in calls], [1, 20, 20])


class IntuitReliabilityTests(unittest.TestCase):
    def test_scan_gives_only_intuit_a_longer_request_timeout(self):
        options = []
        def factory(**kw):
            options.append(kw)
            # Model a first byte at 19s: the old 18s timeout fails, 30s succeeds.
            async def raw(url):
                if url.endswith('/robots.txt'):
                    return 200, {}, ''
                if kw['timeout'] < 19:
                    raise TimeoutError('slow first byte')
                name = 'intuit-search.html' if 'search-jobs?' in url else 'intuit-job.html'
                return 200, {}, (FIXTURES / name).read_text()
            return PublicJobsClient(raw, pause=0, **kw)
        async def forbidden(*args):
            self.fail('Intuit must use its public client')
        with tempfile.TemporaryDirectory() as tmp, patch('public_jobs.PublicJobsClient', factory):
            _, _, errors = asyncio.run(jobs.scan(dict(CFG, companies=[INTUIT]), forbidden, Path(tmp) / 'jobs.db', mark=False))
        self.assertEqual(errors, [])
        self.assertEqual(options, [{'timeout': 18}, {'timeout': 30}])

    def test_timeout_reason_is_reported_without_sensitive_exception_text(self):
        for exception in (URLError(TimeoutError('sensitive exception text')), ConnectionResetError('sensitive exception text')):
            async def raw(url):
                if url.endswith('/robots.txt'):
                    return 200, {}, ''
                raise exception
            c = PublicJobsClient(raw, pause=0)
            found, err = asyncio.run(readers.read(INTUIT, CFG, c))
            self.assertEqual(found, [])
            category = 'TimeoutError' if isinstance(exception, URLError) else 'ConnectionResetError'
            self.assertEqual(err, f'search HTTP 0 (transport {category})')
            self.assertEqual(len(c.requests), 2)

    def test_robots_transport_failure_is_identified_before_search_is_requested(self):
        calls = []
        async def raw(url):
            calls.append(url)
            raise TimeoutError
        c = PublicJobsClient(raw, pause=0)
        _, err = asyncio.run(readers.read(INTUIT, CFG, c))
        self.assertEqual(err, 'search HTTP 0 (robots.txt HTTP 0 (transport TimeoutError))')
        self.assertEqual(calls, ['https://jobs.intuit.com/robots.txt'])

    def test_intuit_budget_changes_without_changing_other_readers(self):
        budgets = []
        async def wait(coro, timeout):
            budgets.append(timeout)
            coro.close()
            raise TimeoutError
        with patch('public_readers.asyncio.wait_for', wait):
            self.assertEqual(asyncio.run(readers.read(INTUIT, CFG, None)), ([], 'public source exceeded 60-second budget'))
            self.assertEqual(asyncio.run(readers.read({'provider': 'workable'}, CFG, None)), ([], 'public source exceeded 20-second budget'))
        self.assertEqual(budgets, [60, 20])

    def test_raw_transport_passes_the_timeout_to_urllib(self):
        class Response:
            status = 200
            headers = {}
            def __enter__(self):return self
            def __exit__(self, *args):pass
            def read(self, cap):return b'{}'
        class Opener:
            def open(self, req, timeout):
                observed.append(timeout)
                if timeout < 19:
                    raise TimeoutError
                return Response()
        observed = []
        with patch('urllib.request.build_opener', return_value=Opener()):
            for timeout, status in ((18, 0), (30, 200)):
                c = PublicJobsClient(timeout=timeout, pause=0)
                result = asyncio.run(c._request('https://example.com/jobs', 0))
                self.assertEqual(result[0], status)
        self.assertEqual(observed, [18, 30])


class PacingTests(unittest.TestCase):
    def test_same_origin_is_serialized_and_paced_different_origins_are_independent(self):
        clock = [100.0]
        starts = []
        active = set()
        async def sleep(delay):
            clock[0] += delay
        async def raw(url):
            origin = PublicJobsClient._origin(url)
            self.assertNotIn(origin, active)
            active.add(origin)
            starts.append((url, clock[0]))
            await real_sleep(0)  # Let another company contend for this origin.
            active.remove(origin)
            return 200, {}, ''
        real_sleep = asyncio.sleep
        c = PublicJobsClient(raw, pause=2)
        async def run():
            await asyncio.gather(c.text('https://example.com/jobs/1'), c.text('https://example.com/jobs/2'))
            await c.text('https://other.example/jobs')
        with patch('public_jobs.time.monotonic', lambda: clock[0]), patch('public_jobs.asyncio.sleep', sleep):
            asyncio.run(run())
        same_host = [t for u, t in starts if u.startswith('https://example.com/')]
        self.assertEqual(same_host, [100, 102, 104])
        self.assertEqual(starts[3], ('https://other.example/robots.txt', 104))
        self.assertEqual(starts[4], ('https://other.example/jobs', 106))
