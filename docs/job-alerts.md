# Job-alert emails

Hermes can read subscribed job-alert emails through Gmail's read-only API and send matching listings to Telegram between daily board scans. It does not sign up for alerts, visit listing links, apply to jobs, run email instructions, or use an LLM to extract listings.

## Subscribe manually

- [JPMorganChase](https://www.jpmorganchase.com/careers/how-we-hire/faqs): Talent Community profile, then Info and Alerts → Talent Community preferences.
- [Millennium campus careers](https://campusjobs.mlp.com/careers): Join Talent Network. Community messages may contain news/events instead of complete job listings.
- [Qualcomm](https://careers.qualcomm.com/careers/join): Talent Network for future opportunities.
- [LinkedIn job alerts](https://www.linkedin.com/help/linkedin/answer/a508396): create daily email alerts with the exact employer selected under Company. Keep Citadel and Citadel Securities separate. There is a 20-alert limit; daily/weekly email delivery is not instant.
- [Google Alerts](https://www.google.com/alerts): optional discovery backup, e.g. `site:citadel.com/careers/ "software engineer"`. “As-it-happens” still depends on search indexing. Results without title, employer, location and a listing link cannot become complete job records.

No company is covered merely because an alert has been configured. Verify actual delivered listings first. These published routes were researched; signup forms and live mail templates have not been validated by unit fixtures.

## Validate before enabling

1. Subscribe using the mailbox Hermes is authorized to read.
2. Inspect representative actual emails from every sender/template family, including multi-employer LinkedIn messages. Establish the exact From address; do not guess an address or allow an entire domain.
3. Validate per-listing title, employer, location and link with a private sample. Do not commit real emails, personal information, tracking/unsubscribe tokens or credentials. The saved test fixture is synthetic.
4. Review parser failures and missing fields before relying on coverage. Current parsers support explicit structured JobPosting data and unambiguous HTML listing cards; unsupported/plain-text/discovery-only mail is retained and reported.
5. Configure the environment, then review the changes before deployment:

```
HERMES_JOB_ALERT_RECORDS=1
HERMES_JOB_ALERT_SENDERS=verified-sender@example.org
HERMES_JOB_ALERT_ACCOUNTS=personal
HERMES_JOB_ALERT_INTERVAL_SECONDS=300
```

The account value is the existing local token label. Omitting it uses all working accounts. Missing senders prevents mailbox searches. The five-minute interval is independent of the daily source scan; values are clamped to 60–3600 seconds. Gmail API reads never mark email read, change labels, or send mail.

## Check an actual exported message

In Gmail, open a real job alert, choose **Show original**, verify the sender and
Gmail authentication results, then **Download original**. Keep the `.eml` private.
Run:

```sh
python3 -m job_alert_validation path-to-private-message.eml
```

The command performs no network requests, email writes, database writes or worker
activation. It prints the exact sender and separate title, employer, location and
link for each listing. Exit code 0 means all detected listing links parsed; code 2
means incomplete/unsupported input. An exported header is not proof of authentic
mail. Compare every listing against the actual message before configuring the
existing exact-sender allowlist. Never enable a sender based on a newsletter or
sign-in notification. Parser success does not guarantee that future templates or
emails contain every vacancy.

## Delivery and recovery

- Each poll fetches at most eight emails and four Gmail list pages. Durable fixed windows, page tokens, pending message IDs and account rotation advance through backlogs. Exact From headers are rechecked after fetching messages. The importer does not require job-related subject words; the digest's subject filter remains unchanged.
- Partly parsed and unrecognized messages retain a bounded body and are not marked processed. Other mail can advance. Cached bodies are retried after a parser version upgrade, or explicitly through `/jobalerts retry`; retries are bounded and respect the current exact-sender allowlist.
- Undelivered records are not removed by age or hidden behind a newest-1000 slice. Thirty-day cleanup applies only to records already acknowledged in the shared jobs seen-store. Retained data can grow until resolved/delivered.
- Each poll sends at most four small message batches. The normal job rules and company ratings apply. A large batch falls back to one listing if needed; an oversized single listing is retained with an error. Links are not fetched and Telegram previews are disabled.
- Sent listings and filtered rejections use the same native/URL keys as `/jobs`. Worker, manual and scheduled job deliveries share a lock. Acknowledgements happen after Telegram accepts a message. A lost Telegram response can cause a duplicate on retry; it must not discard an undelivered alert.
- Native daily/manual messages also defer acknowledgements until sending succeeds. Truncated listings are not acknowledged by these callers. Default `jobs.scan(mark=True)` remains compatible for other callers.
- Notifications distinguish unread posting details: email text may omit experience, visa, citizenship, pay or employment requirements. Missing requirements are not proof of eligibility.

## Commands

- `/jobalerts`: enabled state, poll interval, sender count, last attempt/success, queued jobs, retained unparsed-email count and last error.
- `/jobalerts poll`: run a bounded email-only poll now. No careers feeds are fetched.
- `/jobalerts retry`: reparse up to two oldest cached unsupported messages with the current parser, then show status. Newly recovered listings are delivered on the next poll.

Changed failures generate one warning and recovery generates one notice. Repeated identical failures remain visible in status without sending a warning every five minutes. Expired Google connections still need renewal. The bot must be running; a daily/manual job scan can delay the mail worker while it holds the shared delivery lock.

The worker reduces latency after an email arrives. It cannot make providers deliver sooner or guarantee that their alerts contain every vacancy. Gmail push/Pub/Sub is not required by this implementation and is not configured.
