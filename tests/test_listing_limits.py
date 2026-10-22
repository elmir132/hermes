import asyncio
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import jobs
import public_readers
from test_jobs import cfg_with, NOW

COMPANY = {'name':'Acme','provider':'workday','host':'acme.example.org','tenant':'acme','site':'Careers','page_cap':1}
FACETS = [{'facetParameter':'locations','values':[{'descriptor':'New York','id':'ny'},{'descriptor':'San Francisco','id':'sf'}]}]
POST = {'title':'Software Engineer, New Grad','externalPath':'/job/1','locationsText':'New York','postedOn':'Posted Today'}


class ListingLimitTests(unittest.IsolatedAsyncioTestCase):
    def client(self, total=80, failure=None):
        calls=[]
        async def get(url, body=None):
            calls.append((url,body))
            if body is None:
                return 200, {'jobPostingInfo':{'jobDescription':'Build software.','timeType':'Full time'}}
            if body['limit']==1:
                return 200, {'facets':FACETS}
            if failure and body['appliedFacets']['locations']==['sf']:
                return failure, {}
            group=body['appliedFacets']['locations'][0]
            return 200, {'total':total,'jobPostings':[dict(POST,externalPath=f'/job/{group}{i}') for i in range(20)]}
        return get,calls

    async def test_cap_is_partial_but_keeps_other_location_queries(self):
        get,calls=self.client()
        found,error=await jobs.fetch_workday(COMPANY,get,NOW)
        self.assertEqual(len(found),40)
        self.assertIsInstance(error,jobs.ListingCap)
        self.assertIn('read 20 of 80',error)
        self.assertEqual(len(calls),3)  # Existing budget, including metadata.

    async def test_exact_total_is_complete_and_429_still_stops(self):
        get,calls=self.client(total=20)
        self.assertIsNone((await jobs.fetch_workday(COMPANY,get,NOW))[1])
        get,calls=self.client(failure=429)
        found,error=await jobs.fetch_workday(COMPANY,get,NOW)
        self.assertEqual(error,'listing HTTP 429')
        self.assertEqual(len(found),20)
        self.assertEqual(len(calls),3)

    async def test_missing_total_and_malformed_listing_not_claimed_complete(self):
        async def get(url,body):
            if body['limit']==1:return 200,{'facets':FACETS[:1]}
            return 200, {'jobPostings':[dict(POST,externalPath=f'/job/{i}') for i in range(20)]}
        self.assertIsInstance((await jobs.fetch_workday(COMPANY,get,NOW))[1],jobs.ListingCap)
        async def short(url,body):
            if body['limit']==1:return 200,{'facets':FACETS}
            return 200,{'total':80,'jobPostings':[POST]}
        self.assertIn('short listing response', (await jobs.fetch_workday(COMPANY,short,NOW))[1])
        async def bad(url,body):
            if body['limit']==1:return 200,{'facets':FACETS}
            return 200,{'jobPostings':'malformed'}
        self.assertIn('invalid listing response',(await jobs.fetch_workday(COMPANY,bad,NOW))[1])

    async def test_partial_warning_does_not_disable_guarded_or_paced_details(self):
        from types import SimpleNamespace
        for reader,cap in ((public_readers.fetch_guarded_workday,2),(public_readers.fetch_paced_workday,3)):
            get,calls=self.client()
            found,error=await reader(COMPANY,cfg_with([COMPANY]),SimpleNamespace(json=get),NOW)
            self.assertIsInstance(error,jobs.ListingCap)
            self.assertEqual(sum(job.get('details_checked',False) for job in found),cap)
            self.assertEqual(sum(body is None for _,body in calls),cap)

    async def test_scan_records_partial_and_keeps_last_complete_success(self):
        get,calls=self.client()
        cfg=cfg_with([COMPANY])
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'jobs.db'
            last_good=NOW-timedelta(hours=1)
            jobs._record_source_status(path,[(COMPANY,'workday','acme','Acme',[{}],None)],last_good)
            _,_,errors=await jobs.scan(cfg,get,path,now=NOW,mark=False,persist=True)
            self.assertTrue(any('listing cap reached' in err for err in errors))
            with closing(sqlite3.connect(path)) as con:
                outcome,good=con.execute('select last_outcome,last_success_at from jobs_source_status').fetchone()
            self.assertEqual(outcome,'partial')
            self.assertEqual(good,last_good.isoformat())

    async def test_guarded_listing_call_budget_cannot_be_a_silent_empty_success(self):
        from types import SimpleNamespace
        calls = []
        facets = FACETS + [{'facetParameter':f'remote{i}', 'values':[{'descriptor':'US Remote','id':str(i)}]} for i in range(4)]
        async def get(url,body=None):
            calls.append((url,body))
            if body is None:return 200,{'jobPostingInfo':{'jobDescription':'Build software.'}}
            if body['limit']==1:return 200,{'facets':facets}
            return 200,{'total':1,'jobPostings':[dict(POST,externalPath='/job/'+str(len(calls)))]}
        found,error=await public_readers.fetch_guarded_workday(COMPANY,cfg_with([COMPANY]),SimpleNamespace(json=get),NOW)
        self.assertIsInstance(error,jobs.ListingCap)
        self.assertIn('4 calls',error)
        self.assertEqual(sum(body is not None for _,body in calls),4)
        self.assertTrue(found)
