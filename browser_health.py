"""Observe cached Mac-reader data only; no browser, careers or Gmail requests."""
from contextlib import closing
from datetime import datetime, timezone
import json
import re
import sqlite3

import jobs


def problems(cfg, now=None):
    out = []
    for company in cfg.get('companies', []):
        if company.get('provider') != 'browser':
            continue
        _, error = jobs.browser_jobs(company, cfg, now)
        if error:
            out.append((company['name'], error))
    return sorted(out)


async def run_once(cfg, path, send, now=None):
    now = now or datetime.now(timezone.utc)
    failures = problems(cfg, now)
    # A growing age must not create another warning every hour.
    signature = json.dumps([(name, re.sub(r'browser data is \d+ hours old', 'browser data stale', error))
                            for name, error in failures]) if failures else ''
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('CREATE TABLE IF NOT EXISTS jobs_browser_health (id INTEGER PRIMARY KEY, notice TEXT NOT NULL, checked_at TEXT NOT NULL)')
        row = con.execute('SELECT notice FROM jobs_browser_health WHERE id=1').fetchone()
        previous = row[0] if row else ''
    if signature != previous:
        if failures:
            text = ('⚠️ Mac job-source refresh needs attention:\n' +
                    '\n'.join(f'• {name}: {error[:180]}' for name, error in failures) +
                    '\nCached listings may be outdated. Check the Mac refresh and upload; /sources shows coverage.')
        else:
            text = '✅ Mac job-source snapshots are fresh again.'
        # A failed send leaves the previous notice intact for the next check.
        await send(text)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('INSERT OR REPLACE INTO jobs_browser_health VALUES (1,?,?)', (signature, now.isoformat()))
    return bool(failures)
