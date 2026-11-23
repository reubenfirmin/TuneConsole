"""Check ordered choices and fixed policy roles on a real C175 library."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from time import monotonic

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import mix_choices, recommendation_reports
from yt_playlist.core.migrations import choice_evidence_storage, regular
from yt_playlist.core.migrations.choice_evidence_storage import RETIRED
from yt_playlist.core.store import Store


def _rows(conn, query):
    return list(map(tuple, conn.execute(query)))


def _tables(conn):
    return {name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_choice_evidence_upgrade(monkeypatch):
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    options = 'SELECT opportunity_id,option_id,position,descriptor_id,genres_complete,origin FROM mix_choice_options ORDER BY 1,3'
    meanings = 'SELECT id,intent,label,note,genres_json FROM mix_choice_descriptors ORDER BY id'
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        before = _tables(conn)
        options_before = _rows(conn, options)
        descriptors = _rows(conn, meanings)
        learner = _rows(conn, 'SELECT * FROM choice_interest_mix_descriptors ORDER BY 1,2')
        totals = _rows(conn, 'SELECT * FROM choice_format_usage ORDER BY 1,2,3')
        attribution = _rows(conn, 'SELECT impression_id,kind,raw_version,policy_version,historical_version,resolution_reason '
                            'FROM recommendation_impression_policies ORDER BY 1,2')
        served = _rows(conn, 'SELECT version,served FROM recommendation_policies ORDER BY 1')
        assert all((options_before, descriptors, learner, totals, attribution, served))

    changes = list(regular.CHANGES)
    change = next(change for change in changes if change.name == choice_evidence_storage.NAME)
    elapsed = {}
    preparing = None
    cutting_over = False
    def prepare(conn, version):
        nonlocal preparing
        preparing = monotonic()
        change.prepare(conn, version)
        elapsed['preparation'] = monotonic() - preparing

    def apply(conn, context):
        nonlocal cutting_over
        started = monotonic()
        change.apply(conn, context)
        elapsed['cutover_ddl'] = monotonic() - started
        cutting_over = True

    changes[change.version - 1] = replace(change, prepare=prepare, apply=apply)
    monkeypatch.setattr(regular, 'CHANGES', tuple(changes))
    store = Store(path)
    with closing(store.conn) as conn:
        commit = conn.commit

        def commit_and_measure():
            nonlocal cutting_over
            commit()
            if cutting_over:
                elapsed['preparation_through_cutover'] = monotonic() - preparing
                cutting_over = False

        monkeypatch.setattr(conn, 'commit', commit_and_measure)
        store.init_schema()
        after = _tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert options_before == _rows(conn, '''SELECT m.opportunity_id,o.value->>0,o.value->>1,o.value->>2,
            o.value->>3,o.value->>4 FROM mix_choice_menus m,json_each(m.options) o ORDER BY 1,3''')
        assert descriptors == _rows(conn, meanings)
        assert learner == _rows(conn, '''SELECT p.value->>'id',c.key,c.value->>'$.descriptor_id'
            FROM settings p,json_each(p.value,'$.document.contexts') c
            WHERE p.key GLOB 'choice_learned:*' AND c.value->>'$.descriptor_id' IS NOT NULL ORDER BY 1,2''')
        assert totals == _rows(conn, '''SELECT p.value->>'id',u.value->>0,u.value->>1,u.value->>2
            FROM prepared_display_manifests p,json_each(p.value,'$.format_usage') u
            WHERE p.key GLOB 'prepared_report:*' ORDER BY 1,2,3''')
        assert attribution == _rows(conn, '''
            SELECT id,'pool',CAST(json_extract(provenance,'$.pool_policy') AS TEXT),
                pool_policy_version,pool_historical_version,pool_policy_reason FROM rec_served_impressions
            WHERE pool_policy_version IS NOT NULL OR pool_historical_version IS NOT NULL OR pool_policy_reason IS NOT NULL
            UNION ALL SELECT id,'primary',model_version,primary_policy_version,primary_historical_version,
                primary_policy_reason FROM rec_served_impressions ORDER BY 1,2''')
        assert served == _rows(conn, 'SELECT version,served FROM recommendation_policies ORDER BY 1')
        usage = recommendation_reports.choice_usage(conn)
        for _, format, field, value in totals:
            assert usage[field][format] == value
        option = options_before[0]
        with conn.atomic():
            mix_choices.collect_dictionary(conn)
        assert mix_choices.projection_facts(conn, option[0], None, mixes=True)['options'][option[1]][
            'descriptor_id'] == option[3]
        for _, _, identity in learner:
            assert mix_choices.read_descriptor(conn, identity)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert recommendation_reports.choice_usage(conn) == usage
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
                  exact_offered_options=len(options_before), exact_descriptors=len(descriptors),
                  exact_learner_bindings=len(learner), exact_choice_usage_facts=len(totals),
                  exact_policy_roles=len(attribution), referenced_descriptors_kept_during_cleanup=True,
                  reopened=True, foreign_key_errors=0, page_size=choice_evidence_storage.PAGE_SIZE,
                  elapsed_seconds={key: round(seconds, 4) for key, seconds in elapsed.items()})
    report_file('choice-evidence-c181.json').write_text(json.dumps(report, indent=2) + '\n')
