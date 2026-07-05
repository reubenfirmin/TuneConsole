import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from yt_playlist.web.routes import home
from yt_playlist.rec import trend_rollups as tr
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _client(store, now=1000.0):
    """A real app, built the way tests/test_home.py does, so the mutating routes (GET / and the
    dismiss POST) run through their actual wiring rather than calling the handler functions directly."""
    iid = store.upsert_identity("main", "cred", None, True)
    # The spotlight lives in the alerts row, which Home gates on a completed first sync (and the
    # route only stamps shown-state under the same gate): mark the library synced.
    store.set_setting("last_sync_at", "900")
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now)
    return TestClient(app, base_url="http://127.0.0.1")


def _seed(store, sig="discovery_spike:100"):
    store.put_proposals("trend_rollups",
                        {"spotlight": {"signature": sig, "headline": "h", "detail": "d",
                                       "anchor": "discovery"}}, 0.0)


def test_due_when_novel_and_not_snoozed(store):
    _seed(store)
    assert spotlight_due_sig(store, now=1000.0) == "discovery_spike:100"


def test_not_due_when_already_shown(store):
    _seed(store)
    store.set_setting("trend_spotlight_last_signature", "discovery_spike:100")
    assert home.spotlight_due(store, now=1000.0) is None


def test_not_due_within_min_interval(store):
    _seed(store, sig="streak:30")
    store.set_setting("trend_spotlight_last_signature", "old_sig")
    store.set_setting("trend_spotlight_last_shown_at", "1000.0")
    # 2 days later (< 5-day interval) -> suppressed even though the signature is novel
    assert home.spotlight_due(store, now=1000.0 + 2 * 86400) is None
    # 6 days later (> interval) -> shows
    assert home.spotlight_due(store, now=1000.0 + 6 * 86400)["signature"] == "streak:30"


def test_snooze_blocks_same_signature(store):
    _seed(store, sig="streak:30")
    store.set_setting("trend_spotlight_dismissed_signature", "streak:30")
    store.set_setting("trend_spotlight_dismissed_at", "1000.0")
    assert home.spotlight_due(store, now=1000.0 + 10 * 86400) is None      # within 30-day snooze
    assert home.spotlight_due(store, now=1000.0 + 40 * 86400)["signature"] == "streak:30"   # snooze expired


def test_snooze_blocks_the_whole_kind_not_just_the_exact_signature(store):
    """L4: song_of_week (and several other detectors) mint a fresh signature every time they refire
    (a new week, a new track) -- a per-EXACT-signature snooze let a dismissed kind re-nudge on the very
    next occurrence under a new signature, defeating the point of dismissing it. Dismissing
    "song_of_week:14:k1" must snooze the song_of_week KIND, so next week's DIFFERENT song_of_week
    signature stays quiet too, while an unrelated kind is unaffected."""
    store.set_setting("trend_spotlight_dismissed_signature", "song_of_week:14:k1")
    store.set_setting("trend_spotlight_dismissed_at", "1000.0")
    _seed(store, sig="song_of_week:21:k2")   # a later week's DIFFERENT song_of_week signature
    assert home.spotlight_due(store, now=1000.0 + 6 * 86400) is None       # still snoozed: same kind
    _seed(store, sig="streak:30")            # an unrelated kind is not caught by the song_of_week snooze
    assert home.spotlight_due(store, now=1000.0 + 6 * 86400)["signature"] == "streak:30"
    _seed(store, sig="song_of_week:28:k3")
    # snooze expired (30 days) -> the kind is due again under its newest signature
    assert home.spotlight_due(store, now=1000.0 + 40 * 86400)["signature"] == "song_of_week:28:k3"


def test_absent_when_no_rollup(store):
    assert home.spotlight_due(store, now=1000.0) is None


def spotlight_due_sig(store, now):
    c = home.spotlight_due(store, now)
    return c["signature"] if c else None


# --- Integration tests: the three mutating paths, exercised through the real routes/build(). ---

def test_home_render_stamps_shown_and_then_stays_silent(store):
    """GET / renders the card for a due candidate, stamps the shown-signature + shown-at settings, and
    a second render (same signature, same instant) is silent per the same-signature rule."""
    _seed(store, sig="discovery_spike:100")
    c = _client(store, now=1000.0)

    assert store.get_setting("trend_spotlight_last_signature") is None   # sanity: nothing stamped yet
    html = c.get("/").text
    assert 'id="trend-spotlight"' in html
    assert "h" in html and "d" in html            # headline/detail from _seed's candidate

    assert store.get_setting("trend_spotlight_last_signature") == "discovery_spike:100"
    assert store.get_setting("trend_spotlight_last_shown_at") == "1000.0"

    # Same signature, no new rollup -> silent on the next render.
    html2 = c.get("/").text
    assert 'id="trend-spotlight"' not in html2


def test_dismiss_route_snoozes_and_home_stays_silent(store):
    """POST /trends/spotlight/dismiss writes the snooze setting for the given signature, and a
    subsequent Home render stays silent for that signature."""
    _seed(store, sig="streak:30")
    assert spotlight_due_sig(store, now=1000.0) == "streak:30"   # sanity: it would show if not dismissed

    c = _client(store, now=1000.0)
    r = c.post("/trends/spotlight/dismiss", params={"sig": "streak:30"})
    assert r.status_code == 200
    assert store.get_setting("trend_spotlight_dismissed_signature") == "streak:30"
    assert store.get_setting("trend_spotlight_dismissed_at") == "1000.0"

    html = c.get("/").text
    assert 'id="trend-spotlight"' not in html


# --- build()'s own stamp (rec/trend_rollups.py), fixture patterns borrowed from
# tests/rec/test_trend_rollups_build.py. ---

@pytest.fixture(autouse=True)
def _toy_genres(monkeypatch):
    from yt_playlist.util import genre_map
    monkeypatch.setattr(genre_map, "family", lambda g: (g or "").lower())
    monkeypatch.setattr(genre_map, "family_distance", lambda a, b: 0.0 if a == b else 0.5)


def _snap(store, day, keys):
    """One history snapshot at taken_at = day*86400 containing `keys`. Returns snapshot id."""
    cur = store.conn.execute("INSERT INTO history_snapshots(identity_id, taken_at) VALUES (1, ?)",
                             (day * 86400.0,))
    sid = cur.lastrowid
    for k in keys:
        store.conn.execute("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?, ?)", (sid, k))
    store.conn.commit()
    return sid


def _track(store, key, artist, genre=""):
    store.conn.execute(
        "INSERT INTO tracks(identity_key, video_id, title, artist, genre) VALUES (?,?,?,?,?)",
        (key, "v" + key, "T" + key, artist, genre))
    store.conn.commit()


def test_build_stamps_spotlight_review_month(store):
    """When build() computes a month-in-review recap AND it's the candidate that actually wins the
    spotlight cascade (no insight outranks it here), it stamps trend_spotlight_review_month so the next
    build() doesn't re-fire the month-rollover detector for the same month. now=59*86400.0 (Mar 1,
    1970-03) so Feb (the day-31 snapshot) is a genuinely completed past month under month_review's own
    `now` argument (not real wall-clock time.time())."""
    store.upsert_identity("me", "c", None, True)   # identity id=1, referenced by history_snapshots FK
    _track(store, "k1", "A1", "house")
    for d in (0, 1, 2, 3):
        _snap(store, d, ["k1"])
    _snap(store, 31, ["k1"])
    now = 59 * 86400.0

    assert store.get_setting("trend_spotlight_review_month") is None   # sanity: unset before build()
    payload = tr.build(store, now)
    assert payload["review"]["month"] == "1970-02"
    assert payload["spotlight"]["signature"] == "month_review:1970-02"   # review actually won the slot
    assert store.get_setting("trend_spotlight_review_month") == "1970-02"
