"""Opt-in, read-only ingestion of a user's job-alert emails. Never fetch listing URLs.

Set HERMES_JOB_ALERT_RECORDS=1 plus HERMES_JOB_ALERT_SENDERS (exact addresses).
Templates without explicit per-listing company and location are left unparsed.
No LLM, instructions, attachments, applications, subscriptions, or email writes.
"""
import hashlib
import html
import json
import re
import sqlite3
import time
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import digest

MAX_EMAILS = 8
MAX_LIST_PAGES = 4
MAX_LISTINGS = 100


def canonical_url(value):
    """Normalize only known tracking parameters, without following redirects."""
    try:
        p = urlsplit(html.unescape(value).strip())
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            return ''
        if re.search(r'[\x00-\x20\x7f]', value):
            return ''
        if (p.hostname.lower() == 'linkedin.com' or p.hostname.lower().endswith('.linkedin.com')):
            match = re.fullmatch(r'/(?:comm/)?jobs/view/(?:[^/]*-)?(\d+)/?', p.path)
            if match:
                return 'https://www.linkedin.com/jobs/view/' + match[1]
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in ('trk', 'trackingid', 'refid', 'source', 'gh_src')]
        return urlunsplit(('https', p.hostname.lower(), p.path.rstrip('/') or '/', urlencode(query), ''))
    except (ValueError, TypeError):
        return ''


def url_key(url):
    value = canonical_url(url)
    return 'url:' + hashlib.sha256(value.encode()).hexdigest() if value else ''


class Cards(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.cards = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.blocked += 1
        if tag in ('tr', 'article'):
            self.stack.append({'text': [], 'links': [], 'anchor': None})
        if tag == 'a' and self.stack:
            self.stack[-1]['anchor'] = [attrs.get('href', ''), []]

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.blocked:
            self.blocked -= 1
        if tag == 'a' and self.stack:
            card = self.stack[-1]
            if card['anchor']:
                card['links'].append((card['anchor'][0], ' '.join(card['anchor'][1])))
                card['anchor'] = None
        if tag in ('tr', 'article') and self.stack:
            self.cards.append(self.stack.pop())

    def handle_data(self, text):
        if self.blocked or not self.stack:
            return
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            card = self.stack[-1]
            card['text'].append(text)
            if card['anchor']:
                card['anchor'][1].append(text)


def record(title, company, location, url, text='', published=''):
    from jobs import _clean
    fields = [_clean(x)[:200] for x in (title, company, location)]
    link = canonical_url(url)
    if not all(fields) or not link:
        return None
    return {'key': url_key(link), 'company': fields[1], 'title': fields[0], 'location': fields[2],
            'url': link, 'remote': bool(re.search(r'\bremote\b', fields[2], re.I)),
            'published': published, 'text': _clean(text)[:20000], 'employment': '', 'pay': None,
            'details_checked': False, 'location_verified': True, 'origin': 'job alert email'}


def parse_listings(body):
    """Accept structured JobPosting data or explicit single-listing HTML cards.

    LinkedIn cards must name each company; the sender/subject is never the company.
    Ambiguous templates produce no records, rather than guessed job attributes.
    """
    from public_readers import job_postings, schema_address, schema_regions
    out = []
    for raw in job_postings(body):
        organization = raw.get('hiringOrganization') or {}
        if not isinstance(organization, dict):
            continue
        location = schema_address(raw.get('jobLocation'))
        if not location and str(raw.get('jobLocationType')).upper() == 'TELECOMMUTE':
            regions = schema_regions(raw.get('applicantLocationRequirements'))
            location = 'Remote, ' + regions if regions else ''
        row = record(raw.get('title', ''), organization.get('name', ''), location,
                     raw.get('url', ''), raw.get('description', ''), raw.get('datePosted', ''))
        if row:
            out.append(row)
    parser = Cards()
    parser.feed(body[:200000])
    for card in parser.cards:
        links = [(canonical_url(u), t) for u, t in card['links'] if t and canonical_url(u)]
        links = [(u, t) for u, t in links if re.search(r'/jobs?/|/postings/|/job/', urlsplit(u).path)]
        if len({u for u, _ in links}) != 1:
            continue
        url, title = links[0]
        lines = card['text']
        company = next((x.split(':', 1)[1].strip() for x in lines if x.lower().startswith('company:')), '')
        location = next((x.split(':', 1)[1].strip() for x in lines if x.lower().startswith('location:')), '')
        # LinkedIn's combined company/location line. Require exactly two fields.
        if not company or not location:
            combined = [x.split(' · ') for x in lines if ' · ' in x]
            combined = [x for x in combined if len(x) == 2]
            if len(combined) == 1:
                company, location = combined[0]
        row = record(title, company, location, url)
        if row:
            out.append(row)
    return list({r['key']: r for r in out[:MAX_LISTINGS]}.values())


def _db(path):
    con = sqlite3.connect(path)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS job_alert_queue (key TEXT PRIMARY KEY, payload TEXT NOT NULL, added INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS job_alert_mail_seen (account TEXT, message_id TEXT, PRIMARY KEY(account,message_id));
    CREATE TABLE IF NOT EXISTS job_alert_cursor (account TEXT PRIMARY KEY, payload TEXT NOT NULL);
    ''')
    return con


def _html_body(payload):
    # Do not truncate at the digest's summary length; multi-company emails need all cards.
    if payload.get('mimeType') == 'text/html':
        return digest._decode((payload.get('body') or {}).get('data', ''))[:200000]
    return '\n'.join(_html_body(p) for p in payload.get('parts', []))[:200000]


async def fetch_records(accounts, get_json, db_path, senders=None, now=None):
    """Persist bounded pages and queued listings before advancing a Gmail cursor.

    Eight messages total per invocation. Fixed search windows and persisted page
    tokens/pending IDs prevent newer mail from hiding older pages. No Gmail writes.
    """
    allowed = digest.parse_job_alert_senders(senders)
    if not allowed:
        return [], []
    now = int(now or time.time())
    remaining, pages, errors, stopped = MAX_EMAILS, 0, [], False
    with closing(_db(db_path)) as con:
        # Least recently advanced accounts first, preventing mailbox starvation.
        states = {a: json.loads(v) for a, v in con.execute('SELECT account,payload FROM job_alert_cursor')}
        ordered = sorted(accounts, key=lambda a: states.get(a[0], {}).get('updated', 0))
        for account, token in ordered:
            if not remaining or stopped:
                break
            state = states.get(account, {'after': now - 30*86400, 'before': now, 'pending': [], 'page': None})
            while remaining:
                if not state['pending']:
                    if pages >= MAX_LIST_PAGES:
                        break
                    query = digest.job_alert_query(allowed).replace('newer_than:2d', f"after:{state['after']} before:{state['before']}")
                    params = {'q': query, 'maxResults': min(remaining, MAX_EMAILS)}
                    if state.get('page'):
                        params['pageToken'] = state['page']
                    status, data = await get_json('https://gmail.googleapis.com/gmail/v1/users/me/messages', token, params)
                    pages += 1
                    if status != 200:
                        # Invalid cursors restart the same fixed window, retaining message IDs.
                        if status == 400 and state.get('page'):
                            state['page'] = None
                        stopped = status in (403, 429)
                        errors.append(f'{account}: alert listing HTTP {status}')
                        break
                    state['pending'] = [m['id'] for m in (data or {}).get('messages', [])[:MAX_EMAILS] if m.get('id')]
                    state['page'] = (data or {}).get('nextPageToken')
                    if not state['pending']:
                        state.update(after=max(state['after'], state['before']-3600), before=now, page=None)
                        break
                message_id = state['pending'][0]
                seen = con.execute('SELECT 1 FROM job_alert_mail_seen WHERE account=? AND message_id=?', (account, message_id)).fetchone()
                if not seen:
                    remaining -= 1
                    status, message = await get_json('https://gmail.googleapis.com/gmail/v1/users/me/messages/' + message_id, token, {'format': 'full'})
                    if status != 200:
                        stopped = status in (403, 429)
                        errors.append(f'{account}: alert message HTTP {status}')
                        break  # Keep the ID pending on failure.
                    payload = (message or {}).get('payload') or {}
                    records = []
                    if digest._sender_matches_allowlist(digest.header(payload, 'From'), allowed):
                        records = parse_listings(_html_body(payload))
                        if not records:
                            errors.append(f'{account}: alert template has no recognized listings')
                    with con:
                        con.executemany('INSERT OR IGNORE INTO job_alert_queue VALUES (?,?,?)', [(r['key'], json.dumps(r), now) for r in records])
                        con.execute('INSERT OR IGNORE INTO job_alert_mail_seen VALUES (?,?)', (account, message_id))
                state['pending'].pop(0)
                if not state['pending'] and not state.get('page'):
                    state.update(after=max(state['after'], state['before']-3600), before=now, page=None)
                    break
            state['updated'] = now
            with con:
                con.execute('INSERT OR REPLACE INTO job_alert_cursor VALUES (?,?)', (account, json.dumps(state)))
        with con:
            con.execute('DELETE FROM job_alert_queue WHERE added < ?', (now-30*86400,))
        queued = [json.loads(v) for (v,) in con.execute('SELECT payload FROM job_alert_queue ORDER BY added DESC LIMIT 1000')]
    return queued, errors


def native_key(row, company):
    """Match only a configured board identity, preserving existing jobs_seen keys."""
    url = canonical_url(row.get('url', ''))
    p = urlsplit(url)
    slug = company.get('slug', '')
    name = company.get('name', row['company'])
    provider = company.get('provider')
    if provider == 'greenhouse' and p.hostname in ('boards.greenhouse.io', 'job-boards.greenhouse.io'):
        match = re.fullmatch('/' + re.escape(slug) + r'/jobs/(\d+)', p.path)
        if match:
            return f'gh:{name}:{match[1]}'
    if provider in ('lever', 'ashby', 'ashby_html', 'rippling'):
        host = {'lever': 'jobs.lever.co', 'ashby': 'jobs.ashbyhq.com', 'ashby_html': 'jobs.ashbyhq.com', 'rippling': 'ats.rippling.com'}[provider]
        middle = '/jobs/' if provider == 'rippling' else '/'
        match = re.fullmatch('/' + re.escape(slug) + middle + r'([0-9a-f-]{36})', p.path)
        if p.hostname == host and match:
            prefix = {'lever': 'lv', 'ashby': 'ab', 'ashby_html': 'ab', 'rippling': 'rippling'}[provider]
            return f'{prefix}:{name}:{match[1]}'
    if provider == 'workday' and p.hostname == company.get('host') and p.path.startswith('/' + company.get('site', '') + '/job/'):
        return f"wd:{name}:" + p.path[len('/' + company['site']):]
    return row['key']
