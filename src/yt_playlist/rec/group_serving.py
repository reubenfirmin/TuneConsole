"""Prepare versioned musical interests and mix candidates from production pools."""
import json
import logging
from dataclasses import dataclass, field
import threading

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_stopping
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.work_diagnostics import maintenance_job
from yt_playlist.rec import group_learning, group_preparation, mode_surfaces

SURFACE = 'adaptive_group_preview'
SCHEMA = 3
PHASE = 'rec: adaptive mix shadow'


def enabled(store):
    return store.settings.get_setting('rec_group_serving') != 'legacy'


def checkpoint(store):
    prepared = store.rec_proposals.group_checkpoint() if enabled(store) else None
    return prepared if prepared and prepared['schema'] == SCHEMA and prepared['mix_regions'] else None


def matching_preview(store, generation):
    saved = checkpoint(store)
    if saved is None or saved['generation'] != generation:
        return None
    preview = store.rec_proposals.get_proposals(SURFACE)
    # Publication can advance between reading the checkpoint and its payload.
    return preview if preview and preview['generation'] == generation else None


def policy(store):
    return 'adaptive' if checkpoint(store) else 'legacy'


def calculate(store, head, now, *, progress=None):
    progress = progress if progress is not None else {}
    candidates = prepare_candidates(store, head, now, progress=progress)
    return compose(store, head, now, candidates, progress=progress)


def prepare_candidates(store, head, now, *, progress=None):
    """Candidate eligibility and diversity do not consume allocation reports."""
    from yt_playlist.library.group_composition import candidates
    return candidates(store).prepare(head, now, progress=progress)


def compose(store, head, now, prepared_candidates, *, progress, allocation_at=None):
    """Apply current learning after the expensive, independently guarded pages."""
    stats = group_learning.current_state(store, head, now if allocation_at is None else allocation_at)
    progress['allocation_expires_at'] = stats.get('expires_at')
    return compose_facts(head, now, prepared_candidates, stats=stats, enabled=enabled(store))


from yt_playlist.rec.group_preview_values import candidate_frame


def compose_facts(head, now, prepared_candidates, *, stats, enabled):
    allocation = group_learning.allocation(head, stats)
    groups = [group for group in head['groups'] if not group.get('legacy')]
    by_version = {group['version']: group for group in groups}
    prepared = {identity: {**group, 'frames': {lane: frame.copy() for lane, frame in group['frames'].items()}}
                for identity, group in prepared_candidates['groups'].items()}
    for group in groups:
        prepared[group['group_id']]['allocation'] = allocation.get(group['group_id'], 0.)
    for group in groups:
        if not group.get('leaf'):
            continue
        record = prepared[group['group_id']]
        for frame, candidates in record['frames'].items():
            fallback = None
            parent = by_version.get(group['parent_version'])
            while (not candidates['full_mix'] or not group.get('display')) and parent:
                parent_mix = prepared[parent['group_id']]['frames'].get(frame, {})
                if parent.get('display') and parent_mix.get('full_mix'):
                    fallback = parent['version']
                    break
                parent = by_version.get(parent['parent_version'])
            candidates['fallback_version'] = fallback
    menus = {}
    for frame in prepared_candidates['pools']:
        weights = {}
        for group in groups:
            if not group.get('leaf'):
                continue
            candidates = prepared[group['group_id']]['frames'][frame]
            version = (group['version'] if group.get('display') and candidates['full_mix']
                       else candidates['fallback_version'])
            if version:
                weights[version] = weights.get(version, 0.) + allocation.get(group['group_id'], 0.)
        total = sum(weights.values())
        menus[frame] = [{'group_version': version, 'group_id': by_version[version]['group_id'],
                         'name': by_version[version]['name'], 'probability': weight / total}
                        for version, weight in weights.items()] if total else []
    context = {'schema': 1, 'at': now, 'grouping_policy': head['policy'], 'group_generation': head['generation'],
               'musical_groups': [{key: group.get(key) for key in
                   ('group_id', 'version', 'parent_version', 'name', 'size')} for group in groups]}
    result = {'schema': SCHEMA, 'at': now, 'generation': head['generation'], 'shadow': not enabled,
            'serving_policy': 'adaptive' if enabled else 'legacy',
            'groups': list(prepared.values()), 'context': context,
            'counts': head['payload']['counts'], 'menus': menus}
    generation = prepared_candidates['generation']
    result['recommendation_generations'] = [generation] if generation is not None else []
    return result


@dataclass
class PreviewWork:
    guard: object
    at: float
    head: dict
    progress: dict = field(default_factory=dict)
    result: dict | None = None
    candidates: dict | None = None
    publication: object = None

    def candidates_current(self, now):
        expires = self.progress.get('expires_at')
        return (now >= self.at and (expires is None or now < expires)
                and self.guard.is_current())

    def current(self, now):
        if not self.candidates_current(now):
            return False
        if self.publication is None:
            return True
        expires = self.progress.get('allocation_expires_at')
        return (expires is None or now < expires) and self.publication.is_current()


class Preparation:
    """Retain detached page progress, never a connection or pinned WAL reader."""
    def __init__(self, store):
        self.store, self.pending, self.complete = store, None, None
        self.lock = threading.RLock()

    def due(self, now):
        with self.lock:
            return self.pending is not None or self.complete is None or not self.complete.current(now)

    def step(self, now, seconds):
        store = self.store
        with self.lock:
            try:
                # A preview frame owns its completed pages. Do not interrupt a
                # page halfway through SQL/eligibility and read it again.
                with maintenance_job(PHASE):
                    if store.settings.get_setting('rec_group_shadow') == '0':
                        self.pending = self.complete = None
                        store.maintenance_diagnostics.resolve_wait(PHASE, 'disabled')
                        return None
                    if self.complete is not None and self.complete.current(now):
                        return self.complete.result
                    if self.pending is not None and not self.pending.current(now):
                        self.pending = None
                    if self.pending is None and self.complete is not None and self.complete.candidates_current(now):
                        self.pending = PreviewWork(self.complete.guard, self.complete.at, self.complete.head,
                                                   {'expires_at': self.complete.progress.get('expires_at')},
                                                   candidates=self.complete.candidates)
                    # Catalog views also expose enrichment/preparation timestamps;
                    # hydration consumes their metadata and profile definitions.
                    with (store.read_snapshot() as snapshot,
                          column_group_reads(snapshot.store, 'recordings', 'catalog')):
                        reader = snapshot.store
                        try:
                            if self.pending is None:
                                head = reader.taste_groups.preview_head()
                                if head is None:
                                    store.maintenance_diagnostics.record('deferred', phase=PHASE, reasons=('group-head',))
                                    return None
                                self.pending = PreviewWork(snapshot, now, head, {
                                    'expires_at': reader.rec_model.next_suppression_change(now)})
                            work = self.pending
                            if work.candidates is None:
                                work.candidates = prepare_candidates(reader, work.head, work.at,
                                    progress=work.progress)
                                work.progress.pop('frames', None)
                        finally:
                            # An interrupted slice can leave completed frames in memory.
                            if self.pending is not None:
                                self.pending.guard.store._snapshot_reads.include(reader._snapshot_reads.dependencies)
                            snapshot.release_reader()
                    # Allocation/report arrivals never own candidate progress.
                    # Read them in a fresh view, then validate both cohorts under
                    # the publication's writer reservation.
                    with store.read_snapshot() as publication:
                        publication.store._snapshot_reads.include(work.guard.store._snapshot_reads.dependencies)
                        result = compose(publication.store, work.head, work.at, work.candidates,
                                         progress=work.progress, allocation_at=now)
                        encoded = json.dumps(result)

                        with store.rec_proposals.prepare_home(encoded, work.at, surface=SURFACE) as prepared:
                            def publish():
                                if not work.current(now):
                                    raise WorkBudgetExceeded('Adaptive preview candidate inputs changed before publication')
                                store.rec_proposals.publish_home(prepared, input_revision=publication._changes)

                            check_stopping()
                            if publication.publish(publish):
                                work.publication, work.result = publication, result
                                self.complete, self.pending = work, None
                                store.maintenance_diagnostics.record('completed', phase=PHASE)
                                return result
            except WorkBudgetExceeded as error:
                logging.getLogger(__name__).debug('Adaptive preview deferred: %s', error)
                store.maintenance_diagnostics.record('deferred', phase=PHASE, reasons=('group-preview-budget',))
            except WorkClosing:
                self.pending = None
                raise
            return None


def _preparation(store):
    with store._lock:
        if not hasattr(store, '_group_preview_preparation'):
            store._group_preview_preparation = Preparation(store)
        return store._group_preview_preparation


def due(store, now):
    return _preparation(store).due(now)


def prepare(store, now, *, seconds=10):
    return _preparation(store).step(now, seconds)


def view(store):
    """Retain a complete preview until its replacement is ready, without mixing generations."""
    preview = store.rec_proposals.get_proposals(SURFACE)
    generation = store.taste_groups.head_generation()
    if not preview or not generation:
        return None
    visible = []
    by_version = {group['version']: group for group in preview['groups']}
    for group in preview['groups']:
        if not group.get('display'):
            continue
        parent = by_version.get(group['parent_version'])
        seen = {group['version']}
        while parent and parent['version'] not in seen and not parent.get('display'):
            seen.add(parent['version'])
            parent = by_version.get(parent['parent_version'])
        if parent and parent['version'] in seen:
            parent = None
        visible.append({**group, 'parent_name': parent['name'] if parent else None,
                        'mix_ready': any(frame['full_mix'] for frame in group['frames'].values())})
    return {**preview, 'groups': visible, 'updating': preview['generation'] != generation}


def progress(store):
    return {**group_preparation.status(store), **store.taste_groups.preparation_progress(),
            'enabled': store.settings.get_setting('rec_group_shadow') != '0'}
