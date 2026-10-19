import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
CFG = jobs.load_config()


def run(c):
    return asyncio.run(c)


def amazon_raw(i, title, loc="New York, New York, USA", sched="full-time", intern=None, posted="October 13, 2026"):
    return {"id": f"x{i}", "id_icims": str(1000 + i), "title": title, "normalized_location": loc, "job_path": f"/en/jobs/{1000 + i}/slug",
            "posted_date": posted, "job_schedule_type": sched, "is_intern": intern, "description": "<p>Build things.</p>",
            "basic_qualifications": "<ul><li>2 years of experience</li></ul>", "preferred_qualifications": ""}


FACETS = {"total": 40, "jobPostings": [], "facets": [{"facetParameter": "locationMainGroup", "values": [
    {"facetParameter": "locations", "descriptor": "Locations", "values": [
        {"descriptor": "New York, NY", "id": "ny1", "count": 30}, {"descriptor": "New York, NY", "id": "ny2", "count": 10},
        {"descriptor": "Santa Clara, CA", "id": "sc", "count": 5}]}]}]}


class FakeNet:
    """Answers Amazon searches, Workday searches (GET or POST) and Workday detail pages."""
    def __init__(self, amazon=None, workday_postings=None, detail=None, fail=()):
        self.amazon, self.postings, self.detail, self.fail, self.calls = amazon or [], workday_postings or [], detail or {}, fail, []

    async def __call__(self, url, body=None):
        self.calls.append((url, body))
        if any(f in url for f in self.fail):
            return 503, {}
        if "amazon.jobs" in url:
            return 200, {"jobs": self.amazon}
        if url.endswith("/jobs") and body is not None:
            if body["limit"] == 1:
                return 200, FACETS
            return 200, {"total": len(self.postings), "jobPostings": self.postings[body["offset"]: body["offset"] + body["limit"]]}
        for path, info in self.detail.items():
            if url.endswith(path):
                return 200, {"jobPostingInfo": info}
        return 404, {}


class AmazonTests(unittest.TestCase):
    def test_roles_are_normalized(self):
        j = jobs.amazon_jobs({"jobs": [amazon_raw(1, "Software Development Engineer", intern=None)]}, "Amazon")[0]
        self.assertEqual((j["company"], j["location"], j["employment"]), ("Amazon", "New York, New York, USA", "fulltime"))
        self.assertEqual(j["url"], "https://www.amazon.jobs/en/jobs/1001/slug")
        self.assertTrue(j["published"].startswith("2026-10-13"))
        self.assertIn("2 years of experience", j["text"])

    def test_internships_are_marked(self):
        j = jobs.amazon_jobs({"jobs": [amazon_raw(1, "Software Development Engineer Intern", intern="true")]}, "Amazon")[0]
        self.assertEqual(j["employment"], "intern")
        self.assertIsNone(jobs.score_job(dict(j, tier=2), CFG, NOW))

    def test_scan_keeps_new_york_roles_and_skips_others_and_old_experience(self):
        net = FakeNet(amazon=[amazon_raw(1, "Software Development Engineer"), amazon_raw(2, "Software Development Engineer", "Seattle, Washington, USA"),
                              amazon_raw(3, "Software Development Engineer II", "New York, New York, USA")])
        cfg = dict(CFG, companies=[{"name": "Amazon", "provider": "amazon", "tier": 2, "queries": ["software development engineer"]}])
        text, n, errors = run(jobs.scan(cfg, net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(errors, [])
        self.assertIn("Amazon  [tier 2]: Software Development Engineer", text)
        self.assertNotIn("Seattle", text)
        self.assertIn("https://www.amazon.jobs/en/jobs/1001/slug", text)

    def test_the_request_asks_for_new_york_and_is_gentle(self):
        net = FakeNet(amazon=[])
        cfg = dict(CFG, companies=[{"name": "Amazon", "provider": "amazon", "tier": 2, "queries": ["a", "b"]}])
        run(jobs.scan(cfg, net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(len(net.calls), 4)                     # one request per query and place (New York, San Francisco)
        self.assertIn("New+York", net.calls[0][0])
        self.assertIn("San+Francisco", net.calls[2][0])


class WorkdayTests(unittest.TestCase):
    COMPANY = {"name": "Acme", "provider": "workday", "host": "acme.wd5.myworkdayjobs.com", "tenant": "acme", "site": "Careers", "tier": 2, "search": "engineer"}

    def test_new_york_filter_ids_are_found(self):
        self.assertEqual(jobs._facet_ids(FACETS["facets"]), {"locations": ["ny1", "ny2"]})

    def test_age_text(self):
        for text, n in [("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 5 Days Ago", 5), ("Posted 30+ Days Ago", 30), ("", None)]:
            self.assertEqual(jobs.workday_age_days(text), n, text)

    def test_roles_are_listed_with_their_description_and_time_type_checked(self):
        postings = [{"title": "Software Engineer, New Grad", "externalPath": "/job/US-NY-New-York/SE_JR1", "locationsText": "3 Locations", "postedOn": "Posted 2 Days Ago"},
                    {"title": "Software Engineer (Part Time)", "externalPath": "/job/US-NY-New-York/SE_JR2", "locationsText": "US, NY, New York", "postedOn": "Posted Today"}]
        net = FakeNet(workday_postings=postings, detail={"/job/US-NY-New-York/SE_JR1": {"jobDescription": "<p>You will build APIs. Visa sponsorship is available.</p>", "timeType": "Full time"},
                                                           "/job/US-NY-New-York/SE_JR2": {"jobDescription": "<p>Part time.</p>", "timeType": "Part time"}})
        cfg = dict(CFG, companies=[self.COMPANY])
        text, n, errors = run(jobs.scan(cfg, net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(errors, [])
        self.assertIn("Acme  [tier 2]: Software Engineer, New Grad", text)
        self.assertIn("New York (one of 3 Locations)", text)
        self.assertIn("✅ mentions sponsorship", text)
        self.assertIn("https://acme.wd5.myworkdayjobs.com/Careers/job/US-NY-New-York/SE_JR1", text)
        self.assertNotIn("Part Time", text)

    def test_the_search_asks_workday_for_new_york_only(self):
        net = FakeNet(workday_postings=[])
        run(jobs.scan(dict(CFG, companies=[self.COMPANY]), net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        searches = [b for _, b in net.calls if b and b["limit"] != 1]
        self.assertEqual(searches[0]["appliedFacets"], {"locations": ["ny1", "ny2"]})

    def test_a_site_that_fails_is_reported_and_others_still_run(self):
        net = FakeNet(fail=["acme.wd5"])
        text, n, errors = run(jobs.scan(dict(CFG, companies=[self.COMPANY]), net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(errors, ["Acme: HTTP 503"])

    def test_a_site_without_a_new_york_filter_is_reported(self):
        async def net(url, body=None):
            return 200, {"total": 0, "jobPostings": [], "facets": []}
        _, n, errors = run(jobs.scan(dict(CFG, companies=[self.COMPANY]), net, os.path.join(tempfile.mkdtemp(), "t.db"), now=NOW))
        self.assertEqual(errors, ["Acme: no New York, San Francisco or remote filter found"])

    def test_paging_stops_at_the_limit(self):
        postings = [{"title": f"Software Engineer {i}", "externalPath": f"/job/p{i}", "locationsText": "US, NY, New York", "postedOn": "Posted Today"} for i in range(400)]
        net = FakeNet(workday_postings=postings)
        found, err = run(jobs.fetch_workday(dict(self.COMPANY), net, NOW))
        self.assertEqual(len(found), jobs.WORKDAY_PAGE * jobs.WORKDAY_PAGES)


if __name__ == "__main__":
    unittest.main()


class WorkdayWiderPlacesTests(unittest.TestCase):
    COMPANY = {"name": "Acme", "provider": "workday", "host": "acme.wd5.myworkdayjobs.com", "tenant": "acme", "site": "Careers", "tier": 2, "search": "engineer"}
    FACETS = {"total": 3, "jobPostings": [], "facets": [
        {"facetParameter": "locations", "values": [{"descriptor": "US, NY, New York", "id": "ny"}, {"descriptor": "San Francisco, CA", "id": "sf"},
                                                    {"descriptor": "Austin, TX", "id": "aus"}]},
        {"facetParameter": "remoteType", "values": [{"descriptor": "Remote", "id": "rem"}]}]}

    def test_extra_places_are_found_per_filter_group(self):
        self.assertEqual(jobs._facet_ids(self.FACETS["facets"]), {"locations": ["ny"]})
        self.assertEqual(jobs._facet_ids(self.FACETS["facets"], match=jobs._is_extra_place), {"locations": ["sf"], "remoteType": ["rem"]})

    def test_each_group_is_queried_separately_and_unknown_places_are_labelled_unverified(self):
        asked = []

        async def net(url, body=None):
            if body["limit"] == 1:
                return 200, self.FACETS
            asked.append(body["appliedFacets"])
            key = list(body["appliedFacets"].values())[0][0]
            return 200, {"total": 1, "jobPostings": [{"title": f"Software Engineer {key}", "externalPath": f"/job/{key}",
                                                     "locationsText": "2 Locations" if key == "rem" else "San Francisco, CA", "postedOn": "Posted Today"}]}

        found, err = run(jobs.fetch_workday(dict(self.COMPANY), net, NOW))
        self.assertIsNone(err)
        self.assertEqual(asked, [{"locations": ["ny"]}, {"locations": ["sf"]}, {"remoteType": ["rem"]}])   # never ANDed together
        by = {j["title"]: j for j in found}
        self.assertEqual(by["Software Engineer rem"]["location"], "San Francisco Bay Area or remote (one of 2 Locations)")
        self.assertIs(by["Software Engineer rem"]["location_verified"], False)
        self.assertTrue(by["Software Engineer rem"]["remote"])
        self.assertNotIn("location_verified", by["Software Engineer sf"])
