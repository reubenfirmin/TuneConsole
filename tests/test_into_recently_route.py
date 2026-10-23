from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from yt_playlist.rec import into_recently
from yt_playlist.providers import wikipedia
from tests.conftest import FakeClient

NOW = 1_800_000_000


def _client(store):
    return TestClient(create_app(store, lambda: FakeClient(), now_fn=lambda: NOW), base_url="http://127.0.0.1")


def _heard(store, *videos):
    listener = store.identities.upsert_identity('story-listener', 'channel', None, True)
    events = [(store.tracks.identity_key_for_video(video), None, NOW - 86400) for video in videos]
    store.history.import_listening_events(listener, events, source='spotify_export', source_timezone='UTC')


def test_card_renders_from_fetched_summary(store, monkeypatch):
    monkeypatch.setattr(into_recently, "ranked_subjects",
                        lambda s, now, epoch=0: [{"kind": "genre", "subject": "genre:shoegaze",
                                                  "display": "shoegaze"}])
    monkeypatch.setattr(wikipedia, "fetch_summary",
                        lambda kind, display: {"display": "shoegaze", "title": "Shoegaze",
                                               "extract": "Shoegaze is a subgenre of indie rock.\n\nThe second paragraph finishes here.",
                                               "thumbnail": "http://img", "url": "http://wiki/Shoegaze"})
    into_recently.prepare(store, NOW)
    html = _client(store).get("/home/into-recently").text
    assert "Into recently" in html
    assert "shoegaze" in html
    assert "Shoegaze is a subgenre" in html
    assert "http://wiki/Shoegaze" in html
    assert 'class="into-read" href="http://wiki/Shoegaze"' in html
    assert '>Read more</a>' in html
    assert '<p>The second paragraph finishes here.</p>' in html
    assert 'aria-label="Read more about shoegaze on Wikipedia"' in html


def test_genre_without_article_image_has_text_layout_instead_of_album_art(store, monkeypatch):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store, 'ritmo|artist', 'Ritmo', 'Artist', video_id='ritmo', genre='Shoegaze',
                       thumbnail='http://local/ritmo.jpg')
    _heard(store, 'ritmo')
    subject = {"kind": "genre", "subject": "genre:shoegaze", "display": "shoegaze"}
    artist = {'kind': 'artist', 'subject': 'artist:Artist', 'display': 'Artist'}
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: [subject, artist])
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda kind, display: {
        'title': display, 'extract': f'About {display}.',
        'thumbnail': None if kind == 'genre' else 'http://local/portrait.jpg',
        'url': f'http://wiki/{display}'})
    into_recently.prepare(store, NOW)
    response = _client(store).get('/home/into-recently')
    assert len(response.context['cards']) == 2
    assert response.context['cards'][0]['kind'] == 'artist'
    genre = response.context['cards'][1]
    assert genre['thumbnail'] is None
    assert 'into-text-only' in response.text
    assert genre['songs'][0]['title'] == 'Ritmo'
    assert response.text.count('class="into-art"') == 1
    assert 'http://local/ritmo.jpg' in response.text  # Its cover belongs to the song, not the genre.
    assert '>Read more</a>' in response.text


def test_worker_prepares_three_matching_song_cards_and_request_uses_only_the_cache(store, monkeypatch):
    from tests.conftest import seed_catalog_track
    from yt_playlist.repos.charts import ChartsRepo

    subject = {'kind': 'genre', 'subject': 'genre:dub techno', 'display': 'dub techno'}
    for i in range(4):
        seed_catalog_track(store, f'song{i}|band', f'Song {i}', 'Band & Guest', video_id=f'video{i}',
                           genre='Dub Techno', thumbnail=f'http://local/song{i}.jpg')
    seed_catalog_track(store, 'unrelated|band', 'Unrelated', 'Band', video_id='unrelated', genre='Rock')
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: [subject])
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda *a: {
        'title': 'Dub techno', 'extract': 'A particular scene.', 'thumbnail': 'http://genre/art.jpg',
        'url': 'http://wiki/Dub_techno'})
    assert not into_recently.prepare(store, NOW)

    def forbidden(*a, **kw):
        raise AssertionError('The request must not select or rank story songs')

    monkeypatch.setattr(ChartsRepo, 'story_tracks', forbidden)
    response = _client(store).get('/home/into-recently')
    assert [song['title'] for song in response.context['cards'][0]['songs']] == ['Song 0', 'Song 1', 'Song 2']
    assert response.text.count('class="into-song"') == 3
    assert response.text.count('class="into-song-play"') == 3
    assert 'href="https://music.youtube.com/watch?v=video0"' in response.text
    assert 'href="/artist?name=Band%20%26%20Guest"' in response.text
    assert 'Unrelated' not in response.text
    assert 'into-heading' not in response.text


def test_worker_connects_genre_and_artist_articles_using_recorded_credits(store, monkeypatch):
    from tests.conftest import seed_catalog_track
    from yt_playlist.repos.rec_query import RecQueryRepo

    subjects = [{'kind': kind, 'subject': f'{kind}:{name}', 'display': name} for kind, name in [
        ('artist', 'Rrose'), ('artist', 'Slowdive'), ('genre', 'minimal'),
        ('artist', 'Tin Man'), ('genre', 'shoegaze')]]
    entry = store.tracks.upsert_track('collab', 'Together', 'Rrose & Guest', '', 180,
        credits=[{'name': 'Rrose', 'id': 'UC_rrose'}, {'name': 'Guest', 'id': 'UC_guest'}])
    with store.conn:
        store.conn.execute("UPDATE catalog_tracks SET genre='Minimal' WHERE id=?", (entry,))
    seed_catalog_track(store, 'slow|slowdive', 'Slow', 'Slowdive', video_id='slow', genre='Shoegaze')
    seed_catalog_track(store, 'acid|tinman', 'Acid', 'Tin Man', video_id='acid', genre='Acid')
    _heard(store, 'collab', 'slow', 'acid')
    for subject in subjects:
        store.wiki.put(subject['subject'], subject['kind'], subject['display'],
            {'extract': f"About {subject['display']}.", 'thumbnail': 'http://img',
             'url': f"http://wiki/{subject['display']}"}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)
    assert not into_recently.prepare(store, NOW)

    def forbidden(*a, **kw):
        raise AssertionError('Connections must be prepared by the worker')

    monkeypatch.setattr(RecQueryRepo, 'artist_genre_years_for', forbidden)
    response = _client(store).get('/home/into-recently')
    assert [[c['display'] for c in group] for group in response.context['groups']] == [
        ['Rrose', 'minimal'], ['shoegaze', 'Slowdive'], ['Tin Man']]
    assert response.context['groups'][0][0]['songs'][0]['title'] == 'Together'
    assert response.text.count('into-linked') == 2


@pytest.mark.parametrize('genre,historical', [('Goa Trance', False), ('Shoegaze', True)])
def test_common_or_familiar_genre_supports_an_artist_instead_of_leading(store, monkeypatch, genre, historical):
    from tests.conftest import seed_catalog_track

    seed_catalog_track(store, 'song|band', 'Song', 'Band', video_id='song', genre=genre)
    _heard(store, 'song')
    if historical:
        identity = store.identities.upsert_identity('listener', 'channel', None, True)
        events = [('song|band', None, NOW - (100 + i) * 86400) for i in range(12)]
        store.history.import_listening_events(identity, events, source='spotify_export', source_timezone='UTC')
    subjects = [{'kind': 'genre', 'subject': f'genre:{genre.lower()}', 'display': genre},
                {'kind': 'artist', 'subject': 'artist:Band', 'display': 'Band'}]
    for subject in subjects:
        store.wiki.put(subject['subject'], subject['kind'], subject['display'],
            {'title': subject['display'], 'extract': f"About {subject['display']}.",
             'thumbnail': 'http://image', 'url': f"http://wiki/{subject['display']}"}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)

    assert not into_recently.prepare(store, NOW)
    response = _client(store).get('/home/into-recently')
    group, = response.context['groups']
    assert [card['display'] for card in group] == ['Band', genre]
    assert not group[1]['lead_allowed']
    assert 'data-thread="artist:Band"' in response.text
    assert group[1]['thumbnail'] == 'http://image'


@pytest.mark.parametrize('genre,thumbnail', [('Goa Trance', 'http://genre/image'), ('Dub Techno', None)])
def test_a_common_or_imageless_genre_cannot_be_a_standalone_story(store, monkeypatch, genre, thumbnail):
    subject = {'kind': 'genre', 'subject': f'genre:{genre.lower()}', 'display': genre}
    store.wiki.put(subject['subject'], 'genre', genre,
        {'title': genre, 'extract': 'A complete paragraph.', 'thumbnail': thumbnail,
         'url': 'http://wiki/genre'}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: [subject])

    assert not into_recently.prepare(store, NOW)
    assert into_recently.load(store, NOW) == ([], False)


def test_story_rotation_keeps_connected_spreads_whole_within_nine_story_limit(store):
    cards = []
    for i in range(4):
        for j in range(3):
            subject = f'artist:Band {i}-{j}'
            cards.append({'kind': 'artist', 'subject': subject, 'display': f'Band {i}-{j}',
                          'group': f'artist:Band {i}-0', 'extract': 'A complete paragraph.',
                          'thumbnail': 'http://img', 'color': 'var(--accent)', 'seed': subject,
                          'genre': None, 'depth': 2})
    store.rec_surface.put_proposals(into_recently.SURFACE, {
        'schema': into_recently.SCHEMA, 'built_at': NOW, 'pending': False, 'cards': cards}, NOW)
    for epoch in range(4):
        selected, pending = into_recently.load(store, NOW, epoch=epoch)
        assert not pending and len(selected) == 9
        groups = into_recently.story_groups(selected)
        assert len(groups) == 3 and all(len(group) == 3 for group in groups)
        assert selected[0]['subject'] == f'artist:Band {epoch}-0'


def test_wrong_fresh_articles_are_replaced_before_building_a_related_spread(store, monkeypatch):
    from tests.conftest import seed_catalog_track

    # Broad track tags cannot establish this connection. The corrected artist
    # article explicitly identifies the narrower scene discussed by the genre.
    seed_catalog_track(store, 'hood|hood', 'Untitled', 'Robert Hood', video_id='hood', genre='Techno')
    _heard(store, 'hood')
    subjects = [{'kind': 'artist', 'subject': 'artist:Robert Hood', 'display': 'Robert Hood'},
                {'kind': 'genre', 'subject': 'genre:minimal', 'display': 'minimal'}]
    for subject, wrong in zip(subjects, ['Robert Scott (musician)', 'Minimal music']):
        store.wiki.put(subject['subject'], subject['kind'], subject['display'],
            {'title': wrong, 'extract': 'A story about the wrong subject.',
             'thumbnail': 'http://img', 'url': 'http://wiki/wrong'}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)
    calls = []

    def fetch(kind, display):
        calls.append(display)
        return {'title': 'Minimal techno' if kind == 'genre' else display,
                'extract': 'Minimal techno is a subgenre.' if kind == 'genre' else
                           'Robert Hood is a producer known for minimal techno.',
                'thumbnail': None if kind == 'genre' else 'http://img',
                'url': 'http://wiki/' + display}

    # The first bounded pass corrects the artist; its newly available article
    # then establishes the narrower connection for the following worker pass.
    assert into_recently.prepare(store, NOW, fetch_fn=fetch)
    assert not into_recently.prepare(store, NOW, fetch_fn=fetch)
    assert calls == ['Robert Hood', 'minimal']
    response = _client(store).get('/home/into-recently')
    assert [[c['display'] for c in group] for group in response.context['groups']] == [
        ['Robert Hood', 'minimal']]
    assert 'wrong subject' not in response.text
    assert 'Robert Scott' not in response.text


def test_genre_list_articles_do_not_become_stories(store, monkeypatch):
    subject = {"kind": "genre", "subject": "genre:dub techno", "display": "dub techno"}
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: [subject])
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda *a: {
        'title': 'List of dub techno artists', 'extract': 'This is a list.',
        'thumbnail': None, 'url': 'http://wiki/List'})
    into_recently.prepare(store, NOW)
    assert into_recently.load(store, NOW) == ([], False)
    assert 'This is a list.' not in _client(store).get('/home/into-recently').text


def test_specific_topic_redirecting_to_a_genre_primer_is_not_published(store, monkeypatch):
    subjects = [
        {'kind': 'genre', 'subject': 'genre:dub techno', 'display': 'dub techno'},
        {'kind': 'artist', 'subject': 'artist:New band', 'display': 'New band'},
    ]
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda *a: None)
    for subject, title in zip(subjects, ['Techno', 'New band']):
        store.wiki.put(subject['subject'], subject['kind'], subject['display'],
            {'title': title, 'extract': f'About {title}.', 'thumbnail': 'http://img',
             'url': f'http://wiki/{title}'}, NOW)
    into_recently.prepare(store, NOW)
    cards, pending = into_recently.load(store, NOW)
    assert [card['display'] for card in cards] == ['New band']
    assert not pending


def test_previous_story_selection_is_reprepared_before_serving(store):
    store.rec_surface.put_proposals(into_recently.SURFACE, {
        'schema': into_recently.SCHEMA - 1, 'built_at': NOW, 'pending': False,
        'cards': [{'kind': 'genre', 'subject': 'genre:techno', 'display': 'techno',
                   'extract': 'A genre primer.', 'thumbnail': None, 'color': 'var(--accent)',
                   'seed': 'Someone', 'genre': 'techno', 'depth': 2}],
    }, NOW)
    scheduled = []
    assert into_recently.load(store, NOW, schedule=lambda: scheduled.append(True)) == ([], True)
    assert scheduled == [True]


def test_cached_indie_rock_and_redirected_primers_are_filtered_before_display(store):
    cards = []
    for name, title in [('indie rock', 'Indie rock'), ('shoegaze', 'Alternative rock'),
                        ('dub techno', 'Dub techno')]:
        cards.append({'kind': 'genre', 'subject': f'genre:{name}', 'display': name, 'title': title,
                      'extract': f'About {title}.', 'thumbnail': 'http://genre.jpg' if name == 'dub techno' else None,
                      'lead_allowed': name == 'dub techno', 'color': 'var(--accent)',
                      'seed': None, 'genre': name, 'depth': 2})
    store.rec_surface.put_proposals(into_recently.SURFACE, {
        'schema': into_recently.SCHEMA, 'built_at': NOW, 'pending': False, 'cards': cards,
    }, NOW)
    scheduled = []
    result, pending = into_recently.load(store, NOW, schedule=lambda: scheduled.append(True))
    assert [card['display'] for card in result] == ['dub techno']
    assert pending and scheduled == [True]


def test_card_caches_and_does_not_refetch(store, monkeypatch):
    monkeypatch.setattr(into_recently, "ranked_subjects",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:khruangbin",
                                                  "display": "Khruangbin"}])
    calls = {"n": 0}

    def fetch(kind, display):
        calls["n"] += 1
        return {"display": display, "title": "Khruangbin", "extract": "A trio.",
                "thumbnail": None, "url": "http://wiki/K"}
    monkeypatch.setattr(wikipedia, "fetch_summary", fetch)
    into_recently.prepare(store, NOW)
    c = _client(store)
    c.get("/home/into-recently")
    c.get("/home/into-recently")
    assert calls["n"] == 1


def test_prepared_stories_fill_the_collection_without_duplicate_articles(store, monkeypatch):
    subjects = [{"kind": "artist", "subject": f"artist:Band {i}", "display": f"Band {i}"}
                for i in range(12)]
    for i, subject in enumerate(subjects):
        # Two related facets may resolve to the same article. It remains one story.
        page = 0 if i == 1 else i
        store.wiki.put(subject["subject"], subject["kind"], subject["display"],
                       {"extract": f"About band {i}.", "thumbnail": f"http://img/{i}",
                        "url": f"http://wiki/Band_{page}"}, 1e12)
    monkeypatch.setattr(into_recently, "ranked_subjects", lambda *a, **kw: subjects)

    def forbidden(*a):
        raise AssertionError("Already cached")

    monkeypatch.setattr(wikipedia, "fetch_summary", forbidden)
    into_recently.prepare(store, NOW)
    cards = _client(store).get("/home/into-recently").context["cards"]
    assert [card["display"] for card in cards] == ["Band 0", *[f"Band {i}" for i in range(2, 10)]]


def test_worker_lookups_are_bounded_and_do_not_hide_later_cached_stories(store, monkeypatch):
    subjects = [{"kind": "artist", "subject": f"artist:Band {i}", "display": f"Band {i}"}
                for i in range(10)]
    store.wiki.put(subjects[-1]["subject"], "artist", "Band 9",
                   {"extract": "A cached band.", "thumbnail": "http://img/9", "url": "http://wiki/9"}, 1e12)
    monkeypatch.setattr(into_recently, "ranked_subjects", lambda *a, **kw: subjects)
    lookups = []

    def miss(kind, display):
        lookups.append(display)
        return None

    monkeypatch.setattr(wikipedia, "fetch_summary", miss)
    assert into_recently.prepare(store, NOW)  # another small worker pass will handle the remaining subjects
    cards = _client(store).get("/home/into-recently").context["cards"]
    assert lookups == ["Band 0", "Band 1", "Band 2"]
    assert [card["display"] for card in cards] == ["Band 9"]


def test_empty_when_no_subject(store, monkeypatch):
    monkeypatch.setattr(into_recently, "ranked_subjects", lambda s, now: [])
    into_recently.prepare(store, NOW)
    r = _client(store).get("/home/into-recently")
    assert r.status_code == 200
    assert "Into recently" not in r.text


def test_empty_when_wikipedia_misses(store, monkeypatch):
    monkeypatch.setattr(into_recently, "ranked_subjects",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:nobody",
                                                  "display": "Nobody"}])
    monkeypatch.setattr(wikipedia, "fetch_summary", lambda kind, display: None)
    into_recently.prepare(store, NOW)
    r = _client(store).get("/home/into-recently")
    assert "Into recently" not in r.text


def test_falls_back_to_next_subject_when_first_fails(store, monkeypatch):
    # The reported bug: a rotation landed on a subject with no Wikipedia page and the card blanked.
    # Now the route walks past the dead subject to the next one that resolves.
    monkeypatch.setattr(into_recently, "ranked_subjects",
                        lambda s, now, epoch=0: [
                            {"kind": "artist", "subject": "artist:nopage", "display": "NoPage"},
                            {"kind": "genre", "subject": "genre:shoegaze", "display": "shoegaze"}])

    def fetch(kind, display):
        if display == "shoegaze":
            return {"display": "shoegaze", "title": "Shoegaze", "extract": "A genre.",
                    "thumbnail": "http://img", "url": "http://wiki/Shoegaze"}
        return None
    monkeypatch.setattr(wikipedia, "fetch_summary", fetch)
    into_recently.prepare(store, NOW)
    html = _client(store).get("/home/into-recently").text
    assert "shoegaze" in html and "A genre." in html    # fell through the failed first subject


def test_artist_card_uses_local_thumbnail_color_and_cta(store, monkeypatch):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,'k|khruangbin','T','Khruangbin',track_id=1,thumbnail='http://local/k.jpg')
    store.conn.commit()
    monkeypatch.setattr(into_recently, "ranked_subjects",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:Khruangbin",
                                                  "display": "Khruangbin"}])
    monkeypatch.setattr(wikipedia, "fetch_summary",
                        lambda kind, display: {"display": "Khruangbin", "title": "Khruangbin",
                                               "extract": "A trio.", "thumbnail": None, "url": "http://wiki/K"})
    into_recently.prepare(store, NOW)
    html = _client(store).get("/home/into-recently").text
    assert "http://local/k.jpg" in html                 # fell back to the local thumbnail
    assert "wiki-subject" in html and "--subj" in html  # coloured/glowing heading
    assert "/clusters?seed=Khruangbin" in html and "depth=2" in html  # explore CTA deep-link
    assert "label=Khruangbin" in html                   # names the new canvas (switcher badge)


def test_card_hidden_when_no_thumbnail_anywhere(store, monkeypatch):
    monkeypatch.setattr(into_recently, "ranked_subjects",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:Ghost",
                                                  "display": "Ghost"}])
    monkeypatch.setattr(wikipedia, "fetch_summary",
                        lambda kind, display: {"display": "Ghost", "title": "Ghost",
                                               "extract": "Spooky.", "thumbnail": None, "url": "u"})
    into_recently.prepare(store, NOW)
    assert "Into recently" not in _client(store).get("/home/into-recently").text


def test_refresh_failure_preserves_stale_card_and_retries(store, monkeypatch):
    subj = {"kind": "artist", "subject": "artist:Khruangbin", "display": "Khruangbin"}
    monkeypatch.setattr(into_recently, "ranked_subjects", lambda *a, **kw: [subj])
    store.wiki.put(subj["subject"], "artist", "Khruangbin",
                   {"extract": "A trio.", "thumbnail": "http://img", "url": "http://wiki"}, 0)
    calls = []

    def failed(*args):
        calls.append(args)
        raise wikipedia.LookupUnavailable("offline")

    monkeypatch.setattr(wikipedia, "fetch_summary", failed)
    assert not into_recently.prepare(store, NOW)  # an outage uses retry backoff, not a busy worker loop
    client = _client(store)
    assert "A trio." in client.get("/home/into-recently").text
    assert "A trio." in client.get("/home/into-recently").text
    assert len(calls) == 1
    assert store.wiki.get(subj["subject"])["fetched_at"] == 0
    deadline = store.wiki._retry_after[subj["subject"]]
    assert not store.wiki.should_refresh(subj["subject"], store.wiki.get(subj["subject"]), deadline - 1)
    assert store.wiki.should_refresh(subj["subject"], store.wiki.get(subj["subject"]), deadline)
    into_recently.refresh_summary(store, subj, deadline,
                                  lambda *a: {"extract": "Recovered", "thumbnail": "http://new"})
    assert store.wiki.get(subj["subject"])["extract"] == "Recovered"
    assert subj["subject"] not in store.wiki._retry_after


def test_cold_fragment_only_schedules_work_and_prepared_reads_do_no_ranking(store, monkeypatch):
    client = _client(store)
    scheduled = []
    monkeypatch.setattr(client.app.state.ctx.rec_worker, "trigger_story_refresh", lambda: scheduled.append(True))

    def forbidden(*a, **kw):
        raise AssertionError("Story preparation must stay in the worker")

    monkeypatch.setattr(into_recently, "ranked_subjects", forbidden)
    monkeypatch.setattr(into_recently, "decorate", forbidden)
    monkeypatch.setattr(wikipedia, "fetch_summary", forbidden)
    response = client.get("/home/into-recently")
    assert response.status_code == 200 and scheduled == [True]
    assert 'aria-label="Preparing your stories"' in response.text
    card = {"kind": "artist", "subject": "artist:Band", "display": "Band", "extract": "A band.",
            "thumbnail": "http://img", "url": "http://wiki/Band", "color": "var(--accent)",
            "seed": "Band", "genre": None, "depth": 2}
    store.rec_surface.put_proposals(into_recently.SURFACE,
        {"schema": into_recently.SCHEMA, "built_at": NOW, "cards": [card], "pending": False}, NOW)
    response = client.get("/home/into-recently")
    assert "A band." in response.text and scheduled == [True]
    assert 'aria-label="Read about Band on Wikipedia"' in response.text


def test_worker_drains_small_story_batches_without_rebuilding_home_or_models(store, monkeypatch):
    import logging
    from types import SimpleNamespace
    from yt_playlist.core.activity import Activity
    from yt_playlist.rec import home_prepared
    from yt_playlist.rec.rec_worker import RecWorker

    subjects = [{"kind": "artist", "subject": f"artist:Band {i}", "display": f"Band {i}"}
                for i in range(9)]
    monkeypatch.setattr(into_recently, "ranked_subjects", lambda *a: subjects)
    calls = []

    def fetch(kind, display):
        calls.append(display)
        assert not store.conn.in_transaction
        return {"extract": "About the band.", "thumbnail": "http://img", "url": "http://wiki/" + display}

    monkeypatch.setattr(wikipedia, "fetch_summary", fetch)
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__),
                                       now_fn=lambda: NOW), debounce_s=0)
    launches = []
    monkeypatch.setattr(worker, "_start_loop", lambda: launches.append(True))

    def forbidden(*a, **kw):
        raise AssertionError("Article work must not rebuild recommendation collections")

    monkeypatch.setattr(home_prepared, "prepare", forbidden)
    monkeypatch.setattr(worker, "_do_rebuild", forbidden)
    for _ in range(10):
        worker.trigger_story_refresh()
    assert launches == [True] and worker.jobs.has('stories')
    try:
        worker._run_pending()
        cards, pending = into_recently.load(store, NOW)
        assert len(cards) == 9 and not pending
        assert calls == [subject["display"] for subject in subjects]
        assert not worker.busy
    finally:
        worker.shutdown()


def test_readable_genre_clusters_survive_misses_and_keep_developed_article_near_the_top(store, monkeypatch):
    from tests.conftest import seed_catalog_track

    subjects = []
    # The first two minimal artists have no article; they must not occupy the
    # companion slots ahead of a readable artist after the old twelve-item cutoff.
    for i in range(14):
        name = f'No article {i}'
        subjects.append({'kind': 'artist', 'subject': f'artist:{name}', 'display': name})
        store.wiki.put(f'artist:{name}', 'artist', name, None, NOW)
        if i < 2:
            seed_catalog_track(store, f'empty{i}', 'Empty', name, video_id=f'empty{i}', genre='Minimal')
    for genre, artist, words in [('Minimal', 'Minimal artist', 45),
                                  ('Indie Pop', 'Pop artist', 35),
                                  ('Alternative Rock', 'Developed artist', 149)]:
        seed_catalog_track(store, artist, 'A song', artist, video_id=artist, genre=genre)
        _heard(store, artist)
        for kind, name in [('genre', genre.lower()), ('artist', artist)]:
            subject = {'kind': kind, 'subject': f'{kind}:{name}', 'display': name}
            subjects.append(subject)
            store.wiki.put(subject['subject'], kind, name, {
                'title': 'Minimal techno' if name == 'minimal' else name,
                'extract': ('A complete paragraph. ' * (words // 3)) if kind == 'artist' else f'About {genre}.',
                'thumbnail': 'http://portrait' if kind == 'artist' else None,
                'url': f'http://wiki/{name}'}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)

    def unexpected_fetch(*a):
        raise AssertionError('All candidates have a fresh confirmed answer')

    assert not into_recently.prepare(store, NOW, fetch_fn=unexpected_fetch)
    groups = _client(store).get('/home/into-recently').context['groups']
    assert [[card['display'] for card in group] for group in groups] == [
        ['Developed artist', 'alternative rock'], ['Minimal artist', 'minimal'], ['Pop artist', 'indie pop']]
    assert all(not group[1]['lead_allowed'] for group in groups)


def test_cached_genre_list_is_replaced_with_an_actual_context_article(store, monkeypatch):
    from tests.conftest import seed_catalog_track

    seed_catalog_track(store, 'band', 'Song', 'Band', video_id='band', genre='Rock')
    _heard(store, 'band')
    subjects = [{'kind': kind, 'subject': f'{kind}:{name}', 'display': name}
                for kind, name in [('genre', 'rock'), ('artist', 'Band')]]
    store.wiki.put('genre:rock', 'genre', 'rock', {'title': 'List of rock genres',
        'extract': 'A list.', 'url': 'http://wiki/List'}, NOW)
    store.wiki.put('artist:Band', 'artist', 'Band', {'title': 'Band', 'extract': 'Band is a rock band.',
        'thumbnail': 'http://portrait', 'url': 'http://wiki/Band'}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)
    calls = []

    def fetch(kind, display):
        calls.append(display)
        return {'title': 'Rock music', 'extract': 'An actual paragraph about rock music.',
                'thumbnail': None, 'url': 'http://wiki/Rock_music'}

    assert not into_recently.prepare(store, NOW, fetch_fn=fetch)
    assert calls == ['rock']
    cards, _ = into_recently.load(store, NOW)
    assert [card['display'] for card in cards] == ['Band', 'rock']
    assert not cards[1]['lead_allowed'] and 'A list.' not in cards[1]['extract']


def test_coarse_tags_and_original_artist_biographies_do_not_make_remixes_rock_examples(store, monkeypatch):
    from tests.conftest import seed_catalog_track

    artists = [
        ('suzuki', 'KEIICHI SUZUKI', "Satellite Serenade (The Orb's Transasianexpress Mix) [Mixed]",
         'A composer who co-founded a rock band and also wrote video game soundtracks.'),
        ('yuuf', 'Yuuf', 'Silky Spring', 'An instrumental group blending psychedelia and global sounds.'),
        ('band', 'Guitar band', 'Guitar song', 'A rock band known for its guitar songs.'),
    ]
    subjects = [{'kind': 'genre', 'subject': 'genre:rock', 'display': 'rock'}]
    store.wiki.put('genre:rock', 'genre', 'rock', {'title': 'Rock music', 'extract': 'About rock music.',
        'url': 'http://wiki/Rock'}, NOW)
    for video, artist, title, extract in artists:
        seed_catalog_track(store, video, title, artist, video_id=video, genre='Rock')
        subject = {'kind': 'artist', 'subject': f'artist:{artist}', 'display': artist}
        subjects.append(subject)
        store.wiki.put(subject['subject'], 'artist', artist, {'title': artist, 'extract': extract,
            'thumbnail': 'http://portrait', 'url': f'http://wiki/{video}'}, NOW)
    _heard(store, 'suzuki', 'yuuf', 'band')
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)

    assert not into_recently.prepare(store, NOW)
    cards, pending = into_recently.load(store, NOW)
    assert not pending
    groups = into_recently.story_groups(cards)
    rock_group, = [group for group in groups if any(c['kind'] == 'genre' for c in group)]
    assert [card['display'] for card in rock_group] == ['Guitar band', 'rock']
    assert [song['title'] for song in rock_group[1]['songs']] == ['Guitar song']
    assert {'KEIICHI SUZUKI', 'Yuuf'} <= {card['display'] for card in cards}


def test_story_connection_uses_recent_recording_instead_of_artists_dominant_old_genre(store, monkeypatch):
    from tests.conftest import seed_catalog_track

    for i in range(4):
        seed_catalog_track(store, f'old{i}', f'Old guitar song {i}', 'Changing artist',
                           video_id=f'old{i}', genre='Rock')
    seed_catalog_track(store, 'new', 'Current sound', 'Changing artist', video_id='new', genre='Dub Techno')
    _heard(store, 'new')
    subjects = [{'kind': kind, 'subject': f'{kind}:{name}', 'display': name}
                for kind, name in [('artist', 'Changing artist'), ('genre', 'rock'), ('genre', 'dub techno')]]
    for subject in subjects:
        store.wiki.put(subject['subject'], subject['kind'], subject['display'], {
            'title': subject['display'], 'extract': 'A complete paragraph.', 'thumbnail': 'http://portrait',
            'url': f"http://wiki/{subject['display']}"}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)

    assert not into_recently.prepare(store, NOW)
    cards, _ = into_recently.load(store, NOW)
    assert {card['display'] for card in cards} == {'Changing artist', 'dub techno'}


def test_only_visible_known_stories_count_and_fragment_polls_do_not_count_again(store, monkeypatch):
    subject = {'kind': 'artist', 'subject': 'artist:Band', 'display': 'Band'}
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: [subject])
    store.wiki.put(subject['subject'], 'artist', 'Band', {'title': 'Band', 'extract': 'About Band.',
        'thumbnail': 'http://portrait', 'url': 'http://wiki/Band'}, NOW)
    into_recently.prepare(store, NOW)
    client = _client(store)
    for _ in range(2):
        client.get('/home/into-recently')
    assert store.rec_surface.story_views() == {}
    for subjects in [['artist:Band'], ['artist:Band', 'artist:Unknown'], ['artist:Band']]:
        assert client.post('/home/into-recently/impressions', json={'subjects': subjects}).status_code == 204
    assert store.rec_surface.story_views() == {'artist:Band': {'views': 1, 'last_shown': NOW}}


def test_story_variety_waits_for_next_edition_and_survives_restart(tmp_path, monkeypatch):
    from yt_playlist.core.store import Store

    path = tmp_path / 'stories.db'
    store = Store(path)
    store.init_schema()
    subjects = [{'kind': 'artist', 'subject': f'artist:Band {i}', 'display': f'Band {i}'} for i in range(15)]
    for subject in subjects:
        store.wiki.put(subject['subject'], 'artist', subject['display'], {
            'title': subject['display'], 'extract': 'About the band.', 'thumbnail': 'http://portrait',
            'url': 'http://wiki/' + subject['display']}, NOW)
    monkeypatch.setattr(into_recently, 'ranked_subjects', lambda *a: subjects)
    into_recently.prepare(store, NOW)
    original, _ = into_recently.load(store, NOW)
    assert [s['subject'] for s in original] == [s['subject'] for s in subjects[:9]]
    store.rec_surface.record_story_views([s['subject'] for s in original], NOW)
    # More warming/polling must not replace an article while it is being read.
    into_recently.prepare(store, NOW + 60)
    assert into_recently.load(store, NOW + 60)[0] == original
    # A later edition uses persisted visible history, not only the top taste ranks.
    store.conn.close()
    store = Store(path)
    store.init_schema()
    later = NOW + 2 * 86400
    into_recently.prepare(store, later)
    selected, _ = into_recently.load(store, later)
    assert {s['subject'] for s in subjects[9:]} <= {s['subject'] for s in selected}
    assert len(selected) == 9
    store.conn.close()
