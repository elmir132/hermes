"""Read-only coverage view of CURRENT configured sources, not historical row counts."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

STATES = ('healthy', 'stale', 'partial', 'failed', 'never-checked')


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, AttributeError, TypeError):
        return None


def coverage(cfg, path, now=None):
    import jobs
    now = now or datetime.now(timezone.utc)
    rows, pending = {}, {}
    if Path(path).exists():
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'jobs_source_status' in tables:
                rows = {r['source_key']: dict(r) for r in con.execute('SELECT * FROM jobs_source_status')}
            if 'jobs_pending' in tables:
                pending = dict(con.execute('''SELECT source_key,COUNT(*) FROM jobs_pending
                    WHERE status='new' AND last_seen>=? AND
                    (ready=0 OR json_extract(payload,'$.details_checked')=0) GROUP BY source_key''',
                    ((now-timedelta(days=2)).isoformat(),)).fetchall())
    result = []
    for company in cfg.get('companies', []):
        key = jobs._source_key(company)
        row = rows.get(key, {})
        good = parse_time(row.get('last_success_at'))
        error = row.get('last_error')
        if not row.get('last_attempt_at'):
            state = 'never-checked'
        elif error:
            state = 'partial' if row.get('last_outcome') == 'partial' else 'failed'
        elif good is None or now-good >= timedelta(days=2):
            state = 'stale'
        elif pending.get(key):
            state = 'partial'
            error = f'{pending[key]} stored match(es) with unread or unfinished details'
        else:
            state = 'healthy'
        if company.get('provider') == 'browser':
            found, cache_error = jobs.browser_jobs(company, cfg, now)
            if cache_error:
                state = 'stale' if 'browser data is ' in cache_error else ('partial' if found else 'failed')
                error = cache_error
        result.append(dict(key=key, company=company['name'], state=state,
                           last_success_at=row.get('last_success_at'), last_error=error))
    counts = Counter(r['state'] for r in result)
    return {state:counts[state] for state in STATES}, result


def summary(counts):
    return 'Sources: ' + ' · '.join(f'{state} {counts[state]}' for state in STATES) + ' (/sources)'


def limitations(cfg):
    """Known reader bounds, independent of HTTP health and pending-row counts."""
    providers = {c['provider'] for c in cfg.get('companies', [])}
    lines = []
    if 'google' in providers:
        lines.append('Google: first results page per configured query/location; newest ordering and complete coverage are not verified.')
    if 'apple' in providers:
        lines.append('Apple: search listings only; posting pay, visa and experience details are not read.')
    names = [c['name'] for c in cfg.get('companies', []) if c['provider']=='browser']
    if names:
        lines.append('Mac snapshots (' + ', '.join(names)[:250] + '): refreshed by the Mac reader; posting details are not read.')
    if 'workday' in providers:
        lines.append('Workday: bounded queries per location/facet group, not one universal 100-role employer limit. Truncated reads are partial.')
    if providers:
        lines.append('Detail budgets vary by reader; the shared scan budget is 50, with additional provider-specific budgets. Unread details are not verified eligibility.')
    return lines


def pages(cfg, path, now=None):
    import jobs
    counts, rows = coverage(cfg, path, now)
    heading = summary(counts) + '\nLatest recorded state; not a live rescan. Healthy does not mean every vacancy/detail was read. Stale = no success for 2+ days; Mac snapshots expire after 26h by default.'
    lines = []
    for state in ('failed', 'stale', 'partial'):
        for row in sorted((r for r in rows if r['state'] == state), key=lambda r:r['company']):
            error = str(row['last_error'] or 'no recent complete success').replace('\n',' ')[:180]
            lines.append(f"• {row['company']} — {state}; last good {jobs._format_scan_stamp(row['last_success_at'])}; {error}")
    never = [r['company'] for r in rows if r['state']=='never-checked']
    if never:
        lines.append('Never checked: '+', '.join(never[:12]) + (f' (+{len(never)-12} more)' if len(never)>12 else ''))
    limits = limitations(cfg)
    if limits:
        lines.extend(['Known reader limits:', *limits])
    output, text = [], heading
    for line in lines:
        if len((text+'\n'+line).encode('utf-16-le'))//2 > 3400:
            output.append(text)
            text = 'Source coverage (continued)'
        text += '\n'+line
    output.append(text)
    return output


def unchecked_companies(cfg, path):
    """Current board identities that have never had an attempt; no file writes."""
    import jobs
    attempted = set()
    path = Path(path)
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as con:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs_source_status'").fetchone()
            if exists:
                attempted = {row[0] for row in con.execute("SELECT source_key FROM jobs_source_status WHERE last_attempt_at IS NOT NULL AND last_attempt_at<>''")}
    return [company for company in cfg.get('companies', []) if jobs._source_key(company) not in attempted]
