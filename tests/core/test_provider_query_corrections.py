"""Correcting one query changes admission without discarding provider evidence."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import metadata
from yt_playlist.core.store import Store


@pytest.mark.parametrize('version', [79, 94, 107])
@pytest.mark.parametrize('correction', ['choice', 'selection', 'reset'])
def test_changed_query_resumes_its_retry_across_installed_formats(
        tmp_path, _pristine_installed_schema, version, correction):
    path = tmp_path / 'query.db'
    with closing(sqlite3.connect(path)) as copied:
        _pristine_installed_schema(version).backup(copied)
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        entry = store.tracks.upsert_track('query', 'Song', 'Original artist', None, 200)
        other = store.tracks.upsert_track('other', 'Other', 'Artist', None, 200)
        if correction == 'reset':
            store.tracks.set_track_artist(entry, 'Mistaken artist')
        track = store.tracks.track_for_waterfall(entry)
        other_track = store.tracks.track_for_waterfall(other)
        for item, provider in ((track, 'lastfm'), (track, 'deezer'), (other_track, 'lastfm')):
            store.enrichment_state.record_enrichment_attempt(item['identity_key'], provider,
                {'genre': 'Rock'}, retry=True, video_id=item['video_id'], now=1000,
                retry_after=5000, error='controlled prior failure')
        prior_other = store.enrichment_state.enrichment_provider_state(other_track['recording_id'])
        prior = store.enrichment_state.enrichment_provider_state(track['recording_id'])
        # Repeating the same observation cannot bypass a genuine query retry.
        if correction == 'selection':
            with store.conn:
                metadata.select(store.conn, entry, 'artist', track['artist'], 'youtube')
        else:
            store.tracks.set_track_artist(entry, track['artist'])
        assert store.enrichment_state.enrichment_provider_state(track['recording_id']) == prior
        if correction == 'choice':
            store.tracks.set_track_artist(entry, 'Corrected artist')
        elif correction == 'selection':
            with store.conn:
                metadata.select(store.conn, entry, 'artist', 'Corrected artist', 'youtube')
        else:
            store.tracks.reset_track_artist(entry)
        current = store.enrichment_state.enrichment_provider_state(track['recording_id'])
        assert current['lastfm']['retry_at'] == current['lastfm']['failures'] == 0
        assert current['lastfm']['fields'] == prior['lastfm']['fields'] == {'genre': 'Rock'}
        assert current['lastfm']['status'] == 'retry'
        assert current['deezer'] == prior['deezer']
        assert store.enrichment_state.enrichment_provider_state(other_track['recording_id']) == prior_other
        assert [row['id'] for row in store.enrichment_queue.next_enrich_batch(
            10, providers=['lastfm'], now=1001)] == [entry]
    finally:
        store.conn.close()
