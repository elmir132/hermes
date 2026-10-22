import asyncio
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import browser_health
import browser_reader
import jobs
import source_health
from test_jobs import NOW


class BrowserHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)/'cache.json'
        self.db = Path(self.tmp.name)/'jobs.db'
        self.company = {'name':'Meta','provider':'browser','source':'Meta','file':str(self.cache)}
        self.cfg = {'companies':[self.company]}
        self.send = AsyncMock()

    def write(self, when=NOW, source_when=None, error=None):
        source = {'jobs':[{'title':'Software Engineer','url':'https://example.org/jobs/1','location':'New York'}], 'error':error}
        if source_when is not None:
            source['fetched_at'] = source_when
        browser_reader.write_snapshot(self.cache, {'fetched_at':when.isoformat(),'sources':{'Meta':source}})

    async def test_age_warning_once_then_recovery_persisted_across_restarts(self):
        self.write(NOW-timedelta(hours=27))
        self.assertTrue(await browser_health.run_once(self.cfg, self.db, self.send, NOW))
        await browser_health.run_once(self.cfg, self.db, self.send, NOW+timedelta(hours=2))
        self.assertEqual(self.send.await_count, 1)
        self.write(NOW+timedelta(hours=2))
        self.assertFalse(await browser_health.run_once(self.cfg, self.db, self.send, NOW+timedelta(hours=2)))
        self.assertEqual(self.send.await_count, 2)
        self.assertIn('fresh again', self.send.await_args.args[0])

    async def test_failed_send_is_not_acknowledged(self):
        self.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await browser_health.run_once(self.cfg, self.db, self.send, NOW)
        self.send.side_effect = None
        await browser_health.run_once(self.cfg, self.db, self.send, NOW)
        self.assertEqual(self.send.await_count, 2)

    async def test_source_stamp_overrides_global_and_coverage_does_not_claim_healthy(self):
        self.write(source_when=(NOW-timedelta(hours=30)).isoformat())
        jobs._record_source_status(self.db, [(self.company,'browser','Meta','Meta',[{}],None)], NOW)
        counts, rows = source_health.coverage(self.cfg,self.db,NOW)
        self.assertEqual(counts['stale'], 1)
        self.assertIn('30 hours', rows[0]['last_error'])

    async def test_invalid_missing_or_future_stamp_and_bad_shapes_are_errors(self):
        for stamp in ('', 'broken', (NOW+timedelta(hours=1)).isoformat()):
            self.write(source_when=stamp)
            self.assertIn('timestamp', jobs.browser_jobs(self.company,self.cfg,NOW)[1])
        for data in ([], {'sources':[]}, {'sources':{'Meta':{'jobs':None}}}):
            self.cache.write_text(json.dumps(data))
            self.assertIn('invalid', jobs.browser_jobs(self.company,self.cfg,NOW)[1])

    async def test_monitor_never_fetches_sites(self):
        self.write()
        with patch('urllib.request.urlopen', side_effect=AssertionError('no HTTP')):
            self.assertFalse(await browser_health.run_once(self.cfg,self.db,self.send,NOW))
        self.send.assert_not_awaited()

    async def test_failed_atomic_write_preserves_previous_snapshot(self):
        self.write()
        before = self.cache.read_bytes()
        with patch.object(browser_reader.json,'dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                browser_reader.write_snapshot(self.cache, {'sources':{}})
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(list(Path(self.tmp.name).glob('.cache.json*')), [])

    async def test_real_main_loop_uses_cache_monitor_and_plain_text_send(self):
        from types import SimpleNamespace
        from test_job_alert_worker import main_functions
        pause = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        bot = SimpleNamespace(send_message=AsyncMock())
        scope = {'asyncio':SimpleNamespace(sleep=pause, to_thread=asyncio.to_thread),
                 'jobs':jobs, 'DB_PATH':self.db, 'bot':bot, 'ALLOWED_USER':123}
        scope = main_functions(['_browser_health_loop'], scope)
        with patch.object(jobs,'load_config',return_value=self.cfg):
            with self.assertRaises(asyncio.CancelledError):
                await scope['_browser_health_loop']()
        bot.send_message.assert_awaited_once()
        self.assertIsNone(bot.send_message.await_args.kwargs['parse_mode'])
        self.assertTrue(bot.send_message.await_args.kwargs['disable_web_page_preview'])

    async def test_partial_reader_updates_only_its_own_timestamp_without_network(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        self.write(NOW)
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.launch.return_value
        module = SimpleNamespace(sync_playwright=lambda: manager)
        with patch.dict(sys.modules, {'playwright':SimpleNamespace(sync_api=module), 'playwright.sync_api':module}):
            with patch.object(browser_reader,'read_source',return_value=([],None)):
                with patch.object(browser_reader.time,'sleep'):
                    result = browser_reader.run(only='Meta',out=self.cache,queries=['software engineer'])
        self.assertEqual(result['fetched_at'],NOW.isoformat())
        self.assertIn('fetched_at',result['sources']['Meta'])
        self.assertNotEqual(result['sources']['Meta']['fetched_at'],NOW.isoformat())
