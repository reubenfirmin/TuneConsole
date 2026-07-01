from yt_playlist.rec import into_recently


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


class FakeStore:
    """Just enough surface for ranked_subjects: monkeypatch transient.facet_leans separately."""
    def __init__(self, favorites, corpus, wiki=None):
        self._favorites = favorites      # [artist names], all-time order
        self._corpus = corpus            # {genre: count}
        self.wiki = wiki

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
    store = FakeStore(favorites=[], corpus={"rock": 800, "jazz": 50, "ambient": 150})
    _leans(monkeypatch, {"genre:rock": 3.0, "genre:jazz": 1.0})
    out = into_recently.ranked_subjects(store, now=0.0)[0]
    assert out["display"] == "jazz"
    assert out["kind"] == "genre"


def test_returns_none_when_everything_obvious(monkeypatch):
    store = FakeStore(favorites=["Radiohead"], corpus={"rock": 1000})
    _leans(monkeypatch, {"artist:Radiohead": 4.0, "genre:rock": 4.0})
    assert into_recently.ranked_subjects(store, now=0.0) == []


def test_ignores_era_facets_and_negative_leans(monkeypatch):
    store = FakeStore(favorites=[], corpus={"jazz": 10})
    _leans(monkeypatch, {"era:1990s": 9.0, "artist:Boards of Canada": -2.0})
    assert into_recently.ranked_subjects(store, now=0.0) == []


def test_subject_color_genre_maps_to_family_else_accent():
    assert into_recently.subject_color({"kind": "genre", "subject": "genre:techno"}) == "#15e98c"
    # unknown genre token falls back to the accent
    assert into_recently.subject_color({"kind": "genre", "subject": "genre:zzz"}) == into_recently._DEFAULT_COLOR
    # artists always use the accent
    assert into_recently.subject_color({"kind": "artist", "subject": "artist:X"}) == into_recently._DEFAULT_COLOR


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
