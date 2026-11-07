import json
import sqlite3

import pytest

from yt_playlist.core import catalog
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.providers.waterfall import TrackSink, run_waterfall
from tests.test_waterfall import FakeProv
from tests.historical_support import upgrade_fixture
from tests.metadata_provenance_support import metadata_provenance
from tests.prepared_fixture_support import historical_template


@pytest.fixture(scope='module')
def duplicate_decisions_template(_pristine_installed_schema):
    def build(source):
        subjects = {}
        for reverse in (False, True):
            for values in (('Rock', 'Pop'), ('Rock', '')):
                offset = len(subjects) * 10
                first, second = 3 + offset, 7 + offset
                video = f'duplicate-{offset}'
                rows = [(first, values[0], 10), (second, values[1], 20)]
                if reverse:
                    rows.reverse()
                for track, value, at in rows:
                    source.conn.execute('INSERT INTO tracks(id,identity_key,video_id,title,artist,genre) '
                        'VALUES (?,?,?,?,?,?)', (track, f'key-{track}', video, 'Song', 'Artist', value))
                    source.conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,'
                        'resolved_value,updated_at) VALUES (?,\'genre\',\'[]\',1,?,?)', (track, value, at))
                subjects[reverse, values] = (video, second)
        source.conn.commit()
        # One genuine populated replay contains four independent conflicts.
        # Each parameter restores its own SQL/files before resolving its case.
        upgrade_fixture(source)
        return {case: (source.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?',
                                          (video,)).fetchone()[0], alias)
                for case, (video, alias) in subjects.items()}

    with historical_template(_pristine_installed_schema(7), build, database=':memory:') as template:
        yield template


@pytest.mark.parametrize('values', [('Rock','Pop'),('Rock','')])
@pytest.mark.parametrize('reverse', [False,True])
def test_duplicate_legacy_decisions_reconcile_without_arbitrary_winner(store, duplicate_decisions_template, values, reverse):
    s = store
    entry, alias = duplicate_decisions_template.restore(s)[reverse, values]
    assert s.tracks.get_track_enrichment(entry)[0] == ''
    detail = s.enrichment_reports.enrichment_conflict_detail(entry,'genre')
    assert {o['value'] for o in detail['options']} >= set(values)
    conflict = s.conn.execute('SELECT resolved,resolved_value FROM enrichment_conflict WHERE track_id=?', (entry,)).fetchone()
    assert tuple(conflict) == (0,None)
    assert s.conn.execute('SELECT COUNT(*) FROM entry_field_choices WHERE entry_id=?', (entry,)).fetchone()[0] == 0
    choices = s.conn.execute('SELECT value_json,chosen_at FROM metadata_decisions '
                             'WHERE entry_id=? AND legacy_ref IS NOT NULL ORDER BY chosen_at', (entry,)).fetchall()
    assert [(json.loads(r[0]),r[1]) for r in choices] == [(values[0],10),(values[1],20)]
    before = metadata_provenance(s, entry)
    s.init_schema()
    assert metadata_provenance(s, entry) == before
    s.enrichment.resolve_conflict(alias,'genre',values[1],now=30)
    assert s.tracks.get_track_enrichment(entry)[0] == values[1]
    assert tuple(s.conn.execute('SELECT resolved,resolved_value FROM enrichment_conflict WHERE track_id=?',
                               (entry,)).fetchone()) == (1,values[1])
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_provider_refresh_selection_and_blank_override_with_worker_sink(store):
    track = store.tracks.upsert_track('video','Song','Artist',None,200)
    a = FakeProv('musicbrainz',{'Song':{'genre':'Rock','bpm':120}})
    b = FakeProv('discogs',{'Song':{'genre':'Pop','bpm':125}})
    config = [{'name':p.name,'enabled':True} for p in (a,b)]

    def run():
        run_waterfall(store,[store.tracks.track_for_waterfall(track)],config,lambda e:None,
                      registry={p.name:p for p in (a,b)},sink_for=lambda t:TrackSink(store,t))

    run()
    assert store.tracks.get_track_enrichment(track)[0] == 'Rock'
    assert store.tracks.get_track_audio(track)[0] == 120
    a._by_title['Song'] = {'genre':'Electronic','bpm':122}
    run()
    assert store.tracks.get_track_enrichment(track)[0] == 'Electronic'
    assert store.tracks.get_track_audio(track)[0] == 122
    store.tracks.set_track_genre(track,'')
    with store.conn:
        catalog.choose_field(store.conn,track,'bpm',None)
    a._by_title['Song'] = {'genre':'House','bpm':128}
    run()
    fields = metadata_provenance(store, track)['fields']
    assert fields['genre'] == {'value':'','override':True,'source':'user'}
    assert fields['bpm'] == {'value':None,'override':True,'source':'user'}
    from yt_playlist.core import genre_classifications
    recording = store.tracks.catalog_subject(track)['recording_id']
    assert genre_classifications.read(store.conn, 'recordings', recording) == 'House'
    assert tuple(store.conn.execute('SELECT bpm,typeof(bpm) FROM recordings WHERE id=?',
                                    (recording,)).fetchone()) == (128,'real')
    with store.conn:
        catalog.withdraw_choice(store.conn, track, 'genre')
        catalog.withdraw_choice(store.conn, track, 'bpm')
    fields = metadata_provenance(store, track)['fields']
    assert fields['genre'] == {'value':'House','override':False,'source':'musicbrainz'}
    assert store.conn.execute("SELECT COUNT(*) FROM metadata_observations o JOIN metadata_deliveries d "
                              "ON d.id=o.delivery_id WHERE d.provider IN ('musicbrainz','discogs') "
                              "AND o.field IN ('genre','bpm')").fetchone()[0] == 12


def test_populated_upgrade_does_not_invent_user_intent_and_rolls_back(tmp_path):
    s = Store(tmp_path / 'state.db')
    migrate(s.conn,registry=MIGRATIONS[:19])
    s.conn.execute("INSERT INTO recordings(id,identity_key,genre) VALUES (1,'legacy','Rock')")
    s.conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,orig_title) VALUES ('v',1,'Edited','Original')")
    s.conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v')")
    s.conn.execute("INSERT INTO entry_field_choices VALUES (1,'title','\"Edited\"','user')")
    s.conn.commit()

    def interrupted(conn, context):
        MIGRATIONS[19].apply(conn,context)
        raise RuntimeError('interrupted metadata migration')

    with pytest.raises(MigrationError,match='interrupted metadata'):
        migrate(s.conn,registry=(*MIGRATIONS[:19],Migration(20,MIGRATIONS[19].name,interrupted)))
    assert not s.conn.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_decisions'").fetchone()
    assert s.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 19
    upgrade_fixture(s)
    fields = metadata_provenance(s, 1)['fields']
    assert fields['genre'] == {'value':'Rock','override':False,'source':'legacy_unknown'}
    assert fields['title'] == {'value':'Edited','override':True,'source':'user'}
    assert fields['orig_title']['value'] == 'Original'
    before = metadata_provenance(s, 1)
    s.init_schema()
    assert metadata_provenance(s, 1) == before
    assert not (tmp_path / 'backups').exists()
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_resolution_failure_preserves_choice_and_retry_completes(store):
    """A failed receipt preserves the earlier choice; retry finishes the ordered workflow."""
    track = store.tracks.upsert_track('video','Song','Artist',None,200)
    store.enrichment.upsert_conflict(track,'genre',[{'provider':'a','value':'Rock'},
                                                  {'provider':'b','value':'Pop'}])
    store.conn.execute("CREATE TEMP TRIGGER reject_resolution BEFORE UPDATE ON metadata_conflicts "
                       "BEGIN SELECT RAISE(ABORT,'interrupted resolution'); END")
    with pytest.raises(sqlite3.IntegrityError,match='interrupted resolution'):
        store.enrichment.resolve_conflict(track,'genre','')
    assert store.tracks.get_track_enrichment(track)[0] == ''
    assert store.conn.execute('SELECT COUNT(*) FROM entry_field_choices').fetchone()[0] == 1
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_runtime_choices').fetchone()[0] == 0
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_decisions').fetchone()[0] == 1
    assert store.conn.execute('SELECT resolved FROM enrichment_conflict').fetchone()[0] == 0
    assert not store.conn.in_transaction
    store.conn.execute('DROP TRIGGER reject_resolution')
    store.enrichment.resolve_conflict(track, 'genre', '')
    assert store.tracks.get_track_enrichment(track)[0] == ''
    assert store.conn.execute('SELECT resolved,resolved_value FROM enrichment_conflict').fetchone()[:] == (1, '')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
