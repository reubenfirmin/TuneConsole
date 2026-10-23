from yt_playlist.rec import into_recently
from types import SimpleNamespace
from yt_playlist.web import theme


class FakeWiki:
    """In-memory stand-in for WikiRepo: records puts, and a `fresh` set controls is_fresh."""
    def __init__(self):
        self.rows = {}
        self.fresh = set()           # subjects is_fresh() should report as still fresh

    def get(self, subject):
        return self.rows.get(subject)

    def put(self, subject, kind, display, payload, now):
        self.rows[subject] = {"subject": subject, "kind": kind, "display": display,
                              "found": 1 if payload else 0, "fetched_at": now,
                              **(payload or {})}

    def is_fresh(self, row, now):
        return row["subject"] in self.fresh

    def should_refresh(self, subject, row, now):
        return row is None or not self.is_fresh(row, now)


class FakeStore:
    """Just enough surface for ranked_subjects: monkeypatch transient.facet_leans separately."""
    def __init__(self, favorites, corpus, wiki=None, recent=None):
        self._favorites = favorites      # [artist names], all-time order
        self._corpus = corpus            # {genre: count}
        self.wiki = wiki
        self.recent = recent
        self.charts = SimpleNamespace(top_artists=self.top_artists,
                                      corpus_distribution=self.corpus_distribution,
                                      story_activity=self.story_activity)

    def story_activity(self, since):
        if self.recent is not None:
            return self.recent
        result = {'artist': {}, 'genre': {}}
        for facet in into_recently.transient.facet_leans(self, since + into_recently.RECENT_WINDOW_S):
            kind, _, name = facet.partition(':')
            if kind in result:
                result[kind][name.removeprefix('other:')] = {'plays': 1, 'last_played': since + 1}
        return result

    def top_artists(self, limit=100, since=None):
        return [{"artist": a, "plays": 0, "thumbnail": None} for a in self._favorites[:limit]]

    def corpus_distribution(self, dimension):
        assert dimension == "genre"
        return dict(self._corpus)


def _leans(monkeypatch, leans):
    monkeypatch.setattr(into_recently.transient, "facet_leans", lambda store, now: leans)


def test_excludes_settled_favorite_artist(monkeypatch):
    store = FakeStore(favorites=["The Beatles"], corpus={})
    _leans(monkeypatch, {"artist:The Beatles": 5.0, "artist:Khruangbin": 1.0})
    out = into_recently.ranked_subjects(store, now=0.0)
    assert out == [{"kind": "artist", "subject": "artist:Khruangbin", "display": "Khruangbin"}]


def test_suppresses_broad_staple_genre(monkeypatch):
    store = FakeStore(favorites=[], corpus={"shoegaze": 800, "dub techno": 50, "ambient": 150})
    _leans(monkeypatch, {"genre:shoegaze": 3.0, "genre:dub techno": 1.0})
    out = into_recently.ranked_subjects(store, now=0.0)[0]
    assert out["display"] == "dub techno"
    assert out["kind"] == "genre"


def test_common_genre_remains_eligible_as_context(monkeypatch):
    store = FakeStore(favorites=["Radiohead"], corpus={"rock": 1000})
    _leans(monkeypatch, {"artist:Radiohead": 4.0, "genre:rock": 4.0})
    subjects = into_recently.ranked_subjects(store, now=0.0)
    assert subjects == [{'kind': 'genre', 'subject': 'genre:rock', 'display': 'rock'}]
    assert not into_recently._specific_genre_card(subjects[0])


def test_ignores_era_facets_and_negative_leans(monkeypatch):
    store = FakeStore(favorites=[], corpus={"jazz": 10})
    _leans(monkeypatch, {"era:1990s": 9.0, "artist:Boards of Canada": -2.0})
    assert into_recently.ranked_subjects(store, now=0.0) == []


def test_specific_genres_win_over_catch_all_electronic(monkeypatch):
    store = FakeStore(favorites=[], corpus={})
    _leans(monkeypatch, {"genre:electronic": 10, "genre:other:electronic": 8,
                         "genre:electronic music": 6, "genre:shoegaze": 1})
    assert [s['display'] for s in into_recently.ranked_subjects(store, 0)] == ['shoegaze']


def test_common_genres_can_supply_context_but_cannot_lead(monkeypatch):
    store = FakeStore(favorites=[], corpus={})
    _leans(monkeypatch, {
        'genre:techno': 100, 'genre:other:Techno': 90, 'genre:house': 80,
        'genre:rock': 70, 'genre:jazz': 60, 'genre:ambient': 50,
        'genre:indie rock': 45, 'genre:alternative': 44, 'genre:classic rock': 43,
        'genre:indie pop': 42, 'genre:other:Indie Rock': 41,
        'genre:Indie & Alternative Rock': 40,
        'genre:heavy metal': 39, 'genre:punk rock': 38,
        'genre:dub techno': 2, 'genre:shoegaze': 1, 'artist:New band': 3,
    })
    subjects = into_recently.ranked_subjects(store, 0)
    by_name = {s['display']: s for s in subjects}
    assert {'New band', 'dub techno', 'shoegaze', 'indie rock', 'indie pop', 'techno'} <= by_name.keys()
    assert all(not into_recently._specific_genre_card(by_name[name])
               for name in ('indie rock', 'indie pop', 'techno', 'house', 'rock', 'jazz'))
    assert all(into_recently._specific_genre_card(by_name[name]) for name in ('dub techno', 'shoegaze'))


def test_lingering_positive_lean_does_not_make_an_old_artist_recent(monkeypatch):
    recent = {'artist': {'Current artist': {'plays': 1}}, 'genre': {}}
    store = FakeStore(favorites=[], corpus={}, recent=recent)
    _leans(monkeypatch, {'artist:Marc Almond': 100, 'artist:Current artist': .1})
    assert [s['display'] for s in into_recently.ranked_subjects(store, 100_000_000)] == ['Current artist']


def test_unseen_recent_subjects_precede_top_artists_shown_a_few_days_ago():
    now = 100 * 86400
    subjects = [{'kind': 'artist', 'subject': f'artist:{name}', 'display': name}
                for name in ['Top one', 'Top two', 'Less familiar', 'Another direction']]
    appearances = {subject['subject']: {'views': 10, 'last_shown': now - 2 * 86400}
                   for subject in subjects[:2]}
    order = into_recently._variety_order(subjects, appearances, now)
    assert [s['display'] for s in order] == ['Less familiar', 'Another direction', 'Top one', 'Top two']


def test_thin_pool_uses_least_recently_seen_subjects_without_hiding_everything():
    now = 100 * 86400
    subjects = [{'kind': 'artist', 'subject': f'artist:{i}', 'display': str(i)} for i in range(3)]
    appearances = {s['subject']: {'views': 4, 'last_shown': now - (i + 1) * 86400}
                   for i, s in enumerate(subjects)}
    assert [s['display'] for s in into_recently._variety_order(subjects, appearances, now)] == ['2', '1', '0']


def test_an_unseen_artist_can_lead_a_cluster_instead_of_repeating_its_longest_artist():
    cards = [{'kind': 'artist', 'subject': f'artist:{name}', 'display': name, 'group': 'artist:Seen',
              'thumbnail': 'portrait', 'extract': 'word ' * words, 'recently_shown': seen}
             for name, words, seen in [('Seen', 150, True), ('Unseen', 50, False)]]
    assert [card['display'] for card in into_recently.story_groups(cards)[0]] == ['Unseen', 'Seen']


def test_subject_color_genre_maps_to_family_else_accent():
    # colours are token references resolved against static/tokens.css, never literals
    default = theme.genre_tint(None)
    assert into_recently.subject_color({"kind": "genre", "subject": "genre:techno"}) == "var(--genre-techno)"
    # a subgenre resolves through its family
    assert into_recently.subject_color({"kind": "genre", "subject": "genre:psytrance"}) == "var(--genre-trance)"
    # unknown genre token falls back to the accent
    assert into_recently.subject_color({"kind": "genre", "subject": "genre:zzz"}) == default
    # artists always use the accent
    assert into_recently.subject_color({"kind": "artist", "subject": "artist:X"}) == default


def _warm(wiki, subject, now=0.0):
    """Mark a subject as having a usable, fresh card in the fake cache."""
    wiki.put(subject, "artist", subject.split(":", 1)[1], _hit("artist", subject), now)
    wiki.fresh.add(subject)


def test_subjects_for_epoch_rotates_through_warm_subjects(monkeypatch):
    # The reported bug: only the top-3 order rotated, so the walk collapsed onto the single subject
    # that resolved. Now warm (cached) subjects rotate one-per-epoch, so consecutive epochs lead with
    # DIFFERENT subjects instead of the same one forever.
    wiki = FakeWiki()
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0, "artist:Bravo": 2.0, "artist:Charlie": 1.0})
    for s in ("artist:Alpha", "artist:Bravo", "artist:Charlie"):
        _warm(wiki, s)
    leads = [into_recently.subjects_for_epoch(store, 0.0, epoch=e)[0]["display"] for e in range(4)]
    assert leads == ["Alpha", "Bravo", "Charlie", "Alpha"]


def test_subjects_for_epoch_warm_lead_then_cold_fallback(monkeypatch):
    # Cold (un-cached) subjects trail the warm ones so a cold start still has something to try, but a
    # warm subject always leads once one exists (the card never collapses back to a non-resolving head).
    wiki = FakeWiki()
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0, "artist:Bravo": 2.0, "artist:Charlie": 1.0})
    _warm(wiki, "artist:Charlie")                  # only the weakest is warm
    order = [s["display"] for s in into_recently.subjects_for_epoch(store, 0.0, epoch=0)]
    assert order[0] == "Charlie"                   # warm leads despite being lowest-ranked
    assert set(order[1:]) == {"Alpha", "Bravo"}    # cold subjects follow as fallbacks


def test_subjects_for_epoch_no_warm_keeps_rank_order(monkeypatch):
    wiki = FakeWiki()
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0, "artist:Bravo": 2.0})
    order = [s["display"] for s in into_recently.subjects_for_epoch(store, 0.0, epoch=5)]
    assert order == ["Alpha", "Bravo"]             # nothing warm -> plain rank order, no rotation


# --- prewarm_pool: the RecWorker step that pre-fetches the whole pool's wiki cards ----------------

def _hit(kind, display):
    """A fake fetch that always resolves to a usable card."""
    return {"title": display, "extract": f"about {display}", "thumbnail": "t", "url": "u"}


def test_prewarm_fetches_and_caches_top_subjects(monkeypatch):
    wiki = FakeWiki()
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0, "artist:Bravo": 2.0, "artist:Charlie": 1.0})
    n = into_recently.prewarm_pool(store, now=10.0, fetch_fn=_hit)
    assert n == 3
    assert set(wiki.rows) == {"artist:Alpha", "artist:Bravo", "artist:Charlie"}
    assert wiki.rows["artist:Alpha"]["found"] == 1


def test_prewarm_skips_subjects_already_fresh(monkeypatch):
    wiki = FakeWiki()
    wiki.put("artist:Alpha", "artist", "Alpha", _hit("artist", "Alpha"), now=0.0)
    wiki.fresh.add("artist:Alpha")            # Alpha is still fresh -> must not refetch
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0, "artist:Bravo": 2.0})
    calls = []
    n = into_recently.prewarm_pool(store, now=10.0,
                                   fetch_fn=lambda k, d: calls.append(d) or _hit(k, d))
    assert n == 1                              # only Bravo fetched
    assert calls == ["Bravo"]


def test_prewarm_negative_caches_misses(monkeypatch):
    wiki = FakeWiki()
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0})
    n = into_recently.prewarm_pool(store, now=10.0, fetch_fn=lambda k, d: None)
    assert n == 1
    assert wiki.rows["artist:Alpha"]["found"] == 0    # miss is negative-cached, not skipped


def test_prewarm_respects_limit(monkeypatch):
    wiki = FakeWiki()
    store = FakeStore(favorites=[], corpus={}, wiki=wiki)
    _leans(monkeypatch, {"artist:Alpha": 3.0, "artist:Bravo": 2.0, "artist:Charlie": 1.0})
    n = into_recently.prewarm_pool(store, now=10.0, fetch_fn=_hit, limit=2)
    assert n == 2
    assert set(wiki.rows) == {"artist:Alpha", "artist:Bravo"}


def test_story_connections_follow_specific_equivalent_genres_and_recent_rank():
    from types import SimpleNamespace

    subjects = [{'kind': kind, 'subject': f'{kind}:{name}', 'display': name}
                for kind, name in [('artist', 'Minimal artist'), ('artist', 'Acid artist'),
                                   ('genre', 'minimal'), ('artist', 'Second minimal artist'),
                                   ('artist', 'Third minimal artist'), ('genre', 'shoegaze'),
                                   ('artist', 'IDM artist'), ('genre', 'idm')]]
    metadata = {'Minimal artist': {'genre': 'MINIMAL'},
                'Acid artist': {'genre': 'Acid'},
                'Second minimal artist': {'genre': 'Minimal'},
                'Third minimal artist': {'genre': 'minimal'},
                'IDM artist': {'genre': 'Intelligent Dance Music'}}
    recordings = {name: [{'title': 'A song', **values}] for name, values in metadata.items()}
    store = SimpleNamespace(charts=SimpleNamespace(story_activity=lambda since: {'recordings': recordings}),
                            wiki=SimpleNamespace(get=lambda subject: None))

    groups = into_recently._connected_subjects(store, subjects)
    assert [[s['display'] for s in group] for group in groups] == [
        ['minimal', 'Minimal artist', 'Second minimal artist'], ['Acid artist'],
        ['Third minimal artist'], ['shoegaze'], ['idm', 'IDM artist']]
    # A shared techno family did not attach the acid artist to the minimal story.
    # The group has at most two companions and introduces no extra artists.
    assert sorted(s['subject'] for group in groups for s in group) == sorted(s['subject'] for s in subjects)
