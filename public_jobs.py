"""Conservative transport for new public job sources, with no credentials or retries."""
import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = 'hermes-job-watcher/1.0'


def robots_policy(text):
    """Use urllib's policy and honor later matching groups it otherwise ignores.

    Repeated wildcard groups (e.g. higher.gs.com) can hide a blanket Disallow
    from urllib.robotparser's first-match behavior. Require every applicable
    group to allow and honor explicit disallows, rather than treating parser
    ordering or blank-line quirks as permission.
    """
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    groups, current, rules = [], [], []
    for line in text.splitlines() + ['User-agent: end-of-file-sentinel']:
        line = line.split('#', 1)[0].strip()
        key, sep, value = line.partition(':')
        if not sep:
            continue
        if key.lower() == 'user-agent':
            if rules:
                groups.append((current, rules))
                current, rules = [], []
            current.append(value.strip().lower())
        elif current:
            rules.append(line)
    applicable = []
    for agents, lines in groups:
        if any(a == '*' or a in USER_AGENT.lower() for a in agents):
            group = RobotFileParser()
            group.parse(['User-agent: *', *lines])
            applicable.append(group)
            disallows = [line for line in lines if line.lower().startswith('disallow:')]
            if disallows:
                restrictive = RobotFileParser()
                restrictive.parse(['User-agent: *', *disallows])
                applicable.append(restrictive)
    return parser, applicable


class PublicJobsClient:
    """Each URL once, exact-path robots checks, checked redirects, pause per host.

    A new instance is scoped to one scan. A 403/429 stops the whole origin.
    Robots 4xx except 429 means no rules (RFC 9309); 5xx/unreachable
    policies fail closed. Ordinary page 403/429 still stops the origin.
    raw(url) is injectable for tests and returns (status, headers, text).
    """
    def __init__(self, raw=None, pause=1.0, timeout=25):
        self.timeout = timeout
        self.locks = {}
        self.raw = raw or self._raw
        self.pause = pause
        self.policies = {}
        self.cache = {}
        self.last = {}
        self.stopped = set()
        self.requests = []

    async def _raw(self, url, body=None):
        from urllib import error, request

        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def read():
            opener = request.build_opener(NoRedirect)
            headers = {"User-Agent": USER_AGENT}
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                with opener.open(req, timeout=self.timeout) as response:
                    return response.status, dict(response.headers), response.read(12_000_000).decode("utf-8", "replace")
            except error.HTTPError as exc:
                return exc.code, dict(exc.headers), exc.read(100_000).decode("utf-8", "replace")
        return await asyncio.to_thread(read)

    async def _request(self, url, delay, body=None, policy_request=False):
        origin = self._origin(url)
        key = url if body is None else (url, json.dumps(body, sort_keys=True))
        # Several boards share one ATS host. Serialize requests there so
        # concurrent companies still respect the host's pause and stop state.
        lock = self.locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if key in self.cache:
                return self.cache[key]
            if origin in self.stopped:
                return 0, {}, ''
            await asyncio.sleep(max(0, delay - (time.monotonic() - self.last.get(origin, 0))))
            try:
                result = await self.raw(url) if body is None else await self.raw(url, body)
            except Exception:
                result = (0, {}, '')
            self.last[origin] = time.monotonic()
            self.cache[key] = result
            self.requests.append({'url': url, 'status': result[0]})
            if result[0] == 429 or (result[0] == 403 and not policy_request):
                self.stopped.add(origin)
            return result

    @staticmethod
    def _origin(url):
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password:
            raise ValueError('public jobs require an HTTPS URL without credentials')
        return f'https://{parts.netloc}'

    @staticmethod
    def _challenge(text):
        return bool(re.search(r'/_fs-ch-|<title>\s*(?:Just a moment|Access Denied)|cf_chl_opt|verify you are human', text, re.I))

    async def _robots(self, origin):
        url = origin + '/robots.txt'
        for _ in range(6):
            self._origin(url)  # Reject credentials and non-HTTPS redirects.
            status, headers, policy = await self._request(url, self.pause, policy_request=True)
            if status in (301, 302, 303, 307, 308):
                location = headers.get('location') or headers.get('Location')
                if not location:
                    break
                url = urljoin(url, location)
                continue
            if 400 <= status < 500 and status != 429:
                return ''
            if status == 200 and not self._challenge(policy):
                return policy
            break
        return 'User-agent: *\nDisallow: /'

    async def text(self, url, depth=0, body=None):
        origin = self._origin(url)
        if origin not in self.policies:
            self.policies[origin] = robots_policy(await self._robots(origin))
        parser, groups = self.policies[origin]
        if not parser.can_fetch(USER_AGENT, url) or any(not p.can_fetch(USER_AGENT, url) for p in groups):
            return 0, ''
        delay = max(self.pause, parser.crawl_delay(USER_AGENT) or 0)
        status, headers, response = await self._request(url, delay, body)
        if status == 200 and self._challenge(response):
            self.stopped.add(origin)
            return 0, ''
        if status in (301, 302, 303, 307, 308):
            location = headers.get('location') or headers.get('Location')
            if location and depth < 5:
                return await self.text(urljoin(url, location), depth + 1, None if status in (301, 302, 303) else body)
            return 0, ''
        return status, response

    async def json(self, url, body=None):
        status, body = await self.text(url, body=body)
        try:
            return status, json.loads(body) if status == 200 else {}
        except (ValueError, TypeError):
            return status, None
