"""Group and recommendation custody for immutable prepared output."""
import hashlib
import time

from . import group_evidence, recommendation_references
from .evidence_ledger import canonical
from . import recommendation_custody

FIELDS = ('context', 'choice_context', 'album_choice_context')
PREPARED = frozenset({'album_prepared', 'home_prepared', 'home_tuned', 'home_choice', 'adaptive_group_preview'})


def context(store, captured, *, ledger=None):
    """Resolve captured reference fields without expanding model matrices."""
    from yt_playlist.controllers.group_reference_context import resolve
    ledger = store.rec_ledger.evidence() if ledger is None else ledger
    return resolve(captured, ledger, recommendations=recommendation_references.enabled(store))


def replace_prepared(store, surface, data, at, *, historical=False, resolved_contexts=None, owner_prefix=None,
                     recommendation_generations=None):
    """Replace only this output's references in its publication transaction."""
    if (not getattr(store, '_compact_group_files', False)
            or surface not in PREPARED and not surface.startswith('model_pair:')):
        return
    if not store.conn.in_transaction:
        raise RuntimeError('Prepared musical group roots require an outer transaction')
    at = time.time() if at is None else at
    if not isinstance(data, dict):
        data = {}
    comparison = surface.startswith('model_pair:')
    kind = 'comparison' if comparison else 'prepared'
    if comparison and isinstance(data.get('data'), dict):
        data = data['data']
    recommendations = recommendation_references.enabled(store)
    if recommendations:
        if recommendation_generations is None:
            recommendation_references.prepared(store.conn, kind, surface, data, owner_prefix=owner_prefix)
        else:
            identity = (owner_prefix or 'proposal:' + surface) + ':recommendations'
            recommendation_custody.release(store.conn, kind, identity)
            recommendation_references.retain(store.conn, kind, identity, recommendation_generations)
    for field in FIELDS:
        identity = (owner_prefix or 'proposal:' + surface) + ':' + field
        existing = store.conn.execute('SELECT reference_digest FROM group_evidence_owners '
                                      'WHERE kind=? AND external_key=?', (kind, identity)).fetchone()
        if historical and existing is not None:
            continue
        value = data.get(field)
        if field in {'choice_context', 'album_choice_context'}:
            value = None  # Listening choices do not own historical group snapshots.
        if recommendations:
            recommendation_custody.release(store.conn, kind, identity)
        if surface == 'adaptive_group_preview' and field == 'context' and data.get('generation'):
            value = {'group_generation': data['generation']}
        if value:
            try:
                if resolved_contexts is None:
                    value = context(store, value)
                elif field in resolved_contexts:
                    value = resolved_contexts[field]
                else:
                    raise ValueError('Historical prepared context is no longer retained')
            except ValueError:
                if not historical:
                    raise
                raw = value.get('artifact', identity) if isinstance(value, dict) else identity
                group_evidence.retain_unknown_context(store.conn, kind, identity, raw, 'missing_context', at)
                continue
            generation, versions = group_evidence.references(value)
            if recommendations:
                recommendation_references.retain(store.conn, kind, identity,
                                                  recommendation_references.references(value))
            if generation is not None or versions:
                digest = hashlib.sha256(canonical([generation, versions])).hexdigest()
                if existing is not None and existing[0] != digest:
                    group_evidence.release(store.conn, kind, identity)
                group_evidence.retain(store.conn, kind, identity, value, at,
                                     strict=False if historical else generation is not None)
                continue
        group_evidence.release(store.conn, kind, identity)
