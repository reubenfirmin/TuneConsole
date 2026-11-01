"""Explicit resumable choice preparation; no historical work runs at startup.

Run python -m yt_playlist.core.preparation.mix_choices DATABASE --batches 10.
Each prefix publishes files before SQL, recompiles learning before releasing
obsolete choice roots, and leaves active grouping/build/output owners alone.
"""
import argparse
import hashlib
import heapq
import json
from .. import fao
from pathlib import Path
import time

from .. import group_evidence, mix_choices
from ..evidence_ledger import canonical
from ...rec import choice_evidence, group_roots

BATCH = 32
MAX_BYTES = 8 * 1024 * 1024


def status(conn):
    row = conn.execute('SELECT phase,cursor FROM mix_choice_preparation WHERE id=1').fetchone()
    return dict(row) if row else {'phase': 'menus', 'cursor': ''}


def _advance(conn, phase, cursor=''):
    conn.execute('INSERT INTO mix_choice_preparation(id,phase,cursor) VALUES (1,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET phase=excluded.phase,cursor=excluded.cursor', (phase, cursor))


def _context(store, ledger, offer):
    identity = offer['context']
    kind = 'mixes' if offer['format'] == 'mixes' else 'independent'
    original = ledger.get('artifact', identity)
    if original is None:
        from ..choice_checkpoints import checkpoint
        saved = checkpoint(ledger, identity)
        original = saved['context'] if saved else None
    if not isinstance(original, dict):
        # Keep a missing source explicit: neither a current timestamp nor a
        # current catalog lookup can establish its historical inputs.
        original = {'missing_context': True}
    digest = hashlib.sha256(canonical(original)).digest()
    context = mix_choices.context(original, mixes=kind == 'mixes')
    if original.get('missing_context'):
        context['missing_context'] = True
    target = ledger.artifact(context)
    return (identity, kind, target, digest), original.get('at', offer['at'])


def _menus(store, state):
    ledger = store.rec_ledger.evidence()
    # Only a bounded number of payloads is retained. New-format producers
    # publish their own projections, including IDs before this scan cursor.
    paths = heapq.nsmallest(BATCH, (p for p in (ledger.directory / 'opportunity').glob('*/*.json')
                                   if p.stem > state['cursor']), key=lambda p: p.stem)
    used, prepared = 0, []
    for path in paths:
        size = fao.stat(path).logical_bytes
        if size > MAX_BYTES:
            raise ValueError('Historical menu exceeds preparation byte budget')
        if used and used + size > MAX_BYTES:
            break
        offer = json.loads(fao.read_bytes(path))
        if hashlib.sha256(offer['id'].encode()).hexdigest() != path.stem:
            raise ValueError('Historical menu identity does not match its file')
        projection, at = _context(store, ledger, offer)
        prepared.append((path, offer, projection, at))
        used += size
    with group_roots.writer(store):
        for path, offer, projection, at in prepared:
            identity, kind, target, digest = projection
            prior = store.conn.execute('SELECT context,source_digest FROM choice_context_projections '
                                       'WHERE source_context=? AND kind=?', (identity, kind)).fetchone()
            if prior and tuple(prior) != (target, digest):
                raise ValueError('Choice context source changed during preparation')
            store.conn.execute('INSERT INTO choice_context_projections VALUES (?,?,?,?) ON CONFLICT DO NOTHING', projection)
            if offer['format'] == 'mixes':
                prior = store.conn.execute('SELECT 1 FROM mix_choice_menus WHERE opportunity_id=?', (offer['id'],)).fetchone()
                if not prior:
                    mix_choices.publish_menu(store.conn, {**offer, 'context_at': at}, target, source=offer)
            _advance(store.conn, 'menus', path.stem)
        if not paths:
            _advance(store.conn, 'learners')
    return len(paths)


def step(store, now=None):
    """Commit one prefix. Restart repeats only the last uncommitted prefix."""
    now = time.time() if now is None else now
    with group_roots.admission(store):
        state = status(store.conn)
        if state['phase'] == 'ready':
            return False
        if state['phase'] == 'menus':
            _menus(store, state)
        elif state['phase'] == 'learners':
            choice_evidence.compile_report(store, now)
            with group_roots.writer(store):
                _advance(store.conn, 'displays')
        elif state['phase'] == 'displays':
            _display(store, state)
        else:
            with group_roots.writer(store):
                owners = store.conn.execute("SELECT kind,external_key FROM group_evidence_owners WHERE "
                    "kind IN ('offer','response','checkpoint') OR "
                    "(kind IN ('prepared','comparison') AND (external_key LIKE '%:choice_context' "
                    "OR external_key LIKE '%:album_choice_context' OR external_key LIKE 'choice-learning:%')) "
                    'ORDER BY kind,external_key LIMIT 1').fetchall()
                for kind, identity in owners:
                    # Each owner may expand to thousands of versions. Retire
                    # one bounded relation prefix instead of one giant DELETE.
                    count = store.conn.execute('DELETE FROM group_evidence_versions WHERE '
                        '(kind,external_key,external_version) IN (SELECT kind,external_key,external_version '
                        'FROM group_evidence_versions WHERE kind=? AND external_key=? LIMIT 128)', (kind, identity)).rowcount
                    if not count:
                        # These obsolete ownership relations are independent
                        # of the still-readable prepared display/publication.
                        # The replacement learners committed in the prior phase.
                        for table in ('choice_learning_group_roots', 'home_display_group_roots'):
                            if store.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone():
                                store.conn.execute(f'DELETE FROM {table} WHERE kind=? AND external_key=?', (kind, identity))
                        group_evidence.release(store.conn, kind, identity)
                        if store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_generation_refs'").fetchone():
                            from ...repos import recommendation_publication
                            recommendation_publication.release(store.conn, kind, identity)
                if not owners:
                    _advance(store.conn, 'ready')
    return True


def _display(store, state):
    """Replace one guarded display through its normal publication boundary."""
    from .. import home_outputs
    from ...rec.choice_context import context_manifest
    if not store._home_display_files:
        with group_roots.writer(store):
            _advance(store.conn, 'roots')
        return
    row = store.conn.execute('SELECT h.surface,p.id,p.built_at,p.input_revision FROM home_display_heads h '
        'JOIN home_display_publications p ON p.id=h.publication_id WHERE h.surface>? ORDER BY h.surface LIMIT 1',
        (state['cursor'],)).fetchone()
    if row is None:
        with group_roots.writer(store):
            _advance(store.conn, 'roots')
        return
    raw = home_outputs.encoded(store.conn, row['surface'])
    if raw is None:
        raise ValueError('Prepared display unavailable during choice preparation')
    value = home_outputs.prepare(row['surface'], raw, row['built_at']).value
    if isinstance(value, dict) and any(value.get(f) for f in ('choice_context','album_choice_context')):
        value = dict(value)
        ledger = store.rec_ledger.evidence()
        for field in ('choice_context','album_choice_context'):
            if value.get(field):
                original = context_manifest(ledger, value[field])
                compact = mix_choices.context(original, mixes=field == 'choice_context')
                value[field] = {'artifact': ledger.artifact(compact)}
        prepared = home_outputs.prepare(row['surface'], canonical(value).decode(), row['built_at'])
        with group_roots.writer(store):
            current = store.conn.execute('SELECT publication_id FROM home_display_heads WHERE surface=?', (row['surface'],)).fetchone()
            if current[0] != row['id']:
                return  # Another publication won; inspect its current input next turn.
            home_outputs.publish(store.conn, prepared, store=store, input_revision=row['input_revision'])
            _advance(store.conn, 'displays', row['surface'])
    else:
        with group_roots.writer(store):
            _advance(store.conn, 'displays', row['surface'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--batches', type=int, default=1)
    args = parser.parse_args()
    if args.batches < 1:
        parser.error('--batches must be positive')
    from ..store import Store
    store = Store(args.database)
    try:
        store.init_schema()
        for _ in range(args.batches):
            if not step(store):
                break
        print(json.dumps(status(store.conn)))
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
