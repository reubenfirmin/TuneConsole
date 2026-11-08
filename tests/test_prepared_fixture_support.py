"""Prepared fixture clones keep real files and mutations independently owned."""
import os
from pathlib import Path
import sqlite3

import pytest

from tests.prepared_fixture_support import historical_template, prepared_template, roots
from tests.rec.test_home_prepared import service
from yt_playlist.core import profile_files
from yt_playlist.rec import home_prepared


@pytest.mark.parametrize('disk', [False, True])
def test_open_prepared_consumer_keeps_private_writes_and_real_snapshot_guards(
        tmp_path, _pristine_current_schema, disk):
    def seed(source):
        source.settings.set_setting('private_input', 'prepared')

    with prepared_template(_pristine_current_schema, seed) as template:
        with template.open(tmp_path / 'consumer.db' if disk else ':memory:') as target:
            if disk:
                assert target.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert not target._schema_compatibility
            with target.read_snapshot() as snapshot:
                assert snapshot.store.settings.get_setting('private_input') == 'prepared'
                target.settings.set_setting('private_input', 'private write')
                assert not snapshot.publish(lambda: pytest.fail('Published stale cloned input'))
            with target.read_snapshot() as snapshot:
                assert snapshot.store.settings.get_setting('private_input') == 'private write'
                assert snapshot.publish(lambda: None)
            assert template.source.settings.get_setting('private_input') == 'prepared'
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            target.conn.execute('SELECT 1')


def test_cloning_replaced_schema_does_not_copy_guards_for_retired_owners(
        tmp_path, _pristine_current_schema):
    def seed(source):
        source.conn.execute('CREATE TABLE fixture_retired_owner (value TEXT)')
        source._snapshot_revisions.refresh()
        # A genuine schema replacement can leave unreachable TEMP trigger
        # catalog rows for the vanished owner. Do not forge migration history.
        _pristine_current_schema.backup(source.conn)
        source.settings.set_setting('replacement_input', 'prepared')

    with prepared_template(_pristine_current_schema, seed) as template:
        with template.open(tmp_path / 'replaced.db') as target:
            assert not target.conn.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name='fixture_retired_owner'").fetchone()
            assert not target.conn.execute(
                "SELECT 1 FROM sqlite_temp_schema WHERE tbl_name='fixture_retired_owner'").fetchone()
            with target.read_snapshot() as snapshot:
                assert snapshot.store.settings.get_setting('replacement_input') == 'prepared'
                target.settings.set_setting('replacement_input', 'changed')
                assert not snapshot.publish(lambda: pytest.fail('Missed replacement-schema write'))
            assert template.source.settings.get_setting('replacement_input') == 'prepared'


@pytest.mark.parametrize('fixture_name', ['store', 'file_backed_store'])
@pytest.mark.parametrize('validate_inputs', [False, True])
def test_restoring_identical_schema_fences_old_readers_and_preserves_write_guards(
        request, fixture_name, validate_inputs, _pristine_current_schema):
    target = request.getfixturevalue(fixture_name)
    target.settings.set_setting('fixture_input', 'before restore')
    calculations, statements = [], []

    def value(reader):
        def calculate():
            calculations.append(True)
            return reader.settings.get_setting('fixture_input')
        return reader.memoized_read('fixture-restore-input', calculate)

    def seed(source):
        source.settings.set_setting('fixture_input', 'template value')
        source.rec_ledger.record_serving_policy('fixture-policy', {'ranker': 'fixture'}, 1000)

    with prepared_template(_pristine_current_schema, seed) as template:
        assert template.coordinates, 'Exercise real immutable-file relocation'
        target.conn.set_trace_callback(statements.append)
        try:
            with target.read_snapshot() as old:
                assert value(old.store) == 'before restore'
                old.release_reader()
                template.restore(target)
                assert not old.publish(lambda: pytest.fail('Published before database replacement'),
                                       validate_inputs=validate_inputs)
            with target.read_snapshot() as current:
                assert value(current.store) == 'template value'
                assert current.store.rec_ledger.policy_configuration('fixture-policy') == {'ranker': 'fixture'}
                target.settings.set_setting('fixture_input', 'after restore')
                assert not current.publish(lambda: pytest.fail('Missed a real post-restore write'))
            with target.read_snapshot() as current:
                assert value(current.store) == 'after restore'
                assert current.publish(lambda: None)
        finally:
            target.conn.set_trace_callback(None)
        assert template.source.settings.get_setting('fixture_input') == 'template value'
    assert len(calculations) == 3
    assert not any(sql.startswith('CREATE TEMP TRIGGER') or (
        sql.startswith('DROP TRIGGER') and 'snapshot_revision_' in sql) for sql in statements)
    assert not target.conn.in_transaction


@pytest.mark.parametrize('fixture_name', ['store', 'file_backed_store'])
def test_restored_schema_with_added_owner_gets_real_revision_guards(
        request, fixture_name, _pristine_current_schema):
    store = request.getfixturevalue(fixture_name)
    def seed(source):
        source.conn.execute('CREATE TABLE fixture_added_owner (id INTEGER PRIMARY KEY,value TEXT)')
        with source.conn:
            source.conn.execute("INSERT INTO fixture_added_owner VALUES (1,'template value')")

    with prepared_template(_pristine_current_schema, seed) as template:
        template.restore(store)
        # Guards must already match the cloned schema, rather than being
        # reconstructed by the first snapshot. Existing pinned readers are
        # fenced separately by the replacement's database generation.
        assert store._snapshot_revisions.schema_version == store.conn.execute(
            'PRAGMA main.schema_version').fetchone()[0]
        assert 'fixture_added_owner' in store._snapshot_revisions.tables
        with store.read_snapshot() as snapshot:
            assert snapshot.store.conn.execute('SELECT value FROM fixture_added_owner').fetchone()[0] == 'template value'
            with store.conn:
                store.conn.execute("UPDATE fixture_added_owner SET value='new value'")
            assert not snapshot.publish(lambda: pytest.fail('New owner was not guarded'))
        with store.read_snapshot() as snapshot:
            assert snapshot.store.conn.execute('SELECT value FROM fixture_added_owner').fetchone()[0] == 'new value'
            assert snapshot.publish(lambda: None)
        assert template.source.conn.execute('SELECT value FROM fixture_added_owner').fetchone()[0] == 'template value'


@pytest.mark.parametrize('fail_build', [False, True])
@pytest.mark.parametrize('historical', [False, True])
def test_prepared_template_isolates_construction_and_restores_caller_settings(
        _pristine_current_schema, tmp_path, monkeypatch, fail_build, historical):
    caller = {'YT_PLAYLIST_HOME': str(tmp_path / 'caller-home'),
              'YT_PLAYLIST_AUDIO_VIBES': '1',
              'YT_PLAYLIST_AUDIO_VIBE_MODEL': 'caller-model',
              'DEEPSEEK_API_KEY': 'fixture-test-key'}
    for key, value in caller.items():
        monkeypatch.setenv(key, value)
    sources, homes = [], []
    builder = historical_template if historical else prepared_template
    options = {'database': tmp_path / 'historical.db'} if historical else {}

    def build(source):
        sources.append(source)
        homes.append(Path(os.environ['YT_PLAYLIST_HOME']))
        assert homes[-1] != Path(caller['YT_PLAYLIST_HOME'])
        assert homes[-1].is_dir()
        assert os.environ['YT_PLAYLIST_AUDIO_VIBES'] == '0'
        assert os.environ['YT_PLAYLIST_AUDIO_VIBE_MODEL'] == ''
        assert 'DEEPSEEK_API_KEY' not in os.environ
        source.settings.set_setting('constructed', 'private template')
        if fail_build:
            raise ValueError('Interrupted template construction')
        return 'prepared'

    if fail_build:
        with pytest.raises(ValueError, match='Interrupted template construction'):
            with builder(_pristine_current_schema, build, **options):
                pytest.fail('A failed builder must not yield a template')
    else:
        with builder(_pristine_current_schema, build, **options) as template:
            assert template.result == 'prepared'
            assert template.source.settings.get_setting('constructed') == 'private template'
            assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
            assert {key: os.environ.get(key) for key in caller} == caller
            assert homes[0].is_dir()
    assert {key: os.environ.get(key) for key in caller} == caller
    assert not homes[0].exists()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        sources[0].conn.execute('SELECT 1')


def test_shared_home_templates_use_the_test_model(home_seed_template, home_prepared_template):
    for template in (home_seed_template, home_prepared_template):
        version = template.source.settings.get_setting('rec_vibe_version')
        assert version and version.endswith('/heuristic'), version
    assert len(home_prepared_template.result['menus'][0]['protos']) == 4


@pytest.mark.parametrize('tracks_per_lane,published', [(4, True), (8, True), (8, False)])
def test_home_seed_templates_keep_matching_unprepared_inputs(
        home_seed_templates, store, file_backed_store, tracks_per_lane, published):
    import numpy as np

    template = home_seed_templates(tracks_per_lane, publish=published)
    assert home_seed_templates(tracks_per_lane, publish=published) is template
    first, second = template.restore(store), template.restore(file_backed_store)
    assert first == second == template.result
    genres = ('Jazz', 'Techno', 'Classical', 'Ambient', 'Trance', 'Indie Rock')
    lanes = {'wheelhouse', 'explore', 'comfort', 'fresh'}
    assert first['_meta']['comfort_pool'] == 100
    assert first['_meta']['full_pools'] is True
    if published:
        assert first['_meta']['musical_generation']
        assert first['_vibes']['pools']
    else:
        assert first['_meta'] == {'comfort_pool': 100, 'full_pools': True}
        assert '_vibes' not in first
    for mid in map(str, range(6)):
        assert set(first[mid]) == lanes
        assert all(len(rows) == tracks_per_lane for rows in first[mid].values())
    assert set(first['all']) == lanes
    assert all(len(rows) == 6 * tracks_per_lane for rows in first['all'].values())
    keys = {row['key'] for rows in first['all'].values() for row in rows}
    assert len(keys) == 24 * tracks_per_lane
    profiles = template.source.musical_profiles.by_keys(keys)
    assert set(profiles) == keys
    library = template.source.rec_query.tracks_by_keys(keys)
    discovery = template.source.discovery.discovered_tracks_by_keys(keys)
    assert set(library) | set(discovery) == keys
    heads = template.source.recommendations.heads()
    for target in (template.source, store, file_backed_store):
        assert target.rec_query.tracks_by_keys(keys) == library
        assert target.discovery.discovered_tracks_by_keys(keys) == discovery
        assert target.musical_profiles.by_keys(keys) == profiles
        assert target.recommendations.heads() == heads
        assert target.rec_proposals.has_proposals('mode_bundles') == published
        assert target.rec_proposals.get_proposals(home_prepared.SURFACE) is None
        assert target.conn.execute('SELECT count(*) FROM library_memberships').fetchone()[0] == 18 * tracks_per_lane
        assert target.conn.execute('SELECT count(*) FROM discovery_memberships').fetchone()[0] == 6 * tracks_per_lane
        modes = target.modes.list_modes()
        assert len(modes) == 6
        for mid, (mode, genre) in enumerate(zip(modes, genres)):
            assert (mode['mode_id'], mode['label'], mode['size']) == (mid, genre, 50)
            assert mode['families'] == mode['rep_keys'] == []
            np.testing.assert_array_equal(mode['centroid'], np.eye(6, dtype=np.float32)[mid])
        assert getattr(target, '_fixture_bundle_payload', {}) == template.bundles
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.source.settings.set_setting('private_home_seed', 'forbidden')

    destinations = [roots(target.conn) for target in (store, file_backed_store)]
    retained = []
    for name, directory in template.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if not source.is_file():
                continue
            paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
            exact = source.read_bytes()
            assert all(path.read_bytes() == exact for path in paths)
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
            if exact:
                retained.append((paths, exact))
    assert retained
    paths, exact = retained[0]
    paths[1].write_bytes(b'Private Home seed file mutation')
    assert paths[0].read_bytes() == paths[2].read_bytes() == exact
    store.settings.set_setting('private_home_seed', 'first clone')
    first['0']['wheelhouse'][0]['title'] = 'Private result row'
    store._fixture_bundle_payload.clear()
    store._fixture_bundle_payload['private'] = True
    assert second == template.result
    for target in (template.source, file_backed_store):
        assert target.settings.get_setting('private_home_seed') is None
        assert getattr(target, '_fixture_bundle_payload', {}) == template.bundles


@pytest.mark.parametrize('template_name', [
    'genre_candidates_template', 'fresh_genre_candidates_template',
    'adaptive_library_template', 'fresh_home_template',
])
def test_home_candidate_templates_keep_private_complete_inputs(
        request, template_name, store, file_backed_store):
    template = request.getfixturevalue(template_name)
    first, second = template.restore(store), template.restore(file_backed_store)
    assert first == second == template.result
    assert len({template.source._evidence_key, store._evidence_key, file_backed_store._evidence_key}) == 3
    bundle = template.bundles
    keys = {item['key'] for items in bundle['all'].values() for item in items}
    library = template.source.rec_query.tracks_by_keys(keys)
    discovery = template.source.discovery.discovered_tracks_by_keys(keys)
    assert set(library) | set(discovery) == keys
    home = template.source.rec_proposals.get_proposals(home_prepared.SURFACE)
    if template_name == 'adaptive_library_template':
        from yt_playlist.rec import group_serving
        assert len(template.result[0]) == 64
        assert group_serving.policy(template.source) == 'adaptive'
        assert home is None
    else:
        count, modes = (20, 6) if template_name == 'genre_candidates_template' else (8, 2)
        assert len(template.source.modes.list_modes(active_only=True)) == modes
        for mid in map(str, range(modes)):
            lanes = {'wheelhouse', 'explore', 'comfort', 'fresh'} if modes == 6 else {
                'wheelhouse', 'explore', 'comfort'}
            assert set(bundle[mid]) == lanes
            assert all(len(items) == count for items in bundle[mid].values())
        assert (home is not None) == (template_name == 'fresh_home_template')

    for target in (store, file_backed_store):
        assert target._fixture_bundle_payload == bundle
        assert target.recommendations.heads() == template.source.recommendations.heads()
        assert target.rec_query.tracks_by_keys(keys) == library
        assert target.discovery.discovered_tracks_by_keys(keys) == discovery
        assert target.rec_proposals.get_proposals(home_prepared.SURFACE) == home
        assert target.accounts.get_accounts() == template.source.accounts.get_accounts()
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.source.settings.set_setting('private_home_candidate', 'forbidden')

    destinations = [roots(target.conn) for target in (store, file_backed_store)]
    retained = []
    for name, directory in template.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if not source.is_file():
                continue
            paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
            exact = source.read_bytes()
            assert all(path.read_bytes() == exact for path in paths)
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
            if exact:
                retained.append((paths, exact))
    assert retained
    paths, exact = retained[0]
    paths[1].write_bytes(b'Private consumer file mutation')
    assert paths[0].read_bytes() == paths[2].read_bytes() == exact
    store.settings.set_setting('private_home_candidate', 'first clone')
    store._fixture_bundle_payload['all']['wheelhouse'].clear()
    if isinstance(first, dict):
        first.clear()
        assert second == template.result and second
    elif first is not None:
        first[0].clear()
        first[1]['groups'].clear()
        assert len(second[0]) == 64 and second[1]['groups'] == template.result[1]['groups']
    for target in (template.source, file_backed_store):
        assert target.settings.get_setting('private_home_candidate') is None
        assert target._fixture_bundle_payload == bundle


def test_radio_genre_template_keeps_complete_private_inputs(
        radio_genre_template, store, file_backed_store, request):
    import numpy as np

    from yt_playlist.rec import embedding_vectors

    template = radio_genre_template
    first, second = template.restore(store), template.restore(file_backed_store)
    assert first == second == template.result
    assert set(first) == set(map(str, range(1, 10)))
    assert all(len(rows) == 12 for rows in first.values())
    keys = {row['key'] for rows in first.values() for row in rows}
    assert len(keys) == 108
    library = template.source.rec_query.tracks_by_keys(keys)
    assert set(library) == keys
    labels = ['Electronic'] * 5 + ['Synthwave', 'Jazz', 'Rock', 'Ambient']
    metadata = [tuple(row) for row in template.source.conn.execute(
        'SELECT video_id,genre,mb_year FROM catalog_tracks ORDER BY video_id')]
    assert len(metadata) == 108
    assert {row[2] for row in metadata} == {'2001'}
    assert {row[1] for row in metadata} == set(labels)
    for target in (template.source, store, file_backed_store):
        assert target.rec_query.tracks_by_keys(keys) == library
        assert target._fixture_bundle_payload == template.bundles
        assert target.recommendations.heads() == template.source.recommendations.heads()
        assert [tuple(row) for row in target.conn.execute(
            'SELECT video_id,genre,mb_year FROM catalog_tracks ORDER BY video_id')] == metadata
        assert target.conn.execute('SELECT count(*) FROM library_memberships').fetchone()[0] == 108
        modes = target.modes.list_modes()
        assert len(modes) == 9
        for index, (mode, label) in enumerate(zip(modes, labels)):
            assert (mode['mode_id'], mode['label'], mode['size']) == (index + 1, label, 12)
            assert mode['families'] == [[label, 12]]
            assert mode['rep_keys'] == []
            assert mode['space'] == embedding_vectors.content_space_id(target)
            angle = index * .01 if index < 6 else (index - 5) * 1.2
            np.testing.assert_array_equal(mode['centroid'], np.array(
                [np.cos(angle), np.sin(angle)], dtype=np.float32))
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.source.settings.set_setting('private_radio_genre', 'forbidden')

    destinations = [roots(target.conn) for target in (store, file_backed_store)]
    retained = []
    copied = 0
    for name, directory in template.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if not source.is_file():
                continue
            paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
            exact = source.read_bytes()
            assert all(path.read_bytes() == exact for path in paths)
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
            copied += 1
            if exact and not source.name.startswith('.') and not source.name.endswith('.lock'):
                retained.append((paths, exact))
    assert retained
    request.node.user_properties.append(('radio_genre_retained_files', str(copied)))
    paths, exact = retained[0]
    paths[1].write_bytes(b'Private radio genre file mutation')
    assert paths[0].read_bytes() == paths[2].read_bytes() == exact
    store.settings.set_setting('private_radio_genre', 'first clone')
    first['1'][0]['genre'] = 'Private genre row'
    store._fixture_bundle_payload['1']['wheelhouse'][0]['title'] = 'Private bundle row'
    assert second == template.result
    for target in (template.source, file_backed_store):
        assert target.settings.get_setting('private_radio_genre') is None
        assert target._fixture_bundle_payload == template.bundles


@pytest.mark.parametrize('variant', ['inputs', 'backend'])
def test_measured_sounds_template_keeps_complete_private_inputs(
        variant, measured_sounds_template, measured_home_templates, store, file_backed_store):
    from yt_playlist.rec import home_vibe, mix_tuning, mode_surfaces

    template = (measured_sounds_template if variant == 'inputs'
                else measured_home_templates(variant))
    first, second = store, file_backed_store
    template.restore(first)
    template.restore(second)
    bundle = template.bundles
    assert set(bundle['_vibes']['pools']) == set(home_vibe.PRESETS)
    for mid in range(6):
        for lane in mode_surfaces.CARD_SURFACES:
            items = bundle[str(mid)][lane]
            assert len(items) == 20
            assert sum(item['musical']['dimensions']['intensity']['value'] < .5
                       for item in items) == 8
    keys = {item['key'] for items in bundle['all'].values() for item in items}
    assert len(keys) == 480
    assert template.source.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 480
    assert len(template.source.rec_query.library_keys()) == 360
    assert len(template.source.discovery.discovered_tracks_by_keys(keys)) == 120
    profiles = template.source.musical_profiles.by_keys(keys)
    assert len(profiles) == len(keys)
    heads = template.source.recommendations.heads()
    assert {head['surface'] for head in heads} >= {'vibe:' + preset for preset in home_vibe.PRESETS}
    for target in (template.source, first, second):
        assert target.musical_profiles.by_keys(keys) == profiles
        assert target.recommendations.heads() == heads
        assert len(target.accounts.get_accounts()) == (0 if variant == 'inputs' else 1)
        home = target.rec_proposals.get_proposals('home_prepared')
        if variant == 'inputs':
            assert not home
        else:
            assert home == template.source.rec_proposals.get_proposals('home_prepared')
            assert home['menus'] and len(home['menus'][0]['protos']) == 4
        assert target.settings.get_setting(home_vibe.SETTING) is None
        assert target.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None
        assert target.rec_model.get_leans() == {}
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert first._fixture_bundle_payload == second._fixture_bundle_payload == bundle
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.source.settings.set_setting('private_sound_fixture', 'forbidden')

    # Check every retained file, including source evidence and typed outputs,
    # rather than proving only that SQL and one profile file were copied.
    destinations = [roots(target.conn) for target in (first, second)]
    copied = 0
    for name, directory in template.files.items():
        for source in directory.rglob('*'):
            if not source.is_file():
                continue
            paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
            expected = source.read_bytes()
            assert all(path.read_bytes() == expected for path in paths)
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
            copied += 1
    assert copied

    publication = first.conn.execute('SELECT publication FROM recording_profile_files LIMIT 1').fetchone()[0]
    relative = profile_files.relative('recording', publication)
    original = (profile_files.directory(second.conn) / relative).read_bytes()
    (profile_files.directory(first.conn) / relative).write_bytes(b'Private corrupted profile')
    assert (profile_files.directory(second.conn) / relative).read_bytes() == original
    assert (profile_files.directory(template.source.conn) / relative).read_bytes() == original
    first.settings.set_setting('private_sound_fixture', 'first consumer')
    first._fixture_bundle_payload['_vibes']['pools']['relax']['fresh'][0]['musical']['dimensions']['intensity']['value'] = -1
    for target in (template.source, second):
        assert target.settings.get_setting('private_sound_fixture') is None
        assert target._fixture_bundle_payload == bundle


def test_prepared_clones_survive_source_removal_and_keep_private_mutations(
        _pristine_current_schema, store, file_backed_store, home_prepared_template, tmp_path):
    def build(source):
        home = home_prepared_template.restore(source)
        bundles = source._fixture_bundle_payload
        assert home and len(home['menus'][0]['protos']) == 4
        return {'keys': [row['key'] for row in bundles['all']['wheelhouse']], 'nested': [1], 'home': home}
    with prepared_template(_pristine_current_schema, build) as template:
        expected = template.source.musical_profiles.by_keys(template.result['keys'])
        assert len(expected) == 24
        source_files = profile_files.directory(template.source.conn)
        assert source_files.exists()
        first = template.restore(store)
        second = template.restore(file_backed_store)
        assert len({template.source._evidence_key, store._evidence_key, file_backed_store._evidence_key}) == 3
        assert len({source_files, profile_files.directory(store.conn),
                    profile_files.directory(file_backed_store.conn)}) == 3
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            template.source.conn.execute("INSERT INTO settings VALUES ('private','forbidden')")
        first['nested'].append(2)
        first['home']['menus'][0]['protos'][0]['tracks'][0]['title'] = 'Private payload edit'
        store._fixture_bundle_payload['all']['wheelhouse'].clear()
        assert second['nested'] == template.result['nested'] == [1]
        assert second['home'] == template.result['home']
        assert len(file_backed_store._fixture_bundle_payload['all']['wheelhouse']) == 24
        assert len(template.bundles['all']['wheelhouse']) == 24
    # Closing the memory source removes its profile files. Neither restored
    # consumer can borrow bytes from that source.
    assert not source_files.exists()
    for target in (store, file_backed_store):
        assert target.musical_profiles.by_keys(second['keys']) == expected
        payload = home_prepared.load(target, 1000)
        assert payload == second['home']
        assert len(home_prepared.cards(target, payload, 1000, service(target), record=False).protos) == 4
        assert target.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert file_backed_store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    store.settings.set_setting('private', 'first clone')
    assert file_backed_store.settings.get_setting('private') is None
