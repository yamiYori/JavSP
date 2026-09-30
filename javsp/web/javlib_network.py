"""JavLibrary-specific route discovery, sessions and backoff.

Use a reachable official mirror and preserve its route/session. A Cloudflare
challenge is reported as such; it is never interpreted as a missing movie.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import lxml.html
import requests

from javsp.web.exceptions import SiteBlocked, WebsiteError


logger = logging.getLogger(__name__)
PERMANENT_URL = 'https://www.javlibrary.com'
MIRROR_API = 'https://api.github.com/users/javlibcom'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


def origin(url):
    parts = urlsplit(str(url))
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('JavLib 地址必须是没有用户名和密码的 HTTP(S) URL')
    return urlunsplit((parts.scheme, parts.netloc, '', '', ''))


def mirror_from_bio(bio):
    """The official account publishes either '== c99i ==' or a full URL."""
    if not isinstance(bio, str):
        return ''
    match = re.search(r'https://(?:www\.)?[a-z0-9-]+\.[a-z]{2,63}\b', bio, re.I)
    if match:
        return origin(match.group())
    match = re.search(r'==\s*([a-z0-9][a-z0-9-]{0,61})\s*==', bio, re.I)
    return f'https://www.{match.group(1).lower()}.com' if match else ''


def response_problem(response):
    """Do not accept a 200 challenge, parking page or an empty response."""
    text = response.text
    lower = text.lower()
    if response.status_code == 429:
        return 'HTTP 429 (rate limited)'
    if (response.headers.get('cf-mitigated', '').lower() == 'challenge'
            or 'cf-chl-' in lower or '/cdn-cgi/challenge-platform/' in lower
            or '<title>just a moment' in lower):
        return f'HTTP {response.status_code} (Cloudflare challenge)'
    if response.status_code != 200:
        return f'HTTP {response.status_code}'
    try:
        html = lxml.html.fromstring(text)
    except (ValueError, lxml.etree.ParserError):
        return 'invalid HTML'
    if html.xpath("//div[@id='video_info']"):
        return ''
    if (html.xpath("//div[@id='rightcolumn']")
            and html.xpath("//form[contains(@action, 'vl_searchbyid.php')]")):
        return ''
    return 'not a JavLibrary page'


def retry_after_seconds(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


class JavlibClient:
    def __init__(self, settings, configured_url, proxy_server=None, timeout=10,
                 session_factory=requests.Session, probe_budget=20):
        self.settings = settings
        self.configured_url = configured_url
        self.proxy_server = str(proxy_server) if proxy_server else None
        self.timeout = timeout
        self.probe_budget = probe_budget
        self.deadline = None
        self.session_factory = session_factory
        self.sessions = {}
        self.selected = None
        self.candidates = None
        self.blocked_until = 0.0
        self.next_request = 0.0
        self.lock = threading.RLock()

    def routes(self):
        if self.settings.mode == 'proxy':
            if not self.proxy_server:
                raise WebsiteError('JavLib: mode=proxy 需要 network.proxy_server')
            return ['proxy']
        if self.settings.mode == 'direct':
            return ['direct']
        return ['direct', 'proxy'] if self.proxy_server else ['direct']

    def session(self, base, route):
        key = (base, route)
        if key not in self.sessions:
            session = self.session_factory()
            # proxies={} alone still allows requests to inherit system/env proxies.
            session.trust_env = False
            session.max_redirects = 3
            session.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9'})
            session.proxies = ({'http': self.proxy_server, 'https': self.proxy_server}
                               if route == 'proxy' else {})
            self.sessions[key] = session
        return self.sessions[key]

    def discover(self):
        for route in self.routes():
            try:
                response = self.session('https://api.github.com', route).get(
                    MIRROR_API, timeout=self.bounded_timeout(5))
                response.raise_for_status()
                mirror = mirror_from_bio(response.json().get('bio'))
                if mirror:
                    return mirror
            except (requests.RequestException, ValueError, AttributeError, WebsiteError):
                # Do not log proxy URLs, cookies or exception text containing credentials.
                logger.debug('JavLib: official mirror discovery failed (%s)', route)
        return ''

    def bounded_timeout(self, maximum):
        remaining = maximum if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise WebsiteError('JavLib: 路线探测时间已用完')
        return min(maximum, remaining)

    def bases(self):
        if self.candidates is None:
            urls = [self.settings.base_url or self.configured_url]
            if self.settings.discover_mirror:
                urls.append(self.discover())
            urls.append(PERMANENT_URL)
            self.candidates = list(dict.fromkeys(origin(url) for url in urls if url))
        return self.candidates

    def fetch(self, base, route, path):
        delay = self.next_request - time.monotonic()
        if delay > 0:
            self.bounded_timeout(delay)
            if self.deadline is not None and time.monotonic() + delay >= self.deadline:
                raise WebsiteError('JavLib: 路线探测时间已用完')
            time.sleep(delay)
        try:
            response = self.session(base, route).get(
                base + path, timeout=self.bounded_timeout(self.timeout if self.selected else min(5, self.timeout)))
        finally:
            self.next_request = time.monotonic() + self.settings.minimum_interval
        if origin(response.url) != base:
            # Do not promote arbitrary redirect hosts into trusted mirror candidates.
            raise WebsiteError('JavLib: 镜像跳转到了其他域名，请更新 network.javlib.base_url')
        return response

    def block(self, response=None):
        delay = self.settings.cooldown
        if response is not None:
            delay = max(delay, retry_after_seconds(response.headers.get('Retry-After')))
        self.blocked_until = time.monotonic() + delay
        self.selected = None
        # Re-discover after cooldown, so a rotating mirror does not stay stale forever.
        self.candidates = None

    def get(self, url):
        with self.lock:
            remaining = self.blocked_until - time.monotonic()
            if remaining > 0:
                raise SiteBlocked(f'JavLib: 冷却中，约 {remaining:.0f} 秒后再试')
            self.deadline = None if self.selected else time.monotonic() + self.probe_budget
            parts = urlsplit(url)
            if parts.netloc and (not self.selected or origin(url) != self.selected[0]):
                raise WebsiteError('JavLib: 拒绝访问搜索结果中的外部链接')
            path = urlunsplit(('', '', parts.path or '/cn/', parts.query, ''))
            if not path.startswith('/cn/'):
                raise WebsiteError('JavLib: 无效的元数据页面路径')
            if self.selected:
                base, route = self.selected
                try:
                    response = self.fetch(base, route, path)
                except (requests.RequestException, WebsiteError):
                    self.block()
                    raise SiteBlocked(f'JavLib: {base} ({route}) 连接失败，已进入冷却') from None
                problem = response_problem(response)
                if not problem:
                    return response
                self.block(response)
                raise SiteBlocked(f'JavLib: {base} ({route}): {problem}，已进入冷却')
            failures = []
            bases = self.bases()
            for route in self.routes():
                for base in bases:
                    if time.monotonic() >= self.deadline:
                        failures.append('route probe budget exhausted')
                        break
                    try:
                        response = self.fetch(base, route, path)
                        problem = response_problem(response)
                    except (requests.RequestException, WebsiteError) as exc:
                        problem = type(exc).__name__
                        response = None
                    if not problem:
                        self.selected = (base, route)
                        logger.info('JavLib: 已选择 %s (%s)', base, route)
                        return response
                    failures.append(f'{base} ({route}): {problem}')
                    logger.debug('JavLib: %s', failures[-1])
                    if response is not None and response.status_code == 429:
                        # A rate limit is not a reason to switch IPs or mirror hosts.
                        self.block(response)
                        raise SiteBlocked('JavLib: ' + failures[-1] + '，已进入冷却')
            self.block()
            raise SiteBlocked('JavLib: 没有可用路线；' + '; '.join(failures))

    def close(self):
        with self.lock:
            for session in self.sessions.values():
                session.close()
            self.sessions.clear()
            self.selected = None
