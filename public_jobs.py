"""Conservative transport for new public job sources, with no credentials or retries."""
import asyncio
import json
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
    Unavailable robots (except 404/410) fail closed; no browser or API fallback.
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

    async def _raw(self, url):
        from urllib import error, request

        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        def read():
            opener = request.build_opener(NoRedirect)
            req = request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with opener.open(req, timeout=self.timeout) as response:
                    return response.status, dict(response.headers), response.read(12_000_000).decode("utf-8", "replace")
            except error.HTTPError as exc:
                return exc.code, dict(exc.headers), exc.read(100_000).decode("utf-8", "replace")
        return await asyncio.to_thread(read)

    async def _request(self, url, delay):
        origin = self._origin(url)
        # Several boards share one ATS host. Serialize requests there so
        # concurrent companies still respect the host's pause and stop state.
        lock = self.locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if url in self.cache:
                return self.cache[url]
            if origin in self.stopped:
                return 0, {}, ''
            await asyncio.sleep(max(0, delay - (time.monotonic() - self.last.get(origin, 0))))
            try:
                result = await self.raw(url)
            except Exception:
                result = (0, {}, '')
            self.last[origin] = time.monotonic()
            self.cache[url] = result
            self.requests.append({'url': url, 'status': result[0]})
            if result[0] in (403, 429):
                self.stopped.add(origin)
            return result

    @staticmethod
    def _origin(url):
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password:
            raise ValueError('public jobs require an HTTPS URL without credentials')
        return f'https://{parts.netloc}'

    async def text(self, url, depth=0):
        origin = self._origin(url)
        if origin not in self.policies:
            status, _, body = await self._request(origin + '/robots.txt', self.pause)
            if status in (404, 410):
                body = ''
            elif status != 200:
                body = 'User-agent: *\nDisallow: /'
            self.policies[origin] = robots_policy(body)
        parser, groups = self.policies[origin]
        if not parser.can_fetch(USER_AGENT, url) or any(not p.can_fetch(USER_AGENT, url) for p in groups):
            return 0, ''
        delay = max(self.pause, parser.crawl_delay(USER_AGENT) or 0)
        status, headers, body = await self._request(url, delay)
        if status in (301, 302, 303, 307, 308):
            location = headers.get('location') or headers.get('Location')
            if location and depth < 5:
                return await self.text(urljoin(url, location), depth + 1)
            return 0, ''
        return status, body

    async def json(self, url):
        status, body = await self.text(url)
        try:
            return status, json.loads(body) if status == 200 else {}
        except (ValueError, TypeError):
            return status, None
