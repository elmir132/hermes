import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

BLOOMBERG = '''
<article class="article article--result" id="article--1"><div class="article__header"><div class="article__header__text">
<h3 class="article__header__text__title"><a class="link" href="https://bloomberg.avature.net/careers/JobDetail/Software-Engineer-AI-Platform/45001">
 Software Engineer, AI Platform &amp; Tools </a></h3>
<div class="article__header__text__subtitle"><span class="list-item-location">New York, New York, United States of America</span></div></div></div></article>
<article class="article article--result" id="article--2"><div class="article__header"><div class="article__header__text">
<h3><a class="link" href="https://evil.example.com/careers/JobDetail/Software-Engineer/1">Software Engineer</a></h3>
<div><span class="list-item-location">New York</span></div></div></div></article>
'''
TWO_SIGMA = '''
<article class="article article--result" id="article--1"><div class="article__header"><div class="article__header__text">
<h3><a class="link" href="https://careers.twosigma.com/careers/JobDetail/New-York-United-States-Infrastructure-Engineer-Linux/13981">
 Infrastructure Engineer - Linux </a></h3><div class="article__header__content"><div class="article__header__content__text">
<span class="paragraph_inner-span">United States - NY New York</span><div class="article__header__content__sub-text">
<span class="paragraph_inner-span">Engineering</span><span class="paragraph_inner-span">Experienced</span></div></div></div></div></div></article>
'''


def deshaw_page(records):
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"regularJobs": records}}}) + "</script></html>"


def record(rid, title, slug, office="New York", description="", active=True):
    return {"id": rid, "displayName": title, "office": [{"abbreviation": "X", "name": office}],
            "data": {"id": rid, "displayName": title, "jobUrl": slug, "activeOnJobsListing": active,
                     "jobDescription": {"websiteDescription": description}}}


class AvatureParserTests(unittest.TestCase):
    def test_bloomberg_title_location_and_id_are_read_and_foreign_hosts_are_ignored(self):
        found = jobs.parse_avature_html(BLOOMBERG, "Bloomberg", "bloomberg.avature.net")
        self.assertEqual(len(found), 1)
        job = found[0]
        self.assertEqual(job["title"], "Software Engineer, AI Platform & Tools")
        self.assertEqual(job["location"], "New York, New York, United States of America")
        self.assertEqual(job["key"], "avature:Bloomberg:45001")
        self.assertTrue(job["location_verified"])
        self.assertIs(job["details_checked"], False)

    def test_two_sigma_location_is_the_first_span_and_the_level_goes_to_employment(self):
        job = jobs.parse_avature_html(TWO_SIGMA, "Two Sigma", "careers.twosigma.com")[0]
        self.assertEqual(job["location"], "United States - NY New York")
        self.assertEqual(job["employment"], "Engineering Experienced")

    def test_an_internship_level_is_filtered_out_by_the_existing_employment_rule(self):
        page = TWO_SIGMA.replace("Experienced", "Internship").replace("Infrastructure Engineer - Linux", "Software Engineer - Intern")
        job = jobs.parse_avature_html(page, "Two Sigma", "careers.twosigma.com")[0]
        self.assertIsNone(jobs.score_job(dict(job, tier=1), jobs.load_config()))

    def test_a_page_without_articles_gives_no_jobs(self):
        self.assertEqual(jobs.parse_avature_html("<html></html>", "X", "x.example"), [])
        self.assertEqual(jobs.parse_avature_html(None, "X", "x.example"), [])


class AvatureFetchTests(unittest.TestCase):
    def setUp(self):
        self.old = jobs.PAGE_DELAY
        jobs.PAGE_DELAY = 0

    def tearDown(self):
        jobs.PAGE_DELAY = self.old

    def page(self, ids):
        return "".join(
            f'<article class="article article--result"><h3><a class="link" href="https://careers.twosigma.com/careers/JobDetail/Role-{i}/{i}">Software Engineer {i}</a></h3>'
            f'<span class="paragraph_inner-span">United States - NY New York</span></article>' for i in ids)

    def test_pages_are_followed_with_the_offset_until_a_page_adds_nothing(self):
        urls = []

        async def get_html(url):
            urls.append(url)
            return 200, {0: self.page([1, 2]), 2: self.page([3, 4]), 4: self.page([3, 4])}.get(int(url.split("jobOffset=")[1]) if "jobOffset" in url else 0, "")

        found, err = asyncio.run(jobs.fetch_avature({"name": "Two Sigma", "search": "https://careers.twosigma.com/careers/OpenRoles"}, get_html))
        self.assertIsNone(err)
        self.assertEqual([j["key"].rsplit(":", 1)[1] for j in found], ["1", "2", "3", "4"])
        self.assertEqual(urls, ["https://careers.twosigma.com/careers/OpenRoles", "https://careers.twosigma.com/careers/OpenRoles?jobOffset=2",
                                "https://careers.twosigma.com/careers/OpenRoles?jobOffset=4"])

    def test_an_http_error_is_reported_and_keeps_what_was_read(self):
        async def get_html(url):
            return (200, self.page([1, 2])) if "jobOffset" not in url else (503, "")

        found, err = asyncio.run(jobs.fetch_avature({"name": "Two Sigma", "search": "https://careers.twosigma.com/careers/OpenRoles"}, get_html))
        self.assertEqual(len(found), 2)
        self.assertEqual(err, "HTTP 503 on page 2")

    def test_the_page_limit_is_reported_not_silent(self):
        old = jobs.AVATURE_PAGE_LIMIT
        jobs.AVATURE_PAGE_LIMIT = 2
        try:
            counter = iter(range(1, 100))

            async def get_html(url):
                n = next(counter)
                return 200, self.page([n * 10 + 1, n * 10 + 2])

            found, err = asyncio.run(jobs.fetch_avature({"name": "Two Sigma", "search": "https://careers.twosigma.com/careers/OpenRoles"}, get_html))
        finally:
            jobs.AVATURE_PAGE_LIMIT = old
        self.assertEqual(len(found), 4)
        self.assertIn("stopped after 2 pages", err)


class DeShawTests(unittest.TestCase):
    def test_new_york_role_with_pay_is_read_from_the_embedded_json(self):
        page = deshaw_page([record(5375, "Applied AI Engineer", "Applied-AI-Engineer-5375", description="Base salary range: $225,000 to $275,000 per year.")])
        job = jobs.parse_deshaw_html(page)[0]
        self.assertEqual(job["title"], "Applied AI Engineer")
        self.assertEqual(job["location"], "New York")
        self.assertEqual(job["url"], "https://www.deshaw.com/careers/Applied-AI-Engineer-5375")
        self.assertEqual(job["key"], "deshaw:5375")
        self.assertIs(job["details_checked"], True)
        self.assertIsNotNone(job["pay"])

    def test_unsafe_slugs_and_inactive_roles_are_skipped(self):
        page = deshaw_page([record(1, "A", "../../etc/passwd"), record(2, "B", "B-2", active=False), record(3, "C", "C-3")])
        self.assertEqual([j["key"] for j in jobs.parse_deshaw_html(page)], ["deshaw:3"])

    def test_a_missing_or_broken_json_block_gives_no_jobs(self):
        self.assertEqual(jobs.parse_deshaw_html("<html></html>"), [])
        self.assertEqual(jobs.parse_deshaw_html('<script id="__NEXT_DATA__">{broken</script>'), [])
        self.assertEqual(jobs.parse_deshaw_html('<script id="__NEXT_DATA__">{"props": {}}</script>'), [])


class ScanWiringTests(unittest.TestCase):
    def test_config_lists_the_three_firms_with_the_new_providers(self):
        providers = {c["name"]: c["provider"] for c in jobs.load_config()["companies"]}
        self.assertEqual(providers["Bloomberg"], "avature")
        self.assertEqual(providers["Two Sigma"], "avature")
        self.assertEqual(providers["D. E. Shaw"], "deshaw")

    def test_scan_reads_both_providers_and_reports_a_missing_html_reader(self):
        cfg = dict(jobs.load_config())
        cfg["companies"] = [{"name": "D. E. Shaw", "provider": "deshaw", "tier": 1}]
        page = deshaw_page([record(7, "Full-Stack Software Engineer", "Full-Stack-Software-Engineer-7")])

        async def get_html(url):
            return 200, page

        async def get_json(url):
            return 0, None

        db = os.path.join(tempfile.mkdtemp(), "j.db")
        text, count, errors = asyncio.run(jobs.scan(cfg, get_json, db, mark=False, get_html=get_html))
        self.assertEqual(count, 1)
        self.assertIn("Full-Stack Software Engineer", text)
        _, _, errors = asyncio.run(jobs.scan(cfg, get_json, db, mark=False, get_html=None))
        self.assertIn("HTML reader is not configured", " ".join(errors))


if __name__ == "__main__":
    unittest.main()
