"""TV reporting stays playable across committed replacements without a transaction."""
import sqlite3

import pytest

from tests.conftest import FakeClient
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, native_sql_calls, without_query_timing,
)
from tests.test_tv_stream import install_catalog_inputs, video
from yt_playlist.core.store import Store
from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.rec import tv_artwork
from yt_playlist.controllers.playlist_recipes import PlaylistRecipes
from yt_playlist.util.matching import search_squash
from yt_playlist.web.routes.home_tv import playlist_items


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture(autouse=True)
def tv_read_inputs(monkeypatch, native_sql_calls, without_query_timing):
    # These synchronous readers own committed membership/artwork visibility.
    # Genre discovery, query diagnostics and thread admission have other owners.
    bound_catalog(monkeypatch, entries=())


@pytest.mark.parametrize('file_backed', [False, True], ids=['memory', 'wal'])
@pytest.mark.parametrize('artwork', [False, True], ids=['playback', 'display'])
def test_playlist_response_stays_consistent_during_replacement_and_refreshes_next_read(
        tmp_path, _pristine_current_schema, monkeypatch, file_backed, artwork):
    source = Store(tmp_path / 'tv.db' if file_backed else ':memory:')
    try:
        source.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(source, _pristine_current_schema)
        install_catalog_inputs(source, monkeypatch)
        account = source.accounts.upsert_account('TV account', 'cred', None, True)
        videos = [video(index) for index in range(11)]
        client = FakeClient(catalog={item['video_id']: {
            'videoId': item['video_id'], 'title': item['title'],
            'artists': [{'name': item['artist']}]} for item in videos})
        recipe = {'lane': 'tv', 'media': 'video', 'tv_stream': 'live', 'videos': videos[:7]}
        created = create_generated_playlist(source, 'TV', videos[:7], client, 1, account, recipe=recipe)
        playlist = source.playlists.get_playlist(created['pid'])

        def photos(version):
            return {search_squash(item['artist']): {
                'images': [f'https://images.test/{version}/{item["video_id"]}.jpg'],
                'retry_at': 86401} for item in videos}

        source.rec_proposals.put_proposals(tv_artwork.SURFACE, photos('before'), 1)
        read_recipe = PlaylistRecipes.get_recipe
        replaced = False

        def replace_after_recipe(repo, scope):
            nonlocal replaced
            result = read_recipe(repo, scope)
            if not replaced:
                replaced = True
                # Reporting allows later committed membership/artwork. Optional
                # recipe metadata must never be attributed to another video.
                source.playlists.publish_confirmed_tracks(playlist.id, 'Next TV', videos[7:], 2)
                source.playlist_recipes.set_recipe(scope, {**recipe, 'videos': videos[7:]}, 2)
                source.rec_proposals.put_proposals(tv_artwork.SURFACE, photos('after'), 2)
            return result

        monkeypatch.setattr(PlaylistRecipes, 'get_recipe', replace_after_recipe)
        make_reader = source._make_reader
        readers = []

        def open_reader():
            reader = make_reader()
            assert not reader.conn.in_transaction
            readers.append(reader)
            return reader

        monkeypatch.setattr(source, '_make_reader', open_reader)
        before_playlist, before = playlist_items(source, playlist.id, artwork=artwork)
        assert replaced
        assert before_playlist.title == playlist.title
        visible = videos[7:] if file_backed else videos[:7]
        assert [item['video_id'] for item in before] == [item['video_id'] for item in visible]
        if file_backed:
            assert all('source_key' not in item for item in before)
        else:
            assert [item['source_key'] for item in before] == [item['source_key'] for item in visible]
        assert all(item['title'] and item['artist'] and item['duration'] > 0 for item in before)
        if artwork:
            version = 'after' if file_backed else 'before'
            assert all(item['artist_art'] == [f'https://images.test/{version}/{item["video_id"]}.jpg']
                       and not item['artist_art_pending'] for item in before)
            before[0]['artist_art'].append('https://images.test/detached.jpg')
        else:
            assert all('artist_art' not in item for item in before)

        after_playlist, after = playlist_items(source, playlist.id, artwork=artwork)
        assert after_playlist.title == 'Next TV'
        assert [item['video_id'] for item in after] == [item['video_id'] for item in videos[7:]]
        assert [item['source_key'] for item in after] == [item['source_key'] for item in videos[7:]]
        if artwork:
            assert all(item['artist_art'] == [f'https://images.test/after/{item["video_id"]}.jpg']
                       and not item['artist_art_pending'] for item in after)
        else:
            assert all('artist_art' not in item for item in after)

        assert len(readers) == 2
        for reader in readers:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                reader.conn.execute('SELECT 1')
    finally:
        source.conn.close()
