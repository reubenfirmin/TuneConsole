# One-shot like-ratchet repair (rec/repair.py): removes the surplus permanent-weight nudges the
# likeStatus flap baked in (the same likes re-graduating daily). Fixtures mirror the live shape
# that surfaced the bug: a handful of real likes on one artist vs a pile of like-source log rows.
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import recommend, repair
from yt_playlist.util.matching import identity_key

NOW = 1_000_000.0


def _store():
    s = Store(":memory:")
    s.init_schema()                        # runs + stamps the repair on the (empty) fresh DB
    s.delete_setting(repair.STAMP_KEY)     # re-arm so tests exercise it against built fixtures
    return s


def _metrik_fixture(s):
    """3 real likes on one artist, 14 like-source grad-log rows, weight pinned at the 2.0 cap,
    polluted pending pressure in the ledger."""
    for i, title in enumerate(("automata", "thunderblade", "dying light")):
        s.upsert_track(f"m{i}", title, "Metrik", None, None)
        s.record_like(identity_key(title, "Metrik"), NOW - 1000 + i)
    for i in range(14):
        s.log_graduation("artist:Metrik", "like", 2.0, 1.05, 2.0, NOW - 500 + i)
    s.set_weight("artist:Metrik", 2.0, now=NOW)
    s.bump_theme("artist:Metrik", 0.8, NOW)
    return "artist:Metrik"


def test_plan_metrik_shape_entitlement_and_surplus():
    s = _store()
    axis = _metrik_fixture(s)
    plan = repair.plan_like_ratchet_repair(s, NOW)
    adj = {a["axis"]: a for a in plan["adjustments"]}[axis]
    # replaying 3 likes at the old weight 1.0 against threshold 1.2: 1.0 (no), 2.0 -> graduate
    # (0.8 left), 1.8 -> graduate (0.6 left) = 2 entitled; 14 logged - 2 = 12 surplus
    assert (adj["actual"], adj["entitled"], adj["surplus"]) == (14, 2, 12)
    assert adj["weight_before"] == pytest.approx(2.0)
    assert adj["weight_after"] == pytest.approx(2.0 / 1.05 ** 12)   # ~1.114, far off the cap
    assert adj["theme_before"] == pytest.approx(0.8)


def test_plan_is_a_dry_run():
    s = _store()
    axis = _metrik_fixture(s)
    repair.plan_like_ratchet_repair(s, NOW)
    assert s.get_weights(now=NOW)[axis] == pytest.approx(2.0)   # nothing written
    assert s.get_theme(axis) == pytest.approx(0.8)
    assert s.get_setting(repair.STAMP_KEY) is None


def test_apply_repairs_weight_zeroes_theme_and_is_idempotent():
    s = _store()
    axis = _metrik_fixture(s)
    summary = repair.apply_like_ratchet_repair(s, NOW)
    assert summary and s.get_setting(repair.STAMP_KEY) == summary
    assert s.get_weights(now=NOW)[axis] == pytest.approx(2.0 / 1.05 ** 12)
    assert s.get_theme(axis) == pytest.approx(0.0)   # polluted pending pressure gone
    # idempotent: the stamp blocks a second pass even if more log rows appear
    s.log_graduation(axis, "like", 2.0, 1.05, 2.0, NOW + 1)
    assert repair.apply_like_ratchet_repair(s, NOW + 2) is None
    assert s.get_weights(now=NOW)[axis] == pytest.approx(2.0 / 1.05 ** 12)   # unchanged


def test_axes_with_only_legit_graduations_untouched():
    s = _store()
    _metrik_fixture(s)
    for i in range(3):
        s.log_graduation("genre:techno", "play", 1.3, 1.05, 1.3, NOW - 400 + i)
    s.set_weight("genre:techno", 1.3, now=NOW)
    s.bump_theme("genre:techno", 0.5, NOW)
    plan = repair.plan_like_ratchet_repair(s, NOW)
    assert "genre:techno" not in {a["axis"] for a in plan["adjustments"]}
    repair.apply_like_ratchet_repair(s, NOW)
    assert s.get_weights(now=NOW)["genre:techno"] == pytest.approx(1.3)
    assert s.get_theme("genre:techno") == pytest.approx(0.5)     # earned pressure kept


def test_nonlike_entitlement_floors_the_correction():
    s = _store()
    # 5 surplus like graduations (no current likes carry this axis), but 4 play graduations
    # remain earned: the repair may not undo those
    for i in range(5):
        s.log_graduation("genre:funk", "like", 1.3, 1.05, 1.5, NOW - 500 + i)
    for i in range(4):
        s.log_graduation("genre:funk", "play", 1.3, 1.05, 1.5, NOW - 400 + i)
    s.set_weight("genre:funk", 1.5, now=NOW)
    plan = repair.plan_like_ratchet_repair(s, NOW)
    adj = {a["axis"]: a for a in plan["adjustments"]}["genre:funk"]
    assert adj["surplus"] == 5
    # target 1.5 / 1.05**5 ~ 1.175 sits below the earned play entitlement 1.05**4 ~ 1.216
    assert adj["weight_after"] == pytest.approx(1.05 ** 4)


def test_weight_floor_is_one():
    s = _store()
    for i in range(10):
        s.log_graduation("era:2020", "like", 1.3, 1.05, 1.05, NOW - 500 + i)
    s.set_weight("era:2020", 1.05, now=NOW)
    plan = repair.plan_like_ratchet_repair(s, NOW)
    adj = {a["axis"]: a for a in plan["adjustments"]}["era:2020"]
    assert adj["weight_after"] == pytest.approx(1.0)             # never below the neutral prior


def test_backfills_sync_provenance_and_once_ever_stamps():
    s = _store()
    axis = _metrik_fixture(s)
    # a legacy like row from before provenance existed (reason NULL)
    s.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) "
                   "VALUES ('like','old song|band','like',NULL,'',NULL,?)", (NOW - 2000,))
    s.conn.commit()
    repair.apply_like_ratchet_repair(s, NOW)
    assert s.like_provenance("old song|band") == "sync"          # stamped, so never transient
    assert s.recent_liked_with_ts() == []                        # no action-provenance likes at all
    # once-ever stamps were backfilled: a post-repair flap (clear + re-record) cannot re-graduate
    key = identity_key("automata", "Metrik")
    recommend.apply_dislikes(s, {key: "INDIFFERENT"}, NOW + 10)  # like row cleared
    recommend.apply_dislikes(s, {key: "LIKE"}, NOW + 20)         # re-recorded
    assert key in s.recent_liked_keys()
    assert s.get_theme(axis) == pytest.approx(0.0)               # ledger NOT fed again
