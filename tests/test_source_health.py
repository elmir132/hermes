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


class EmptyDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_greenhouse_200_is_partial_unchecked_and_retried(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'jobs.db'
            cfg=cfg_with([{'name':'Acme','provider':'greenhouse','slug':'acme'}])
            calls=[]
            async def feed(url):
                if '/jobs/' not in url:return 200,{'jobs':[gh_job(1,'Software Engineer')]}
                calls.append(url)
                return 200,{'content':None} if len(calls)==1 else {'content':'Build software.'}
            text,count,errors=await jobs.scan(cfg,feed,db,now=NOW,mark=False,persist=True)
            self.assertEqual(count,1)
            self.assertIn('posting details not read',text)
            self.assertTrue(any('empty or invalid description' in e for e in errors))
            self.assertEqual(source_health.coverage(cfg,db,NOW)[0]['partial'],1)
            await jobs.scan(cfg,feed,db,now=NOW,mark=False,persist=True)
            self.assertEqual(len(calls),2)
            self.assertEqual(source_health.coverage(cfg,db,NOW)[0]['healthy'],1)

    async def test_workday_200_with_bad_or_empty_detail_is_partial_not_a_scan_crash(self):
        company={'name':'Acme','provider':'workday','tenant':'acme','site':'Careers','host':'acme.example.org'}
        cfg=cfg_with([company])
        for detail in ({'jobPostingInfo':[]},{'jobPostingInfo':{}},{}):
            async def feed(url,body=None):
                if body and body['limit']==1:return 200,{'facets':[{'facetParameter':'locations','values':[{'descriptor':'New York','id':'ny'}]}]}
                if body:return 200,{'total':1,'jobPostings':[{'title':'Software Engineer','externalPath':'/job/1','locationsText':'New York'}]}
                return 200,detail
            with tempfile.TemporaryDirectory() as d:
                text,count,errors=await jobs.scan(cfg,feed,Path(d)/'jobs.db',now=NOW,mark=False,persist=True)
            self.assertEqual(count,1)
            self.assertIn('posting details not read',text)
            self.assertTrue(any('empty or invalid description' in e for e in errors))

    async def test_empty_rippling_detail_stays_unchecked(self):
        uid='12345678-1234-1234-1234-123456789abc'
        company={'name':'Acme','provider':'rippling','slug':'acme'}
        base=jobs.RIPPLING.format(slug='acme')
        async def feed(url):
            if url==base:return 200,[{'uuid':uid,'name':'Software Engineer','url':f'https://ats.rippling.com/acme/jobs/{uid}', 'workLocation':{'label':'New York'}}]
            return 200,{}
        with tempfile.TemporaryDirectory() as d:
            text,count,errors=await jobs.scan(cfg_with([company]),feed,Path(d)/'jobs.db',now=NOW,mark=False,persist=True)
        self.assertEqual(count,1)
        self.assertIn('posting details not read',text)
        self.assertTrue(any('empty or invalid description' in e for e in errors))

    async def test_legacy_empty_cached_details_are_not_permanently_trusted(self):
        company={'name':'Acme','provider':'greenhouse','slug':'acme'}
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'jobs.db'
            cached=jobs.normalize('greenhouse',gh_job(1,'Software Engineer'),'Acme')
            job_store.upsert(db,[(cached,'greenhouse:acme',True,False)],NOW)
            calls=[]
            async def feed(url):
                if '/jobs/' not in url:return 200,{'jobs':[gh_job(1,'Software Engineer')]}
                calls.append(url)
                return 200,{'content':'Build software.'}
            await jobs.scan(cfg_with([company]),feed,db,now=NOW,mark=False,persist=True)
        self.assertEqual(len(calls),1)


class ReaderLimitDocumentationTests(unittest.TestCase):
    def test_bounds_visible_even_without_pending_roles(self):
        cfg=cfg_with([{'name':'Google','provider':'google'}, {'name':'Apple','provider':'apple'},
                      {'name':'Meta','provider':'browser'}, {'name':'Microsoft','provider':'browser'},
                      {'name':'Netflix','provider':'browser'},
                      {'name':'Acme','provider':'workday','tenant':'acme','site':'Careers','host':'example.org'}])
        with tempfile.TemporaryDirectory() as d:
            text='\n'.join(source_health.pages(cfg,Path(d)/'absent.db',NOW))
        for term in ('first results page','newest ordering','Apple:','Meta, Microsoft, Netflix','not one universal 100-role','shared scan budget is 50'):
            self.assertIn(term,text)
        self.assertIn('Healthy does not mean every vacancy',text)

    def test_no_sources_has_no_reader_bounds(self):
        self.assertEqual(source_health.limitations({'companies':[]}),[])


class FeedDescriptionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rich_feed_without_description_does_not_claim_requirements_checked(self):
        from test_jobs import ab_job,lv_job,FakeFeeds
        companies=[{'name':'AshbyCo','provider':'ashby','slug':'ashbyco'}, {'name':'LeverCo','provider':'lever','slug':'leverco'}]
        feed=FakeFeeds({'job-board/ashbyco':{'jobs':[ab_job(1,'Software Engineer')]}, 'leverco':[lv_job(1,'Software Engineer')]})
        with tempfile.TemporaryDirectory() as d:
            text,count,errors=await jobs.scan(cfg_with(companies),feed,Path(d)/'jobs.db',now=NOW,mark=False,persist=True)
            counts,_=source_health.coverage(cfg_with(companies),Path(d)/'jobs.db',NOW)
        self.assertEqual(count,2)
        self.assertEqual(errors,[])
        self.assertEqual(text.count('posting details not read'),2)
        self.assertEqual(counts['partial'],2)
