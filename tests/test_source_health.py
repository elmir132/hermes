from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import job_store
import jobs
import source_health
from test_jobs import cfg_with, NOW, gh_job
from test_job_alert_worker import main_functions


class SourceCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'jobs.db'
        self.companies=[{'name':n,'provider':'greenhouse','slug':n.lower()} for n in ('Good','Old','Part','Bad','Never')]
        self.cfg=cfg_with(self.companies)

    def record(self, name, when=NOW, error=None, n=1):
        c=next(c for c in self.companies if c['name']==name)
        jobs._record_source_status(self.db,[(c,'greenhouse',c['slug'],name,[{}]*n,error)],when)

    async def test_all_five_states_and_current_config_only(self):
        self.record('Good')
        self.record('Old',NOW-timedelta(days=2))
        self.record('Part',error='page 2 HTTP 503')
        self.record('Bad',NOW-timedelta(days=1))
        self.record('Bad',error='HTTP 429',n=0)
        counts, rows=source_health.coverage(self.cfg,self.db,NOW)
        self.assertEqual(counts,dict.fromkeys(source_health.STATES,1))
        text='\n'.join(source_health.pages(self.cfg,self.db,NOW))
        self.assertIn('Bad — failed',text)
        self.assertIn('HTTP 429',text)
        self.assertIn('Old — stale',text)
        self.assertIn('Part — partial',text)
        self.assertNotIn('0 jobs',text)
        self.assertEqual(sum(source_health.coverage(cfg_with(self.companies[:1]),self.db,NOW)[0].values()),1)

    async def test_fresh_database_does_not_appear_healthy_or_create_a_file(self):
        counts,_=source_health.coverage(self.cfg,self.db,NOW)
        self.assertEqual(counts['never-checked'],5)
        self.assertFalse(self.db.exists())

    async def test_reader_partial_preserves_last_complete_success(self):
        self.record('Part',NOW-timedelta(hours=1))
        self.record('Part',error='page cap after response error')
        _,rows=source_health.coverage(self.cfg,self.db,NOW)
        row=next(r for r in rows if r['company']=='Part')
        self.assertEqual(row['state'],'partial')
        self.assertEqual(row['last_success_at'],(NOW-timedelta(hours=1)).isoformat())

    async def test_unfinished_details_make_healthy_listing_partial_but_old_pending_does_not(self):
        self.record('Good')
        job=jobs.normalize('greenhouse',gh_job(1,'Software Engineer'),'Good')
        job_store.upsert(self.db,[(job,'greenhouse:good',False,False)],NOW)
        self.assertEqual(source_health.coverage(self.cfg,self.db,NOW)[0]['partial'],1)
        with closing(sqlite3.connect(self.db)) as con,con:
            con.execute('UPDATE jobs_pending SET last_seen=?',((NOW-timedelta(days=3)).isoformat(),))
        self.assertEqual(source_health.coverage(self.cfg,self.db,NOW)[0]['partial'],0)

    async def test_old_schema_is_readable_and_migrates_on_next_record(self):
        with closing(sqlite3.connect(self.db)) as con,con:
            con.execute('CREATE TABLE jobs_source_status (source_key TEXT PRIMARY KEY, company TEXT, provider TEXT, last_attempt_at TEXT, last_success_at TEXT, last_error TEXT, last_result_count INTEGER)')
            con.execute('INSERT INTO jobs_source_status VALUES (?,?,?,?,?,?,?)',('greenhouse:good','Good','greenhouse',NOW.isoformat(),NOW.isoformat(),None,2))
        self.assertEqual(source_health.coverage(self.cfg,self.db,NOW)[0]['healthy'],1)
        self.record('Good',error='HTTP 403',n=0)
        self.assertEqual(source_health.coverage(self.cfg,self.db,NOW)[0]['failed'],1)

    async def test_description_failure_is_partial_in_real_scan_path(self):
        self.record('Good',NOW-timedelta(days=1))
        async def feed(url):
            return (503,{}) if '/jobs/' in url else (200,{'jobs':[gh_job(1,'Software Engineer')]})
        text,_,errors=await jobs.scan(cfg_with(self.companies[:1]),feed,self.db,now=NOW,mark=False,persist=True)
        self.assertTrue(errors)
        self.assertIn('partial',text)
        counts,rows=source_health.coverage(self.cfg,self.db,NOW)
        self.assertEqual(rows[0]['state'],'partial')
        self.assertEqual(rows[0]['last_success_at'],(NOW-timedelta(days=1)).isoformat())

    async def test_source_command_uses_real_read_only_formatter_and_authorization(self):
        env=main_functions({'cmd_sources'},dict(allowed=lambda _:True,jobs=jobs,DB_PATH=self.db))
        answer=AsyncMock()
        with patch.object(jobs,'load_config',return_value=self.cfg):
            await env['cmd_sources'](SimpleNamespace(answer=answer))
        self.assertIn('never-checked 5',answer.await_args.args[0])
        env['allowed']=lambda _:False
        answer.reset_mock()
        await env['cmd_sources'](SimpleNamespace(answer=answer))
        answer.assert_not_awaited()

    async def test_all_failed_names_fit_paginated_messages(self):
        companies=[{'name':f'Firm{i}','provider':'greenhouse','slug':f'firm{i}'} for i in range(100)]
        cfg=cfg_with(companies)
        jobs._record_source_status(self.db,[(c,'greenhouse',c['slug'],c['name'],[],'HTTP 503 '+'x'*200) for c in companies],NOW)
        pages=source_health.pages(cfg,self.db,NOW)
        self.assertGreater(len(pages),1)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=3400 for p in pages))
        for c in companies:self.assertIn(c['name']+' — failed','\n'.join(pages))
