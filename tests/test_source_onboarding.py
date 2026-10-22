import asyncio
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

import jobs
import source_health
from test_jobs import cfg_with,NOW
from test_job_alert_worker import main_functions


class NewSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'jobs.db'
        self.old={'name':'Example','provider':'ashby','slug':'old-board'}
        self.new=dict(self.old,slug='new-board')

    async def test_new_or_changed_board_identity_is_not_hidden_by_old_status(self):
        jobs._record_source_status(self.db,[(self.old,'ashby','old-board','Example',[],None)],NOW)
        cfg=cfg_with([self.old,self.new])
        self.assertEqual(source_health.unchecked_companies(cfg,self.db),[self.new])
        jobs._record_source_status(self.db,[(self.new,'ashby','new-board','Example',[],None)],NOW)
        self.assertEqual(source_health.unchecked_companies(cfg,self.db),[])

    async def test_failed_source_not_retried_by_startup_pass(self):
        jobs._record_source_status(self.db,[(self.new,'ashby','new-board','Example',[],'HTTP 429')],NOW)
        self.assertEqual(source_health.unchecked_companies(cfg_with([self.new]),self.db),[])

    async def test_missing_database_is_read_only_and_returns_new_sources(self):
        self.assertEqual(source_health.unchecked_companies(cfg_with([self.new]),self.db),[self.new])
        self.assertFalse(self.db.exists())

    async def test_actual_startup_function_only_refreshes_unchecked_without_mail_or_messages(self):
        cfg=cfg_with([self.old,self.new])
        jobs._record_source_status(self.db,[(self.old,'ashby','old-board','Example',[],None)],NOW)
        refresh=AsyncMock(return_value=[])
        env=main_functions(['_jobs_onboarding_refresh'],{
            'jobs':jobs,'DB_PATH':self.db,'refresh_jobs':refresh,
            'asyncio':SimpleNamespace(sleep=AsyncMock(),to_thread=asyncio.to_thread)})
        with patch.object(jobs,'load_config',return_value=cfg):
            await env['_jobs_onboarding_refresh']()
        refresh.assert_awaited_once()
        self.assertEqual(refresh.await_args.args[0]['companies'],[self.new])
        self.assertFalse(refresh.await_args.kwargs['include_alerts'])
        self.assertTrue(refresh.await_args.kwargs['only_unchecked'])

    async def test_empty_path_and_errors_do_not_crash_main(self):
        refresh=AsyncMock(side_effect=RuntimeError('synthetic failed fetch'))
        env=main_functions(['_jobs_onboarding_refresh'],{'jobs':jobs,'DB_PATH':self.db,'refresh_jobs':refresh,
            'asyncio':SimpleNamespace(sleep=AsyncMock(),to_thread=asyncio.to_thread)})
        with patch.object(jobs,'load_config',return_value=cfg_with([])):
            await env['_jobs_onboarding_refresh']()
        refresh.assert_not_awaited()
        with patch.object(jobs,'load_config',return_value=cfg_with([self.new])):
            await env['_jobs_onboarding_refresh']()
        self.assertEqual(refresh.await_count,1)

    async def test_old_mission_uses_existing_robots_aware_feed_path(self):
        company=next(c for c in jobs.load_config()['companies'] if c['name']=='Old Mission Capital')
        self.assertEqual(company['slug'],'old-mission-capital')
        self.assertTrue(company['public_feed'])

    async def test_scan_lock_recheck_skips_board_already_checked_by_another_refresh(self):
        jobs._record_source_status(self.db,[(self.new,'ashby','new-board','Example',[],None)],NOW)
        forbidden=AsyncMock(side_effect=AssertionError('must not rescan'))
        env=main_functions(['refresh_jobs'],{'jobs':jobs,'DB_PATH':self.db,'JOBS_SCAN_LOCK':asyncio.Lock(),
            '_jobs_get':forbidden,'_jobs_get_html':forbidden})
        errors=await env['refresh_jobs'](cfg_with([self.new]),include_alerts=False,only_unchecked=True)
        self.assertEqual(errors,[])
        forbidden.assert_not_awaited()
