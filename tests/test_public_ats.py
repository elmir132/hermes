import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jobs
from public_jobs import PublicJobsClient

FIXTURES = Path(__file__).parent / 'fixtures'
HTML = (FIXTURES / 'ashby-public.html').read_text()
GH = json.loads((FIXTURES / 'greenhouse-public.json').read_text())
COMPANY = {'name':'Browserbase','slug':'browserbase','provider':'ashby_html','tier':3}


class AshbyPublicTests(unittest.TestCase):
    def test_saved_index_and_detail(self):
        data = jobs.ashby_app_data(HTML)
        found = jobs.ashby_html_jobs(data, COMPANY)
        self.assertEqual(len(found),1)
        job = jobs.ashby_html_detail(found[0], HTML)
        self.assertIn('Dashboard',job['title'])
        self.assertEqual(job['location'],'New York')
        self.assertEqual(job['employment'],'FullTime')
        self.assertEqual(job['published'],'2026-09-23')
        self.assertIn('Technical skills',job['text'])
        self.assertNotIn('<p',job['text'])
        self.assertTrue(job['key'].startswith('ab:Browserbase:'))
        self.assertEqual(job['pay'],(180000,300000))

    def test_empty_malformed_and_wrong_board(self):
        for text in ('','<html/>','window.__appData = undefined;','window.__appData = []'):
            self.assertIsNone(jobs.ashby_app_data(text))
        self.assertEqual(jobs.ashby_html_jobs(None,COMPANY),[])
        bad=jobs.ashby_app_data(HTML)
        bad['jobBoard']['jobPostings'][0]['id']='-'*36
        self.assertEqual(jobs.ashby_html_jobs(bad,COMPANY),[])
        self.assertEqual(jobs.ashby_html_jobs(jobs.ashby_app_data(HTML),dict(COMPANY,slug='other')),[])

    def test_unlisted_confidential_and_mismatched_details_rejected(self):
        job=jobs.ashby_html_jobs(jobs.ashby_app_data(HTML),COMPANY)[0]
        for update in ({'isListed':False},{'isConfidential':True},{'id':'wrong'},{'descriptionHtml':''}):
            data=jobs.ashby_app_data(HTML)
            data['posting'].update(update)
            page='window.__appData = '+json.dumps(data)+';'
            self.assertIsNone(jobs.ashby_html_detail(job,page))

    def test_error_and_empty_paths(self):
        class Client:
            async def text(self,url):return 403,''
        found,err=asyncio.run(jobs.fetch_ashby_html(COMPANY,jobs.load_config(),Client()))
        self.assertEqual(found,[])
        self.assertIn('403',err)
        class Empty:
            async def text(self,url):return 200,'window.__appData = {"organization":{"hostedJobsPageSlug":"browserbase"},"jobBoard":{"jobPostings":[]}};'
        self.assertEqual(asyncio.run(jobs.fetch_ashby_html(COMPANY,jobs.load_config(),Empty())),([],None))

    def test_cap_and_detail_errors(self):
        data=jobs.ashby_app_data(HTML)
        base=data['jobBoard']['jobPostings'][0]
        data['jobBoard']['jobPostings']=[dict(base,id=f'00000000-0000-0000-0000-{n:012d}') for n in range(20)]
        page='window.__appData = '+json.dumps(data)+';'
        calls=[]
        class Client:
            async def text(self,url):
                calls.append(url)
                if url.endswith('/browserbase'):return 200,page
                detail=jobs.ashby_app_data(HTML)
                detail['posting']['id']=url.rsplit('/',1)[-1]
                return 200,'window.__appData = '+json.dumps(detail)+';'
        found,err=asyncio.run(jobs.fetch_ashby_html(COMPANY,jobs.load_config(),Client()))
        self.assertIsNone(err)
        self.assertEqual(len(found),jobs.ASHBY_HTML_DETAILS)
        self.assertEqual(len(calls),1+jobs.ASHBY_HTML_DETAILS)
        class Bad:
            async def text(self,url):return (200,page) if url.endswith('/browserbase') else (429,'')
        _,err=asyncio.run(jobs.fetch_ashby_html(COMPANY,jobs.load_config(),Bad()))
        self.assertIn('429',err)

    def test_seen_postings_do_not_starve_other_candidates(self):
        data=jobs.ashby_app_data(HTML)
        base=data['jobBoard']['jobPostings'][0]
        raws=[dict(base,id=f'00000000-0000-0000-0000-{n:012d}') for n in range(3)]
        data['jobBoard']['jobPostings']=raws
        page='window.__appData = '+json.dumps(data)+';'
        calls=[]
        class Client:
            async def text(self,url):
                calls.append(url)
                if url.endswith('/browserbase'):return 200,page
                detail=jobs.ashby_app_data(HTML)
                detail['posting']['id']=url.rsplit('/',1)[-1]
                return 200,'window.__appData = '+json.dumps(detail)+';'
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'seen.db'
            jobs.mark_seen(db,['ab:Browserbase:'+raws[0]['id']])
            found,err=asyncio.run(jobs.fetch_ashby_html(COMPANY,jobs.load_config(),Client(),db))
        self.assertIsNone(err)
        self.assertEqual(len(found),2)
        self.assertFalse(any(url.endswith(raws[0]['id']) for url in calls))

    def test_stale_capped_posting_does_not_starve_newer_postings(self):
        cfg=jobs.load_config();cfg['companies']=[COMPANY]
        data=jobs.ashby_app_data(HTML)
        first=data['jobBoard']['jobPostings'][0]
        second=dict(first,id='00000000-0000-0000-0000-000000000001',title='Software Engineer Z')
        third=dict(first,id='00000000-0000-0000-0000-000000000002',title='Software Engineer ZZ')
        data['jobBoard']['jobPostings']=[first,second,third]
        index='window.__appData = '+json.dumps(data)+';'
        opened=[]
        class Client:
            def __init__(self,**kwargs):pass
            async def text(self,url):
                if url.endswith('/browserbase'):return 200,index
                opened.append(url)
                detail=jobs.ashby_app_data(HTML)
                ident=url.rsplit('/',1)[-1];detail['posting']['id']=ident
                page='window.__appData = '+json.dumps(detail)+';'
                date='2020-01-01' if ident==first['id'] else '2026-10-01'
                page+='<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','identifier':{'value':ident},'datePosted':date})+'</script>'
                return 200,page
        async def unused(*args):self.fail('unguarded callback called')
        now=datetime(2026,10,20,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory,patch('public_jobs.PublicJobsClient',Client):
            db=Path(directory)/'seen.db'
            asyncio.run(jobs.scan(cfg,unused,db,mark=True,now=now))
            self.assertIn('ab:Browserbase:'+first['id'],jobs.seen_keys(db,['ab:Browserbase:'+first['id']]))
            opened.clear()
            asyncio.run(jobs.scan(cfg,unused,db,mark=True,now=now))
        self.assertTrue(any(url.endswith(third['id']) for url in opened))
        self.assertFalse(any(url.endswith(first['id']) for url in opened))

    def test_scan_uses_guarded_reader(self):
        cfg=jobs.load_config();cfg['companies']=[COMPANY]
        class Client:
            def __init__(self,**kwargs):pass
            async def text(self,url):return 200,HTML
        async def unused(*args):self.fail('unguarded callback called')
        with tempfile.TemporaryDirectory() as directory,patch('public_jobs.PublicJobsClient',Client):
            _,count,errors=asyncio.run(jobs.scan(cfg,unused,Path(directory)/'seen.db',mark=False,now=datetime(2026,10,20,tzinfo=timezone.utc)))
        self.assertEqual(count,1)
        self.assertEqual(errors,[])


class PublicFeedTests(unittest.TestCase):
    def test_saved_greenhouse_content_and_offices(self):
        calls=[]
        class Client:
            async def json(self,url):calls.append(url);return 200,GH
        found,err=asyncio.run(jobs.fetch_public_feed({'provider':'greenhouse','name':'SingleStore','slug':'singlestore'},Client()))
        self.assertIsNone(err)
        self.assertEqual(len(found),1)
        self.assertIn('San Francisco',found[0]['location'])
        self.assertIn('University Graduates',found[0]['text'])
        self.assertNotIn('&lt;',found[0]['text'])
        self.assertNotIn('<p',found[0]['text'])
        self.assertTrue(calls[0].endswith('?content=true'))

    def test_empty_error_and_missing_descriptions(self):
        company={'provider':'greenhouse','name':'Example','slug':'example'}
        class Client:
            async def json(self,url):return status,payload
        for status,payload,expected_error in ((200,{'jobs':[]},False),(200,{},True),(403,{},True),
            (200,{'jobs':[{'id':1,'title':'Software Engineer','absolute_url':'https://example.org/job'}]},True)):
            found,err=asyncio.run(jobs.fetch_public_feed(company,Client()))
            self.assertEqual(found,[])
            self.assertEqual(bool(err),expected_error)

    def test_lever_error_and_empty(self):
        class Client:
            async def json(self,url):return status,payload
        for status,payload,expected_error in ((200,[],False),(200,{},True),(429,{},True)):
            found,err=asyncio.run(jobs.fetch_public_feed({'provider':'lever','name':'Example','slug':'example'},Client()))
            self.assertEqual(found,[])
            self.assertEqual(bool(err),expected_error)


class SharedPublicTransportTests(unittest.TestCase):
    def test_concurrent_same_host_requests_serialized_and_cached(self):
        calls=[];active=0;peak=0
        async def raw(url):
            nonlocal active,peak
            calls.append(url);active+=1;peak=max(peak,active)
            await asyncio.sleep(0.001)
            active-=1
            return 200,{},'' if url.endswith('robots.txt') else '{}'
        client=PublicJobsClient(raw,pause=0)
        async def run():
            await asyncio.gather(*(client.json('https://example.org/jobs') for _ in range(4)))
        asyncio.run(run())
        self.assertEqual(peak,1)
        self.assertEqual(calls,['https://example.org/robots.txt','https://example.org/jobs'])


class PublicSourceConfigTests(unittest.TestCase):
    def test_format_ratings_and_no_board_duplicates(self):
        cfg=jobs.load_config()
        text=jobs.CONFIG_PATH.read_text()
        self.assertEqual(text,json.dumps(cfg,indent=1,ensure_ascii=False)+"\n")
        sources=[c for c in cfg['companies'] if c.get('public_feed') or c['provider']=='ashby_html']
        for company in sources:
            self.assertEqual(company['tier'],jobs.tier_from_ratings(company['ratings']))
            for field in ('pay','prestige','wlb','exits'):
                self.assertIn(company['ratings'][field],range(1,6))
            self.assertIn(company['ratings']['sponsor'],(True,False,None))
            self.assertIn('Estimates',company['ratings_note'])
            self.assertEqual(sum(c.get('slug')==company['slug'] for c in cfg['companies']),1)
