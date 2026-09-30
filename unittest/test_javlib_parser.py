from unittest.mock import Mock

import pytest
import requests

from javsp.config import Cfg, JavlibNetwork
from javsp.datatype import MovieInfo
from javsp.web import javlib
from javsp.web.exceptions import MovieDuplicateError, MovieNotFoundError, WebsiteError


BASE = 'https://mirror.example.com'
DETAIL = '''<html><body><div><div id="rightcolumn">
<div><h3><a>TEST-001 Sample title</a></h3></div>
<img id="video_jacket_img" src="//images.example.com/cover.jpg">
<div id="video_info">
<div id="video_id"><td class="text">TEST-001</td></div>
<div id="video_date"><td class="text">2024-01-01</td></div>
<div id="video_length"><span class="text">100</span></div>
<span class="maker"><a>Example studio</a></span>
</div></div></div></body></html>'''


def response(body, url=BASE + '/cn/sample.html', redirected=False):
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result.encoding = 'utf-8'
    result._content = body.encode()
    result.history = [Mock()] if redirected else []
    return result


@pytest.mark.parametrize('redirected', [True, False])
def test_detail_page_with_or_without_redirect(monkeypatch, redirected):
    get = Mock(return_value=response(DETAIL, redirected=redirected))
    monkeypatch.setattr(javlib.request, 'get', get)
    movie = MovieInfo('TEST-001')
    javlib.parse_data(movie)
    assert movie.dvdid == 'TEST-001'
    assert movie.title == 'Sample title'
    assert movie.cover == 'https://images.example.com/cover.jpg'
    assert movie.url == 'https://www.javlibrary.com/cn/sample.html'
    assert get.call_count == 1


def test_search_page_after_redirect_is_still_searched(monkeypatch):
    search = '<div class="video" id="one"><a href="sample.html"><div class="id">TEST-001</div></a></div>'
    get = Mock(side_effect=[response(search, BASE + '/cn/search', True), response(DETAIL)])
    monkeypatch.setattr(javlib.request, 'get', get)
    javlib.parse_data(MovieInfo('TEST-001'))
    assert get.call_args.args == (BASE + '/cn/sample.html',)


def test_no_results_and_duplicates_are_distinct(monkeypatch):
    monkeypatch.setattr(javlib.request, 'get', Mock(return_value=response('<div>No results</div>')))
    with pytest.raises(MovieNotFoundError):
        javlib.parse_data(MovieInfo('TEST-001'))
    video = '<div class="video" id="one"><a href="sample.html"><div class="id">TEST-001</div></a></div>'
    monkeypatch.setattr(javlib.request, 'get', Mock(return_value=response(video * 2)))
    with pytest.raises(MovieDuplicateError):
        javlib.parse_data(MovieInfo('TEST-001'))


def test_non_detail_search_destination_has_explicit_error(monkeypatch):
    video = '<div class="video" id="one"><a href="sample.html"><div class="id">TEST-001</div></a></div>'
    monkeypatch.setattr(javlib.request, 'get', Mock(side_effect=[response(video), response('<div>Empty</div>')]))
    with pytest.raises(WebsiteError, match='详情页'):
        javlib.parse_data(MovieInfo('TEST-001'))


def test_existing_config_can_omit_javlib_settings():
    data = Cfg().network.model_dump()
    data.pop('javlib', None)
    config = type(Cfg().network).model_validate(data)
    assert config.javlib.mode == 'auto'


@pytest.mark.parametrize('settings', [{'mode': 'unknown'}, {'minimum_interval': -1}, {'cooldown': 0}])
def test_invalid_javlib_config_rejected(settings):
    with pytest.raises(ValueError):
        JavlibNetwork(**settings)
