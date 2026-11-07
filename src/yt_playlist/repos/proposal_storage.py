"""Select a proposal's serving owner and its compatible publication operation.

Native owners supersede explicit preparation selections. An inactive selection
falls back to installed proposal storage. Writes always target the installed
writer, whose journals invalidate or update a prepared selection atomically.
Feature operations (receipts, artwork, paid requests) stay with their owners.
"""
from dataclasses import dataclass
from functools import partial
import json
from typing import Callable

from yt_playlist.core import (album_note_outputs, album_note_requests, choice_learning_state,
    home_offers, home_outputs, library_pool_state, listening_outputs, model_comparison_state,
    model_projections, recommendation_requests, recommendation_work,
    tv_recommendation_state)
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos import proposal_cache


@dataclass(frozen=True)
class Storage:
    encoded: Callable[[], str | None]
    present: Callable[[], bool]
    prepare: Callable[[object, float | None, int | None], Callable[[], object]] | None = None


def _json(reader):
    def encoded():
        value = reader()
        return json.dumps(value) if value is not None else None
    return encoded


def _exists(conn, table, predicate='', parameters=()):
    return conn.execute('SELECT 1 FROM '+table+(' WHERE '+predicate if predicate else '')+
                        ' LIMIT 1', parameters).fetchone() is not None


def _typed(conn, surface, module, reader, present):
    def prepare(raw, now, revision):
        value = module.decode(surface, raw)
        return partial(module.write, conn, surface, value, now)
    return Storage(_json(reader), present, prepare)


def _tv_present(conn, surface, header=None):
    table, predicate = {
        'tv_pool': ('tv_pool_state', 'field_mask<>0'),
        'tv_artist_artwork': ('tv_artist_artwork', ''),
        'tv_artist_artwork_requests': ('tv_artist_requests', ''),
        'tv_visual_checks': ('tv_visual_results', ''),
        'tv_continuations': ('tv_continuation_receipts', "status='complete'"),
    }[surface]
    parameters = ()
    if header is not None:
        from yt_playlist.core.preparation.tv_state import relation
        table = relation(table)
        predicate = 'source=? AND preparation_revision=?'+(' AND '+predicate if predicate else '')
        parameters = (surface, header['preparation_revision'])
    return _exists(conn, table, predicate, parameters)


def _document(conn, owner, revision, present):
    # A selected empty generation owns the miss; it must not expose stale bytes.
    generation = conn.execute('SELECT generation FROM '+owner.tables[0]+' WHERE revision=?',
                              (revision,)).fetchone()[0]
    header = (conn.execute('SELECT * FROM '+owner.preparation.tables(owner.source)[0]+
        ' WHERE source=? AND '+owner.generation_column+'=?', (owner.source, generation)).fetchone()
        if generation is not None else None)
    return Storage(partial(owner.value, conn, revision),
                   lambda: header is not None and present(header))


def _legacy(store, surface, *, validate_home=False):
    conn = store.conn
    def encoded():
        if surface in proposal_cache.SURFACES:
            value = proposal_cache.read(conn, surface, store=store)
            if value is not None:
                return value
        row = conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        return row[0] if row else None
    def present():
        if surface in proposal_cache.SURFACES:
            if getattr(store, '_recommendation_files', False):
                return store.recommendations.head('home' if surface == 'mode_bundles' else 'fresh') is not None
            build = proposal_cache.head(conn, surface, store=store)
            if build is not None:
                return bool(build[3])
        return _exists(conn, 'rec_proposals', "surface=? AND payload NOT IN ('null','{}')", (surface,))
    def prepare(raw, now, revision):
        value = None
        if getattr(store, '_compact_group_files', False):
            value = (home_outputs.prepare(surface, raw, now).value
                     if validate_home and surface in home_outputs.SURFACES else json.loads(raw))
        def write():
            if getattr(store, '_compact_group_files', False):
                from yt_playlist.core import prepared_references
                prepared_references.replace_prepared(store, surface, value, now)
            conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                (surface, raw, now))
        return write
    return Storage(encoded, present, prepare)


def select(store, surface, *, writing=False, validate_home=False):
    """Resolve ownership once for encoded reads, cheap presence or publication."""
    conn = store.conn
    if surface.startswith('model_pair:'):
        if store._model_projections:
            def prepare(raw, now, revision):
                value = model_projections.prepare(conn, surface, json.loads(raw), now, input_revision=revision)
                return partial(model_projections.publish, conn, value, store=store)
            return Storage(_json(partial(model_projections.read, conn, surface)),
                lambda: _exists(conn, 'model_pair_heads', 'run_id=? AND arm=? AND format=?',
                                model_projections.address(surface)), prepare)
        if not writing:
            from yt_playlist.core.preparation.pair_publication import OWNER
            revision = OWNER.selected(conn)
            if revision is not None:
                return Storage(partial(OWNER.value, conn, revision, surface),
                               lambda: OWNER.capture(conn, revision, surface) is not None)
    elif surface in model_comparison_state.SURFACES:
        if store._model_comparison_state:
            return _typed(conn, surface, model_comparison_state,
                partial(model_comparison_state.read, conn, surface),
                lambda: _exists(conn, model_comparison_state.OWNERS[surface][0], 'field_mask<>0'))
        if not writing:
            from yt_playlist.core.preparation import trial_publication
            owner = trial_publication.SOURCES[surface]
            revision = owner.selected(conn)
            if revision is not None:
                return _document(conn, owner, revision, lambda header: bool(header['field_mask']))
    elif surface in library_pool_state.SURFACES:
        if not writing:
            from yt_playlist.core.preparation.clicked_publication import OWNER
            revision = OWNER.selected(conn)
            if revision is not None:
                return Storage(partial(OWNER.value, conn, revision, surface),
                               lambda: OWNER.capture(conn, revision, surface) is not None)
    elif surface in tv_recommendation_state.SURFACES:
        if store._tv_recommendations:
            return _typed(conn, surface, tv_recommendation_state,
                partial(tv_recommendation_state.read, conn, surface), partial(_tv_present, conn, surface))
        if not writing:
            from yt_playlist.core.preparation import tv_publication
            owner = tv_publication.SOURCES[surface]
            revision = owner.selected(conn)
            if revision is not None:
                return _document(conn, owner, revision, partial(_tv_present, conn, surface))
    elif surface in choice_learning_state.SURFACES:
        if store._choice_learning:
            return _typed(conn, surface, choice_learning_state,
                partial(choice_learning_state.read, conn, surface, store=store),
                partial(choice_learning_state.present, conn, surface, store=store))
        if not writing:
            from yt_playlist.core.preparation import choice_publication
            revision = choice_publication.selected(conn)
            if revision is not None:
                def present():
                    header = choice_publication.OWNER.capture(conn, revision, surface)
                    return header is not None and bool(header['field_mask'])
                return Storage(_json(partial(choice_publication.read, conn, surface)),
                               present)
    elif surface in recommendation_requests.SURFACES:
        if store._recommendation_requests:
            return Storage(_json(partial(recommendation_requests.read, conn, surface)),
                partial(recommendation_requests.present, conn, surface),
                lambda raw, now, revision: partial(recommendation_requests.write, conn, surface, json.loads(raw), now))
        if not writing:
            from yt_playlist.core.preparation import demand_publication
            revision = demand_publication.selected(conn)
            if revision is not None:
                return Storage(_json(partial(demand_publication.read, conn, surface, revision)),
                               partial(demand_publication.present, conn, surface, revision))
    elif surface == 'album_note_requests':
        if store._album_note_demand:
            return Storage(_json(partial(album_note_requests.read, conn)), lambda: _exists(conn, surface),
                lambda raw, now, revision: partial(album_note_requests.replace, conn, json.loads(raw), now))
    elif surface == 'home_shown' and home_offers.available(conn):
        prefix = 'prepared_' if home_offers.prepared_owner(conn) else ''
        return Storage(_json(partial(home_offers.read, conn)),
            lambda: _exists(conn, prefix+'home_offer_dedup', 'id=1'),
            lambda raw, now, revision: partial(home_offers.replace, conn, json.loads(raw), now))
    elif surface in recommendation_work.SURFACES and recommendation_work.available(conn):
        prefix = 'prepared_' if recommendation_work.prepared_owner(conn) else ''
        table = 'model_selection_schedule' if surface == 'daily_model_schedule' else 'tv_preparation_state'
        return Storage(_json(partial(recommendation_work.read, conn, surface)),
            lambda: _exists(conn, prefix+table, 'id=1'),
            lambda raw, now, revision: partial(recommendation_work.write, conn, surface, json.loads(raw), now))
    return _legacy(store, surface, validate_home=validate_home)


def read(store, surface, *, presence=False):
    with keyed_reads(store, 'rec_proposals', (surface,)):
        owner = select(store, surface)
        return owner.present() if presence else owner.encoded()


def publish(store, surface, raw, now, *, input_revision=None, standalone=False):
    owner = select(store, surface, writing=True, validate_home=not standalone)
    write = owner.prepare(raw, now, input_revision)
    if standalone and not store.conn.in_transaction:
        store.conn.execute('BEGIN IMMEDIATE')
    write()
