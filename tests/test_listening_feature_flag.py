"""The listening preview defaults on for feedback and can be disabled explicitly."""
import json

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core.features import listening_charts_enabled
from yt_playlist.rec import maintenance
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('value,enabled', [(None,True),('0',False),('true',False),('1',True)])
def test_flag_defaults_on_and_respects_explicit_values(monkeypatch, value, enabled):
    monkeypatch.delenv('YT_PLAYLIST_LISTENING_CHARTS', raising=False)
    if value is not None:
        monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', value)
    assert listening_charts_enabled() is enabled


def test_disabled_routes_assets_and_cached_tabs_are_hidden(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','0')
    ListeningAnalyticsRepo(store).publish(json.dumps({'available':True}), 1)
    ListeningAnalyticsRepo(store).publish(json.dumps({'available':True,'stories':[]}),1,'listening_edition')
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        charts = client.get('/charts')
        assert charts.status_code == 200
        assert 'Listening analytics</button>' not in charts.text
        assert 'listening-analytics.js' not in charts.text
        assert 'listening-editorial.js' not in charts.text
        assert "choose('home')" not in charts.text
        assert 'data-listening-analytics' not in charts.text
        assert 'listening-editorial.js' not in client.get('/').text
        assert client.get('/charts?view=analytics', follow_redirects=False).status_code == 303
        assert client.get('/charts/listening-data').status_code == 404
        assert client.get('/charts/listening-chunk/' + 'a'*24).status_code == 404
        assert client.get('/charts?view=home',follow_redirects=False).status_code == 303
        assert client.post('/charts/listening-impressions', json={}).status_code == 404
        assert client.post('/charts/listening-timezone',json={'timezone':'UTC'}).status_code == 404


def test_enabled_analytics_is_the_last_chart_tab(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', '1')
    ListeningAnalyticsRepo(store).publish(json.dumps({'available':True}), 1)
    with TestClient(create_app(store, lambda: {})) as client:
        charts = client.get('/charts').text
        assert charts.index('Playlists</button>') < charts.index('Listening analytics</button>')
        assert 'listening-analytics.js' in charts
        assert client.get('/charts?view=analytics').status_code == 200
        assert client.get('/charts/listening-data').status_code == 200


def test_editorial_is_the_charts_home_and_never_an_application_home_tab(store,monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','1')
    ListeningAnalyticsRepo(store).publish(json.dumps({'available':True,'id':'edition','stories':[]}),1,'listening_edition')
    with TestClient(create_app(store,lambda:{})) as client:
        charts=client.get('/charts').text
        assert "view: 'home'" in charts
        assert charts.index("choose('home')")<charts.index('Top songs</button>')
        assert 'data-listening-edition' in charts
        assert 'listening-editorial.js' in charts
        home=client.get('/').text
        assert 'data-listening-edition' not in home and 'listening-editorial.js' not in home
        assert 'data-dash="portrait"' not in home


def test_no_editorial_evidence_hides_the_charts_home_tab(store,monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','1')
    with TestClient(create_app(store,lambda:{})) as client:
        charts=client.get('/charts').text
        assert "view: 'songs'" in charts
        assert "choose('home')" not in charts
        assert 'data-listening-edition' not in charts


def test_disabled_feature_does_not_schedule_analytics(store, monkeypatch):
    import logging
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','0')
    jobs = []
    projections = maintenance.Projections(store, lambda: 1, logging.getLogger(__name__),
        accepting=lambda: True, schedule=lambda pending: jobs.append(pending) or True,
        execute=lambda *args: None)
    projections.poll()
    assert jobs and 'analytics' not in jobs[0]
