# What source coverage means

A successful listing read proves that a configured reader returned successfully.
It does not prove every vacancy, description, salary, visa requirement or new-grad
opportunity was found. `/sources` separates failed/stale/unfinished reads and shows
known reader bounds even when there are no pending listings.

## Correct interpretation of the current readers

- Google reads the first results page for each configured query/location. It does
  not request later pages. Current requests do not establish newest-first ordering;
  do not promise that every newest role appears. Detail reads for discovered links
  may use the shared scan budget. Existing access restrictions remain unchanged.
- Ordinary Workday uses up to five 20-role pages for the New York facet query and
  two 20-role pages for each extra Bay Area/remote facet group. Some sources use a
  lower company `page_cap`. Salesforce uses its smaller configured listing budget
  and at most three details; guarded tenants use at most four listing requests and
  two details. Therefore “100 roles per company/search” is not a universal cap.
  Hitting a page/request cap is partial coverage, not a complete successful board.
  These caps remain; this change does not fetch deeper pages or rotate queries.
- Apple search results and all three browser-file sources (Meta, Microsoft and
  Netflix) contain listings without posting descriptions. Their pay/visa/experience
  fields remain unread. They do not gain descriptions on a later scan merely by
  waiting for the generic detail budget.
- The shared scan detail budget is 50 across eligible Greenhouse, ordinary Workday,
  Rippling and Google detail work. Other readers have independent limits. Therefore
  “at most 50 posting pages per entire scan” is not accurate. Unattempted queued
  details are prepared ahead of retrying failed ones, within existing budgets.
- Meta/Microsoft/Netflix discovery depends on the Mac's scheduled reader. The
  hourly cache uploader only recovers transfers of existing changed files; it
  does not run a new job search. Freshness checks report missed refreshes.
- Daily reads cover the configured employer list; hourly checks cover the selected
  fast lane outside quiet hours. Polling frequency does not guarantee immediate
  publication-to-notification delivery or complete entry-level coverage.

## Queue initialization and delivery

An hourly notification baseline must suppress old notifications without suppressing
eligibility/detail preparation. Its discoveries stay available for manual/daily
browsing after preparation. If rows are still unprepared, `/jobs` says they await
checks; it does not incorrectly report no jobs. A full refresh is still needed to
populate sources not included in the hourly lane after an initially empty upgrade.

Known restrictions remain: email-only employers require actual validated alerts;
unknown pay and eligibility need review; closed/changed postings are not detected
by these consistency fixes. No title, salary, visa, location, employer or rating
rules were changed, and there are no additional employer requests beyond the
existing page/detail caps. Backlog preparation may use budgets that were previously
left idle by the baseline bug.
