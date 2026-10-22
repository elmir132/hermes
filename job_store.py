"""Durable job discovery and delivery state. No network access or Telegram dependency."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

STATUSES = ('new', 'sent', 'dismissed', 'rejected', 'closed')


def connect(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript('''
    CREATE TABLE IF NOT EXISTS jobs_pending (
        key TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
        location TEXT NOT NULL, url TEXT NOT NULL, pay TEXT, flags TEXT NOT NULL,
        tier INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('new','sent','dismissed','rejected','closed')),
        ready INTEGER NOT NULL, score REAL NOT NULL, payload TEXT NOT NULL,
        source_key TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS jobs_pending_delivery ON jobs_pending(status,ready,score DESC,first_seen);
    CREATE TABLE IF NOT EXISTS jobs_pending_alias (alias TEXT PRIMARY KEY, key TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS jobs_pending_alias_key ON jobs_pending_alias(key);
    CREATE TABLE IF NOT EXISTS jobs_seen (job_key TEXT PRIMARY KEY, seen_at TEXT);
    CREATE TABLE IF NOT EXISTS jobs_public_ids (key TEXT PRIMARY KEY, public_id TEXT NOT NULL UNIQUE COLLATE NOCASE);
    CREATE TABLE IF NOT EXISTS jobs_message_roles (
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, number INTEGER NOT NULL,
        key TEXT NOT NULL, PRIMARY KEY(chat_id,message_id,number));
    ''')
    with con:
        for row in con.execute('SELECT key FROM jobs_pending WHERE key NOT IN (SELECT key FROM jobs_public_ids)'):
            ensure_id(con, row['key'])
    return con


def ensure_id(con, key):
    row = con.execute('SELECT public_id FROM jobs_public_ids WHERE key=?', (key,)).fetchone()
    if row:
        return row[0]
    digest = hashlib.sha256(key.encode()).hexdigest().upper()
    for length in range(5, len(digest)+1):
        candidate = 'J' + digest[:length]
        if not con.execute('SELECT 1 FROM jobs_public_ids WHERE public_id=?', (candidate,)).fetchone():
            con.execute('INSERT INTO jobs_public_ids VALUES (?,?)', (key, candidate))
            return candidate
    raise ValueError('job identity collision')


def stamp(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


def upsert(path, entries, now=None):
    """entries: (job, source_key, ready, rejected). Alias identity is never title-based.

    Preserve delivery and terminal states, and never replace checked details with
    a shallow listing. Existing jobs_seen rows migrate into sent state lazily.
    """
    from job_alerts import url_key
    with closing(connect(path)) as con, con:
        for job, source, ready, rejected in entries:
            aliases = list(dict.fromkeys(k for k in (job['key'], url_key(job['url'])) if k))
            placeholders = ','.join('?' for _ in aliases)
            existing = con.execute(f'SELECT key FROM jobs_pending_alias WHERE alias IN ({placeholders})', aliases).fetchone()
            key = existing['key'] if existing else job['key']
            seen = con.execute(f'SELECT 1 FROM jobs_seen WHERE job_key IN ({placeholders}) LIMIT 1', aliases).fetchone()
            status = 'rejected' if rejected else ('sent' if seen else 'new')
            payload = dict(job)
            # Only compact application/fit evidence is retained, not entire careers pages.
            payload['text'] = payload.get('text', '')[:20000]
            con.execute('''INSERT INTO jobs_pending VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  status=CASE WHEN jobs_pending.status IN ('dismissed','rejected','closed') THEN jobs_pending.status
                              WHEN excluded.status='rejected' THEN 'rejected'
                              WHEN excluded.status='sent' THEN 'sent' ELSE jobs_pending.status END,
                  company=CASE WHEN excluded.ready THEN excluded.company ELSE jobs_pending.company END,
                  title=CASE WHEN excluded.ready THEN excluded.title ELSE jobs_pending.title END,
                  location=CASE WHEN excluded.ready THEN excluded.location ELSE jobs_pending.location END,
                  url=CASE WHEN excluded.ready THEN excluded.url ELSE jobs_pending.url END,
                  pay=CASE WHEN excluded.ready THEN excluded.pay ELSE jobs_pending.pay END,
                  flags=CASE WHEN excluded.ready THEN excluded.flags ELSE jobs_pending.flags END,
                  tier=excluded.tier, score=excluded.score,
                  ready=MAX(jobs_pending.ready,excluded.ready),
                  payload=CASE WHEN excluded.ready OR NOT jobs_pending.ready THEN excluded.payload ELSE jobs_pending.payload END
                ''', (key, job['company'], job['title'], job['location'], job['url'],
                      json.dumps(job.get('pay')), json.dumps(job.get('flags', []), ensure_ascii=False), job.get('tier', 0),
                      stamp(now), stamp(now), status, int(ready), job.get('score', 0),
                      json.dumps(payload, ensure_ascii=False), source))
            ensure_id(con, key)
            con.executemany('INSERT OR IGNORE INTO jobs_pending_alias VALUES (?,?)', [(alias, key) for alias in aliases])


def acknowledge(con, keys):
    """Called inside the SAME transaction as jobs_seen acknowledgement."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='jobs_pending'").fetchone():
        return
    keys = list(dict.fromkeys(keys))
    for start in range(0, len(keys), 400):
        part = keys[start:start+400]
        con.execute('UPDATE jobs_pending SET status=\'sent\' WHERE status=\'new\' AND key IN '
                    '(SELECT key FROM jobs_pending_alias WHERE alias IN (' + ','.join('?' for _ in part) + '))', part)


def set_status(path, key, status):
    if status not in STATUSES:
        raise ValueError('unknown job status')
    with closing(connect(path)) as con, con:
        return con.execute('UPDATE jobs_pending SET status=? WHERE key=?', (status, key)).rowcount > 0


def counts(path):
    with closing(connect(path)) as con:
        return dict(con.execute('SELECT status,COUNT(*) FROM jobs_pending GROUP BY status').fetchall())


def batch(path, cfg, *, include_seen=False, companies=None, exclude_keys=None, limit=None, now=None, number_from=1):
    """Read a complete-message batch locally. Selection never acknowledges delivery.

    SQL cursors stream payloads; no whole-history materialization. Overlong rows
    stay pending. The caller serializes selection/send/ack, never network scans.
    """
    import jobs
    from job_alerts import url_key
    limit = limit or cfg.get('max_per_message', 12)
    excluded = set(exclude_keys or ())
    selected, acknowledgements, per_company = [], [], {}
    header = 'Stored matches: /fit <ID>, /save <ID>, /applied <ID>. For a list number, reply to this message.'
    text = header
    with closing(connect(path)) as con:
        query = "SELECT p.key,p.payload,i.public_id FROM jobs_pending p JOIN jobs_public_ids i ON i.key=p.key WHERE ready=1 AND status IN " + ("('new','sent')" if include_seen else "('new')")
        params = []
        if companies is not None:
            if not companies:
                return 'No pending matching roles in this selection.', [], []
            query += ' AND company IN (' + ','.join('?' for _ in companies) + ')'
            params.extend(companies)
        query += ' ORDER BY score DESC,first_seen,p.key'
        for row in con.execute(query, params):
            job = json.loads(row['payload'])
            if row['key'] in excluded or job['key'] in excluded:
                continue
            if not include_seen and con.execute('SELECT 1 FROM jobs_pending_alias a JOIN jobs_seen s ON s.job_key=a.alias WHERE a.key=? LIMIT 1', (row['key'],)).fetchone():
                continue
            if not jobs.score_job(job, cfg, now):
                continue
            name = job['company']
            if per_company.get(name, 0) >= (cfg.get('max_per_company') or 10**6):
                continue
            import job_requirements
            job['flags'] = job_requirements.flags(job)
            where = job['location'] if len(job['location']) <= 60 else job['location'][:57] + '...'
            if job.get('location_verified') is False:
                where += ' (search filter; listing location unverified)'
            tier = f" [tier {job['tier']}{jobs.rating_label(job.get('ratings'))}]" if job.get('tier') else ''
            age = jobs.age_days(job, now)
            posted = f' · posted {age}d ago' if age is not None else ''
            block = (f"\n\n• {name}{tier}: {job['title']}  #{len(selected)+number_from} [{row['public_id']}]\n  {where}{posted}"
                     f"\n  {'; '.join(job.get('flags', []))}\n  {job['url']}")
            if len((text + block).encode('utf-16-le')) // 2 > 3400:
                continue
            text += block
            selected.append(dict(job, store_key=row['key'], job_id=row['public_id']))
            acknowledgements.extend([job['key'], url_key(job['url'])])
            per_company[name] = per_company.get(name, 0) + 1
            if len(selected) >= limit:
                break
    if not selected:
        text = 'No pending matching roles. Scans refill the stored list automatically; /jobs refresh checks now.'
    return text, selected, list(dict.fromkeys(acknowledgements))


def states(path):
    with closing(connect(path)) as con:
        return {r['key']: (r['ready'] and json.loads(r['payload']).get('details_checked') is not False, r['status'])
                for r in con.execute('SELECT key,ready,status,payload FROM jobs_pending')}


def identity(path, alias):
    with closing(connect(path)) as con:
        row = con.execute('SELECT a.key,i.public_id FROM jobs_pending_alias a JOIN jobs_public_ids i ON i.key=a.key WHERE a.alias=?', (alias,)).fetchone()
        return dict(row) if row else None


def remember_message(path, chat_id, message_id, rows, number_from=1):
    """Freeze the original number->job mapping; later lists never overwrite it."""
    with closing(connect(path)) as con, con:
        for number, row in enumerate(rows, number_from):
            key = row.get('store_key')
            if key is None:
                record = con.execute('SELECT key FROM jobs_pending_alias WHERE alias=?', (row.get('key', ''),)).fetchone()
                key = record[0] if record else None
            if key:
                con.execute('INSERT OR IGNORE INTO jobs_message_roles VALUES (?,?,?,?)', (chat_id,message_id,number,key))


def resolve(path, value, *, chat_id=None, message_id=None):
    value = value.strip().lstrip('#').upper()
    with closing(connect(path)) as con:
        if value.isdigit():
            if len(value) > 9:
                return None
            if chat_id is None or message_id is None:
                return None  # Never guess which list a bare number refers to.
            row = con.execute('SELECT p.key,p.payload,i.public_id FROM jobs_message_roles m JOIN jobs_pending p ON p.key=m.key JOIN jobs_public_ids i ON i.key=p.key WHERE m.chat_id=? AND m.message_id=? AND m.number=?', (chat_id,message_id,int(value))).fetchone()
        else:
            row = con.execute('SELECT p.key,p.payload,i.public_id FROM jobs_pending p JOIN jobs_public_ids i ON i.key=p.key WHERE i.public_id=?', (value,)).fetchone()
        return dict(json.loads(row['payload']), store_key=row['key'], job_id=row['public_id']) if row else None
