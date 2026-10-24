"""Album page: renders the track table, and creates a playlist from the album."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from yt_playlist.rec import choice_evidence


def test_album_choices_serve_independently_of_missing_mix_preparation(store, monkeypatch):
    from yt_playlist.rec import album_choices, home_prepared
    c, _fc, _iid = _client(store)
    store.rec_proposals.put_proposals(album_choices.SURFACE, {
        'built_at': 1, 'context': {'at': 1},
        'items': [{'browse_id': 'MPREb_x', 'title': 'The Album', 'artist': 'Artist X',
                   'year': '2021', 'genre': 'Jazz', 'score': .9}]}, 1)

    def forbidden(*args, **kwargs):
        raise AssertionError('Album serving must not rank or wait for Mix tapes')

    monkeypatch.setattr(album_choices, 'prepare', forbidden)
    monkeypatch.setattr(home_prepared, 'load', forbidden)
    response = c.get('/home/album-choices')
    assert response.status_code == 200
    assert 'aria-label="Play The Album"' in response.text
    assert 'class="gtag">Jazz</span>' in response.text
    assert 'album-familiarity--unknown' in response.text and 'role="meter"' not in response.text
    assert 'hx-trigger' not in response.text.split('>', 1)[0]
    assert len(store.rec_ledger.evidence().records('opportunity')) == 1


def test_album_choices_waiting_fragment_refreshes_until_worker_publishes(store):
    from yt_playlist.rec import album_choices
    c, _fc, _iid = _client(store)
    waiting = c.get('/home/album-choices')
    assert waiting.status_code == 200
    assert 'hx-get="/home/album-choices" hx-trigger="every 3s"' in waiting.text
    store.rec_proposals.put_proposals(album_choices.SURFACE, {
        'built_at': 1, 'context': {},
        'items': [{'browse_id': 'MPREb_x', 'title': 'The Album', 'artist': 'Artist X'}]}, 1)
    ready = c.get('/home/album-choices')
    assert 'The Album' in ready.text
    assert 'hx-get="/home/album-choices" hx-trigger="every 3s"' not in ready.text


def test_refresh_excludes_the_held_four_without_editing_their_ledger(store):
    from tests.rec.test_album_menu import records
    c, _fc, _iid = _client(store)
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1, 'context': {}, 'items': records()}, 1)
    c.get('/home/album-choices')
    original, = store.rec_ledger.evidence().records('opportunity')
    response = c.post('/home/album-choices/refresh', data={'opportunity_id': original['id']})
    assert response.status_code == 200
    newer = [row for row in store.rec_ledger.evidence().records('opportunity') if row['id'] != original['id']]
    assert len(newer) == 1 and len(newer[0]['options']) == 4
    assert not {row['browse_id'] for row in original['options']} & {row['browse_id'] for row in newer[0]['options']}
    assert store.rec_ledger.evidence().get('opportunity', original['id']) == original
    c.get('/home/album-choices')
    assert len(store.rec_ledger.evidence().records('opportunity')) == 2


def test_album_controls_capture_policy_and_surface_effective_values_in_taste(store):
    from tests.rec.test_album_menu import records
    from yt_playlist.rec import album_tuning
    c, _fc, _iid = _client(store)
    for album in records()[:6]:
        store.collection.add_saved_album({**album, 'browse': album['browse_id']})
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1, 'context': {'at': 1},
                                                       'items': records()}, 1)
    before = c.get('/home/album-choices')
    assert 'Four albums. Take one' not in before.text
    assert 'Refresh selection' in before.text
    assert 'Use these settings for 4 hours.' in before.text
    assert 'Another set' not in before.text and 'Pick a record' not in before.text
    response = c.post('/home/album-choices/tune', data={
        'novelty': '100', 'genre': 'jazz'})
    assert response.status_code == 200
    assert album_tuning.state(store, 1)['genres'] == ['jazz']
    offers = store.rec_ledger.evidence().records('opportunity')
    offer = next(row for row in offers if all(item['genre'] == 'Jazz' for item in row['options']))
    context = choice_evidence.resolve_context(store, {'artifact': offer['context']})
    assert context['album_menu']['controls'] == {'novelty': 1}
    assert context['album_menu']['presentation'] == 'equal_album_stories_v2'
    assert context['album_menu']['artist_cap'] == 1
    assert context['album_menu']['discovery_requested'] == 4
    assert context['album_menu']['discovery_offered'] == 3
    assert context['album_menu']['coverage_gap'] == 1
    count = len(offers)
    c.get('/home/album-choices')
    assert len(store.rec_ledger.evidence().records('opportunity')) == count
    taste = c.get('/taste/panels/recommendations')
    assert 'Effective in Albums: 100% · temporary' in taste.text
    assert 'Current genre filter: Jazz.' in taste.text
    assert taste.text.count('id="param-album_menu_novelty"') == 1
    assert 'One mood' not in response.text and 'Back catalog' not in response.text
    assert c.post('/taste/reset-album-tuning').headers['hx-refresh'] == 'true'
    assert not album_tuning.state(store, 1)['remaining']
    assert c.post('/home/album-choices/tune', data={
        'novelty': 'nan'}).status_code == 422


def test_new_album_state_issues_current_policy_without_rewriting_held_offers(store):
    from tests.rec.test_album_menu import records
    from yt_playlist.rec import album_choices
    c, _fc, _iid = _client(store)
    prepared = records()[6:10]
    target = prepared[0]
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1, 'context': {}, 'items': prepared}, 1)
    assert c.get('/home/album-choices').status_code == 200
    original, = store.rec_ledger.evidence().records('opportunity')
    assert all(item['classification'] == 'discovery' for item in original['options'])
    album_choices.set_feedback(store, target['browse_id'], 'known', 2)
    assert c.get('/home/album-choices').status_code == 200
    changed = next(row for row in store.rec_ledger.evidence().records('opportunity') if row['id'] != original['id'])
    item = next(item for item in changed['options'] if item['browse_id'] == target['browse_id'])
    assert item['classification'] == 'known'
    context = choice_evidence.resolve_context(store, {'artifact': changed['context']})
    assert context['album_menu']['discovery_offered'] == 3
    held = store.rec_ledger.evidence().get('opportunity', original['id'])
    assert held == original
    assert store.rec_proposals.get_proposals('album_prepared')['items'] == prepared


def test_album_notes_use_cached_sources_without_request_network_and_keep_held_identity(store, monkeypatch):
    from yt_playlist.providers import release_notes
    c, _fc, _iid = _client(store)
    album = {'browse_id': 'record', 'artist': 'Artist X', 'title': 'Record', 'score': .8}
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1, 'context': {}, 'items': [album]}, 1)
    store.wiki.put('artist:artist x', 'artist', 'Artist X', {
        'title': 'Artist X', 'extract': 'An artist with a distinctive sound.',
        'url': 'https://en.wikipedia.org/wiki/Artist_X'}, 1)
    store.rec_proposals.publish_album_note('record', {'rating': {
        'value': 4.2, 'scale': 5, 'count': 18, 'source': 'Discogs community',
        'url': 'https://www.discogs.com/release/123'}, 'retry_at': 100}, 1)
    def forbidden(*args):
        raise AssertionError('Requests must never fetch editorial providers')
    monkeypatch.setattr(release_notes, 'fetch', forbidden)
    assert '4.2' in c.get('/home/album-choices').text
    oid = store.rec_ledger.evidence().records('opportunity')[0]['id']
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1, 'context': {}, 'items': []}, 1)
    notes = c.get('/home/album-notes', params={'browse': 'record', 'opportunity': oid})
    assert notes.status_code == 200
    assert 'distinctive sound' not in notes.text
    assert 'Discogs community: 4.2 out of 5, 18 ratings' in notes.text
    assert 'album-stars-fill' in notes.text and 'width:84.0%' in notes.text
    assert 'arrow' not in notes.text and 'wikipedia.org' not in notes.text
    assert c.get('/home/album-notes?browse=unoffered').status_code == 404


def test_album_familiarity_uses_track_plays_without_counts_or_changing_held_experiment(store):
    c, _fc, iid = _client(store)
    entry = store.tracks.upsert_track('v1', 'One', 'Artist X', 'The Album', None,
                                     album_browse_id='MPREb_x')
    store.collection.cache_album_tracklist('MPREb_x', 'The Album', 'Artist X', None, [],
        entries=[{'entry_id': entry, 'track_number': 1}, {'entry_id': entry, 'track_number': 2}])
    store.tracks.upsert_track('other', 'Unrelated', 'Artist X', 'Other Album', None,
                             album_browse_id='other-album')
    store.history.record_history_plays(iid, 86400, [('one|artist x', 'today'), ('unrelated|artist x', 'today')],
                                      source_timezone='UTC')
    store.history.record_history_plays(iid, 2 * 86400, [('one|artist x', 'today')], source_timezone='UTC')
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1, 'context': {}, 'items': [
        {'browse_id': 'MPREb_x', 'title': 'The Album', 'artist': 'Artist X', 'genre': 'Jazz'},
        {'browse_id': 'unknown', 'title': 'Unheard', 'artist': 'Other Artist'}]}, 1)
    response = c.get('/home/album-choices')
    assert 'Listening familiarity for The Album: Familiar.' in response.text
    assert 'Listening familiarity for Unheard: Listening history unavailable' in response.text
    assert 'album-listening-meter' not in response.text and 'role="meter"' not in response.text
    assert 'complete album listens' in response.text
    original, = store.rec_ledger.evidence().records('opportunity')
    assert all('listening_plays' not in item for item in original['options'])
    for at in (2 * 86400 + 3600, 2 * 86400 + 7200):
        store.history.record_play_event(iid, 'one|artist x', 'v1', at, source_timezone='UTC')
    store.history.record_play_event(iid, 'one|artist x', 'v1', 3 * 86400 + 3600,
                                    source_timezone='UTC', context_choice=True)
    assert store.collection.album_listening_counts(['MPREb_x', 'unknown']) == {'MPREb_x': 4, 'unknown': 0}
    assert 'Listening familiarity for The Album: Frequently played.' in c.get('/home/album-choices').text
    assert store.rec_ledger.evidence().records('opportunity') == [original]


def test_album_familiarity_is_stable_across_release_length_and_marks_partial_coverage():
    from yt_playlist.web.routes.home_discovery import _familiarity
    assert _familiarity({'plays': 20, 'tracks': 10, 'complete': True}) == _familiarity(
        {'plays': 2, 'tracks': 1, 'complete': True})
    assert _familiarity({'plays': 0, 'tracks': 10, 'complete': True})['level'] == 'unplayed'
    assert _familiarity({'plays': 1, 'tracks': 10, 'complete': True})['level'] == 'light'
    assert _familiarity({'plays': 20, 'tracks': 1, 'complete': False})['level'] == 'partial'


def _album(browse="MPREb_x"):
    return {browse: {
        "title": "The Album", "year": "2021",
        "artists": [{"name": "Artist X"}],
        "thumbnails": [{"url": "http://t/1.jpg", "width": 300, "height": 300}],
        "tracks": [{"title": "One", "videoId": "v1", "duration": "3:01", "artists": [{"name": "Artist X"}]},
                   {"title": "Two", "videoId": "v2", "duration": "2:40", "artists": [{"name": "Artist X"}]}],
    }}


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = FakeClient(albums=_album())
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), fc, iid


def test_album_page_renders_table_and_create_form(store):
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "The Album" in r.text and "Artist X" in r.text
    assert "One" in r.text and "Two" in r.text                 # the track table
    assert "/album/create-playlist" in r.text                  # the create-playlist form
    assert 'aria-label="Save album to library" aria-pressed="false"' in r.text
    assert 'class="alink" href="/artist?name=Artist%20X"' in r.text
    assert '<a class="ptitle" href="/track/v1?' in r.text
    assert 'class="pl-play" href="https://music.youtube.com/watch?v=v1"' in r.text
    assert 'library-hero release-hero album-release-hero has-art' in r.text
    assert 'album-page-head' not in r.text and 'album-detail.css' not in r.text
    assert 'data-vid="v1"' in r.text and 'data-vid="v2"' in r.text
    assert '>Plays<' in r.text and 'like-btn' in r.text
    assert 'More like this album' not in r.text


def test_open_album_inline_keeps_the_complete_tracklist_and_feedback(store):
    c, _fc, _iid = _client(store)
    r = c.get('/album/inline?browse=MPREb_x&opportunity=held-menu')
    assert r.status_code == 200
    assert 'One' in r.text and 'Two' in r.text
    assert '<html' not in r.text and 'album-page-head' not in r.text
    assert '/track/v1?' in r.text and '/album/feedback' in r.text
    assert 'value="held-menu"' in r.text
    assert '/collection/save-album' in r.text
    assert 'class="track-table"' in r.text and 'data-vid="v1"' in r.text
    assert 'album-inline-tracks' not in r.text
    assert not store.collection.saved_album_ids()
    assert not store.rec_ledger.evidence().records('response')


def test_unsaved_album_and_expansion_reuse_identical_standard_rows(store):
    import re
    c, _fc, _iid = _client(store)
    table = lambda html: re.search(r'(<table class="track-table".*?</table>)', html, re.S).group(1)
    page = table(c.get('/album?browse=MPREb_x').text)
    inline = table(c.get('/album/inline?browse=MPREb_x').text)
    assert page == inline
    assert page.count('<th ') == 10
    assert not store.collection.saved_album_ids()
    assert store.charts.album_tracks_detail('MPREb_x') == []


def test_album_fetch_retains_exact_upload_lengths_for_the_shared_clock_without_saving(store):
    from yt_playlist.web.routes.bridge import _now_playing_snapshot
    c, fc, _iid = _client(store)
    fc._albums['MPREb_x']['audioPlaylistId'] = 'OLAK5uy_album'
    assert c.get('/album/inline?browse=MPREb_x').status_code == 200
    bridge = c.app.state.ctx.bridge
    bridge.now_playing = _now_playing_snapshot({
        'videoId': 'v1', 'title': 'One', 'artist': 'Artist X', 'position': 30,
        'duration': 32, 'playlist': 'OLAK5uy_album'})
    status = c.get('/bridge/status').json()['now_playing']
    assert status['position'] == 30 and status['duration'] == 181
    bridge.now_playing = _now_playing_snapshot({
        'videoId': 'v2', 'title': 'Two', 'artist': 'Artist X', 'position': 0,
        'duration': 181, 'playlist': 'OLAK5uy_album'})
    assert c.get('/bridge/status').json()['now_playing']['duration'] == 160
    assert not store.collection.saved_album_ids()
    assert store.charts.album_tracks_detail('MPREb_x') == []


def test_inline_fetch_failure_does_not_show_incidental_library_subset(store):
    c, fc, _iid = _client(store)
    fc._albums.clear()
    store.tracks.upsert_track('v1', 'One', 'Artist X', 'The Album', None, album_browse_id='MPREb_x')
    r = c.get('/album/inline?browse=MPREb_x')
    assert 'The tracklist couldn’t be loaded.' in r.text and 'Try again' in r.text
    assert '<ol' not in r.text


def test_create_playlist_from_album_redirects_to_new_playlist(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    c, fc, iid = _client(store)
    r = c.post("/album/create-playlist", data={"browse_id": "MPREb_x", "name": "My Album Mix"})
    assert r.status_code == 200
    new_pl = next(p for p in store.playlists.get_playlists() if p.title == "My Album Mix")
    assert new_pl.created_at == 1.0
    assert new_pl.thumbnail == "http://t/1.jpg"
    assert r.headers["hx-redirect"] == f"/playlist/{new_pl.id}"
    assert store.playlists.get_playlist_track_ids(new_pl.id)             # tracks were added
    assert fc.created and fc.added[0][1] == ["v1", "v2"]       # created on YouTube with the album's tracks


def test_unsaved_album_shows_full_live_tracks_not_incidental_library_subset(store):
    """An album that merely shares a track with one of your playlists must still render the FULL
    live-fetched album, not the partial library subset. Regular sync stamps each track's
    album_browse_id, so an unsaved album can have a single incidental library row. That must not
    shadow the real 8-track album."""
    # one incidental library track tagged with this album's browse_id (as playlist sync would)
    store.tracks.upsert_track("v1", "One", "Artist X", "The Album", None, album_browse_id="MPREb_x")
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "One" in r.text and "Two" in r.text   # BOTH live tracks, not just the incidental one
    assert '1 of 2 tracks in your library. The album is not saved.' in r.text
    assert 'aria-label="Save album to library"' in r.text


def test_album_head_tools_show_share_and_enrich_icons(store):
    """The album head shows the share + enrich icons (parity with playlists), even when unsaved."""
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "/album/MPREb_x/share.txt" in r.text          # share icon links to the album .txt
    assert 'aria-label="Enrich"' in r.text               # the single waterfall enrich icon


def test_album_share_txt_lists_live_track_urls(store):
    c, _fc, _iid = _client(store)
    r = c.get("/album/MPREb_x/share.txt")
    assert r.status_code == 200
    assert "https://music.youtube.com/watch?v=v1" in r.text
    assert "https://music.youtube.com/watch?v=v2" in r.text
    assert "attachment" in r.headers["content-disposition"]


def test_album_play_requires_connection_and_confirms_exact_queue_separately(store):
    from types import SimpleNamespace
    c, fc, _iid = _client(store)
    fc._albums['MPREb_x']['audioPlaylistId'] = 'OLAK5uy_album'
    oid = choice_evidence.issue(store, 'albums', [{'id': 'MPREb_x', 'score': 1}], 0, context={})
    sent = []
    bridge = SimpleNamespace(connected=False, send_control=sent.append)
    c.app.state.ctx.bridge = bridge
    response = c.post('/album/play', data={'browse_id': 'MPREb_x', 'opportunity_id': oid})
    assert response.status_code == 422
    assert not sent and store.rec_ledger.evidence().records('response') == []
    bridge.connected = True
    assert c.post('/album/play', data={'browse_id': 'MPREb_x', 'opportunity_id': oid}).status_code == 204
    assert sent[0]['url'] == 'https://music.youtube.com/watch?v=v1&list=OLAK5uy_album'
    row = choice_evidence.compile_report(store, 2)['formats']['albums']
    assert row['choices'] == 1 and row['starts'] == 0
    choice_evidence.record_playback(store, {'videoId': 'unrelated', 'playlist': 'other'}, 2)
    assert choice_evidence.compile_report(store, 3)['formats']['albums']['starts'] == 0
    choice_evidence.record_playback(store, {'videoId': 'v1', 'playlist': 'OLAK5uy_album'}, 3)
    choice_evidence.record_playback(store, {'videoId': 'v1', 'playlist': 'OLAK5uy_album'}, 4)
    row = choice_evidence.compile_report(store, 5)['formats']['albums']
    assert row['starts'] == 1 and row['experience'] == 1


def test_successive_deliberate_album_selections_from_one_menu_are_retained(store):
    from types import SimpleNamespace
    c, fc, _iid = _client(store)
    fc._albums['MPREb_x']['audioPlaylistId'] = 'OLAK-one'
    fc._albums['MPREb_other'] = {**fc._albums['MPREb_x'], 'audioPlaylistId': 'OLAK-two'}
    oid = choice_evidence.issue(store, 'albums', [
        {'id': 'MPREb_x', 'keys': ['one|artist'], 'score': 1},
        {'id': 'MPREb_other', 'keys': ['two|artist'], 'score': .9}], 0, context={})
    c.app.state.ctx.bridge = SimpleNamespace(connected=True, send_control=lambda message: None)
    for browse, action, queue in [('MPREb_x', 'first', 'OLAK-one'),
                                  ('MPREb_other', 'second', 'OLAK-two'),
                                  ('MPREb_x', 'third', 'OLAK-one')]:
        for retry in range(2):
            assert c.post('/album/play', data={'browse_id': browse, 'opportunity_id': oid,
                                              'action_id': action}).status_code == 204
        choice_evidence.record_playback(store, {'videoId': 'v1', 'playlist': queue}, 2)
    report = choice_evidence.compile_report(store, 3)['formats']['albums']
    assert report['choices'] == 3 and report['independent_episodes'] == 1
    contexts = store.rec_proposals.get_proposals('choice_interests')['contexts']
    assert {row['option_id'] for row in contexts} == {'MPREb_x', 'MPREb_other'}
    assert sum(row['episode_support'] for row in contexts) == 1
    replay = choice_evidence.replay(store, 3)
    assert len(replay['source_ids']) == 3


def test_album_share_uses_full_live_album_not_incidental_library_track(store):
    """An unsaved album with one incidental library track (stamped by playlist sync) must still share
    the FULL live album, not just that one track."""
    store.tracks.upsert_track("v1", "One", "Artist X", "The Album", None, album_browse_id="MPREb_x")
    c, _fc, _iid = _client(store)
    r = c.get("/album/MPREb_x/share.txt")
    assert r.status_code == 200
    assert "v1" in r.text and "v2" in r.text   # both album tracks, not just the incidental v1
    assert len([ln for ln in r.text.splitlines() if ln.strip()]) == 2


def test_create_playlist_from_album_requires_browse(store):
    c, _fc, _iid = _client(store)
    r = c.post("/album/create-playlist", data={"browse_id": "", "name": "x"})
    assert r.status_code == 422 and "album" in r.text.lower()


def test_saved_album_folds_in_durations_and_renders_liked_and_plays(store):
    """Saving an album folds its live tracks into the library WITH durations parsed from the YTM
    'M:SS' strings (issue: Length not populated), and the saved table renders the liked heart and
    the Plays column, parity with the playlist view."""
    store.collection.add_saved_album({"browse": "MPREb_x", "title": "The Album", "artist": "Artist X"})
    c, _fc, _iid = _client(store)
    r = c.get("/album?browse=MPREb_x")
    assert r.status_code == 200
    assert "3:01" in r.text and "2:40" in r.text          # durations parsed and rendered back as M:SS
    assert "like-btn" in r.text                            # the liked heart toggle
    assert ">Plays<" in r.text                             # the Plays column header
    assert 'aria-label="Album is in your library"' in r.text and '/collection/save-album' not in r.text
    assert '<a class="ptitle" href="/track/v1?' in r.text
    assert 'class="alink" href="/artist?name=Artist%20X"' in r.text
    # the folded-in tracks actually carry the parsed duration (seconds), not None
    assert {t["title"]: t["duration"] for t in store.charts.album_tracks_detail("MPREb_x")} \
        == {"One": 181, "Two": 160}
