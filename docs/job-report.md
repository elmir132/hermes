# Job reports and match scores

Daily, hourly, `/jobs`, and the opt-in alert worker share a compact plain-text
listing card: company + stable ID, title, location/date, match/evidence scores,
base/annual total, visa wording, important warnings and a complete URL. Plain text
keeps posting content inert; Telegram link previews remain disabled by callers.
No company accounts, paid data feeds, models or extra requests are needed.

Employer rating columns are no longer repeated on each listing. The report score
summarizes preferences and disclosed evidence. `/jobscore <ID>` explains the
components without calling the resume-fit model. Reply-to-message list numbers
also work through the existing stable resolver. `/fit` is unchanged.

## Match out of 100 (heuristic, not hiring odds)

| Component | Maximum | Rule |
|---|---:|---|
| Entry level | 30 | Junior title + read description: 30; junior title only: 24; generic title: 15; explicit 1 year: 20; 2 years: 12; 3+ years: 0. |
| Location | 25 | New York 25; Bay Area 20; accepted remote region 16. Unverified/unspecified location capped at 10. |
| Pay evidence | 20 | Up to 8 for stated base reaching the configured floor (4 when the range crosses it), plus 12 for disclosed annual total/subtotal supporting the target, 6 when it only possibly reaches it. |
| Visa evidence | 10 | Explicit positive visa-sponsorship wording in read text: 10; unknown: 0. Company reputation never verifies a role's sponsorship. |
| Company estimate | 10 | Half the sum of the four existing 1–5 ratings, rounded. Unknown ratings: 0. Ratings remain estimates and are not modified. |
| Recency | 5 | Up to 7 days: 5; 8–14: 4; 15–30: 2; older: 1; no posting date: 0. |

Known no-sponsorship wording caps the overall score at 39, and explicit PhD
requirements cap it at 49. A role outside checked existing title/location/age,
experience, pay or explicit citizenship/clearance eligibility rules scores 0.
Caps are explained by `/jobscore`. Scoring never removes jobs or adds eligibility
rules; original matching, priority sorting and company caps are preserved.

The separate **Evidence N/5** count covers stated experience/early-career evidence,
location, annual compensation, visa wording (positive or negative), and posting
date. It is a count of available fields, not confidence that all requirements were
parsed, a probability, or proof of suitability. A fully documented unsuitable
role may have high evidence but a low match score.

## Pay and warnings

- Known base and posted total ranges are shown. Sums of stated annual components
  say **Annual subtotal** rather than pretending a complete package is known.
- Unknown total stays unknown. Unpriced bonus/equity mentions receive no pay
  points. Target bonus/equity values remain variable, with an explicit warning.
- Signing bonuses are separate, one-time amounts, excluded from recurring annual
  totals. Unknown information is not filled with market estimates.
- Experience, graduation/start windows, PhD, export/clearance and sponsorship
  warnings remain visible. Descriptions not read are explicitly unverified.
- A high score does not promise an offer, salary, sponsorship, visa eligibility or
  a start date that suits the applicant. List order still follows the existing
  priority rules; the new display score does not reorder the queue.

## Delivery safety

Queue selection budgets whole cards against the existing 3,400 UTF-16-unit limit.
Listings outside a message remain queued; IDs, number mappings, deduplication and
acknowledgement-after-success semantics are unchanged. Stored flags and posting
evidence are not rewritten by browsing. Legacy nonpersistent scans retain their
existing layout; persistent scans used by the alert worker use the shared cards.
