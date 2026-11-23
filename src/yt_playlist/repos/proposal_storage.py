"""Select a proposal's serving owner and publication operation."""
from dataclasses import dataclass
from functools import partial
import json
from typing import Callable

from yt_playlist.core import (album_note_requests, choice_learning_state,
    home_offers, listener_state, recommendation_requests,
    recommendation_work, tv_recommendation_state)
from yt_playlist.core.db_revisions import setting_reads


SETTING_SURFACES = (*listener_state.SURFACES, *recommendation_requests.SETTINGS,
                    home_offers.KEY)


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


def select(store, surface):
    """Resolve ownership once for encoded reads, cheap presence or publication."""
    conn = store.conn
    if surface in listener_state.SURFACES:
        return Storage(partial(listener_state.encoded, conn, surface),
            partial(listener_state.present, conn, surface),
            lambda raw, now, revision: partial(listener_state.write, conn, surface, raw, now))
    if surface in tv_recommendation_state.SURFACES:
        return _typed(conn, surface, tv_recommendation_state,
            partial(tv_recommendation_state.read, conn, surface), partial(tv_recommendation_state.present, conn, surface))
    if surface in choice_learning_state.SURFACES:
        return _typed(conn, surface, choice_learning_state,
            partial(choice_learning_state.read, conn, surface, store=store),
            partial(choice_learning_state.present, conn, surface, store=store))
    if surface in recommendation_requests.SURFACES:
        return Storage(_json(partial(recommendation_requests.read, conn, surface)),
            partial(recommendation_requests.present, conn, surface),
            lambda raw, now, revision: partial(recommendation_requests.write, conn, surface, json.loads(raw), now))
    if surface == 'album_note_requests':
        return Storage(_json(partial(album_note_requests.read, conn)),
            lambda: _exists(conn, 'album_provider_ids', 'note_pending=1'),
            lambda raw, now, revision: partial(album_note_requests.replace, conn, json.loads(raw), now))
    if surface == home_offers.KEY:
        return Storage(_json(partial(home_offers.read, conn)),
            lambda: _exists(conn, 'settings', 'key=?', (home_offers.KEY,)),
            lambda raw, now, revision: partial(home_offers.replace, conn, json.loads(raw), now))
    if surface in recommendation_work.SURFACES:
        reader = partial(recommendation_work.read, conn, surface)
        return Storage(_json(reader), partial(recommendation_work.present, conn, surface),
            lambda raw, now, revision: partial(recommendation_work.write, conn, surface, json.loads(raw), now))
    return None


def document(store, surface):
    storage = select(store, surface)
    if storage is not None:
        if surface in SETTING_SURFACES:
            with setting_reads(store, (surface,)):
                return storage.encoded()
        return storage.encoded()
    return None


def present(store, surface):
    storage = select(store, surface)
    if storage is not None:
        if surface in SETTING_SURFACES:
            with setting_reads(store, (surface,)):
                return storage.present()
        return storage.present()
    return False


def publish(store, surface, raw, now, *, input_revision=None, standalone=False):
    storage = select(store, surface)
    if storage is None:
        raise ValueError(f'Unknown recommendation surface: {surface}')
    write = storage.prepare(raw, now, input_revision)
    if standalone and not store.conn.in_transaction:
        store.conn.execute('BEGIN IMMEDIATE')
    write()
