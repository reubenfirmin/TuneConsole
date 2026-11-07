"""First-listening summaries derive from evidence rather than writable mixed keys."""
import pytest

from tests.conftest import seed_catalog_track


@pytest.fixture
def listener(store):
    store.accounts.upsert_account('me','c',None,True)
    from yt_playlist.core import listening
    with store.conn:
        listening.calendar(store.conn,1,'UTC')
    return store


def track(store,key,artist):
    seed_catalog_track(store,key,'T'+key,artist,video_id='v'+key)


def observe(store,day,keys):
    store.history.record_history_plays(1,day*86400,keys,source_timezone='UTC')


def names(store):
    labels = store.trends.artist_labels()
    return {labels[aid]:day for aid,day in store.trends.first_play_map('artist').items()}


def test_first_play_combines_precision_without_fabricating_exact_time(listener):
    s = listener
    for key,artist in [('k1','A1'),('k2','A1'),('k3','A2'),('k4','A3')]:
        track(s,key,artist)
    observe(s,2,['k1','k3'])
    observe(s,5,['k1','k2'])
    observe(s,9,['k3','k4'])
    s.history.import_play_events(1,[('k1',None,86400),('k4',None,8*86400)])
    s.trends.refresh_first_play()
    assert s.trends.first_play_map('track')=={'k1':1,'k2':5,'k3':2,'k4':8}
    assert names(s)=={'A1':1,'A2':2,'A3':8}
    coarse_only = s.conn.execute('SELECT first_exact_play_at,first_observed_day FROM recording_listening_summary '
        'WHERE recording_id=(SELECT id FROM recordings WHERE identity_key=?)',('k2',)).fetchone()
    assert tuple(coarse_only)==(None,'1970-01-06')


def test_late_evidence_rebuild_can_move_first_day_in_both_directions(listener):
    s = listener
    track(s,'k1','A1')
    s.history.import_play_events(1,[('k1',None,5*86400)],source='live')
    s.trends.refresh_first_play()
    assert s.trends.first_play_map('track')=={'k1':5}
    s.history.import_play_events(1,[('k1',None,2*86400)],source='takeout')
    assert not s.trends.first_play_map('track')
    s.trends.refresh_first_play()
    assert names(s)=={'A1':2}
    from yt_playlist.core import listening
    with s.conn:
        listening.remove_source(s.conn,'takeout',1)
    s.trends.refresh_first_play()
    assert names(s)=={'A1':5}


def test_discard_and_deterministic_rebuild_preserve_evidence(listener):
    s = listener
    track(s,'k1','A1')
    track(s,'k2','A2')
    observe(s,3,['k1'])
    observe(s,6,['k2'])
    s.trends.refresh_first_play()
    before = {t:list(map(tuple,s.conn.execute('SELECT * FROM '+t+' ORDER BY 1,2'))) for t in
        ('recording_listening_summary','artist_listening_summary')}
    evidence = list(map(tuple,s.conn.execute('SELECT * FROM listening_observations')))
    with s.conn:
        for table in before:
            s.conn.execute('DELETE FROM '+table)
    s.trends.refresh_first_play()
    assert {t:list(map(tuple,s.conn.execute('SELECT * FROM '+t+' ORDER BY 1,2'))) for t in before}==before
    assert list(map(tuple,s.conn.execute('SELECT * FROM listening_observations')))==evidence
    s.trends.clear_first_play()
    assert not s.trends.first_play_map('track')
    s.trends.refresh_first_play()
    assert names(s)=={'A1':3,'A2':6}


def test_old_checkpoint_cannot_hide_earlier_observations(listener):
    s = listener
    observe(s,2,['k1','k2'])
    observe(s,5,['k3'])
    s.settings.set_setting('trend_first_play_watermark','999')
    observe(s,1,['k1'])
    s.trends.refresh_first_play()
    assert s.trends.first_play_map('track')=={'k1':1,'k2':2,'k3':5}
    assert s.settings.get_setting('trend_first_play_watermark') is None


def test_summary_read_projection_cannot_become_an_independent_evidence_writer(listener):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError,match='view'):
        listener.conn.execute("INSERT INTO trend_first_play VALUES ('artist','Name',1,86400,'invented')")
