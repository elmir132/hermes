# Stored jobs

Scans save title/location matches to SQLite before output caps. Full descriptions
already returned by a feed are filtered across the entire discovered set. Readers
which need separate descriptions keep the existing request cap; unfinished jobs
remain stored and later scans advance through them. Never-attempted details take
priority over retrying failed details, so failures cannot consume every later
scan's budget. Delivery order still uses the existing role score. Unread details are labelled.
Cached descriptions from shallow feeds are reused; they are not a live guarantee
that requirements or availability remain unchanged.

`/jobs` (or `/jobs next`) sends the next pending batch from local storage, without
fetching career sites. `/jobs all` includes previously sent prepared matches and
does not acknowledge them again. `/jobs refresh` explicitly fills the queue from
the sources before browsing it. Daily and hourly loops fill the same store and
send from it. An empty initial store needs a scheduled or explicit refresh. Hourly notification-baseline exclusions never block preparing discoveries for manual browsing; the baseline still suppresses old hourly notifications. Discovered but unprepared rows are reported as awaiting checks, not as no matching jobs.

Selection is not delivery. Only a successful Telegram send updates the shared
seen-store and marks queued roles sent, in one SQLite transaction. Failed sends
and restarts retain pending jobs; ambiguous network responses can cause duplicate
notifications. Complete listing blocks are selected to fit the message budget.
Oversized individual links remain pending rather than being truncated/acknowledged.

The queue stores new/sent/dismissed/rejected/closed states, first/last discovery,
compact posting evidence, and native/URL aliases. Rejections need no notification.
Existing seen keys migrate lazily when their roles are next discovered. Terminal
states and sent history survive the old seen-store retention window. No inferred
closure, automatic cleanup of unseen jobs, or changed-posting monitoring is done.
Rows can grow over time; no raw careers pages or mailbox bodies are added here.

Existing job filters, ratings, source lists, and network page/detail budgets stay
unchanged. A cached role is rechecked against title/location/age rules when viewed.
The displayed count is the prepared selection, not proof that the source returned
every vacancy. Source and detail failures must still be reviewed separately.

## Stable references

Each listing shows a short `J`-prefixed ID, for example `[J7AC2B]`. IDs derive from
the stored identity and are saved permanently. Hash-prefix collisions extend the
new ID; an existing ID is never reassigned. `/fit <ID>`, `/save <ID>` and
`/applied <ID> [resume]` work after restart and after newer notifications.

List numbers work when replying to the original jobs message, for example reply
with `/fit 3`. That message's mapping is persisted. A bare `/fit 3` is refused
instead of guessing which list is intended. Messages sent before this feature
have no durable mapping; open `/jobs` and use its IDs. Application IDs shown by
`/pipeline` and used by `/mark` remain separate from job IDs.

## Coverage

`/sources` reads the current configuration and recorded source state without
fetching any site. It reports healthy, stale (no complete success in 2+ days),
partial, failed and never-checked counts, with failed/stale/partial source names,
last good times and errors. These categories are exclusive: an explicit current
failure/partial result takes precedence over stale; missing attempts are never
checked. Removed configuration entries are not counted.

Description failures are partial source reads, preserving the last complete
success time. Recently discovered pending matches with unfinished/unread details
also make a source partial. A cap/empty snapshot is not proof every role was read.
`/jobs` includes these coverage counts alongside stored roles. `/sources` may send
several bounded messages to include every failed and stale source.
