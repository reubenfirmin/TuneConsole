# tests/test_generated_play_signal.py
"""Playing a generated playlist is a verdict on the PLAYLIST, not on each track the app chose for it.

The app's own suggestions must not come back to it as evidence of taste, or it tells you you're
"into" an artist you never picked. The embedding baskets already hold this line (generated_only_keys:
"plays don't lift the quarantine, promotion does"); these pin it for the transient model, which is
what drives the Home "You're into X recently" card.
"""
from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params, recommend, transient
from yt_playlist.repos.base import GENERATED_GROUP


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _song(store, playlist_db_id, vid, title, artist, genre):
    """One tagged song, sitting in one playlist."""
    tid = store.upsert_track(vid, title, artist, None, 200)
    store.set_track_genre(tid, genre)
    store.set_playlist_tracks(playlist_db_id, [tid])
    return tid


def _library(store):
    iid = store.upsert_identity("main", "cred", None, True)
    mine = store.upsert_playlist(iid, "PLMINE", "My Mix", 1, "h", 1.0)
    gen = store.upsert_playlist(iid, "PLGEN", "Road Trip: Beach Run", 1, "h", 1.0)
    store.set_playlist_group("PLGEN", GENERATED_GROUP)
    return iid, mine, gen


def test_a_play_from_a_generated_playlist_is_not_taste_evidence():
    s = _store()
    iid, mine, gen = _library(s)
    _song(s, gen, "v_gen", "Sunday Mornin", "Kris Kristofferson", "Country")
    _song(s, mine, "v_own", "My Song", "My Band", "Techno")
    # Both played today. The YTM history sync is day-granular and carries NO playlist attribution,
    # so this is exactly how a generated playlist's plays arrive when the live bridge didn't see them.
    s.record_history_plays(iid, 1000.0, ["sunday mornin|kris kristofferson", "my song|my band"])

    leans = transient.facet_leans(s, 1000.0)

    assert leans.get("artist:My Band", 0.0) > 0.0            # a song of yours: evidence
    assert leans.get("artist:Kris Kristofferson", 0.0) == 0.0  # the app's own pick: not evidence
    country = recommend.genre_map.family("Country")
    assert leans.get(f"genre:{country}", 0.0) == 0.0        # nor does its genre lean


def test_rating_the_track_still_counts():
    """"...unless I rate it." A like is a deliberate statement and flows through its own channel."""
    s = _store()
    iid, mine, gen = _library(s)
    _song(s, gen, "v_gen", "Sunday Mornin", "Kris Kristofferson", "Country")
    s.set_setting("last_sync_at", "1000")
    s.record_like("sunday mornin|kris kristofferson", 1000.0, provenance="action")

    leans = transient.facet_leans(s, 1000.0)

    assert leans.get("artist:Kris Kristofferson", 0.0) > 0.0


def test_promoting_the_playlist_makes_its_plays_count():
    """Promotion is the deliberate act that lifts the quarantine - the rule the embedding already
    follows. Once you've adopted the playlist, listening to it is ordinary listening."""
    s = _store()
    iid, mine, gen = _library(s)
    _song(s, gen, "v_gen", "Sunday Mornin", "Kris Kristofferson", "Country")
    s.record_history_plays(iid, 1000.0, ["sunday mornin|kris kristofferson"])
    assert transient.facet_leans(s, 1000.0).get("artist:Kris Kristofferson", 0.0) == 0.0

    s.set_playlist_group("PLGEN", "")            # promoted out of the Generated bucket

    assert transient.facet_leans(s, 1000.0).get("artist:Kris Kristofferson", 0.0) > 0.0


def test_a_generated_track_you_also_own_still_counts():
    """The quarantine is about songs that are ONLY in a generated playlist. One that also sits in a
    real playlist of yours is yours, whatever else it turns up in."""
    s = _store()
    iid, mine, gen = _library(s)
    tid = s.upsert_track("v_both", "Both Places", "Shared Artist", None, 200)
    s.set_track_genre(tid, "Techno")
    s.set_playlist_tracks(gen, [tid])
    s.set_playlist_tracks(mine, [tid])
    s.record_history_plays(iid, 1000.0, ["both places|shared artist"])

    assert transient.facet_leans(s, 1000.0).get("artist:Shared Artist", 0.0) > 0.0


def test_evidence_plays_still_drops_radio_provenance():
    """The older provenance filter (machine-queued radio) keeps working alongside the new one."""
    s = _store()
    iid, mine, gen = _library(s)
    _song(s, mine, "v_own", "My Song", "My Band", "Techno")
    s.set_setting("radio_playlist_ytm", "PLRADIO")
    s.record_play_event(iid, "my song|my band", "v_own", 1000.0, playlist_ytm_id="PLRADIO")

    limit = rec_params.get_param(s, "recent_play_limit")
    assert transient.evidence_plays(s, limit) == []
