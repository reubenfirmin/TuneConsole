"""Independent checks for the user-visible mix precision and storage contract."""
import hashlib
import json
import sqlite3

import pytest

from yt_playlist.core import group_evidence, mix_choices
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.preparation import mix_choices as preparation
from yt_playlist.rec import choice_evidence as choices, group_learning, home_cards, recipes


def menu(store, identity='menu', options=None, at=100):
    return choices.issue(store, 'mixes', options or [
        {'id': 'a', 'label': 'Stay close', 'note': 'Deep cuts', 'intent': 'comfort', 'genres': ['Deep House']},
        {'id': 'b', 'label': 'Explore', 'note': 'Further out', 'intent': 'fresh', 'genres': ['Jazz']}],
        at, context={'at': at, 'group_generation': 'unneeded', 'groups': [{'centroid': [1, 0]}]},
        opportunity_id=identity)


def head():
    return {'generation': 'current', 'groups': [
        {'group_id': 'house', 'version': 'h', 'parent_version': None, 'leaf': False, 'genres': [('Deep House', 20)]},
        {'group_id': 'one', 'version': '1', 'parent_version': 'h', 'leaf': True, 'genres': [('Deep House', 10)]},
        {'group_id': 'two', 'version': '2', 'parent_version': 'h', 'leaf': True, 'genres': [('Deep House', 10)]},
        {'group_id': 'jazz', 'version': 'j', 'parent_version': None, 'leaf': True, 'genres': [('Jazz', 20)]}]}


def compile_groups(store, now=110, responses=None):
    ledger = store.rec_ledger.evidence()
    offers = {o['id']:o for o in ledger.records('opportunity')}
    state = group_learning.compile(store, offers, responses if responses is not None else ledger.records('response'), now, head())
    return state, {r['group_id']:r for r in state['rows']}


def test_new_menu_keeps_only_descriptor_and_decision_receipts(store):
    menu(store)
    ledger = store.rec_ledger.evidence()
    offer = ledger.get('opportunity', 'menu')
    assert set(offer['options'][0]) == {'id', 'descriptor_id', 'genres_complete', 'genre_origin'}
    assert ledger.get('artifact', offer['context']) == {'at': 100, 'schema': 2, 'precision': mix_choices.PRECISION}
    assert mix_choices.expand(store.conn, offer['options'][0])['genres'] == ['Deep House']
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind IN ('offer','response')").fetchone()
    choices.respond(store, 'menu', 'choice', 102, option_id='a', details={'tracks':[{'key':'secret'}], 'playlist_id': 8})
    assert ledger.records('response')[0]['details'] == {'playlist_id': 8}
    choices.compile_report(store, 110)
    interest = store.rec_proposals.get_proposals('choice_interests')['contexts'][0]
    assert interest['descriptor_id'] == offer['options'][0]['descriptor_id']
    assert interest['keys'] == [] and interest['feature'] is None and interest['mode_id'] is None
    assert store.conn.execute('SELECT count(*) FROM choice_interest_features').fetchone()[0] == 0


def test_duplicate_sets_are_verified_and_raw_labels_survive_taxonomy_changes(store, monkeypatch):
    with store.conn:
        a = mix_choices.descriptor(store.conn, {'genres':['House', 'Deep House', 'House'], 'label':'A'})
        b = mix_choices.descriptor(store.conn, {'genres':['Deep House', 'House'], 'label':'A'})
        c = mix_choices.descriptor(store.conn, {'genres':['Deep House', 'House'], 'label':'B'})
    assert a == b and c != a
    assert store.conn.execute('SELECT count(*) FROM mix_choice_genre_sets').fetchone()[0] == 1
    assert mix_choices.read_descriptor(store.conn, a)['genres'] == ['Deep House', 'House']
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
        store.conn.execute("UPDATE mix_choice_raw_genres SET label='changed'")
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn:
        store.conn.execute('DELETE FROM mix_choice_genre_members')
    # Deliberately corrupt the dictionary: a digest alone must never prove equality.
    with store.conn:
        store.conn.execute('DROP TRIGGER mix_choice_genre_members_delete')
        store.conn.execute('DELETE FROM mix_choice_genre_members')
    with pytest.raises(ValueError, match='collision'):
        mix_choices.descriptor(store.conn, {'genres':['House','Deep House']})


def test_menu_identity_does_not_change_with_hidden_membership_or_group_version(store):
    options = [{'id':'x', 'label':'Stay close', 'genres':['House'], 'tracks':[{'key':'a'}], 'group_version':'old'}]
    first = choices.present(store, 'mixes', options, 100, context={'at':90, 'group_generation':'old'}, window='held')
    options[0].update(tracks=[{'key':'b'}], group_version='new', recipe={'secret':True})
    second = choices.present(store, 'mixes', options, 101, context={'at':90, 'group_generation':'new'}, window='held')
    assert first == second
    assert store.conn.execute('SELECT count(*) FROM mix_choice_menus').fetchone()[0] == 1
    # A displayed tuning selection changes the menu identity; hidden playback
    # inputs still do not. All three menus can share the same compact descriptor.
    relaxed = choices.present(store, 'mixes', options, 102, context={'at': 90},
                              window='held', vibe='relax')
    assert choices.present(store, 'mixes', options, 103, context={'at': 90},
                           window='held', vibe='relax') == relaxed
    energized = choices.present(store, 'mixes', options, 104, context={'at': 90},
                                window='held', vibe='energize')
    assert len({first, relaxed, energized}) == 3
    offer = choices.opportunity(store, relaxed)
    assert offer['vibe'] == 'relax' and 'recipe' not in offer['options'][0]
    assert offer['options'][0]['descriptor_id'] == choices.opportunity(store, first)['options'][0]['descriptor_id']
    with pytest.raises(ValueError, match='displayed Vibe'):
        choices.issue(store, 'mixes', options, 105, context={}, vibe='unknown')


@pytest.fixture
def retirement_store(_pristine_current_schema, tmp_path):
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core.store import Store

    store = Store(tmp_path / 'retirement.db')
    try:
        initialize_consumer(store, _pristine_current_schema)
        yield store
    finally:
        store.conn.close()


@pytest.mark.parametrize('boundary', ['unlink', 'references'])
def test_interrupted_retirement_reconciles_real_sql_after_restart(retirement_store, monkeypatch, boundary):
    from yt_playlist.core import fao
    from yt_playlist.core.activity import WorkBudgetExceeded
    from yt_playlist.core.store import Store
    from yt_playlist.rec import group_roots

    store = retirement_store
    menu(store)
    choices.respond(store, 'menu', 'visible', 101, visible=['a', 'b'])
    ledger = store.rec_ledger.evidence()
    remove = fao.delete

    def cutoff(path, **kwargs):
        result = remove(path, **kwargs)
        if path == ledger._path('opportunity', 'menu'):
            raise WorkBudgetExceeded('Real menu unlink finished')
        return result

    def reconcile(offers, checkpoints):
        group_roots.retired(store, offers, checkpoints)
        if boundary == 'references':
            raise WorkBudgetExceeded('Real SQL reconciliation finished')

    if boundary == 'unlink':
        monkeypatch.setattr(fao, 'delete', cutoff)
    with group_roots.admission(store), pytest.raises(WorkBudgetExceeded):
        ledger.maintain(200, retire_references=reconcile)
    monkeypatch.setattr(fao, 'delete', remove)
    if boundary == 'unlink':
        assert store.conn.execute('SELECT 1 FROM mix_choice_menus').fetchone()
    else:
        assert not store.conn.execute('SELECT 1 FROM mix_choice_menus').fetchone()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        with group_roots.admission(reopened):
            result = reopened.rec_ledger.evidence().maintain(200,
                retire_references=lambda offers, checkpoints: group_roots.retired(reopened, offers, checkpoints))
        assert not result['deferred']
        assert not reopened.rec_ledger.evidence().records('response')
        assert not reopened.conn.execute('SELECT 1 FROM mix_choice_menus').fetchone()
        assert not reopened.conn.execute('SELECT 1 FROM mix_choice_options').fetchone()
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


def test_artist_fallback_is_frozen_from_the_rendered_values(store, monkeypatch):
    class Query:
        def artist_genre_years_for(self, artists): return {'Artist': {'genre':'Acid House'}}
        def track_decades(self, keys): return {}
    monkeypatch.setattr(store, 'rec_query', Query())
    genres, _ = recipes.theme_counts(store, [{'key':'x', 'artist':'Artist', 'genre':None}])
    menu(store, options=[{'id':'fallback', 'genres':list(genres)}])
    offer = store.rec_ledger.evidence().get('opportunity','menu')
    assert mix_choices.expand(store.conn, offer['options'][0])['genres'] == ['acid house']
    monkeypatch.setattr(store, 'rec_query', None)
    assert mix_choices.expand(store.conn, offer['options'][0])['genres'] == ['acid house']


def test_indistinguishable_children_get_only_a_shared_parent_prior(store):
    menu(store)
    choices.respond(store,'menu','visible',101,visible=['a','b'])
    choices.respond(store,'menu','choice',102,option_id='a')
    state, rows = compile_groups(store)
    assert rows['house']['positive'] == .25 and rows['jazz']['negative'] == .25
    for child in ('one','two'):
        assert rows[child]['positive'] == rows[child]['negative'] == 0
        assert rows[child]['shared_strength'] == .25
    # Renaming/rebuilding versions and centroids has no historical dependency.
    assert not any(state['excluded'].values())


def test_unseen_missing_delayed_out_of_order_withdrawn_and_correlated_evidence(store):
    for i in range(3):
        menu(store, f'm{i}')
        choices.respond(store,f'm{i}','visible',101,visible=['a'])
        choices.respond(store,f'm{i}','choice',103,option_id='a')
    ledger = store.rec_ledger.evidence()
    events = ledger.records('response')
    # Choice has happened but is not known yet; visibility does not imply success.
    delayed = [{**e,'observed_at':120} if e['kind']=='choice' else e for e in events]
    assert compile_groups(store, responses=delayed)[1]['house']['positive'] == 0
    state, rows = compile_groups(store, now=121, responses=list(reversed(delayed)))
    assert rows['house']['positive'] == .25  # Three repeated menus, one area/day.
    assert rows['jazz']['negative'] == 0  # Never visible.
    for i in range(3):
        choices.respond(store,f'm{i}','withdraw',105,details={'action_id':f'm{i}:choice:a'})
    assert compile_groups(store)[1]['house']['positive'] == 0
    assert compile_groups(store)[1]['house']['picked'] == 0
    assert compile_groups(store, now=104)[1]['house']['positive'] == .25


def test_track_model_replay_is_explicitly_unavailable_and_coverage_unknown(store):
    menu(store)
    choices.respond(store,'menu','visible',101,visible=['a','b'])
    choices.respond(store,'menu','choice',102,option_id='a')
    choices.respond(store,'menu','start',103,option_id='a',details={'video_id':'v'})
    class TrackPredictor:
        def predict(self,*args): pytest.fail('Track replay cannot consume a genre menu')
        def observe(self,*args): pytest.fail('Unsupported mix evidence cannot train this candidate')
    assert choices.replay(store,110,TrackPredictor())['formats']['mixes']['unsupported_precision'] == 1
    row = choices.compile_report(store,110)['formats']['mixes']
    assert row['known_coverage_episodes'] == 0 and row['mean_track_coverage'] is None


def test_mix_choice_identity_ignores_track_replacement_and_group_refresh():
    proto = {'lane':'fresh','label':'Explore','note':'A wider view','genres':['House'],
             'tracks':[{'key':'x','video_id':'v'}], 'recipe':{'track_keys':['x']}}
    identity = home_cards.experiment_id(proto)
    proto.update(tracks=[{'key':'y'}], recipe={'track_keys':['y']}, group_version='new')
    assert home_cards.experiment_id(proto) == identity


def test_mix_interest_influences_genres_without_a_current_track_centroid(store, monkeypatch):
    import numpy as np
    from yt_playlist.rec import choice_learning, scoring, transient
    from yt_playlist.rec.taste_context import PlaylistTaste
    menu(store)
    choices.respond(store, 'menu', 'choice', 102, option_id='a')
    choices.compile_report(store, 110)
    assert any(key.startswith('genre:') and weight > 0 for key, weight in transient.facet_leans(store,110).items())
    interests = choice_learning.contexts(store,110)
    # Challenge both the descriptor projection and an unconverted selected-track
    # projection whose keys happen to exist in today's vector matrix.
    monkeypatch.setattr(choice_learning,'contexts',lambda *a: interests + [
        {'format':'mixes','option_id':'old','keys':['matching-today'],'feature':[0,1]}])
    taste = PlaylistTaste(['Saved'],np.array([[1.,0.]]),np.array([1.]),[1])
    result = scoring._with_choices(store,taste,np.array([[0.,1.]]),{'matching-today':0},content=True,now=110)
    assert result.titles == ['Saved']
    np.testing.assert_array_equal(result.centroids,taste.centroids)
    np.testing.assert_array_equal(result.weights,taste.weights)


def test_legacy_preparation_restarts_and_does_not_guess_missing_artist_genres(store, monkeypatch):
    ledger = store.rec_ledger.evidence()
    context = ledger.artifact({'at':90,'groups':[{'centroid':[1,0]}], 'policy':{'album_input':'keep'}})
    old = {'schema':1,'id':'legacy','format':'mixes','at':100,'observed_at':100,'episode_id':'e',
           'context':context,'prediction':None,'options':[{'id':'partial','tracks':[{'genre':'House'},{'genre':None}]}]}
    ledger.put('opportunity','legacy',old)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(store.conn,'offer','legacy',{'groups':[]},100)
    assert preparation.step(store,110)
    offer = ledger.get('opportunity','legacy')
    option = mix_choices.expand(store.conn,offer['options'][0])
    assert option['genres'] == ['house'] and option['genres_complete'] is False
    assert store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key='legacy'").fetchone()
    original_compile = choices.compile_report
    monkeypatch.setattr(choices,'compile_report',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('publication failed')))
    preparation.step(store,110)  # Advance the completed menu scan.
    with pytest.raises(RuntimeError,match='publication failed'):
        preparation.step(store,110)
    assert store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key='legacy'").fetchone()
    monkeypatch.setattr(choices,'compile_report',original_compile)
    while preparation.step(store,110): pass
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key='legacy'").fetchone()
    assert preparation.status(store.conn)['phase'] == 'ready'
    assert not preparation.step(store,110)
    from yt_playlist.core import fao
    assert json.loads(fao.read(ledger._path('opportunity', 'legacy')).bytes) == old
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_album_and_radio_inputs_keep_their_own_precision_without_group_roots(store):
    for format in ('albums','radio'):
        choices.issue(store,format,[{'id':'own','feature':[1,0],'tracks':[{'key':'x'}]}],100,
                      context={'at':90,'policy':{'model':'own'},'groups':[{'centroid':[9,9]}]}, opportunity_id=format)
        offer = store.rec_ledger.evidence().get('opportunity',format)
        assert offer['options'][0]['feature'] == [1,0]
        assert store.rec_ledger.evidence().get('artifact',offer['context']) == {'at':90,'policy':{'model':'own'}}
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='offer'").fetchone()


def test_unpublished_sql_is_collected_and_live_learning_keeps_its_descriptor(store, monkeypatch):
    ledger = store.rec_ledger.evidence()
    original_put = ledger.put
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    monkeypatch.setattr(ledger, 'put', lambda *a, **k: (_ for _ in ()).throw(OSError('failed publication')))
    with pytest.raises(OSError, match='failed publication'):
        menu(store, 'orphan')
    assert store.conn.execute('SELECT count(*) FROM mix_choice_menus').fetchone()[0] == 1
    assert mix_choices.collect_unpublished(store) == 1
    while mix_choices.collect_dictionary(store.conn): pass
    assert store.conn.execute('SELECT count(*) FROM mix_choice_descriptors').fetchone()[0] == 0
    store.conn.commit()
    monkeypatch.setattr(ledger, 'put', original_put)
    menu(store)
    choices.respond(store, 'menu', 'choice', 102, option_id='a')
    choices.compile_report(store, 110)
    descriptor = store.rec_proposals.get_proposals('choice_interests')['contexts'][0]['descriptor_id']
    with store.conn:
        mix_choices.retire(store.conn, ['menu'])
        while mix_choices.collect_dictionary(store.conn): pass
    assert mix_choices.read_descriptor(store.conn, descriptor)['genres'] == ['Deep House']


def test_choice_backup_restores_descriptors_and_files_and_fences_retirement(store, tmp_path, monkeypatch):
    from yt_playlist.core import choice_evidence_files, fao, policy_documents
    from yt_playlist.core.store import Store
    monkeypatch.setattr(choice_evidence_files, 'PAGE_ROWS', 2)
    menu(store)
    choices.respond(store, 'menu', 'visible', 101, visible=['a','b'])
    choices.respond(store, 'menu', 'choice', 102, option_id='a')
    ledger = store.rec_ledger.evidence()
    original = {str(path.relative_to(ledger.directory)):fao.read(path).bytes
                for path in choice_evidence_files.references(store.conn)}
    clocks = {str(path.relative_to(ledger.directory)):(fao.read(path).info.mtime_ns, fao.read(path).info.ctime_ns)
              for path in choice_evidence_files.references(store.conn)}
    with policy_documents.retained_reader(ledger.directory):
        assert ledger.maintain(1000) == {'deferred':True}
    restored = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(restored.conn)
        restored._bind_repositories()
        manifest = choice_evidence_files.export(store.conn, tmp_path / 'files')
        choice_evidence_files.restore(restored.conn, manifest)
        assert choices.compile_report(restored, 110)['formats'] == choices.compile_report(store,110)['formats']
        actual = {str(path.relative_to(choice_evidence_files.directory(restored.conn))):fao.read(path).bytes
                  for path in choice_evidence_files.references(restored.conn)}
        assert actual == original
        assert {str(path.relative_to(choice_evidence_files.directory(restored.conn))):
                (fao.read(path).info.mtime_ns, fao.read(path).info.ctime_ns)
                for path in choice_evidence_files.references(restored.conn)} == clocks
        source = next((tmp_path / 'files' / 'opportunity').glob('*/*.json'))
        source.write_bytes(b'{}')
        with pytest.raises(ValueError, match='verification'):
            choice_evidence_files.restore(restored.conn, manifest)
        assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restored.conn.close()


def test_backup_snapshot_excludes_a_menu_with_future_sql_ownership(store, tmp_path):
    from tests import file_custody_support as custody
    from yt_playlist.core.store import Store
    menu(store, 'before')
    restored = Store(tmp_path / 'snapshot.db')
    try:
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            assert reader.conn.execute('SELECT count(*) FROM mix_choice_menus').fetchone()[0] == 1
            menu(store, 'future')
            choices.respond(store,'future','choice',102,option_id='a')
            # The in-memory snapshot is a private copy; export owners also require
            # an explicit SQL transaction while collecting its file references.
            reader.conn.execute('BEGIN')
            manifests = custody.export_files(reader.conn, tmp_path / 'full.db')
            assert 'choice_evidence' in manifests
            reader.conn.backup(restored.conn)
        restored._bind_repositories()
        custody.restore_files(restored.conn,manifests)
        assert [o['id'] for o in restored.rec_ledger.evidence().records('opportunity')] == ['before']
        assert restored.rec_ledger.evidence().records('response') == []
    finally:
        restored.conn.close()
