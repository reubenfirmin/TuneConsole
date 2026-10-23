import numpy as np
import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from yt_playlist.rec.mode_eval import choice_evidence, mode_bandit_stats
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _client(store, now=1000.0):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def test_taste_shows_offered_and_picked(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "house", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    store.modes.log_impressions(1, [("wheelhouse", 1)], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=110.0)
    store.modes.log_pick(playlist_id=5, mode_id=1, now=120.0)
    r = _client(store).get("/taste/panels/generated")
    assert r.status_code == 200
    assert "House" in r.text
    assert "Offers" in r.text and "Selections" in r.text
    assert "50/100" in r.text     # Beta(2, 2), not a card appearance forecast
    assert "Estimated range" in r.text
    assert "Dot position: choice score" in r.text
    assert "Bar width: uncertainty" in r.text
    assert "your saved genre weights are separate" in r.text


def _styles(store, count=3):
    store.modes.replace_modes([
        {"mode_id": mid, "label": f"Style {mid}", "families": [],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []}
        for mid in range(1, count + 1)
    ], retired_ids=[], now=1.0)


def test_overview_leaves_choice_score_calculation_in_generated_playlists(store):
    _styles(store, 1)
    for epoch in range(56):
        store.modes.log_impressions(epoch, [("fresh", 1)], now=float(epoch))
    for playlist in range(21):
        store.modes.log_pick(playlist, 1, now=100.0)
    client = _client(store)
    overview = client.get("/taste/panels/overview").text
    assert "21 selections · 56 offers" in overview
    assert "Still learning which groups to bring back" in overview
    assert "38/100" not in overview and "assumed selection" not in overview
    generated = client.get("/taste/panels/generated").text
    assert "21 selections from 56 offers" in generated
    assert "38/100" in generated
    assert "its bar spans" in generated


def test_choice_chart_uses_the_sampler_evidence_and_shows_uncertainty(store):
    _styles(store)
    for epoch in range(100):
        store.modes.log_impressions(epoch, [("fresh", 1), ("comfort", 2)], now=float(epoch))
    for playlist in range(30):
        store.modes.log_pick(playlist, 1, now=100.0)
    evidence = choice_evidence(store)
    a, b, untried = evidence["rows"]
    assert mode_bandit_stats(store) == {1: (30, 100), 2: (0, 100)}
    assert a["estimate"] == pytest.approx(31 / 102)
    assert b["estimate"] == pytest.approx(1 / 102)
    assert untried["estimate"] == 0.5
    assert not untried["has_evidence"]
    assert a["low"] < a["estimate"] < a["high"]
    assert a["high"] - a["low"] < untried["high"] - untried["low"]
    assert evidence["strongest"] == [a]       # no unsupported styles in the Overview summary
    assert evidence == choice_evidence(store)  # stable intervals on reload


def test_choice_evidence_preserves_repeated_selections_without_an_invalid_rate(store):
    _styles(store, 1)
    store.modes.log_impressions(1, [("fresh", 1)], now=1.0)
    for playlist in range(4):
        store.modes.log_pick(playlist, 1, now=2.0)
    row = choice_evidence(store)["rows"][0]
    assert (row["picked"], row["offered"]) == (4, 1)
    assert row["estimate"] == pytest.approx(5 / 6)
    assert 0 <= row["low"] < row["high"] <= 1


def test_orphan_picks_keep_the_sampler_prior_and_retired_styles_are_excluded(store):
    _styles(store, 2)
    store.modes.log_pick(1, 1, now=2.0)
    store.modes.log_impressions(1, [("fresh", 2)], now=1.0)
    store.modes.log_pick(2, 2, now=2.0)
    store.modes.replace_modes([], retired_ids=[2], now=3.0)
    evidence = choice_evidence(store)
    assert evidence["offered"] == 0 and evidence["picked"] == 1
    assert len(evidence["rows"]) == 1
    assert evidence["rows"][0]["estimate"] == 0.5
    assert evidence["strongest"] == []


def test_overview_compares_supported_preferences_without_forecasting_menu_shares(store):
    _styles(store, 4)
    for epoch in range(100):
        store.modes.log_impressions(epoch, [("fresh", 1), ("comfort", 2), ("explore", 3)],
                                    now=float(epoch))
    for playlist in range(30):
        store.modes.log_pick(playlist, 1, now=100.0)
    for playlist in range(30, 34):
        store.modes.log_pick(playlist, 3, now=100.0)
    evidence = choice_evidence(store)
    contrast = evidence["contrast"]
    assert [r["mode_id"] for r in contrast["favored"]] == [1]
    assert [r["mode_id"] for r in contrast["less_supported"]] == [2]
    assert all(a["low"] > b["high"] for a in contrast["favored"]
               for b in contrast["less_supported"])
    html = _client(store).get("/taste/panels/overview").text
    assert "Your choices favor" in html and "<h3>Over</h3>" in html
    assert "30 selections · 100 offers" in html
    assert "0 selections · 100 offers" in html
    assert "Want a different direction?" not in html
    assert 'href="/#directions"' not in html
    assert "choice-chart-row" not in html and "/100" not in html
    assert "Style 4" not in html  # an untested group isn't evidence of a rejection


def test_choice_contrast_waits_when_preferences_overlap(store):
    _styles(store, 2)
    for epoch in range(2):
        store.modes.log_impressions(epoch, [("fresh", 1), ("comfort", 2)], now=float(epoch))
    store.modes.log_pick(1, 1, now=10.0)
    evidence = choice_evidence(store)
    assert evidence["contrast"] is None
    html = _client(store).get("/taste/panels/overview").text
    assert "There isn’t a clear preference to compare yet" in html
    assert "Your choices favor" not in html


def test_choice_contrast_does_not_oppose_or_duplicate_identical_genre_labels(store):
    store.modes.replace_modes([
        {"mode_id": mid, "label": label, "families": [],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []}
        for mid, label in enumerate(["techno", "electro-synth", "Electro-Synth", "techno"], 1)
    ], retired_ids=[], now=1.0)
    for epoch in range(100):
        store.modes.log_impressions(epoch, [("fresh", 1), ("comfort", 2), ("explore", 3),
                                           ("wheelhouse", 4)], now=float(epoch))
    for playlist in range(30):
        store.modes.log_pick(playlist, 1, now=100.0)
    contrast = choice_evidence(store)["contrast"]
    assert [r["label"] for r in contrast["favored"]] == ["techno"]
    assert [r["label"] for r in contrast["less_supported"]] == ["electro-synth"]
