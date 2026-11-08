"""Each specialist's real rollout has distinct bounded behavioral owners."""
from types import SimpleNamespace

import pytest

from tests import test_audio_vibe_cruise as cruise_cases
from tests import test_audio_vibe_release as release_cases
from tests import test_audio_vibe_relax as relax_cases
from tests import test_audio_vibe_uplift as uplift_cases
from tests import test_audio_vibe_energize as energize_cases
from tests import test_audio_vibe_focus as focus_cases
from tests.rec.test_vibe_prediction import drain, fits, track
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader
from yt_playlist.core import features
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.core.store import Store
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor
from yt_playlist.rec import home_vibe, musical_character, vibe_joint_preparation, vibe_preparation


CASES = (('cruise', cruise_cases), ('release', release_cases), ('relax', relax_cases),
         ('uplift', uplift_cases), ('energize', energize_cases), ('focus', focus_cases))


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Model, SQL and file owners do not start an HTTP lifecycle."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('psybient', 'Psybient'),)) as schema:
        yield schema


def test_rollout_schema_input_keeps_real_history_foreign_keys_and_snapshot_guards(_pristine_current_schema):
    assert _pristine_current_schema.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] > 0
    assert _pristine_current_schema._revision_script
    assert not _pristine_current_schema.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module', params=CASES, ids=[name for name, _ in CASES])
def rollout(request, _pristine_current_schema, tmp_path_factory):
    name, cases = request.param
    root = tmp_path_factory.mktemp('specialist-rollout-' + name)
    store = Store(root/'runtime.db')
    try:
        initialize_consumer(store, _pristine_current_schema)
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(features, 'LOCAL_ENV', root/'.env.local')
            # The independent joint-model lifecycle retains its own real
            # fitting/publication owners. All six individual models stay real.
            patch.setattr(vibe_joint_preparation, 'qualified', lambda: False)
            patch.setenv(shared.ENVIRONMENT_KEY, str(shared.DEFAULT_PATH))
            for other_name, other_cases in CASES:
                module = getattr(other_cases, other_name)
                patch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
                patch.setenv(module.MODE_KEY, 'active')
            module = getattr(cases, name)
            patch.setenv(module.MODE_KEY, 'off')
            with reuse_reporting_reader(store, patch):
                yield SimpleNamespace(name=name, vibe=getattr(module, 'VIBE', name), cases=cases, module=module, root=root,
                    store=store, patch=patch, mode='off', artifact=module.DEFAULT_PATH)
    finally:
        store.conn.close()


@pytest.fixture(autouse=True)
def model_environment(isolated_app_home, monkeypatch, request):
    if 'rollout' not in request.fixturenames:
        return
    state = request.getfixturevalue('rollout')
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, str(shared.DEFAULT_PATH))
    for name, cases in CASES:
        module = getattr(cases, name)
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
        monkeypatch.setenv(module.MODE_KEY, 'active')
    monkeypatch.setenv(state.module.ENVIRONMENT_KEY, str(state.artifact))
    monkeypatch.setenv(state.module.MODE_KEY, state.mode)


@pytest.fixture(scope='module')
def catalog_input(rollout):
    _, rollout.own = track(rollout.store, 'observed_song')
    _, rollout.neighbour = track(rollout.store, 'neighbour')
    rollout.rows = cruise_cases.samples()
    rollout.keys = [rollout.own, rollout.neighbour]
    return rollout


def test_rollout_catalog_input_keeps_distinct_real_subjects(catalog_input):
    assert len(set(catalog_input.keys)) == 2
    assert not catalog_input.store.conn.execute('SELECT 1 FROM measurements').fetchone()


@pytest.fixture(scope='module')
def empty_rollout(catalog_input):
    drain(catalog_input.store, batch=1).close()
    return catalog_input


def test_no_capture_uses_heuristic_fallback(empty_rollout):
    assert fits(empty_rollout.store, empty_rollout.own)[empty_rollout.vibe]['source'] == 'heuristic'


@pytest.fixture(scope='module')
def partial_rollout(empty_rollout):
    for now, row in enumerate(empty_rollout.rows[:2], 1):
        empty_rollout.store.audio_vibes.save_sample(row, now)
    drain(empty_rollout.store, batch=1).close()
    return empty_rollout


def test_partial_capture_keeps_heuristic_fallback(partial_rollout):
    assert fits(partial_rollout.store, partial_rollout.own)[partial_rollout.vibe]['source'] == 'heuristic'


@pytest.fixture(scope='module')
def complete_rollout(partial_rollout):
    state, store = partial_rollout, partial_rollout.store
    store.audio_vibes.save_sample(state.rows[2], 3)
    store.audio_vibes.save_feedback(state.rows[0]['run_id'], [], [state.vibe], predictor.TAXONOMY_VERSION, None, 4)
    state.feedback = store.audio_vibes.feedback(state.rows[0]['run_id'])
    drain(store, batch=1).close()
    state.before = {key: fits(store, key) for key in state.keys}
    state.profiles = musical_character.load(store, state.keys)
    return state


def test_complete_capture_keeps_one_recording_and_real_feedback(complete_rollout):
    state = complete_rollout
    assert state.before[state.own][state.vibe]['source'] == 'audio_measurement'
    assert state.before[state.neighbour][state.vibe]['recordings'] == 1
    assert state.store.audio_vibes.feedback(state.rows[0]['run_id']) == state.feedback


def replace_artifact(state, *, replacement=False):
    if state.name == 'cruise':
        return state.cases.controlled(state.root, state.rows, valence=.2 if replacement else .8)
    if state.name == 'focus':
        return state.cases.controlled(state.root, (.2, .4) if replacement else (.8, 1.))
    return state.cases.controlled(state.root, .3 if replacement else .9)


@pytest.fixture(scope='module')
def active_rollout(complete_rollout):
    state = complete_rollout
    stamp = vibe_preparation.version()
    state.artifact = replace_artifact(state)
    state.mode = 'active'
    state.patch.setenv(state.module.ENVIRONMENT_KEY, str(state.artifact))
    state.patch.setenv(state.module.MODE_KEY, state.mode)
    assert vibe_preparation.version() != stamp
    state.statements = []
    state.store.conn.set_trace_callback(state.statements.append)
    drain(state.store, batch=1).close()
    return state


def test_activation_changes_only_its_own_vote_and_propagates_to_one_neighbour(active_rollout):
    state = active_rollout
    profiles = musical_character.load(state.store, state.keys)
    for key in state.keys:
        after = fits(state.store, key)
        assert set(after) == set(shared.LABELS)
        for vibe in shared.LABELS:
            if vibe != state.vibe:
                assert after[vibe] == state.before[key][vibe]
        assert profiles[key]['vibes']['intensity_fits'] == state.profiles[key]['vibes']['intensity_fits']
        assert home_vibe.affinity({'key': key}, state.vibe, profiles) == after[state.vibe]['score']
    assert fits(state.store, state.own)[state.vibe]['score'] == pytest.approx(.9)
    neighbour = fits(state.store, state.neighbour)[state.vibe]
    assert neighbour['recordings'] == 1
    assert neighbour['score'] > state.before[state.neighbour][state.vibe]['score']
    measured = vibe_preparation.measured({'samples': state.rows, 'metadata': {}}, [], {}, {})['fits']
    shared_scores = {row['vibe']: row['score'] for row in predictor.predict(state.rows)['ranking']}
    for channel, vibe in INTENSITY_CHANNELS.items():
        assert measured[channel]['score'] == shared_scores[vibe]


@pytest.fixture(scope='module')
def replaced_rollout(active_rollout):
    stamp = vibe_preparation.version()
    replace_artifact(active_rollout, replacement=True)
    assert vibe_preparation.version() != stamp
    drain(active_rollout.store, batch=1).close()
    return active_rollout


def test_replaced_artifact_recomputes_existing_capture_without_an_extra_vote(replaced_rollout):
    state = replaced_rollout
    assert fits(state.store, state.own)[state.vibe]['score'] == pytest.approx(.6 if state.name == 'cruise' else .3)
    assert fits(state.store, state.neighbour)[state.vibe]['recordings'] == 1


@pytest.fixture(scope='module')
def continued_rollout(replaced_rollout):
    for now, row in enumerate(cruise_cases.samples(), 10):
        replaced_rollout.store.audio_vibes.save_sample(row, now)
    drain(replaced_rollout.store, batch=1).close()
    return replaced_rollout


def test_new_complete_captures_keep_one_recording_in_the_cohort(continued_rollout):
    assert fits(continued_rollout.store, continued_rollout.neighbour)[continued_rollout.vibe]['recordings'] == 1


@pytest.fixture(scope='module')
def rolled_back(continued_rollout):
    state = continued_rollout
    state.history = {row[0] for row in state.store.conn.execute('SELECT id FROM measurements')}
    state.mode = 'off'
    state.patch.setenv(state.module.MODE_KEY, state.mode)
    drain(state.store, batch=1).close()
    return state


def test_disabling_artifact_restores_exact_fits_without_losing_history_or_feedback(rolled_back):
    state = rolled_back
    assert {key: fits(state.store, key) for key in state.keys} == state.before
    assert state.history <= {row[0] for row in state.store.conn.execute('SELECT id FROM measurements')}
    state.store.conn.set_trace_callback(None)
    forbidden = [sql for sql in state.statements if 'audio_annotation' in sql or 'audio_taxonom' in sql]
    assert not forbidden, forbidden[:3]
    assert state.store.audio_vibes.feedback(state.rows[0]['run_id']) == state.feedback
    assert not state.store.conn.execute('PRAGMA foreign_key_check').fetchone()
