import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence as choices


@pytest.fixture
def store():
    result = Store(":memory:")
    result.init_schema()
    yield result
    result.conn.close()


def offer(store, identity="menu", episode="episode", now=100):
    return choices.issue(store, "albums", [{"id": "a", "score": .9}, {"id": "b", "score": .8}],
                         now, context={"layers": ["now", "session", "recent", "saved"]},
                         opportunity_id=identity, episode_id=episode)


def test_actual_visibility_controls_prediction_denominator_and_actions_are_distinct(store):
    oid = offer(store)
    choices.respond(store, oid, "visible", 101, visible=["b"])
    choices.respond(store, oid, "open", 102, option_id="b")
    choices.respond(store, oid, "choice", 103, option_id="b")
    choices.respond(store, oid, "start", 104, option_id="b")
    assert not choices.respond(store, oid, "start", 105, option_id="b")
    report = choices.compile_report(store, 106)
    row = report["formats"]["albums"]
    assert (row["offered"], row["visible"], row["choices"], row["starts"], row["saves"]) == (1, 1, 1, 1, 0)
    assert row["top_choice_rate"] == 1  # The higher-scored hidden album was never exposed.
    assert report["promotion_supported"] is False
    assert store.rec_proposals.get_proposals("listening_choices")["watermark"] == report["watermark"]


def test_reruns_correlated_choices_and_withdrawals_do_not_multiply_evidence(store):
    for index in range(2):
        oid = offer(store, identity=f"menu{index}")
        choices.respond(store, oid, "choice", 102 + index, option_id="a")
    first = choices.compile_report(store, 110)
    again = choices.compile_report(store, 120)
    assert first["watermark"] == again["watermark"]
    assert again["formats"]["albums"]["choices"] == 2
    assert again["formats"]["albums"]["independent_episodes"] == 1
    assert again["formats"]["albums"]["predicted_choices"] == 0  # No browser visibility.
    choices.respond(store, "menu0", "withdraw", 121, details={"action_id": "menu0:choice:a"})
    assert choices.compile_report(store, 122)["formats"]["albums"]["choices"] == 1
    assert choices.compile_report(store, 110)["formats"]["albums"]["choices"] == 2


def test_unknown_menus_wrong_options_and_chronology_are_not_invented(store):
    assert not choices.respond(store, "missing", "choice", 100, option_id="a")
    oid = offer(store)
    for kwargs in ({"option_id": "wrong"}, {"visible": ["wrong"]}, {}):
        with pytest.raises(ValueError):
            choices.respond(store, oid, "choice", 101, **kwargs)
    with pytest.raises(ValueError):
        choices.respond(store, oid, "choice", 99, option_id="a")
    assert choices.compile_report(store, 99)["source_opportunities"] == 0


def test_mix_playback_resolves_recipe_from_exact_external_queue(store):
    iid = store.identities.upsert_identity("main", "test", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL-generated", "Generated mix", 1, "h", 1)
    oid = choices.issue(store, "mixes", [{"id": "comfort", "tracks": []}], 100,
                        context={}, opportunity_id="mix")
    store.playlist_recipes.set_recipe("PL-generated", {"opportunity_id": oid,
                                                      "option_id": "comfort"}, now=101)
    choices.record_playback(store, {"playlist": "PL-other", "videoId": "same-song"}, 102)
    assert not store.rec_ledger.evidence().records("response")
    choices.record_playback(store, {"playlist": "PL-generated", "videoId": "same-song"}, 103)
    choices.record_playback(store, {"playlist": "PL-generated", "videoId": "same-song"}, 104)
    events = store.rec_ledger.evidence().records("response")
    assert {row["kind"] for row in events} == {"start", "experience"}
    assert len(events) == 2
    assert all(row["details"]["playlist_id"] == pid for row in events)


def test_daily_replay_predicts_before_observing_and_reuses_independent_units(store):
    class Candidate:
        def __init__(self):
            self.observed = []
            self.predictions = []

        def predict(self, context, options):
            self.predictions.append(list(self.observed))
            return [option["id"] for option in options]

        def observe(self, event):
            self.observed.append(event["id"])

    for index in range(3):
        oid = offer(store, identity=f"menu{index}", now=100 + index * 10)
        choices.respond(store, oid, "visible", 101 + index * 10, visible=["a", "b"])
        choices.respond(store, oid, "choice", 102 + index * 10, option_id="a")
    candidate = Candidate()
    replay = choices.replay(store, 130, candidate)
    assert candidate.predictions[0] == []
    assert candidate.predictions[1] == ["menu0:visible:", "menu0:choice:a"]
    row = replay["formats"]["albums"]
    assert row["scored"] == 3 and row["episode_days"] == 1
    assert row["interval95"] is None  # Three decisions in one episode are not three trials.
    first = choices.daily(store, 130)
    assert choices.daily_due(store, 130 + 86399) is False
    assert choices.daily_due(store, 130 + 86400)
    assert choices.daily(store, 140)["source_ids"] == first["source_ids"]
    assert len(store.rec_ledger.evidence().records("report")) == 1


def test_future_context_is_excluded_and_correction_revises_replay(store):
    oid = choices.issue(store, "albums", [{"id": "a", "score": 1}], 100,
                        context={"at": 200}, opportunity_id="future")
    choices.respond(store, oid, "visible", 101, visible=["a"])
    choices.respond(store, oid, "choice", 102, option_id="a")
    row = choices.replay(store, 110)["formats"]["albums"]
    assert row["future_context"] == 1 and row["scored"] == 0
    choices.respond(store, oid, "withdraw", 120, details={"action_id": "future:choice:a"})
    assert choices.replay(store, 121)["formats"]["albums"]["choices"] == 0
    assert choices.replay(store, 110)["formats"]["albums"]["choices"] == 1


def test_delayed_open_cannot_leak_intervening_responses_into_prediction(store):
    class Candidate:
        def __init__(self):
            self.observed = []
            self.predictions = []

        def predict(self, context, options):
            self.predictions.append(list(self.observed))
            return [option["id"] for option in options]

        def observe(self, event):
            self.observed.append(event["id"])

    old = offer(store, identity="old", now=100)
    new = offer(store, identity="new", now=110)
    choices.respond(store, new, "choice", 120, option_id="b")
    choices.respond(store, old, "visible", 130, visible=["a", "b"])
    choices.respond(store, old, "choice", 140, option_id="a")
    candidate = Candidate()
    choices.replay(store, 150, candidate)
    assert candidate.predictions == [[], []]


def test_prepared_context_reference_survives_maintenance_and_issues_exact_capture(store):
    context = {'at': 100, 'layers': {'saved': {'centroids': [[1, 0]]}},
               'policy': {'version': 1, 'basis': 'x' * 70000}}
    reference = choices.freeze_context(store, context)
    assert set(reference) == {'artifact'}
    store.rec_proposals.put_proposals('album_prepared', {'context': reference}, 100)
    # Maintenance must keep a prepared capture even before its first exposure.
    choices.daily(store, 10 ** 12)
    assert choices.resolve_context(store, reference) == context
    changed = choices.freeze_context(store, {**context, 'at': 101})
    assert changed != reference
    artifacts = store.rec_ledger.evidence().records('artifact')
    assert len(artifacts) == 3  # Two small manifests share one large basis component.
    oid = choices.issue(store, 'radio', [{'id': 'branch', 'score': 1}], 101,
        context={**reference, 'radio': {'fingerprint': 'actual-direction-pool'}})
    ledger = store.rec_ledger.evidence()
    capture = choices.resolve_context(store, {'artifact': ledger.get('opportunity', oid)['context']})
    assert capture == {**context, 'radio': {'fingerprint': 'actual-direction-pool'}}
