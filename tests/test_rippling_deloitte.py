import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

UID = "98b559a6-c8b2-4ede-85d9-cbf4883da996"
DELOITTE = '''
<article class="article--result " data-total="310"> <div class="article__content--result"> <div class="article__header__text">
<h3 class="article__header__text__title article__header__text__title--2">
<a href="https://apply.deloitte.com/en_US/careers/JobDetail/Full-Stack-Software-Engineer-Software-Engineer-II/369605" class="link">
 Full Stack Software Engineer - Software Engineer II </a></h3>
<div class="article__header__text__subtitle"> <span> Deloitte US </span> | <span> Deloitte Consulting LLP </span> | <span>New York, New York, United States</span>
</div></div></div></article>
<article class="article--result "><h3><a href="https://evil.example.com/careers/JobDetail/X/1" class="link">Software Engineer</a></h3></article>
'''


def row(uid=UID, name=" Software Engineer, Backend ", place="New York, NY", slug="rippling"):
    return {"uuid": uid, "name": name, "department": {"id": "Engineering"}, "url": f"https://ats.rippling.com/{slug}/jobs/{uid}",
            "workLocation": {"label": place, "id": place}}


def detail(label="SALARIED_FT", pay=None):
    return {"description": {"company": "<p>About us: 10+ years of growth</p>", "role": "<p>Build backend services in Go. 2+ years of experience.</p>"},
            "employmentType": {"label": label, "id": "Salaried, full-time"},
            "payRangeDetails": pay if pay is not None else [{"currency": "USD", "frequency": "YEAR", "rangeStart": 160000.0, "rangeEnd": 240000.0}]}


class DeloitteLayoutTests(unittest.TestCase):
    def test_title_link_and_location_from_the_last_subtitle_span(self):
        found = jobs.parse_avature_html(DELOITTE, "Deloitte", "apply.deloitte.com")
        self.assertEqual(len(found), 1)                       # the foreign-host link is dropped
        job = found[0]
        self.assertEqual(job["title"], "Full Stack Software Engineer - Software Engineer II")
        self.assertEqual(job["location"], "New York, New York, United States")
        self.assertEqual(job["key"], "avature:Deloitte:369605")

    def test_paging_appends_the_offset_with_an_ampersand_when_the_url_has_a_query(self):
        asked = []

        async def get_html(url):
            asked.append(url)
            return 200, DELOITTE if len(asked) == 1 else "<html></html>"

        found, err = asyncio.run(jobs.fetch_avature({"name": "Deloitte", "search": "https://apply.deloitte.com/en_US/careers/SearchJobs/x?a=1"}, get_html))
        self.assertEqual(len(found), 1)
        self.assertIsNone(err)
        self.assertEqual(asked[1], "https://apply.deloitte.com/en_US/careers/SearchJobs/x?a=1&jobOffset=1")


class DeloittePlacesTests(unittest.TestCase):
    PAGE = ("Forward Deployed Engineer- Palantir Engineering and Product Same job available in 3 locations "
            "Atlanta, Georgia, United States New York, New York, United States San Francisco, California, United States "
            "Back to search results Apply now")

    def test_the_city_list_is_read_from_the_posting_text(self):
        self.assertEqual(jobs.avature_places(self.PAGE),
                         "New York, New York, United States; San Francisco, California, United States; Atlanta, Georgia, United States")   # NYC first, then the Bay Area
        self.assertIsNone(jobs.avature_places("No list here"))

    def test_only_multi_location_roles_with_a_passing_title_are_opened(self):
        cfg = jobs.load_config()
        found = [dict(jobs.parse_avature_html(DELOITTE, "Deloitte", "apply.deloitte.com")[0], location="Multiple Locations", url="https://apply.deloitte.com/a/1"),
                 dict(jobs.parse_avature_html(DELOITTE, "Deloitte", "apply.deloitte.com")[0], key="k2", title="Senior Manager, Tax", location="Multiple Locations", url="https://apply.deloitte.com/a/2")]
        opened = []

        async def get_html(url):
            opened.append(url)
            return 200, "<html><body>" + self.PAGE + " " + "x " * 300 + "</body></html>"

        old = jobs.PAGE_DELAY
        jobs.PAGE_DELAY = 0
        try:
            asyncio.run(jobs.resolve_avature_places(found, {"tier": 4}, cfg, get_html))
        finally:
            jobs.PAGE_DELAY = old
        self.assertEqual(opened, ["https://apply.deloitte.com/a/1"])
        self.assertIn("New York, New York", found[0]["location"])
        self.assertTrue(jobs.location_ok(found[0]["location"], False, cfg))
        self.assertEqual(found[1]["location"], "Multiple Locations")


class RipplingTests(unittest.TestCase):
    def test_one_role_listed_in_two_places_becomes_one_job_with_both_places(self):
        other = "11111111-2222-3333-4444-555555555555"
        found = jobs.rippling_jobs([row(), row(place="Remote (United States)"), row(place="New York, NY"), row(uid=other, place="Seattle, WA")],
                                   "Rippling", "rippling")
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["location"], "New York, NY; Remote (United States)")
        self.assertEqual(found[0]["key"], f"rippling:Rippling:{UID}")
        self.assertEqual(found[0]["title"], "Software Engineer, Backend")
        self.assertTrue(found[0]["remote"])
        self.assertTrue(jobs.location_ok(found[0]["location"], found[0]["remote"], jobs.load_config()))
        self.assertFalse(jobs.location_ok(found[1]["location"], found[1]["remote"], jobs.load_config()))

    def test_links_off_the_board_and_broken_rows_are_dropped(self):
        rows = [row(slug="other"), dict(row(), uuid="../x"), dict(row(), name=""), "junk", {"name": "No id"}]
        self.assertEqual(jobs.rippling_jobs(rows, "Rippling", "rippling"), [])

    def test_detail_fills_role_text_and_yearly_pay_and_skips_the_company_blurb(self):
        job = jobs.rippling_jobs([row()], "Rippling", "rippling")[0]
        self.assertTrue(jobs.rippling_detail(job, detail()))
        self.assertIn("backend services", job["text"])
        self.assertNotIn("About us", job["text"])
        self.assertEqual(job["pay"], (160000, 240000))
        self.assertEqual(jobs.required_years(job["text"]), 2)

    def test_monthly_pay_is_not_read_as_yearly_and_interns_are_rejected(self):
        job = jobs.rippling_jobs([row()], "Rippling", "rippling")[0]
        self.assertFalse(jobs.rippling_detail(job, detail(label="TEMP", pay=[{"currency": "USD", "frequency": "MONTH", "rangeStart": 10500.0, "rangeEnd": 10500.0}])))
        self.assertIsNone(job["pay"])

    def test_fetch_errors_are_reported(self):
        async def bad(url, body=None, headers=None):
            return 503, {}
        self.assertEqual(asyncio.run(jobs.fetch_rippling({"name": "Rippling", "slug": "rippling"}, bad)), ([], "HTTP 503"))


if __name__ == "__main__":
    unittest.main()
