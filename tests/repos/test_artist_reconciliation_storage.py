"""Exercise the extracted reconciliation against real storage boundaries."""
import json

import pytest

from yt_playlist.core import legacy_evidence
from yt_playlist.core.store import Store


def identities(store):
    old = store.artists.ensure_artist('Artist')
    target = store.artists.ensure_artist('Artist', provider='youtube', provider_id='channel')
    assert old != target
    return old, target


def test_controller_reconciliation_merges_discovery_and_similarity_before_redirect(store):
    old, target = identities(store)
    for aid, when in ((old, 10), (target, 20)):
        store.discovery.mark_scanned(aid, when)
        store.discovery.upsert_discovered_artist('Artist', when, ['captured'], [], None, when, artist_id=aid)
        store.discovery.record_discovery_impressions('artist', [aid], 'same-view', when)
        store.artist_similarity.cache_similar(aid, [['Neighbor', when]], when)
    assert store.artists.ensure_artist('Artist', provider='youtube', provider_id='channel', previous=old) == target
    assert store.artists.resolve(old) == target
    assert store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:] == (target, 20)
    candidates = store.discovery.get_discovered_artists()
    assert len(candidates) == 1 and candidates[0]['artist_id'] == target
    assert candidates[0]['offered_count'] == 1 and candidates[0]['found_at'] == 10
    assert candidates[0]['because'] == []
    assert store.artist_similarity.cached_similar(target, 30) == [['Neighbor', 20]]
    for aid in (old, target):
        raw = legacy_evidence.original(store.conn, 'artist', 'artist_reconcile:discovered_artists',
                                       json.dumps([old, target, aid]))
        assert json.loads(raw)['explanation']['because'] == ['captured']


def test_reconciliation_retry_keeps_completed_feature_merge_without_double_counting(store, monkeypatch):
    old, target = identities(store)
    for aid, when in ((old, 10), (target, 20)):
        store.discovery.upsert_discovered_artist('Artist', 1, [], [], None, when, artist_id=aid)
        store.discovery.record_discovery_impressions('artist', [aid], 'same-view', when)
    original = store.artist_state.reconcile_artist
    def interrupted(*args, **kwargs):
        assert not store.conn.in_transaction
        raise RuntimeError('interrupted similarity update')
    monkeypatch.setattr(store.artist_state, 'reconcile_artist', interrupted)
    with pytest.raises(RuntimeError, match='interrupted similarity'):
        store.artists.reconcile_provisional(old, target, source='youtube', now=30)
    assert store.artists.resolve(old) == old
    assert store.discovery.get_discovered_artists()[0]['offered_count'] == 1
    monkeypatch.setattr(store.artist_state, 'reconcile_artist', original)
    assert store.artists.reconcile_provisional(old, target, source='youtube', now=30) == target
    assert store.artists.resolve(old) == target
    assert store.discovery.get_discovered_artists()[0]['offered_count'] == 1


def test_retained_original_is_readable_and_insert_once(store):
    old, _ = identities(store)
    payload = json.dumps({'artist_id': old, 'exact': ['captured']})
    store.originals.retain('artist', 'artist_reconcile:sample', 'sample', payload, 'identity evidence', now=10)
    store.originals.retain('artist', 'artist_reconcile:sample', 'sample', 'changed', 'new input', now=20)
    assert legacy_evidence.original(store.conn, 'artist', 'artist_reconcile:sample', 'sample') == payload


@pytest.mark.parametrize('version', [79, 93, 102, 107])
def test_reconciliation_and_retained_originals_keep_installed_formats_writable(tmp_path, _admitted_installed_schema, version):
    template = _admitted_installed_schema(version)
    store = Store(tmp_path / 'installed.db')
    try:
        template.restore(store)
        store._bind_repositories()
        old, target = identities(store)
        for aid, when in ((old, 10), (target, 20)):
            store.discovery.mark_scanned(aid, when)
            store.discovery.upsert_discovered_artist('Artist', 1, [], [], None, when, artist_id=aid)
            store.discovery.record_discovery_impressions('artist', [aid], 'shared-view', when)
            store.artist_similarity.cache_similar(aid, [['Neighbor', when]], when)
        assert store.artists.reconcile_provisional(old, target, source='youtube') == target
        assert store.artists.resolve(old) == target
        candidate, = store.discovery.get_discovered_artists()
        assert candidate['offered_count'] == 1 and candidate['found_at'] == 10
        assert store.artist_similarity.cached_similar(target, 30) == [['Neighbor', 20]]
    finally:
        store.conn.close()


def test_artist_origin_collision_rebinds_sources_and_retains_their_originals(store):
    old, target = identities(store)
    entry = store.tracks.upsert_track('v', 'Song', 'Artist')
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    with store.conn.atomic():
        old_origin = store.conn.execute("INSERT INTO discovery_origins(kind,artist_id,resolution_status) VALUES ('artist',?,'resolved')", (old,)).lastrowid
        new_origin = store.conn.execute("INSERT INTO discovery_origins(kind,artist_id,resolution_status) VALUES ('artist',?,'resolved')", (target,)).lastrowid
        store.conn.executemany('INSERT INTO discovery_artist_sources VALUES (?,?,?)',
            [(old, old_origin, .8), (target, old_origin, .2), (target, new_origin, .3)])
        store.conn.executemany('INSERT INTO discovery_sources(recording_id,origin_id,input_video_id,first_found_at,last_found_at) VALUES (?,?,?,?,?)',
            [(recording, old_origin, 'v', 10, 15), (recording, new_origin, 'v', 12, 20)])
    store.artists.reconcile_provisional(old, target, source='youtube')
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM discovery_artist_sources')] == [(target, new_origin, .8)]
    assert store.conn.execute('SELECT first_found_at,last_found_at FROM discovery_sources').fetchone()[:] == (10, 20)
    assert store.conn.execute('SELECT id FROM discovery_origins').fetchone()[0] == new_origin
    raw = legacy_evidence.original(store.conn, 'discovery', 'discovery_artist_origin_merge',
                                   json.dumps([target, old_origin, new_origin]))
    assert json.loads(raw) == dict(artist_id=target, origin_id=old_origin, score=.8)


@pytest.mark.parametrize('feature', ['discovery', 'artist_state'])
def test_release_credit_reconciliation_can_resume_after_an_independent_feature_write(store, monkeypatch, feature):
    old, target = identities(store)
    store.collection.add_saved_album({'browse': 'B', 'title': 'Album', 'artist': 'Artist'})
    assert store.artists.album_credits('B')[0]['artist_id'] == old
    store.discovery.upsert_discovered_artist('Artist', 1, [], [], None, 10, artist_id=old)
    owner = getattr(store, feature)
    original = owner.reconcile_artist
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        assert not store.conn.in_transaction
        raise RuntimeError('interrupted feature')
    monkeypatch.setattr(owner, 'reconcile_artist', interrupted)
    with pytest.raises(RuntimeError, match='interrupted feature'):
        store.collection.cache_album_tracklist('B', 'Album', 'Artist', None, [], credits=[{'name': 'Artist', 'id': 'channel'}])
    assert store.artists.resolve(old) == old
    assert store.artists.album_credits('B')[0]['artist_id'] == old
    assert store.collection.cached_album_tracklist_ids() == set()
    monkeypatch.setattr(owner, 'reconcile_artist', original)
    store.collection.cache_album_tracklist('B', 'Album', 'Artist', None, [], credits=[{'name': 'Artist', 'id': 'channel'}])
    assert store.artists.resolve(old) == target
    assert store.artists.album_credits('B')[0]['artist_id'] == target
