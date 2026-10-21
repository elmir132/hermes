"""Frequent, bounded job-alert delivery. Reads Gmail; never visits job links."""
from contextlib import closing
from datetime import datetime, timezone
import os
import time

import digest
import job_alerts
import jobs


def enabled():
    return os.environ.get('HERMES_JOB_ALERT_RECORDS') == '1'


def interval():
    try:
        return max(60, min(3600, int(os.environ.get('HERMES_JOB_ALERT_INTERVAL_SECONDS', '300'))))
    except ValueError:
        return 300


def account_names():
    return {x.strip() for x in os.environ.get('HERMES_JOB_ALERT_ACCOUNTS', '').split(',') if x.strip()}


def status(path):
    with closing(job_alerts._db(path)) as con:
        row = con.execute('SELECT last_attempt,last_success,last_error,last_notice FROM job_alert_worker_status WHERE id=1').fetchone()
        result = dict(zip(('last_attempt', 'last_success', 'last_error', 'last_notice'), row or (None, None, None, '')))
        result['unparsed'] = con.execute('SELECT COUNT(*) FROM job_alert_unparsed').fetchone()[0]
        result['queued'] = con.execute('SELECT COUNT(*) FROM job_alert_queue q WHERE NOT EXISTS (SELECT 1 FROM jobs_seen s WHERE s.job_key=q.key)').fetchone()[0]
        return result


def save_status(path, now, error=None, notice=None, completed=True):
    with closing(job_alerts._db(path)) as con, con:
        con.execute('INSERT OR IGNORE INTO job_alert_worker_status(id,last_notice) VALUES (1,?)', ('',))
        if not completed and error is None:
            con.execute('UPDATE job_alert_worker_status SET last_attempt=? WHERE id=1', (now,))
        else:
            con.execute('UPDATE job_alert_worker_status SET last_attempt=?,last_error=?, '
                        'last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE id=1',
                        (now, error, int(completed and not error), now))
        if notice is not None:
            con.execute('UPDATE job_alert_worker_status SET last_notice=? WHERE id=1', (notice,))


def retry(path, senders=None, now=None):
    with closing(job_alerts._db(path)) as con, con:
        return job_alerts.retry_unparsed(con, int(now or time.time()), digest.parse_job_alert_senders(senders), force=True)


async def run_once(cfg, accounts, get_json, path, send, senders=None, now=None, on_delivered=None):
    """Caller serializes this with manual/daily job deliveries.

    Up to eight emails and four small Telegram batches per poll. Only successful
    sends acknowledge records. Ambiguous Telegram timeouts may cause a duplicate
    on retry; they must not cause a lost alert. Publisher uses plain text with
    link previews disabled. No LLM or listing requests are involved.
    """
    now = int(now or time.time())
    allowed = digest.parse_job_alert_senders(senders)
    previous = status(path)
    save_status(path, now, completed=False)
    errors, last_shown, delivered = [], [], 0
    try:
        if not allowed:
            records = []
            errors.append('No exact job-alert senders configured; mailbox not searched')
        else:
            records, errors = await job_alerts.fetch_records(accounts, get_json, path, allowed, now)
            if not accounts:
                errors.append('No working configured Google account; mailbox not searched')
        async def no_jobs_network(*args, **kwargs):
            raise RuntimeError('email worker must not request careers sites')
        stamp = datetime.fromtimestamp(now, timezone.utc)
        for _ in range(4):
            if not records:
                break
            # Limit message size without losing a truncated listing. Reuse the
            # existing job filters, ratings, native keys and URL deduplication.
            for batch_size in (3, 1):
                shown, acknowledgement = [], []
                text, count, scan_errors = await jobs.scan(
                    cfg, no_jobs_network, path, mark=False, limit=batch_size,
                    now=stamp, collect=shown, alert_jobs=records, alert_only=True,
                    ack_keys=acknowledgement, persist=True)
                if (all(row['url'] in text for row in shown) and
                        len(('📬 Job alerts\n\n' + text).encode('utf-16-le')) // 2 <= 3800):
                    break
            else:
                raise ValueError('alert message exceeds safe size; records retained')
            errors.extend(scan_errors)
            if count:
                await send('📬 Job alerts\n\n' + text)
                delivered += count
                last_shown = shown
            jobs.mark_seen(path, acknowledgement, stamp)
            if count and on_delivered is not None:
                on_delivered(shown)
            acknowledged = set(acknowledgement)
            records = [r for r in records if r['key'] not in acknowledged and job_alerts.url_key(r['url']) not in acknowledged]
            if not count:
                break
        error = '; '.join(dict.fromkeys(errors))[:600]
        if error and error != previous['last_notice']:
            await send('⚠️ Job-alert reader: ' + error)
            save_status(path, now, error, notice=error)
        elif not error and previous['last_notice']:
            await send('✅ Job-alert reader recovered.')
            save_status(path, now, notice='')
        else:
            save_status(path, now, error or None)
        return {'count': delivered, 'shown': last_shown, 'errors': errors}
    except Exception as exc:
        # No credentials, private email text or exception messages in status.
        save_status(path, now, 'worker failure: ' + type(exc).__name__)
        raise


def format_status(path):
    info = status(path)
    def stamp(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime('%b %d %H:%M UTC') if value else 'never'
    return '\n'.join([
        'Job-alert email reader: ' + ('enabled' if enabled() else 'disabled'),
        f'Poll interval: {interval()} seconds',
        f'Exact sender addresses configured: {len(digest.parse_job_alert_senders())}',
        f'Last attempt: {stamp(info["last_attempt"])}',
        f'Last successful poll: {stamp(info["last_success"])}',
        f'Pending job records: {info["queued"]}',
        f'Emails needing parser support (retained): {info["unparsed"]}',
        'Last error: ' + (info['last_error'] or 'none'),
    ])
