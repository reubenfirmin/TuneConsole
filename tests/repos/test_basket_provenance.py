"""#83 session baskets exclude generated-only plays; organic and mixed plays still co-occur.

rec_baskets' SESSION basket group uses reconciled account calendar days. A known YTM sync shadow
must drop out when the app's generated playlist was the only evidence for that play. A day where two tracks co-occurred
but one of them is generated-only that day must yield a session basket WITHOUT the generated-only
key (and if that leaves a single track, the basket is dropped entirely by the existing 1 < len(s)
filter -- the same outcome the pre-#83 code already gives an all-alone track). Mixed evidence (an
ordinary play alongside the generated one, on the same day) is conservatively kept IN, so the
session basket stays whole. Playlist/album/artist/content baskets are untouched by this filter.

Basket membership is asserted by content (a frozenset of keys), not list index, since playlist/
album/artist/content baskets also come out of rec_baskets() in the same flat list.
"""
from yt_playlist.util.matching import identity_key
from yt_playlist.repos.rec_query import GENERATED_GROUP

_DAY = 86400
_NOON = 43200


def _mark_generated(store, ytm):
    store.playlists.set_playlist_group(ytm, GENERATED_GROUP)
    store.playlists.mark_generated_playlist(ytm)


def _session_baskets(store):
    """frozensets of every basket rec_baskets() returns, so membership can be asserted without
    caring about basket order or which non-session baskets (playlist/album/artist/content) also
    happen to be present in the same flat list."""
    return {frozenset(b) for b in store.rec_query.rec_baskets()}


def test_generated_only_key_dropped_from_session_basket(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    ka = store.tracks.upsert_track("va", "A", "ArtA", None, None, 1)
    kb = store.tracks.upsert_track("vb", "B", "ArtB", None, None, 1)
    kc = store.tracks.upsert_track("vc", "C", "ArtC", None, None, 1)
    key_a, key_b, key_c = identity_key("A", "ArtA"), identity_key("B", "ArtB"), identity_key("C", "ArtC")
    del ka, kb, kc

    # Day 0: A, B, C co-occur in dated YTM observations. A's ONLY play evidence that day came from
    # the quarantined generated playlist -- it must be dropped from the session basket.
    store.history.record_history_plays(iid, _NOON,
        [(key, 'Today') for key in (key_a, key_b, key_c)], source_timezone='UTC')
    store.history.record_play_event(iid, key_a, "va", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')

    baskets = _session_baskets(store)
    assert frozenset({key_b, key_c}) in baskets            # organic pair survives, minus the tainted key
    assert not any(key_a in b and (key_b in b or key_c in b) for b in baskets)


def test_two_track_day_fully_dropped_when_one_key_is_generated_only(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.tracks.upsert_track("va", "A", "ArtA", None, None, 1)
    store.tracks.upsert_track("vb", "B", "ArtB", None, None, 1)
    key_a, key_b = identity_key("A", "ArtA"), identity_key("B", "ArtB")

    # Day 0: just A (generated-only) and B (organic) co-occur -- once A drops out, only a
    # 1-member basket is left, which the existing 1 < len(s) filter already discards entirely.
    store.history.record_history_plays(iid, _NOON,
        [(key, 'Today') for key in (key_a, key_b)], source_timezone='UTC')
    store.history.record_play_event(iid, key_a, "va", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')

    baskets = _session_baskets(store)
    assert not any({key_a, key_b} <= b for b in baskets)   # never co-occur together
    assert not any(b == {key_b} for b in baskets)          # solo basket never emitted (size filter)


def test_mixed_evidence_day_keeps_the_full_session_basket(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.tracks.upsert_track("va", "A", "ArtA", None, None, 1)
    store.tracks.upsert_track("vb", "B", "ArtB", None, None, 1)
    key_a, key_b = identity_key("A", "ArtA"), identity_key("B", "ArtB")

    # Day 0: A has BOTH a generated play and an ordinary play (spaced past the 1800s live-dedup
    # merge window so they land as two distinct play_events rows) -- mixed evidence, so A stays.
    store.history.record_history_plays(iid, _NOON,
        [(key, 'Today') for key in (key_a, key_b)], source_timezone='UTC')
    store.history.record_play_event(iid, key_a, "va", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    store.history.record_play_event(iid, key_a, "va", 3000.0, playlist_ytm_id=None, source_timezone='UTC')

    baskets = _session_baskets(store)
    assert frozenset({key_a, key_b}) in baskets


def test_organic_day_untouched(store):
    """Sanity: with no generated-playlist provenance at all, session baskets behave exactly as
    before (#83 must be a no-op absent quarantined playlists)."""
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.tracks.upsert_track("va", "A", "ArtA", None, None, 1)
    store.tracks.upsert_track("vb", "B", "ArtB", None, None, 1)
    key_a, key_b = identity_key("A", "ArtA"), identity_key("B", "ArtB")

    store.history.record_history_plays(iid, _NOON,
        [(key, 'Today') for key in (key_a, key_b)], source_timezone='UTC')

    baskets = _session_baskets(store)
    assert frozenset({key_a, key_b}) in baskets
