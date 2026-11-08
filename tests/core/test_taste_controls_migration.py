"""Retained user settings and comparison evidence use durable domain owners."""
import json

import pytest

from yt_playlist.core import collection_recovery, experiment_decision_state, taste_control_state
from yt_playlist.core.migrations import current, migrate, registry


def decision(at):
    return {'decision':'retire','cohort':'cohort','reason':'Insufficient live evidence','at':at,
            'powered':False,'basis':'operator retirement; insufficient live evidence',
            'live_evidence':{'ppr':None,'cosine':None}}


def test_checkpoint_restore_uses_captured_axis_after_new_alias_and_merge(store):
    store.rec_model.set_weight('genre:Previously unresolved',.4,lo=0,now=1)
    unresolved = store.conn.execute("SELECT id FROM taste_axes WHERE kind='unresolved_genre' AND value='Previously unresolved'").fetchone()[0]
    store.rec_model.set_weight('genre:synth pop',.7,lo=0,now=2)
    store.rec_model.save_taste_settings(taste_control_state.CHECKPOINT,[],3)
    store.genres.set_genre_alias('Previously unresolved','drum bass')
    store.genres.merge_genres('synth pop','drum bass',now=4)
    store.rec_model.reset_weights()
    assert store.rec_model.restore_taste_settings(taste_control_state.CHECKPOINT,[])
    assert store.conn.execute('SELECT weight FROM rec_weights WHERE axis_id=?',(unresolved,)).fetchone()[0]==.4
    assert store.rec_model.get_weights(now=2)['genre:drum bass']==.7
    assert store.conn.execute('SELECT count(*) FROM rec_weights').fetchone()[0]==2


def test_comparison_order_and_evidence_are_owned_and_backed_up(store,tmp_path):
    for index in range(2):
        store.tracks.upsert_track(f'video{index}',f'Title {index}','Artist',None,100)
    keys = [store.tracks.identity_key_for_video(f'video{index}') for index in range(2)]
    baseline = [{'key':key,'rank':index+1,'score':.2+index,'lane':'rotation','lane_weight':.5,
                 'trace':{'term':index}} for index,key in enumerate(reversed(keys))]
    store.settings.set_setting(taste_control_state.BASELINE,json.dumps(baseline))
    store.rec_model.save_taste_settings(taste_control_state.CHECKPOINT,[],3)
    store.settings.set_setting('ppr_experiment_decisions',json.dumps([decision(4)]))
    assert json.loads(store.settings.get_setting(taste_control_state.BASELINE))==baseline
    assert [row[0] for row in store.conn.execute('SELECT recording_id FROM taste_comparison_entries ORDER BY position')]==[
        store.conn.execute('SELECT id FROM recordings WHERE identity_key=?',(key,)).fetchone()[0] for key in reversed(keys)]
    store.conn.execute('BEGIN')
    manifest = collection_recovery.export(store.conn,tmp_path/'backup')
    store.conn.rollback()
    collection_recovery.restore(store.conn,manifest)
    assert {row[0] for row in collection_recovery.references(store.conn)}=={
        'taste_settings_checkpoints','taste_comparison_baselines','experiment_decisions'}
    assert json.loads(store.settings.get_setting(taste_control_state.BASELINE))==baseline


def test_experiment_decisions_append_without_rewriting_published_history(store):
    key = 'ppr_experiment_decisions'
    store.settings.set_setting(key,json.dumps([decision(1)]))
    original = tuple(store.conn.execute('SELECT * FROM experiment_decisions').fetchone())
    store.settings.set_setting(key,json.dumps([decision(1),decision(2)]))
    assert tuple(store.conn.execute('SELECT * FROM experiment_decisions ORDER BY id LIMIT 1').fetchone())==original
    assert experiment_decision_state.records(store.conn)==[decision(1),decision(2)]
    with pytest.raises(ValueError,match='rewritten'):
        store.settings.set_setting(key,json.dumps([{**decision(1),'reason':'Replaced'},decision(2),decision(3)]))
    assert experiment_decision_state.records(store.conn)==[decision(1),decision(2)]
    current.validate(store.conn)
