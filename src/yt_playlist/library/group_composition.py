"""Bind group commands to explicit repositories, snapshots and file lifetimes."""
from contextlib import contextmanager

from yt_playlist.controllers.group_preparation import GroupPreparation
from yt_playlist.controllers.group_root_backfill import Backfill
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.group_preparation_state import SERIAL
from yt_playlist.library import group_lifetimes as group_roots
from yt_playlist.library.group_lifetimes import pending_roots


def preparation(store, *, policy, budget):
    from yt_playlist.controllers.group_preparation import report

    @contextmanager
    def capture():
        # Lease lookups can change while staging. Head selection and the exact
        # published generation content still guard the worker's output.
        # The serial schedules work. The captured musical inputs guard its publication.
        with store.read_snapshot(allow_stale_keys={('group_generations', ''), ('settings', 'key:' + SERIAL)}) as snapshot, \
                column_group_reads(snapshot.store, 'recordings', 'catalog'), \
                column_group_reads(snapshot.store, 'prepared_recording_inputs', 'grouping'):
            reader = snapshot.store
            yield snapshot, reader.taste_groups, reader.genres, reader.modes.meta_for

    return GroupPreparation(store.taste_groups, store.prepared_content, store.settings,
        store.maintenance_diagnostics, capture=capture,
        report=lambda phase, now, **kw: report(store.settings, phase, now, **kw), policy=policy, budget=budget)


def backfill(store, **limits):
    return Backfill(store.group_evidence, store.rec_ledger.evidence(),
        admission=lambda: group_roots.admission(store),
        writer=lambda: group_roots.writer(store), resolve=lambda value: group_roots.context(store, value),
        retain=lambda *args, **kw: group_roots.retain(store, *args, **kw),
        release=lambda *args: group_roots.release(store, *args), **limits)


def candidates(store):
    from yt_playlist.controllers.group_candidates import GroupCandidates
    from yt_playlist.rec import mode_rules as mode_surfaces, rec_params, stored_groups

    def capture():
        header = store.recommendations.head('home')
        generation = header['id'] if header else None
        pools = tuple(lane for lane in mode_surfaces.CARD_SURFACES + ('temporal',)
                      if generation is not None and store.recommendations.pool(
                          'home', 'all', lane, generation=generation) is not None)
        return dict(generation=generation, pools=pools,
                    muted=store.rec_model.muted_artists(),
                    cap_artist=int(rec_params.get_param(store, 'modes_artist_cap')),
                    cap_album=int(rec_params.get_param(store, 'modes_album_cap')))

    return GroupCandidates(capture=capture,
        frame=lambda *args, **kw: stored_groups.preview_frame(store, *args, **kw))
