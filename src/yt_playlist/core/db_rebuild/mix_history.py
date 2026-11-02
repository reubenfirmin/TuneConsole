"""Run the mix-choice owner's preparation inside the candidate's bookmarks."""
from functools import partial

from .. import choice_learning_state, group_documents, mix_choices
from ..preparation import mix_choices as preparation
from ...rec import group_root_backfill
from ...repos import compact_group_publication, group_retention
from .build import Batch, Stage


def _prepare(reader, store, cursor, limit, *, at):
    if cursor is None:
        # An installed partial scan may have observed an earlier subset. The
        # immutable menu projection is idempotent; inspect the whole capture.
        store.conn.execute('DELETE FROM mix_choice_preparation')
    progressed = preparation.step(store, at)
    return Batch((cursor or 0) + 1, rows=int(progressed), done=not progressed)


def _roots(reader, store, cursor, limit):
    progressed = group_root_backfill.maintain(store)
    return Batch((cursor or 0) + 1, rows=int(progressed), done=not progressed)


def _groups(reader, store, cursor, limit, *, at):
    progressed = compact_group_publication.cleanup_batch(store.taste_groups, before=at)
    if not progressed:
        selected = group_retention.plan(store.conn)
        if selected is not None:
            group_retention.apply(store.conn, selected)
            progressed = True
    return Batch((cursor or 0) + 1, rows=int(progressed), done=not progressed)


def _files(reader, target, cursor, limit, *, table):
    key, count, done = group_documents.repack_batch(target, table, cursor, min(limit, 128))
    return Batch(key, count, done)


def stages(at):
    # File roots are shared with the retained original and admitted readers.
    # Publish new packed references here; leave collection to ordinary runtime
    # maintenance after those readers and the rebuild's source fences drain.
    return (Stage('mixes:choices', partial(_prepare, at=at), repositories=True),
            Stage('mixes:roots', _roots, repositories=True),
            Stage('mixes:groups', partial(_groups, at=at), repositories=True),
            *(Stage('groups:files:' + table, partial(_files, table=table))
              for table, _keys, _prefix, _kind in group_documents.OWNERS))


def obsolete(kind, identity):
    return (kind in {'offer', 'response', 'checkpoint'} or
            kind in {'prepared', 'comparison'} and
            (identity.endswith((':choice_context', ':album_choice_context')) or
             identity.startswith('choice-learning:')))


# These independently replaceable serving projections are rebuilt from the
# original immutable ledger, rather than copied as historical observations.
LEARNING = frozenset((*choice_learning_state.READ_TABLES, 'choice_learning_heads',
                      'choice_learning_group_roots', 'mix_choice_preparation'))
DICTIONARY = frozenset(name for name, sql in mix_choices.definitions() if sql.startswith('CREATE TABLE')) - LEARNING
