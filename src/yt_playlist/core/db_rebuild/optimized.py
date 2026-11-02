"""Adapt optimized domain converters to the rebuild's single checkpoint.

The schema and conversion rules stay with their domain owners. These adapters
only supply the stable capture time, transaction and rebuild bookmark.
"""
import json
from functools import partial

from .. import metadata_runtime, served_facts
from ..preparation import metadata_runtime as metadata_preparation
from ..preparation.metadata_runtime_verification import verify as verify_metadata
from .build import Batch, Stage


def _metadata(reader, target, cursor, limit, *, stage, at, already_active, direct=False):
    if already_active:
        return Batch(done=True)
    limit = min(limit, 128)
    cursor, count = metadata_preparation.prepare_batch(target, stage, cursor, limit, at=at,
        **({'source': reader} if direct and stage in ('recordings', 'entries', 'choices') else {}))
    return Batch(cursor, count, count < limit)


def _activate_metadata(reader, target, cursor, limit):
    # This candidate is private. Its complete independent read-only verification
    # compares the compact owners with captured evidence before publication.
    metadata_runtime.activate(target)
    return Batch(done=True)


def prepare_served(reader, target, cursor, limit):
    return served_facts.staged_batch(target, min(limit, 128))


def _served(reader, target, cursor, limit, prepared=None):
    count = prepared() if prepared is not None else served_facts.prepare_batch(target, min(limit, 128))
    if count is None:
        return Batch((cursor or 0) + 1, 0, False)
    return Batch((cursor or 0) + count, count, count == 0)


def stages(*, at, metadata_active, native=True, direct=False):
    metadata = tuple(Stage('metadata:' + name, partial(_metadata, stage=name, at=at,
                                                     already_active=metadata_active, direct=direct))
                     for name in metadata_preparation.STAGES if native or name != 'native')
    return (*metadata, Stage('metadata:activate', _activate_metadata),
            Stage('served-facts', _served))


def native_stage(*, metadata_active):
    def copy(reader, target, cursor, limit):
        if metadata_active:
            return Batch(done=True)
        from .. import metadata_native, metadata_runtime_files
        rows = reader.execute('SELECT * FROM rec_musical_sources WHERE source_key>? '
                              'ORDER BY source_key LIMIT ?', (cursor or '', min(limit, 16))).fetchall()
        def documents():
            for row in rows:
                head = metadata_native.installed_runtime_head(reader, row['source_key'], {})
                if head is not None:
                    metadata_native.retain_runtime_head(target, row['source_key'], head)
                yield ('native', (row['source_key'],), row['revision'], {
                    'observations': metadata_native.source_values(reader, row['source_key'],
                        max_bytes=metadata_runtime_files.MAX_BYTES), 'provider': row['provider']})
        metadata_runtime_files.publish_many(target, documents())
        return Batch(rows[-1]['source_key'] if rows else cursor, len(rows), len(rows) < min(limit, 16))
    return Stage('metadata:native', copy, batch_size=16)


def refresh_metadata(reader, target, cursor, limit, *, at):
    """Replace discarded alternative/conflict caches through the normal worker."""
    from .. import provider_frontiers
    target.execute('DELETE FROM provider_refresh_frontiers')
    rows = target.execute("SELECT provider,fingerprint FROM enrichment_provider_config WHERE status='available'").fetchall()
    for provider, fingerprint in rows:
        provider_frontiers.start(target, provider, int(at*1_000_000), fingerprint)
    return Batch(rows=len(rows), done=True)


def verify_refresh(target, *, at):
    expected = [(provider, 0, int(at*1_000_000), fingerprint, 1) for provider, fingerprint in target.execute(
        "SELECT provider,fingerprint FROM enrichment_provider_config WHERE status='available' ORDER BY provider")]
    actual = [tuple(row) for row in target.execute('SELECT provider,cursor,cutoff,fingerprint,revision '
                                                 'FROM provider_refresh_frontiers ORDER BY provider')]
    if actual != expected:
        raise ValueError('Rebuild did not schedule provider re-enrichment')
    if target.execute('SELECT 1 FROM metadata_runtime_conflicts LIMIT 1').fetchone():
        raise ValueError('Rebuild retained a discarded metadata conflict')


def verify(reader, target, *, at, metadata_active, native=True):
    """Check compact serving facts independently of their preparation counters."""
    if not metadata_runtime.active(target) or not served_facts.active(target):
        raise ValueError('Rebuild left an optimized serving owner inactive')
    if not metadata_active:
        # Compare against captured SQL owners, including after activation has
        # changed the public projections to read compact facts.
        verify_metadata(target, {'at': at}, native=native)
    original_tables = {name for (name,) in reader.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    expected_count = 0
    has_facts = 'served_facts' in original_tables
    for original in target.execute('SELECT * FROM rec_served_impressions ORDER BY id'):
        facts = target.execute('SELECT * FROM served_facts WHERE impression_id=?', (original['id'],)).fetchone()
        if facts is None:
            raise ValueError('Rebuild lost served impression facts')
        # Before obsolete JSON is retired, it remains independent captured
        # evidence for every SQL fact and its external diagnostic document.
        captured = (reader.execute('SELECT * FROM served_facts WHERE impression_id=?', (original['id'],)).fetchone()
                    if has_facts else None)
        if captured is not None:
            if tuple(facts) != tuple(captured):
                raise ValueError('Rebuild changed captured served facts')
            expected_values = tuple(captured[field] for field in served_facts.FIELDS)
            source_row = reader.execute('SELECT * FROM rec_served_impressions WHERE id=?', (original['id'],)).fetchone()
            expected = (served_facts.diagnostic(reader, {**dict(source_row), **dict(captured)})
                        if not captured['diagnostic_expired'] else None)
        else:
            expected = json.loads(original['provenance'])
            expected_values = served_facts.extract(expected)
        if tuple(facts[field] for field in served_facts.FIELDS) != expected_values:
            raise ValueError('Rebuild changed served impression facts')
        if not facts['diagnostic_expired']:
            if served_facts.diagnostic(target, {**dict(original), **dict(facts)}) != expected:
                raise ValueError('Rebuild changed served impression diagnostics')
        expected_count += 1
    if target.execute('SELECT count(*) FROM served_facts').fetchone()[0] != expected_count:
        raise ValueError('Rebuild invented served impression facts')
    next_id = target.execute('SELECT next_id FROM served_fact_state WHERE id=1').fetchone()[0]
    previous = (reader.execute('SELECT next_id FROM served_fact_state WHERE id=1').fetchone()[0]
                if 'served_fact_state' in original_tables else 1)
    highest = target.execute('SELECT coalesce(max(id),0)+1 FROM rec_served_impressions').fetchone()[0]
    if next_id != max(previous, highest):
        raise ValueError('Rebuild changed served impression allocation')
