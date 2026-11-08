"""Full-size live experiment evidence, private copies and independent replay."""
from contextlib import closing
import sqlite3

import pytest

from tests.prepared_fixture_support import capture_template, cleanup_memory_files, roots
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from tests.test_experiment_ledger import _seed_mature_live_arms
from yt_playlist.core.store import Store
from yt_playlist.rec import experiments, scorecard


BASE = 100 * 86400
NOW = BASE + 100000


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('mature-evidence-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        yield


@pytest.fixture(scope='module')
def evidence_schema():
    # Evidence consumers do not own schema/vocabulary construction.
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'))) as schema:
        yield schema


def test_evidence_input_is_read_only_and_keeps_real_snapshot_guards(evidence_schema):
    assert evidence_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert evidence_schema._revision_script


@pytest.fixture(scope='module')
def prefix_source(evidence_schema):
    store = Store(':memory:')
    with closing(store.conn):
        initialize_consumer(store, evidence_schema)
        cleanup_memory_files(store, roots(store.conn))
        assert _seed_mature_live_arms(store, episodes=range(1, 100)) == BASE
        yield store


def test_real_prefix_keeps_all_99_distinct_assignments_and_generated_responses(prefix_source):
    assert prefix_source.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 198
    assert len(prefix_source.player_events.player_events_since(0)) == 198
    assert not prefix_source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def frozen_prefix(prefix_source):
    return capture_template(prefix_source, BASE)


def report(store):
    return scorecard.experiment_report(store, NOW - 90 * 86400, NOW, NOW)


def test_exact_99_cluster_prefix_cannot_power_either_arm(frozen_prefix):
    prefix = report(frozen_prefix.source)
    assert set(prefix['live']) == {'c1:ppr', 'c1:cosine'}
    assert all(arm['clusters'] == arm['impressions'] == 99 and not arm['powered']
               for arm in prefix['live'].values())
    assert frozen_prefix.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        with frozen_prefix.source.conn:
            frozen_prefix.source.conn.execute("INSERT INTO settings VALUES ('private','forbidden')")


@pytest.fixture(scope='module')
def memory_consumer(frozen_prefix):
    with frozen_prefix.open(':memory:') as store:
        cleanup_memory_files(store, roots(store.conn))
        yield store


def test_private_memory_copy_has_the_exact_prefix_and_cannot_promote(memory_consumer, frozen_prefix):
    assert report(memory_consumer) == report(frozen_prefix.source)
    with pytest.raises(ValueError, match='100 mature'):
        experiments.decide(memory_consumer, 'promote', 'c1', 'The last episode is still missing', NOW)
    assert not experiments.decisions(memory_consumer)


@pytest.fixture(scope='module')
def sibling_consumer(frozen_prefix, tmp_path_factory):
    path = tmp_path_factory.mktemp('mature-evidence-sibling') / 'sibling.db'
    with frozen_prefix.open(path) as store:
        yield store


def test_private_wal_copy_has_the_exact_prefix_and_cannot_promote(sibling_consumer, frozen_prefix):
    assert sibling_consumer.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert report(sibling_consumer) == report(frozen_prefix.source)
    with pytest.raises(ValueError, match='100 mature'):
        experiments.decide(sibling_consumer, 'promote', 'c1', 'The last episode is still missing', NOW)
    assert not experiments.decisions(sibling_consumer)


def test_all_retained_files_have_equal_bytes_and_three_independent_inodes(
        frozen_prefix, memory_consumer, sibling_consumer):
    assert len({store._evidence_key for store in (frozen_prefix.source, memory_consumer, sibling_consumer)}) == 3
    destinations = [roots(store.conn) for store in (memory_consumer, sibling_consumer)]
    copied = 0
    for name, directory in frozen_prefix.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if source.is_file():
                paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                assert all(path.read_bytes() == source.read_bytes() for path in paths)
                assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                copied += 1
    assert copied


@pytest.fixture(scope='module')
def completed_copies(memory_consumer, sibling_consumer):
    assert _seed_mature_live_arms(memory_consumer, episodes=range(1)) == BASE
    assert _seed_mature_live_arms(sibling_consumer, episodes=range(1), controlled=False) == BASE
    sibling_consumer.settings.set_setting('private_experiment', 'sibling')
    sibling_consumer._fixture_bundle_payload['private'] = ['sibling']
    return memory_consumer, sibling_consumer


def test_real_final_episode_makes_only_the_controlled_copy_eligible(completed_copies, frozen_prefix):
    memory_consumer, sibling_consumer = completed_copies
    assert report(sibling_consumer)['live']['c1:ppr']['assignment_provenance'] == {
        'controlled': 99, 'uncontrolled': 1, 'legacy_cache': 0}
    assert all(arm['clusters'] == 99 and not arm['powered']
               for arm in report(frozen_prefix.source)['live'].values())
    assert memory_consumer.settings.get_setting('private_experiment') is None
    assert frozen_prefix.source.settings.get_setting('private_experiment') is None
    assert memory_consumer._fixture_bundle_payload == frozen_prefix.bundles == {}


@pytest.fixture(scope='module')
def independent_producer(evidence_schema):
    store = Store(':memory:')
    with closing(store.conn):
        initialize_consumer(store, evidence_schema)
        cleanup_memory_files(store, roots(store.conn))
        assert _seed_mature_live_arms(store) == BASE
        yield store


def test_independent_full_producer_keeps_every_real_episode_and_response(independent_producer):
    assert independent_producer.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 200
    assert len(independent_producer.player_events.player_events_since(0)) == 200


def test_restored_then_completed_copy_matches_independent_full_production(completed_copies, independent_producer):
    memory_consumer, _sibling = completed_copies
    assert scorecard.recommendation_scorecard(memory_consumer, BASE + 90, BASE + 1200) == (
        scorecard.recommendation_scorecard(independent_producer, BASE + 90, BASE + 1200))
    actual = report(memory_consumer)
    assert actual == report(independent_producer)
    assert set(actual['live']) == {'c1:ppr', 'c1:cosine'}
    assert all(arm['clusters'] == arm['impressions'] == 100 and arm['powered']
               for arm in actual['live'].values())
    assert memory_consumer.player_events.player_events_since(0) == independent_producer.player_events.player_events_since(0)
    sql = ('SELECT request_id,surface,lane,mode_id,identity_key,rank,score,model_version,provenance,served_at '
           'FROM rec_served_impressions ORDER BY request_id')
    assert list(map(tuple, memory_consumer.conn.execute(sql))) == list(map(tuple, independent_producer.conn.execute(sql)))
    for store in (memory_consumer, independent_producer):
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_generated_responses_remain_quarantined_when_real_powered_live_decision_is_published(completed_copies):
    memory_consumer, _sibling = completed_copies
    score = scorecard.recommendation_scorecard(memory_consumer, BASE + 90, BASE + 1200)
    assert score['generated'] == 100 and score['organic'] == 0
    result = experiments.decide(memory_consumer, 'promote', 'c1', 'Mature live evidence meets guardrails', NOW)
    assert result['state'] == 'retained' and result['decision']['powered']
    assert result['decision']['live_evidence']['ppr']['completion'] == 100
