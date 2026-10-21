import ast
import asyncio
import base64
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import digest
import job_alerts
import job_alert_worker as worker
import jobs

SAMPLE = (Path(__file__).parent / 'fixtures/job_alert_cards.html').read_text()
CFG = jobs.load_config()
NOW = 1792500000
SENDERS = ['alerts@example.org']


def mail(body=SAMPLE, sender=SENDERS[0]):
    return {'payload': {'mimeType': 'text/html', 'headers': [{'name': 'From', 'value': sender}],
                        'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'jobs.db'

    async def test_unparsed_retained_without_blocking_other_emails_and_reparsed_after_upgrade(self):
        calls = []
        async def get(url, token, params):
            calls.append((url, params))
            if url.endswith('/messages'):
                return 200, {'messages': [{'id': 'bad'}, {'id': 'good'}]}
            return 200, mail('<p>Unknown template</p>' if url.endswith('/bad') else SAMPLE)
        rows, errors = await job_alerts.fetch_records([('personal', 'token')], get, self.db, SENDERS, NOW)
        self.assertEqual(len(rows), 2)
        self.assertIn('retained', errors[0])
        self.assertNotIn('subject:', calls[0][1]['q'])
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT message_id FROM job_alert_unparsed').fetchall(), [('bad',)])
            self.assertEqual(con.execute('SELECT message_id FROM job_alert_mail_seen').fetchall(), [('good',)])
        get_no_new = AsyncMock(return_value=(200, {'messages': []}))
        await job_alerts.fetch_records([('personal', 'token')], get_no_new, self.db, SENDERS, NOW+1)
        self.assertTrue(all(c.args[0].endswith('/messages') for c in get_no_new.await_args_list))
        real_parse = job_alerts.parse_email
        with patch.object(job_alerts, 'PARSER_VERSION', 2), patch.object(job_alerts, 'parse_email', lambda body: real_parse(SAMPLE)):
            await job_alerts.fetch_records([('personal', 'token')], get_no_new, self.db, SENDERS, NOW+2)
        self.assertEqual(worker.status(self.db)['unparsed'], 0)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM job_alert_mail_seen WHERE message_id='bad'").fetchone()[0], 1)

    async def test_partial_message_queues_known_rows_and_retains_unknown_ones(self):
        partial = SAMPLE + '<article><a href="https://www.linkedin.com/jobs/view/999">Software Engineer</a></article>'
        get = AsyncMock(side_effect=[(200, {'messages': [{'id': 'mixed'}]}), (200, mail(partial))])
        rows, errors = await job_alerts.fetch_records([('personal', 'token')], get, self.db, SENDERS, NOW)
        self.assertEqual(len(rows), 2)
        self.assertTrue(errors)
        self.assertEqual(worker.status(self.db)['unparsed'], 1)

    async def test_unknown_sender_is_not_archived_or_retried(self):
        get = AsyncMock(side_effect=[(200, {'messages': [{'id': 'spoof'}]}), (200, mail(sender='fake@example.org'))])
        rows, errors = await job_alerts.fetch_records([('personal', 'token')], get, self.db, SENDERS, NOW)
        self.assertEqual((rows, errors), ([], []))
        self.assertEqual(worker.status(self.db)['unparsed'], 0)

    async def test_removed_sender_cannot_be_reprocessed(self):
        with closing(job_alerts._db(self.db)) as con, con:
            con.execute('INSERT INTO job_alert_unparsed VALUES (?,?,?,?,?,?,?)', ('personal', '1', SAMPLE, 0, NOW, NOW, 'removed@example.org'))
        self.assertEqual(worker.retry(self.db, SENDERS, NOW), 0)
        self.assertEqual(job_alerts.pending_records(self.db), [])

    async def test_old_unseen_records_and_more_than_one_thousand_are_not_dropped(self):
        records = [job_alerts.record('Software Engineer', 'Example', 'New York, NY', 'https://example.org/jobs/'+str(i)) for i in range(1002)]
        with closing(job_alerts._db(self.db)) as con, con:
            job_alerts._queue(con, records, NOW-40*86400)
        jobs.mark_seen(self.db, [records[0]['key']])
        get = AsyncMock()
        rows, errors = await job_alerts.fetch_records([], get, self.db, SENDERS, NOW)
        self.assertEqual(len(rows), 1001)
        get.assert_not_awaited()
        self.assertFalse(errors)

    async def test_subject_filter_remains_in_digest_but_not_record_ingestion(self):
        self.assertIn('subject:job', digest.job_alert_query(SENDERS))
        self.assertNotIn('subject:', digest.job_alert_query(SENDERS, subject_filter=False))
        self.assertIsNone(digest.job_alert_query([], subject_filter=False))


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'jobs.db'
        self.rows = job_alerts.parse_listings(SAMPLE)

    async def run_tick(self, send, errors=None):
        with patch.object(job_alerts, 'fetch_records', AsyncMock(return_value=(self.rows, errors or []))):
            return await worker.run_once(CFG, [('personal', 'token')], AsyncMock(), self.db, send, SENDERS, NOW)

    async def test_send_failure_does_not_acknowledge_retry_succeeds_and_native_sources_never_read(self):
        send = AsyncMock(side_effect=TimeoutError('private error text'))
        with self.assertRaises(TimeoutError):
            await self.run_tick(send)
        self.assertEqual(jobs.seen_keys(self.db, [r['key'] for r in self.rows]), set())
        self.assertEqual(worker.status(self.db)['last_error'], 'worker failure: TimeoutError')
        send = AsyncMock()
        result = await self.run_tick(send)
        self.assertEqual(result['count'], 2)
        self.assertEqual(len(jobs.seen_keys(self.db, [r['key'] for r in self.rows])), 2)
        result = await self.run_tick(send)
        self.assertEqual(result['count'], 0)
        self.assertEqual(send.await_count, 1)

    async def test_native_key_mapping_and_url_alias_dedupe_work_without_feed_requests(self):
        company = {'name': 'Example', 'provider': 'greenhouse', 'slug': 'example', 'tier': 2}
        cfg = dict(CFG, companies=[company])
        row = job_alerts.record('Software Engineer', 'Example', 'New York, NY', 'https://job-boards.greenhouse.io/example/jobs/123')
        jobs.mark_seen(self.db, ['gh:Example:123'])
        acknowledgement = []
        get = AsyncMock(side_effect=AssertionError('no feed requests'))
        _, count, _ = await jobs.scan(cfg, get, self.db, mark=False, alert_jobs=[row], alert_only=True, ack_keys=acknowledgement)
        get.assert_not_awaited()
        self.assertEqual(count, 0)
        self.assertIn(row['key'], acknowledgement)

    async def test_acknowledgement_keeps_ratings_and_waits_for_send(self):
        company = {'name': 'Example Alpha', 'provider': 'ashby', 'slug': 'example', 'tier': 2, 'ratings': {'pay': 4, 'prestige': 4, 'wlb': 4, 'exits': 4, 'sponsor': True}}
        cfg = dict(CFG, companies=[company])
        ack, shown = [], []
        get = AsyncMock(side_effect=AssertionError('no job network'))
        text, count, _ = await jobs.scan(cfg, get, self.db, mark=False, alert_jobs=self.rows[:1], alert_only=True, ack_keys=ack, collect=shown)
        self.assertEqual(count, 1)
        self.assertEqual(shown[0]['tier'], 2)
        self.assertIn(self.rows[0]['key'], ack)
        self.assertFalse(jobs.seen_keys(self.db, ack))
        self.assertIn(self.rows[0]['url'], text)
        get.assert_not_awaited()

    async def test_existing_filters_reject_senior_and_explicit_citizenship(self):
        rows = [dict(self.rows[0], key='senior', title='Senior Software Engineer'),
                dict(self.rows[1], key='citizen', text='Must be a U.S. citizen')]
        with patch.object(job_alerts, 'fetch_records', AsyncMock(return_value=(rows, []))):
            send = AsyncMock()
            result = await worker.run_once(CFG, [('personal','token')], AsyncMock(), self.db, send, SENDERS, NOW)
        self.assertEqual(result['count'], 0)
        send.assert_not_awaited()

    async def test_truncation_falls_back_to_smaller_batches_without_losing_jobs(self):
        self.rows = [job_alerts.record('Software Engineer', 'Example', 'New York, NY', 'https://example.org/jobs/'+str(i)+'?'+'q='+'x'*1100) for i in range(4)]
        send = AsyncMock()
        result = await self.run_tick(send)
        self.assertEqual(result['count'], 4)
        delivered = '\n'.join(c.args[0] for c in send.await_args_list)
        for row in self.rows:
            self.assertIn(row['url'], delivered)
        self.assertEqual(len(jobs.seen_keys(self.db, [r['key'] for r in self.rows])), 4)

    async def test_oversized_single_record_is_retained_not_acknowledged(self):
        self.rows = [job_alerts.record('Software Engineer', 'Example', 'New York, NY', 'https://example.org/jobs/1?q='+'x'*5000)]
        send = AsyncMock()
        with self.assertRaises(ValueError):
            await self.run_tick(send)
        self.assertFalse(jobs.seen_keys(self.db, [self.rows[0]['key']]))
        send.assert_not_awaited()

    async def test_failure_and_recovery_notice_once_with_last_good_preserved(self):
        self.rows = []
        send = AsyncMock()
        await self.run_tick(send)
        good = worker.status(self.db)['last_success']
        await self.run_tick(send, ['Gmail HTTP 429'])
        await self.run_tick(send, ['Gmail HTTP 429'])
        self.assertEqual(send.await_count, 1)
        self.assertEqual(worker.status(self.db)['last_success'], good)
        await self.run_tick(send)
        await self.run_tick(send)
        self.assertEqual(send.await_count, 2)
        self.assertIn('recovered', send.await_args.args[0])

    async def test_no_senders_means_no_mailbox_access_and_status_explains(self):
        get, send = AsyncMock(), AsyncMock()
        await worker.run_once(CFG, [], get, self.db, send, [], NOW)
        get.assert_not_awaited()
        self.assertIn('No exact', worker.status(self.db)['last_error'])
        self.assertIn('No exact', send.await_args.args[0])

    def test_interval_and_account_configuration(self):
        for raw, expected in [('5', 60), ('10000', 3600), ('bad', 300), ('300', 300)]:
            with patch.dict(os.environ, {'HERMES_JOB_ALERT_INTERVAL_SECONDS': raw}):
                self.assertEqual(worker.interval(), expected)
        with patch.dict(os.environ, {'HERMES_JOB_ALERT_ACCOUNTS': 'personal, work, personal'}):
            self.assertEqual(worker.account_names(), {'personal','work'})


# Load only individual integration functions: importing the real bot connects
# unrelated services and requires credentials, neither belongs in these tests.
def main_functions(names, values):
    tree = ast.parse((Path(__file__).parent.parent / 'main.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    for n in nodes:n.decorator_list = []
    env = {'asyncio':asyncio, 'Message':object, **values}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])), 'main functions', 'exec'), env)
    return env


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_jobs_commits_only_after_successful_delivery(self):
        events = []
        async def run(**kwargs):
            self.assertFalse(kwargs['mark'])
            kwargs['ack_keys'].append('key')
            return 'jobs'
        async def answer(text, **kw):
            if text == 'jobs':
                events.append('send')
                self.assertIsNone(kw['parse_mode'])
                self.assertTrue(kw['disable_web_page_preview'])
        fake_jobs = SimpleNamespace(mark_seen=lambda *args:events.append('ack'))
        env = main_functions({'cmd_jobs'}, dict(allowed=lambda m:True, run_jobs=run, jobs=fake_jobs, DB_PATH='db', JOBS_RUN_LOCK=asyncio.Lock()))
        msg = SimpleNamespace(text='/jobs', answer=answer)
        await env['cmd_jobs'](msg)
        self.assertEqual(events, ['send','ack'])
        async def fail(text, **kw):
            if text == 'jobs':raise TimeoutError
        msg.answer = fail
        events.clear()
        with self.assertRaises(TimeoutError):await env['cmd_jobs'](msg)
        self.assertEqual(events, [])

    async def test_poll_uses_shared_lock_and_plain_messages(self):
        active = []
        sent = []
        async def fake_worker(*args, **kwargs):
            self.assertFalse(active)
            active.append(1)
            await asyncio.sleep(0)
            await args[4]('alert')
            active.pop()
            return {'shown': []}
        async def publish(*args, **kwargs):sent.append(kwargs)
        env = main_functions({'_poll_job_alerts'}, dict(
            JOBS_RUN_LOCK=asyncio.Lock(), digest=SimpleNamespace(parse_job_alert_senders=lambda:()),
            job_alert_worker=SimpleNamespace(run_once=fake_worker), jobs=SimpleNamespace(load_config=lambda:{}),
            _digest_get=None, DB_PATH='db', bot=SimpleNamespace(send_message=publish), ALLOWED_USER=1, LAST_JOBS=[]))
        await asyncio.gather(env['_poll_job_alerts'](), env['_poll_job_alerts']())
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(x['parse_mode'] is None and x['disable_web_page_preview'] for x in sent))

class AdditionalPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_card_requirements_are_preserved_for_existing_filters(self):
        body = SAMPLE.replace('</td></tr>', '<p>Must be a U.S. citizen.</p></td></tr>', 1)
        rows = job_alerts.parse_listings(body)
        self.assertIn('Must be a U.S. citizen', rows[0]['text'])
        with tempfile.TemporaryDirectory() as tmp:
            _, count, _ = await jobs.scan(CFG, AsyncMock(side_effect=AssertionError('no job network')), Path(tmp)/'jobs.db',
                                          mark=False, alert_only=True, alert_jobs=rows)
        self.assertEqual(count, 1)

    async def test_parser_exception_is_retained_as_unparsed_instead_of_blocking_mail(self):
        with patch.object(job_alerts, 'parse_listings', side_effect=ValueError('untrusted private content')):
            self.assertEqual(job_alerts.parse_email('<broken>'), ([], False))

    async def test_account_selection_does_not_open_other_tokens(self):
        selected = []
        env = main_functions({'_job_alert_accounts'}, dict(
            job_alert_worker=SimpleNamespace(account_names=lambda:{'personal'}),
            _all_token_files=lambda:[Path('token_personal.json'),Path('token_other.json')],
            _load_creds=lambda p:(selected.append(p.name) or SimpleNamespace(token='test-token'))))
        self.assertEqual(env['_job_alert_accounts'](), [('personal','test-token')])
        self.assertEqual(selected, ['token_personal.json'])

    async def test_email_worker_starts_independently_of_daily_job_scan_and_remains_opt_in(self):
        for enabled in (False, True):
            tasks = []
            async def followup():pass
            async def alerts():pass
            def start(coro):
                tasks.append(coro.cr_code.co_name)
                coro.close()
            env = main_functions({'main'}, dict(
                asyncio=SimpleNamespace(create_task=start), init_db=lambda:None, init_honcho=lambda:None,
                PANDASCORE_TOKEN='', DIGEST_DAILY=False, JOBS_DAILY=False, FAST_LANE=False,
                job_alert_worker=SimpleNamespace(enabled=lambda:enabled), _job_alert_loop=alerts,
                _followup_loop=followup, dp=SimpleNamespace(start_polling=AsyncMock()), bot=None))
            await env['main']()
            self.assertEqual('alerts' in tasks, enabled)

    async def test_jobs_all_does_not_acknowledge_records(self):
        fake_jobs = SimpleNamespace(mark_seen=lambda *args:self.fail('jobs all must not mark seen'))
        async def run(**kwargs):
            self.assertTrue(kwargs['include_seen'])
            self.assertFalse(kwargs['mark'])
            kwargs['ack_keys'].append('test-key')
            return 'jobs'
        env = main_functions({'cmd_jobs'}, dict(allowed=lambda m:True, run_jobs=run, jobs=fake_jobs, DB_PATH='db', JOBS_RUN_LOCK=asyncio.Lock()))
        await env['cmd_jobs'](SimpleNamespace(text='/jobs all', answer=AsyncMock()))

    async def test_daily_jobs_waits_for_send_before_acknowledging(self):
        for fail in (False, True):
            sleeps, events = [], []
            async def sleep(delay):
                sleeps.append(delay)
                if len(sleeps) == 3:raise asyncio.CancelledError
            async def run(**kwargs):
                self.assertFalse(kwargs['mark'])
                kwargs['ack_keys'].append('key')
                return 'jobs'
            async def send(*args, **kwargs):
                events.append('send')
                if fail:raise TimeoutError
            env = main_functions({'_jobs_loop'}, dict(
                asyncio=SimpleNamespace(sleep=sleep), JOBS_RUN_LOCK=asyncio.Lock(),
                run_jobs=run, bot=SimpleNamespace(send_message=send), ALLOWED_USER=1,
                jobs=SimpleNamespace(mark_seen=lambda *args:events.append('ack')), DB_PATH='db',
                digest=SimpleNamespace(seconds_until=lambda hour:10), DIGEST_HOUR=8))
            with self.assertRaises(asyncio.CancelledError):await env['_jobs_loop']()
            self.assertEqual(events, ['send'] if fail else ['send','ack'])
