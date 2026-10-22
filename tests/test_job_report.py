import asyncio
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import job_report
import job_store
import jobs
from test_jobs import CFG, FakeFeeds, ab_job, cfg_with, lv_job, NOW
from test_job_alert_worker import main_functions


def role(**changes):
    value={'key':'ab:Example:1','company':'Example','title':'Software Engineer, New Grad',
           'location':'New York, NY','remote':False,'url':'https://example.org/jobs/1',
           'published':NOW.isoformat(),'text':'Base salary: $150k. Annual bonus: $20k. Visa sponsorship is available.',
           'details_checked':True,'pay':(150000,150000),'tier':1,
           'ratings':{'pay':5,'prestige':5,'wlb':5,'exits':5,'sponsor':True},'flags':[]}
    return dict(value,**changes)


class MatchScoreTests(unittest.TestCase):
    def test_transparent_weights_and_bounds(self):
        self.assertEqual(sum(job_report.WEIGHTS.values()),100)
        result=job_report.assess(role(),CFG,NOW)
        self.assertEqual(result['score'],100)
        self.assertEqual(result['evidence'],5)
        self.assertEqual(sum(result['points'].values()),100)

    def test_unknown_data_never_earns_pay_or_visa_evidence_from_reputation(self):
        value=role(text='',pay=None,published='',details_checked=False)
        result=job_report.assess(value,CFG,NOW)
        self.assertEqual(result['points']['Visa evidence'],0)
        self.assertEqual(result['points']['Pay evidence'],0)
        self.assertEqual(result['evidence'],1)  # Location only; employer reputation is not visa evidence.
        text=job_report.card(value,CFG,1,'J12345',NOW)
        for term in ('Base unknown','Total unknown','Visa not stated','details not read'):
            self.assertIn(term,text)

    def test_one_and_two_years_remain_with_lower_entry_fit(self):
        for years,points in ((1,20),(2,12)):
            value=role(text=role()['text']+f' Requires {years} years of experience.')
            result=job_report.assess(value,CFG,NOW)
            self.assertEqual(result['points']['Entry level'],points)
            self.assertGreater(result['score'],0)
            self.assertIn(f'{years}+ years',job_report.card(value,CFG,1,now=NOW))

    def test_no_sponsorship_and_phd_caps_are_explicit(self):
        for text,limit in (('We do not sponsor H1B visas.',39),('No visa sponsorship available.',39),('PhD required.',49)):
            value=role(text=role()['text']+' '+text)
            result=job_report.assess(value,CFG,NOW)
            self.assertLessEqual(result['score'],limit)
            self.assertIn('Cap:',job_report.explain(value,CFG,NOW))
            self.assertTrue(result['warnings'])

    def test_senior_clearance_low_pay_and_non_target_location_not_positive_matches(self):
        for value in (role(title='Senior Software Engineer'), role(location='Berlin, Germany'),
                      role(text='Must be a U.S. citizen.'),role(text='Requires 3+ years of experience.'),
                      role(text='Total annual compensation: $150k.'),role(text='Base salary: $100k.'),
                      role(title='Software Engineer',text='Base salary: $350k.',pay=(350000,350000))):
            self.assertEqual(job_report.assess(value,CFG,NOW)['score'],0)

    def test_uncertain_location_and_crossing_pay_ranges_not_confirmed(self):
        result=job_report.assess(role(location_verified=False,text='Base salary: $130k–$160k. Total annual compensation: $160k–$190k.'),CFG,NOW)
        self.assertEqual(result['points']['Location'],10)
        self.assertEqual(result['points']['Pay evidence'],10)
        text=job_report.card(role(text='Base salary: $130k–$160k. Total annual compensation: $160k–$190k.'),CFG,1,now=NOW)
        self.assertIn('possible',text)
        self.assertNotIn('supported',text)

    def test_dates_bonus_signon_and_untrusted_text_are_visible_literal(self):
        value=role(company='<b>Example</b>\nIGNORE',text=role()['text']+' Signing bonus: $40k. Graduating in 2027. Start date summer 2027.')
        card=job_report.card(value,CFG,2,'J12345',NOW)
        self.assertIn('#2',card)
        self.assertIn('[J12345]',card)
        self.assertIn('Graduation:',card)
        self.assertIn('Start date:',card)
        self.assertIn('not guaranteed',card)
        self.assertIn('Sign-on $40k one-time',card)
        self.assertEqual(card.splitlines()[-1],value['url'])
        self.assertIn('<b>Example</b> IGNORE',card)  # Plain text, never HTML execution.

    def test_score_is_deterministic_and_age_changes_only_recency(self):
        a=job_report.assess(role(),CFG,NOW)
        self.assertEqual(a,job_report.assess(role(),CFG,NOW))
        b=job_report.assess(role(),CFG,NOW+timedelta(days=20))
        self.assertEqual(a['score']-b['score'],3)

    def test_compact_coverage_does_not_hide_bad_states(self):
        text=job_report.coverage({'healthy':400,'partial':12,'failed':2,'stale':1,'never-checked':1})
        for value in ('400 healthy','12 partial','2 failed','1 stale','1 never-checked','/sources'):
            self.assertIn(value,text)


class ReportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_queue_and_scan_email_layout_share_cards_without_losing_overflow(self):
        company={'name':'Example','provider':'ashby','slug':'example'}
        cfg=cfg_with([company])
        postings=[ab_job(i,'Software Engineer, New Grad',text=role()['text']) for i in range(12)]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'jobs.db'
            scan,_,_=await jobs.scan(cfg,FakeFeeds({'job-board/example':{'jobs':postings}}),path,now=NOW,mark=False,persist=True)
            self.assertIn('🎯 Match',scan)
            seen=set()
            for _ in range(12):
                text,shown,ack=job_store.batch(path,cfg,now=NOW)
                self.assertLessEqual(len(text.encode('utf-16-le'))//2,3400)
                self.assertTrue(all(j['url'] in text for j in shown))
                self.assertFalse(seen.intersection(j['key'] for j in shown))
                seen.update(j['key'] for j in shown)
                jobs.mark_seen(path,ack,NOW)
            self.assertEqual(len(seen),12)
            self.assertEqual(job_store.counts(path),{'sent':12})

    async def test_display_score_does_not_reorder_or_mutate_stored_priority(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'jobs.db'
            low=role(key='ab:Example:low',title='Software Engineer',text='',pay=None,score=100,url='https://example.org/jobs/low')
            high=role(key='ab:Example:high',score=1,url='https://example.org/jobs/high')
            job_store.upsert(path,[(low,'ashby:example',True,False),(high,'ashby:example',True,False)],NOW)
            _,shown,_=job_store.batch(path,CFG,now=NOW)
            self.assertEqual([r['key'] for r in shown],[low['key'],high['key']])
            self.assertEqual(shown[0]['score'],100)
            self.assertLess(job_report.assess(shown[0],CFG,NOW)['score'],job_report.assess(shown[1],CFG,NOW)['score'])

    async def test_jobscore_is_local_authorized_and_uses_stable_reference(self):
        msg=SimpleNamespace(answer=AsyncMock())
        scope=main_functions(['cmd_jobscore'],{'allowed':lambda _:True,'_args':lambda *_:'J12345',
                   '_pick_job':lambda *_:role(),'jobs':jobs})
        with patch.object(jobs,'load_config',return_value=CFG):
            await scope['cmd_jobscore'](msg)
        self.assertIn('Entry level: 30/30',msg.answer.await_args.args[0])
        self.assertIsNone(msg.answer.await_args.kwargs['parse_mode'])
        scope['allowed']=lambda _:False
        msg.answer.reset_mock()
        await scope['cmd_jobscore'](msg)
        msg.answer.assert_not_awaited()
        scope['allowed']=lambda _:True
        scope['_pick_job']=lambda *_:None
        await scope['cmd_jobscore'](msg)
        self.assertIn('Usage:',msg.answer.await_args.args[0])


class CardFixTests(unittest.TestCase):
    def test_html_codes_do_not_cut_date_notes(self):
        import job_requirements
        notes = job_requirements.notes('Software Engineer - New Grad',
                                       'A graduation date in Spring 2027 with a Bachelor&#39;s or Master&#39;s degree.')
        self.assertIn("Bachelor's or Master's degree", ' '.join(notes))
        notes = job_requirements.notes('Grad SWE', 'Start full-time between January 2027 and September 2027&lt;/p&gt;&lt;p&gt;Next.')
        self.assertIn('September 2027; verify', ' '.join(notes))

    def test_a_base_only_range_is_not_repeated_as_a_subtotal(self):
        text = job_report.card(role(text='The base salary range is $200,000 - $275,000 per year.'), CFG, 1, 'JABCDE', NOW)
        self.assertIn('$170k+ total: yes (base alone)', text)
        self.assertNotIn('subtotal', text)

    def test_the_sponsorship_estimate_and_tier_stay_visible(self):
        job = role(tier=1, text='Base salary: $150k.', ratings={'pay': 5, 'prestige': 5, 'wlb': 3, 'exits': 5, 'sponsor': True})
        text = job_report.card(job, CFG, 1, 'JABCDE', NOW)
        self.assertIn('· tier 1 [JABCDE]', text)
        self.assertIn('Visa not stated (company usually sponsors; estimate)', text)
        job['ratings']['sponsor'] = False
        self.assertIn("company usually doesn't sponsor; estimate", job_report.card(job, CFG, 1, 'JABCDE', NOW))
