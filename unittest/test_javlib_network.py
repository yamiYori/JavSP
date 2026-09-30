from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from javsp.web import javlib_network as net
from javsp.web.exceptions import SiteBlocked, WebsiteError


DETAIL = '<html><div id="rightcolumn"><div id="video_info"></div></div></html>'
SEARCH = '<html><div id="rightcolumn"></div><form action="vl_searchbyid.php"></form></html>'
PATH = '/cn/vl_searchbyid.php?keyword=TEST-001'


def response(url, body=DETAIL, status=200, **headers):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode()
    result.encoding = 'utf-8'
    result.headers.update(headers)
    return result


def client(**overrides):
    settings = dict(mode='direct', base_url=None, discover_mirror=False,
                    minimum_interval=0, cooldown=300)
    settings.update(overrides)
    return net.JavlibClient(SimpleNamespace(**settings), net.PERMANENT_URL,
                            'http://user:secret@127.0.0.1:11080')


@pytest.mark.parametrize('bio,expected', [
    ('== c99i ==', 'https://www.c99i.com'),
    ('== abc123 ==', 'https://www.abc123.com'),
    ('Mirror: https://www.example.com/cn/', 'https://www.example.com'),
    ('Site unavailable', ''), (None, ''), ('', ''),
])
def test_official_bio_format(bio, expected):
    assert net.mirror_from_bio(bio) == expected


@pytest.mark.parametrize('body,status,headers,valid', [
    (DETAIL, 200, {}, True), (SEARCH, 200, {}, True),
    ('<title>Just a moment...</title>', 200, {}, False),
    (DETAIL, 200, {'cf-mitigated': 'challenge'}, False),
    ('<html>Parked domain</html>', 200, {}, False),
    ('', 200, {}, False), (DETAIL, 429, {}, False), (DETAIL, 503, {}, False),
])
def test_validates_content_and_status(body, status, headers, valid):
    assert bool(net.response_problem(response(net.PERMANENT_URL, body, status, **headers))) != valid


def test_direct_disables_environment_proxy(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9999')
    item = client()
    direct = item.session(net.PERMANENT_URL, 'direct')
    assert direct.trust_env is False
    assert direct.merge_environment_settings(net.PERMANENT_URL, {}, False, True, None)['proxies'] == {}
    proxy = item.session(net.PERMANENT_URL, 'proxy')
    assert proxy.proxies['https'] == item.proxy_server
    assert direct is item.session(net.PERMANENT_URL, 'direct')
    assert direct is not proxy
    direct.cookies.set('test-cookie', 'direct-only')
    assert proxy.cookies.get('test-cookie') is None
    item.close()


def test_primary_is_probed_in_direct_mode(monkeypatch):
    item = client()
    fetch = Mock(return_value=response(net.PERMANENT_URL + PATH))
    monkeypatch.setattr(item, 'fetch', fetch)
    assert item.get(PATH).status_code == 200
    fetch.assert_called_once_with(net.PERMANENT_URL, 'direct', PATH)


def test_stale_mirror_discovery_and_pinned_route(monkeypatch):
    item = client(mode='auto', discover_mirror=True)
    item.configured_url = 'https://old.example.com'
    monkeypatch.setattr(item, 'discover', lambda: 'https://new.example.com')
    calls = []

    def fetch(base, route, path):
        calls.append((base, route))
        if 'old.' in base:
            raise requests.ConnectionError('proxy with credentials must not be logged')
        return response(base + path)

    monkeypatch.setattr(item, 'fetch', fetch)
    item.get(PATH)
    item.get(PATH)
    assert calls == [('https://old.example.com', 'direct'),
                     ('https://new.example.com', 'direct'), ('https://new.example.com', 'direct')]


def test_auto_can_use_configured_proxy(monkeypatch):
    item = client(mode='auto')
    monkeypatch.setattr(item, 'fetch', lambda base, route, path:
                        response(base + path, status=403 if route == 'direct' else 200))
    item.get(PATH)
    assert item.selected == (net.PERMANENT_URL, 'proxy')


def test_challenge_is_not_retried_for_each_movie(monkeypatch):
    item = client()
    fetch = Mock(return_value=response(net.PERMANENT_URL + PATH, '<title>Just a moment...</title>'))
    monkeypatch.setattr(item, 'fetch', fetch)
    with pytest.raises(SiteBlocked, match='Cloudflare'):
        item.get(PATH)
    with pytest.raises(SiteBlocked, match='冷却中'):
        item.get(PATH)
    assert fetch.call_count == 1


def test_rate_limit_honors_retry_after_without_route_rotation(monkeypatch):
    item = client(mode='auto')
    monkeypatch.setattr(net.time, 'monotonic', lambda: 100)
    fetch = Mock(return_value=response(net.PERMANENT_URL + PATH, status=429, **{'Retry-After': '600'}))
    monkeypatch.setattr(item, 'fetch', fetch)
    with pytest.raises(SiteBlocked, match='429'):
        item.get(PATH)
    assert item.blocked_until == 700
    assert fetch.call_count == 1


def test_selected_route_losing_clearance_enters_cooldown(monkeypatch):
    item = client()
    item.selected = (net.PERMANENT_URL, 'direct')
    monkeypatch.setattr(item, 'fetch', Mock(return_value=response(net.PERMANENT_URL + PATH, status=403)))
    with pytest.raises(SiteBlocked, match='403'):
        item.get(PATH)
    assert item.selected is None
    assert item.candidates is None


def test_cooldown_expiry_rediscovers(monkeypatch):
    item = client(discover_mirror=True)
    item.block()
    monkeypatch.setattr(net.time, 'monotonic', lambda: item.blocked_until + 1)
    discover = Mock(return_value='https://new.example.com')
    monkeypatch.setattr(item, 'discover', discover)
    monkeypatch.setattr(item, 'fetch', lambda base, route, path: response(base + path))
    item.get(PATH)
    discover.assert_called_once()


def test_spacing_is_shared_across_routes(monkeypatch):
    item = client(minimum_interval=2)
    now = [0]
    monkeypatch.setattr(net.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(net.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    fake = Mock()
    fake.get.return_value = response(net.PERMANENT_URL + PATH)
    monkeypatch.setattr(item, 'session', lambda *args: fake)
    item.fetch(net.PERMANENT_URL, 'direct', PATH)
    item.fetch(net.PERMANENT_URL, 'proxy', PATH)
    assert now[0] == 2


def test_external_search_link_rejected():
    item = client()
    item.selected = (net.PERMANENT_URL, 'direct')
    with pytest.raises(WebsiteError, match='外部链接'):
        item.get('https://external.example/cn/')


def test_proxy_mode_requires_proxy():
    item = client(mode='proxy')
    item.proxy_server = None
    with pytest.raises(WebsiteError, match='proxy_server'):
        item.routes()


def test_network_errors_do_not_expose_proxy_credentials(monkeypatch):
    item = client()
    monkeypatch.setattr(item, 'fetch', Mock(side_effect=requests.ConnectionError(item.proxy_server)))
    with pytest.raises(SiteBlocked) as error:
        item.get(PATH)
    assert 'secret' not in str(error.value)


def test_retry_after_http_date():
    assert net.retry_after_seconds('Thu, 01 Jan 2099 00:00:00 GMT') > 0
    assert net.retry_after_seconds('Thu, 01 Jan 1970 00:00:00 GMT') == 0
    assert net.retry_after_seconds('invalid') == 0


def test_discovery_uses_json_api(monkeypatch):
    item = client()
    fake = Mock()
    fake.get.return_value.json.return_value = {'bio': '== c99i =='}
    monkeypatch.setattr(item, 'session', lambda *args: fake)
    assert item.discover() == 'https://www.c99i.com'
    fake.get.assert_called_once_with(net.MIRROR_API, timeout=5)


def test_probe_budget_stops_additional_candidates(monkeypatch):
    item = client(mode='auto')
    now = [0]
    monkeypatch.setattr(net.time, 'monotonic', lambda: now[0])

    def fetch(*args):
        now[0] += item.probe_budget
        raise requests.ConnectionError()

    mock = Mock(side_effect=fetch)
    monkeypatch.setattr(item, 'fetch', mock)
    with pytest.raises(SiteBlocked, match='budget exhausted'):
        item.get(PATH)
    assert mock.call_count == 1


def test_redirect_to_unknown_host_is_not_promoted(monkeypatch):
    item = client()
    fake = Mock()
    fake.get.return_value = response('https://unrelated.example/cn/')
    monkeypatch.setattr(item, 'session', lambda *args: fake)
    with pytest.raises(WebsiteError, match='其他域名'):
        item.fetch(net.PERMANENT_URL, 'direct', PATH)
