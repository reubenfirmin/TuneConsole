"""Select a proposal's serving owner and publication operation."""
from dataclasses import dataclass
from functools import partial
import json
from typing import Callable

from yt_playlist.core import (album_note_requests, choice_learning_state,
    home_offers, model_comparison_state, recommendation_requests,
    recommendation_work, tv_recommendation_state)
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


def _tv_present(conn, surface):
    table, predicate = {
        'tv_pool': ('tv_pool_state', 'field_mask<>0'),
        'tv_artist_artwork': ('tv_artist_artwork', ''),
        'tv_artist_artwork_requests': ('tv_artist_requests', ''),
        'tv_visual_checks': ('tv_visual_results', ''),
        'tv_continuations': ('tv_continuation_receipts', "status='complete'"),
    }[surface]
    return _exists(conn, table, predicate)


def _inline(store, surface):
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
                return _exists(conn, 'recommendation_heads', 'surface=?',
                               ('home' if surface == 'mode_bundles' else 'fresh',))
            build = proposal_cache.head(conn, surface, store=store)
            if build is not None:
                return bool(build[3])
        return _exists(conn, 'rec_proposals', "surface=? AND payload NOT IN ('null','{}')", (surface,))
    def prepare(raw, now, revision):
        def write():
            if surface == 'group_choice_bandit':
                try:
                    same = choice_learning_state.unchanged_group_report(
                        json.loads(encoded() or 'null'), json.loads(raw), now)
                except (TypeError, ValueError):
                    same = False
                if same:
                    return
            conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                (surface, raw, now))
        return write
    return Storage(encoded, present, prepare)


def select(store, surface, *, writing=False):
    """Resolve ownership once for encoded reads, cheap presence or publication."""
    conn = store.conn
    if surface in model_comparison_state.SURFACES and store._model_comparison_state:
        return _typed(conn, surface, model_comparison_state,
            partial(model_comparison_state.read, conn, surface),
            lambda: _exists(conn, model_comparison_state.OWNERS[surface][0], 'field_mask<>0'))
    if surface in tv_recommendation_state.SURFACES and store._tv_recommendations:
        return _typed(conn, surface, tv_recommendation_state,
            partial(tv_recommendation_state.read, conn, surface), partial(_tv_present, conn, surface))
    if surface in choice_learning_state.SURFACES and store._choice_learning:
        return _typed(conn, surface, choice_learning_state,
            partial(choice_learning_state.read, conn, surface, store=store),
            partial(choice_learning_state.present, conn, surface, store=store))
    if surface in recommendation_requests.SURFACES and store._recommendation_requests:
        return Storage(_json(partial(recommendation_requests.read, conn, surface)),
            partial(recommendation_requests.present, conn, surface),
            lambda raw, now, revision: partial(recommendation_requests.write, conn, surface, json.loads(raw), now))
    if surface == 'album_note_requests' and store._album_note_demand:
        return Storage(_json(partial(album_note_requests.read, conn)), lambda: _exists(conn, surface),
            lambda raw, now, revision: partial(album_note_requests.replace, conn, json.loads(raw), now))
    if surface == 'home_shown' and home_offers.available(conn):
        return Storage(_json(partial(home_offers.read, conn)), lambda: _exists(conn, 'home_offer_dedup', 'id=1'),
            lambda raw, now, revision: partial(home_offers.replace, conn, json.loads(raw), now))
    if surface in recommendation_work.SURFACES and recommendation_work.available(conn):
        table = 'model_selection_schedule' if surface == 'daily_model_schedule' else 'tv_preparation_state'
        return Storage(_json(partial(recommendation_work.read, conn, surface)),
            lambda: _exists(conn, table, 'id=1'),
            lambda raw, now, revision: partial(recommendation_work.write, conn, surface, json.loads(raw), now))
    return _inline(store, surface)


def document(store, surface):
    with keyed_reads(store, 'rec_proposals', (surface,)):
        return select(store, surface).encoded(), None


def present(store, surface):
    with keyed_reads(store, 'rec_proposals', (surface,)):
        return select(store, surface).present()


def publish(store, surface, raw, now, *, input_revision=None, standalone=False):
    write = select(store, surface, writing=True).prepare(raw, now, input_revision)
    if standalone and not store.conn.in_transaction:
        store.conn.execute('BEGIN IMMEDIATE')
    write()
