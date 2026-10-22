import asyncio
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import job_alerts
import job_store
import jobs
from test_jobs import cfg_with, FakeFeeds, lv_job, gh_job, NOW
from test_job_alert_worker import main_functions


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'jobs.db'
        self.company = {'name': 'Acme', 'provider': 'lever', 'slug': 'acme', 'tier': 2}
        self.cfg = cfg_with([self.company])

    async def ingest(self, rows, **kw):
        feeds = FakeFeeds({'lever': rows})
        return await jobs.scan(self.cfg, feeds, self.db, now=NOW, mark=False, persist=True, **kw)

    def ready(self):
        with closing(job_store.connect(self.db)) as con:
            return con.execute('SELECT COUNT(*) FROM jobs_pending WHERE ready=1').fetchone()[0]

    async def test_all_matches_survive_message_and_company_caps_and_reopen(self):
        await self.ingest([lv_job(i, 'Software Engineer, New Grad') for i in range(17)])
        self.assertEqual(job_store.counts(self.db), {'new': 17})
        self.assertEqual(self.ready(), 17)
        delivered = set()
        for _ in range(6):
            text, shown, ack = job_store.batch(self.db, self.cfg, now=NOW)
            self.assertLessEqual(len(shown), 3)
            self.assertTrue(all(j['url'] in text for j in shown))
            self.assertFalse(delivered.intersection(j['key'] for j in shown))
            delivered.update(j['key'] for j in shown)
            jobs.mark_seen(self.db, ack, NOW)
        self.assertEqual(len(delivered), 17)
        self.assertEqual(job_store.counts(self.db), {'sent': 17})
        self.assertFalse(job_store.batch(self.db, self.cfg, now=NOW)[1])

    async def test_requirements_beyond_output_cap_are_checked_not_queued_for_delivery(self):
        rows = [lv_job(i, 'Software Engineer') for i in range(5)]
        rows += [lv_job(10, 'Software Engineer', text='Requires 5 years of experience.'),
                 lv_job(11, 'Software Engineer', text='Must be a U.S. citizen.'),
                 lv_job(12, 'Software Engineer', text='Salary $80,000 - $100,000')]
        await self.ingest(rows)
        self.assertEqual(job_store.counts(self.db), {'new': 5, 'rejected': 3})

    async def test_listing_failure_does_not_erase_pending_jobs(self):
        await self.ingest([lv_job(1, 'Software Engineer')])
        _, _, errors = await jobs.scan(self.cfg, AsyncMock(return_value=(503, {})), self.db,
                                       mark=False, persist=True, now=NOW)
        self.assertTrue(errors)
        self.assertEqual(len(job_store.batch(self.db, self.cfg, now=NOW)[1]), 1)

    async def test_seen_store_migrates_and_sent_state_survives_seen_expiry(self):
        raw = lv_job(1, 'Software Engineer')
        jobs.mark_seen(self.db, ['lv:Acme:l1'], NOW)
        await self.ingest([raw])
        self.assertEqual(job_store.counts(self.db), {'sent': 1})
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('DELETE FROM jobs_seen')
        await self.ingest([raw])
        self.assertFalse(job_store.batch(self.db, self.cfg, now=NOW)[1])

    async def test_native_and_email_url_alias_have_one_persistent_identity(self):
        raw = lv_job(1, 'Software Engineer')
        await self.ingest([raw])
        row = job_alerts.record('Software Engineer', 'Acme', 'New York', raw['hostedUrl']+'?utm_source=mail')
        await jobs.scan(self.cfg, AsyncMock(), self.db, now=NOW, mark=False,
                        persist=True, alert_only=True, alert_jobs=[row])
        self.assertEqual(job_store.counts(self.db), {'new': 1})
        jobs.mark_seen(self.db, [row['key']], NOW)
        self.assertEqual(job_store.counts(self.db), {'sent': 1})

    async def test_status_and_first_seen_preserved_and_last_seen_advances(self):
        await self.ingest([lv_job(1, 'Software Engineer')])
        key = next(iter(job_store.states(self.db)))
        self.assertTrue(job_store.set_status(self.db, key, 'dismissed'))
        await jobs.scan(self.cfg, FakeFeeds({'lever': [lv_job(1, 'Software Engineer')]}), self.db,
                        mark=False, persist=True, now=NOW+timedelta(hours=1))
        with closing(job_store.connect(self.db)) as con:
            row = con.execute('SELECT first_seen,last_seen,status FROM jobs_pending').fetchone()
        self.assertEqual(row['first_seen'], NOW.isoformat())
        self.assertEqual(row['last_seen'], (NOW+timedelta(hours=1)).isoformat())
        self.assertEqual(row['status'], 'dismissed')
        with self.assertRaises(ValueError):job_store.set_status(self.db, key, 'invalid')

    async def test_detail_cap_rotates_without_losing_discoveries(self):
        cfg = cfg_with([{'name': 'Acme', 'provider': 'greenhouse', 'slug': 'acme'}])
        detail_calls = []
        async def feed(url):
            if '/jobs/' in url:
                detail_calls.append(url)
                return 200, {'content': 'Full time engineering role.'}
            return 200, {'jobs': [gh_job(i, 'Software Engineer') for i in range(5)]}
        with patch.object(jobs, 'DETAIL_LIMIT', 1):
            await jobs.scan(cfg, feed, self.db, mark=False, persist=True, now=NOW)
            self.assertEqual(self.ready(), 2)
            self.assertEqual(job_store.counts(self.db), {'new': 5})
            await jobs.scan(cfg, feed, self.db, mark=False, persist=True, now=NOW)
            self.assertEqual(self.ready(), 4)
            await jobs.scan(cfg, feed, self.db, mark=False, persist=True, now=NOW)
            self.assertEqual(self.ready(), 5)
        self.assertEqual(len(detail_calls), 5)

    async def test_failed_high_priority_details_do_not_starve_unchecked_discoveries(self):
        cfg = cfg_with([{'name':'Acme','provider':'greenhouse','slug':'acme'}])
        calls = []
        async def feed(url):
            if '/jobs/' not in url:
                return 200, {'jobs':[gh_job(i, f'Software Engineer Team{i}') for i in range(4)]}
            calls.append(url)
            return (503,{}) if '/jobs/0?' in url or '/jobs/1?' in url else (200,{'content':'Build software.'})
        with patch.object(jobs,'DETAIL_LIMIT',1):
            await jobs.scan(cfg,feed,self.db,now=NOW,mark=False,persist=True)
            self.assertEqual(len(calls),2)
            await jobs.scan(cfg,feed,self.db,now=NOW,mark=False,persist=True)
            self.assertEqual(len(calls),4)
            self.assertIn('/jobs/2?',calls[2])
            self.assertIn('/jobs/3?',calls[3])
            await jobs.scan(cfg,feed,self.db,now=NOW,mark=False,persist=True)
            self.assertEqual(len(calls),6)  # Retries resume once untouched work is done.
        self.assertEqual(self.ready(),4)

    async def test_discovery_survives_interrupted_detail_request(self):
        cfg = cfg_with([{'name': 'Acme', 'provider': 'greenhouse', 'slug': 'acme'}])
        async def feed(url):
            if '/jobs/' in url:raise asyncio.CancelledError
            return 200, {'jobs': [gh_job(1, 'Software Engineer')]}
        with self.assertRaises(asyncio.CancelledError):
            await jobs.scan(cfg, feed, self.db, mark=False, persist=True, now=NOW)
        self.assertEqual(job_store.counts(self.db), {'new': 1})
        self.assertEqual(self.ready(), 0)

    async def test_include_seen_is_read_only_and_blocks_fit_telegram_limit(self):
        await self.ingest([lv_job(i, 'Software Engineer', text='x'*20000) for i in range(15)])
        text, shown, ack = job_store.batch(self.db, self.cfg, now=NOW)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2, 3400)
        jobs.mark_seen(self.db, ack, NOW)
        before = job_store.counts(self.db)
        self.assertTrue(job_store.batch(self.db, self.cfg, include_seen=True, now=NOW)[1])
        self.assertEqual(before, job_store.counts(self.db))

    async def test_manual_browsing_is_local_even_during_network_scan(self):
        await self.ingest([lv_job(1, 'Software Engineer')])
        shown = []
        env = main_functions({'run_jobs', 'cmd_jobs'}, dict(
            allowed=lambda _:True, jobs=jobs, DB_PATH=self.db,
            JOBS_RUN_LOCK=asyncio.Lock(), LAST_JOBS=shown,
            refresh_jobs=AsyncMock(side_effect=AssertionError('must not scan'))))
        with patch.object(jobs, 'load_config', return_value=self.cfg):
            answer = AsyncMock()
            await asyncio.wait_for(env['cmd_jobs'](SimpleNamespace(text='/jobs', answer=answer)), .5)
        self.assertEqual(answer.await_count, 1)
        self.assertEqual(job_store.counts(self.db), {'sent': 1})
        self.assertIn('Software Engineer', answer.await_args.args[0])

    async def test_failed_manual_send_preserves_durable_pending_state(self):
        await self.ingest([lv_job(1, 'Software Engineer')])
        env = main_functions({'run_jobs', 'cmd_jobs'}, dict(
            allowed=lambda _:True, jobs=jobs, DB_PATH=self.db,
            JOBS_RUN_LOCK=asyncio.Lock(), LAST_JOBS=[]))
        with patch.object(jobs, 'load_config', return_value=self.cfg):
            with self.assertRaises(TimeoutError):
                await env['cmd_jobs'](SimpleNamespace(text='/jobs', answer=AsyncMock(side_effect=TimeoutError)))
        self.assertEqual(job_store.counts(self.db), {'new': 1})
        self.assertEqual(len(job_store.batch(self.db, self.cfg, now=NOW)[1]), 1)

    async def test_fast_lane_uses_queue_overflow_and_never_sends_initial_backlog(self):
        rows = [lv_job(0, 'Software Engineer')]
        feed = FakeFeeds({'lever': rows})
        send = AsyncMock()
        env = main_functions({'refresh_jobs', 'run_jobs', 'run_fast_lane'}, dict(
            jobs=jobs, DB_PATH=self.db, LAST_JOBS=[], JOBS_RUN_LOCK=asyncio.Lock(),
            JOBS_SCAN_LOCK=asyncio.Lock(), _jobs_get=feed, _jobs_get_html=None,
            bot=SimpleNamespace(send_message=send), ALLOWED_USER=1))
        with patch.object(jobs, 'load_config', return_value=self.cfg):
            self.assertEqual(await env['run_fast_lane'](NOW), 0)
            send.assert_not_awaited()
            rows.extend(lv_job(i, 'Software Engineer') for i in range(1, 9))
            self.assertEqual(await env['run_fast_lane'](NOW), 3)
            self.assertEqual(await env['run_fast_lane'](NOW), 3)
            self.assertEqual(await env['run_fast_lane'](NOW), 2)
            self.assertEqual(await env['run_fast_lane'](NOW), 0)
        self.assertEqual(job_store.counts(self.db), {'new': 1, 'sent': 8})

    async def test_preupgrade_fast_baseline_does_not_block_preparation_or_spam_backlog(self):
        raw = lv_job(1, 'Software Engineer')
        jobs.fast_lane_remember(self.db, ['lv:Acme:l1'], NOW)
        job_store.upsert(self.db, [(jobs.normalize('lever', raw, 'Acme'), 'lever:acme', False, False)], NOW)
        text, rows, _ = job_store.batch(self.db, self.cfg, now=NOW)
        self.assertFalse(rows)
        self.assertIn('await eligibility/detail checks', text)
        send = AsyncMock()
        env = main_functions({'refresh_jobs', 'run_jobs', 'run_fast_lane'}, dict(
            jobs=jobs, DB_PATH=self.db, LAST_JOBS=[], JOBS_RUN_LOCK=asyncio.Lock(),
            JOBS_SCAN_LOCK=asyncio.Lock(), _jobs_get=FakeFeeds({'lever':[raw]}), _jobs_get_html=None,
            bot=SimpleNamespace(send_message=send), ALLOWED_USER=1))
        with patch.object(jobs, 'load_config', return_value=self.cfg):
            self.assertEqual(await env['run_fast_lane'](NOW), 0)
        send.assert_not_awaited()
        self.assertEqual(self.ready(), 1)
        self.assertEqual(len(job_store.batch(self.db, self.cfg, now=NOW)[1]), 1)

    async def test_failed_detail_is_labelled_and_can_be_retried_on_a_later_scan(self):
        cfg = cfg_with([{'name': 'Acme', 'provider': 'greenhouse', 'slug': 'acme'}])
        detail_status = 503
        async def feed(url):
            if '/jobs/' in url:return detail_status, {'content': 'Full time role.'}
            return 200, {'jobs': [gh_job(1, 'Software Engineer')]}
        await jobs.scan(cfg, feed, self.db, mark=False, persist=True, now=NOW)
        text, _, _ = job_store.batch(self.db, cfg, now=NOW)
        self.assertIn('posting details not read', text)
        detail_status = 200
        await jobs.scan(cfg, feed, self.db, mark=False, persist=True, now=NOW)
        text, _, _ = job_store.batch(self.db, cfg, now=NOW)
        self.assertNotIn('posting details not read', text)

class StableIdentityTests(unittest.IsolatedAsyncioTestCase):
    setUp = QueueTests.setUp
    ingest = QueueTests.ingest
    async def test_id_survives_reopen_aliases_and_payload_changes(self):
        await self.ingest([lv_job(1, 'Software Engineer')])
        _, rows, _ = job_store.batch(self.db, self.cfg, now=NOW)
        public_id = rows[0]['job_id']
        self.assertTrue(public_id.startswith('J'))
        self.assertEqual(job_store.resolve(self.db, public_id.lower())['url'], rows[0]['url'])
        await self.ingest([lv_job(1, 'Software Engineer, New Grad')])
        self.assertEqual(job_store.resolve(self.db, public_id)['title'], 'Software Engineer, New Grad')
        self.assertEqual(job_store.batch(self.db, self.cfg, now=NOW)[1][0]['job_id'], public_id)

    async def test_short_hash_collisions_extend_without_reassigning_existing_id(self):
        with patch.object(job_store, 'hashlib') as hashing:
            hashing.sha256.side_effect = [SimpleNamespace(hexdigest=lambda:'ABCDE1'+'0'*58),
                                         SimpleNamespace(hexdigest=lambda:'ABCDE2'+'0'*58)]
            await self.ingest([lv_job(1, 'Software Engineer'), lv_job(2, 'Software Engineer')])
        _, rows, _ = job_store.batch(self.db, self.cfg, now=NOW)
        self.assertEqual({r['job_id'] for r in rows}, {'JABCDE','JABCDE2'})
        for row in rows:self.assertEqual(job_store.resolve(self.db,row['job_id'])['key'], row['key'])

    async def test_number_requires_original_reply_and_mapping_survives_new_lists(self):
        await self.ingest([lv_job(1, 'Software Engineer'), lv_job(2, 'Software Engineer')])
        rows = job_store.batch(self.db, self.cfg, now=NOW)[1]
        job_store.remember_message(self.db, 7, 101, rows)
        job_store.remember_message(self.db, 7, 102, list(reversed(rows)))
        self.assertIsNone(job_store.resolve(self.db, '1'))
        self.assertIsNone(job_store.resolve(self.db, '1', chat_id=8, message_id=101))
        self.assertEqual(job_store.resolve(self.db,'1',chat_id=7,message_id=101)['url'], rows[0]['url'])
        self.assertEqual(job_store.resolve(self.db,'1',chat_id=7,message_id=102)['url'], rows[1]['url'])
        job_store.remember_message(self.db,7,101,list(reversed(rows)))
        self.assertEqual(job_store.resolve(self.db,'1',chat_id=7,message_id=101)['url'], rows[0]['url'])

    async def test_pick_job_uses_persistent_id_or_reply_not_memory(self):
        await self.ingest([lv_job(1, 'Software Engineer')])
        rows = job_store.batch(self.db, self.cfg, now=NOW)[1]
        job_store.remember_message(self.db,1,22,rows)
        env = main_functions({'_pick_job'}, dict(DB_PATH=self.db, LAST_JOBS=[{'wrong':'job'}]))
        self.assertIsNone(env['_pick_job']('1'))
        self.assertEqual(env['_pick_job'](rows[0]['job_id'])['url'],rows[0]['url'])
        reply = SimpleNamespace(chat=SimpleNamespace(id=1),reply_to_message=SimpleNamespace(message_id=22))
        self.assertEqual(env['_pick_job']('1',reply)['url'], rows[0]['url'])

    async def test_actual_save_and_applied_handlers_accept_stable_id(self):
        import apply
        await self.ingest([lv_job(1, 'Software Engineer')])
        row = job_store.batch(self.db,self.cfg,now=NOW)[1][0]
        env = main_functions({'_pick_job','_args','cmd_save','cmd_applied'}, dict(
            DB_PATH=self.db, allowed=lambda _:True, apply=apply, LAST_FIT={}))
        with patch.object(apply,'load_profile',return_value=None):
            await env['cmd_save'](SimpleNamespace(text='/save '+row['job_id'],answer=AsyncMock()))
            await env['cmd_applied'](SimpleNamespace(text='/applied '+row['job_id'],answer=AsyncMock()))
        apps=apply.rows(self.db)
        self.assertEqual(len(apps),1)
        self.assertEqual(apps[0]['url'],row['url'])
        self.assertEqual(apps[0]['status'],'applied')

    async def test_actual_send_records_number_mapping_before_acknowledgement(self):
        await self.ingest([lv_job(1,'Software Engineer')])
        sent=SimpleNamespace(chat=SimpleNamespace(id=9),message_id=10)
        env=main_functions({'cmd_jobs','run_jobs','remember_job_message'},dict(
            DB_PATH=self.db, jobs=jobs, LAST_JOBS=[],allowed=lambda _:True,JOBS_RUN_LOCK=asyncio.Lock()))
        with patch.object(jobs,'load_config',return_value=self.cfg):
            await env['cmd_jobs'](SimpleNamespace(text='/jobs',answer=AsyncMock(return_value=sent)))
        self.assertIsNotNone(job_store.resolve(self.db,'1',chat_id=9,message_id=10))
        self.assertEqual(job_store.counts(self.db),{'sent':1})

    async def test_fit_handler_accepts_id_after_restart_without_calling_a_model_when_unconfigured(self):
        import apply
        await self.ingest([lv_job(1,'Software Engineer')])
        row=job_store.batch(self.db,self.cfg,now=NOW)[1][0]
        summarize=AsyncMock(side_effect=AssertionError('no model without profile'))
        env=main_functions({'_pick_job','_args','cmd_fit'},dict(
            DB_PATH=self.db,allowed=lambda _:True,apply=apply,LAST_FIT={},LAST_JOBS=[],
            bot=SimpleNamespace(send_chat_action=AsyncMock()),one_shot=summarize))
        answer=AsyncMock()
        with patch.object(apply,'load_profile',return_value=None):
            await env['cmd_fit'](SimpleNamespace(text='/fit '+row['job_id'],answer=answer,chat=SimpleNamespace(id=1)))
        self.assertIn('Acme: Software Engineer',answer.await_args.args[0])
        self.assertIn('/applied '+row['job_id'],answer.await_args.args[0])
        summarize.assert_not_awaited()
