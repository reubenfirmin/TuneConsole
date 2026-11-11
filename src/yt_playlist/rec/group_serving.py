"""Prepare versioned musical interests and mix candidates from production pools."""
import json
from dataclasses import dataclass, field
import threading
import uuid

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_stopping
from yt_playlist.core import group_preview_work
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.work_diagnostics import maintenance_job
from yt_playlist.rec import group_learning, group_preparation, mode_surfaces, rec_params
from yt_playlist.rec.eligibility import home_exclusions

SURFACE = 'adaptive_group_preview'
SCHEMA = 2
PHASE = 'rec: adaptive mix shadow'


def enabled(store):
    return store.settings.get_setting('rec_group_serving') != 'legacy'


def checkpoint(store):
    prepared = store.rec_proposals.group_checkpoint() if enabled(store) else None
    return prepared if prepared and prepared['schema'] == SCHEMA and prepared['mix_regions'] else None


def policy(store):
    return 'adaptive' if checkpoint(store) else 'legacy'


def calculate(store, head, now, *, progress=None, checkpoint=None):
    progress = progress if progress is not None else {}
    candidates = prepare_candidates(store, head, now, progress=progress, checkpoint=checkpoint)
    return compose(store, head, now, candidates, progress=progress)


def prepare_candidates(store, head, now, *, progress=None, checkpoint=None):
    """Candidate eligibility and diversity do not consume allocation reports."""
    progress = progress if progress is not None else {}
    completed = progress.setdefault('frames', {})
    groups = [group for group in head['groups'] if not group.get('legacy')]
    typed = getattr(store, '_recommendation_files', False)
    generation = None
    if typed:
        from yt_playlist.rec import stored_groups
        header = store.recommendations.head('home')
        generation = header['id'] if header else None
        pools = {lane: None for lane in mode_surfaces.CARD_SURFACES + ('temporal',)
                 if generation is not None and store.recommendations.pool('home', 'all', lane,
                                                                          generation=generation) is not None}
        blocked = set()
    else:
        bundles = store.rec_proposals.get_proposals('mode_bundles') or {}
        pools = bundles.get('all', {})
        keys = {item['key'] for pool in pools.values() for item in pool if item.get('key')}
        blocked = home_exclusions(store, keys, now)
    muted = store.rec_model.muted_artists()
    cap_artist = int(rec_params.get_param(store, 'modes_artist_cap'))
    cap_album = int(rec_params.get_param(store, 'modes_album_cap'))
    prepared = {}
    for group in groups:
        check_stopping()
        members = set(group.get('members', {}))
        frames = {}
        for frame, pool in pools.items():
            frame_key = (group['version'], frame)
            if typed:
                state = completed.setdefault(frame_key, stored_groups.PreviewFrame())
                frames[frame] = stored_groups.preview_frame(store, generation, group['version'], frame, now,
                    cap_artist, cap_album, muted, progress=state, checkpoint=checkpoint)
                continue
            if frame_key in completed:
                frames[frame] = completed[frame_key]
                continue
            eligible, seen = [], set()
            for item in pool:
                key = item.get('key')
                if key in members and key not in blocked and key not in seen and item.get('artist') not in muted:
                    eligible.append(item)
                    seen.add(key)
            diverse = mode_surfaces._diversify(eligible, cap_artist, cap_album)
            frames[frame] = {'eligible': len(diverse), 'full_mix': len(diverse) >= mode_surfaces.PROTO_SIZE,
                             # Keep inspectable identities, not repeated copies of
                             # every track's model/provenance payload at each parent.
                             'tracks': [{field: item.get(field) for field in ('key', 'title', 'artist')}
                                        for item in diverse[:mode_surfaces.PROTO_SIZE]],
                             'keys': [item['key'] for item in eligible]}
            completed[frame_key] = frames[frame]
            if checkpoint is not None:
                checkpoint(group['version'], frame, -1, {'legacy': frames[frame]})
        prepared[group['group_id']] = {key: group.get(key) for key in
            ('group_id', 'version', 'parent_version', 'name', 'subtitle', 'size', 'leaf', 'display')}
        prepared[group['group_id']]['frames'] = frames
    return {'groups': prepared, 'pools': tuple(pools), 'generation': generation, 'typed': typed}


def compose(store, head, now, prepared_candidates, *, progress, allocation_at=None):
    """Apply current learning after the expensive, independently guarded pages."""
    stats = group_learning.current_state(store, head, now if allocation_at is None else allocation_at)
    progress['allocation_expires_at'] = stats.get('expires_at')
    allocation = group_learning.allocation(head, stats)
    groups = [group for group in head['groups'] if not group.get('legacy')]
    by_version = {group['version']: group for group in groups}
    prepared = prepared_candidates['groups']
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
    result = {'schema': SCHEMA, 'at': now, 'generation': head['generation'], 'shadow': not enabled(store),
            'serving_policy': 'adaptive' if enabled(store) else 'legacy',
            'groups': list(prepared.values()), 'context': context,
            'counts': head['payload']['counts'], 'menus': menus}
    if prepared_candidates['typed']:
        generation = prepared_candidates['generation']
        result['recommendation_generations'] = [generation] if generation is not None else []
    return result


@dataclass
class PreviewWork:
    guard: object
    at: float
    progress: dict = field(default_factory=dict)
    result: dict | None = None
    candidates: dict | None = None
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    expected_token: str | None = None
    dependencies: set = field(default_factory=set)

    def current(self, now):
        expires = self.progress.get('expires_at')
        if self.result is not None:
            expires = min((at for at in (expires, self.progress.get('allocation_expires_at'))
                           if at is not None), default=None)
        return (now >= self.at and (expires is None or now < expires)
                and self.guard.is_current(allow_stale_tables=group_preview_work.TABLES))


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
                    # Catalog views also expose enrichment/preparation timestamps;
                    # hydration consumes their metadata and profile definitions.
                    with (store.read_snapshot() as snapshot,
                          column_group_reads(snapshot.store, 'recordings', 'catalog'),
                          column_group_reads(snapshot.store, 'rec_musical_profiles', 'definition')):
                        reader = snapshot.store
                        try:
                            head = (reader.taste_groups.preview_head() if getattr(reader, '_recommendation_files', False)
                                    else reader.taste_groups.head(members=True))
                            if head is None:
                                store.maintenance_diagnostics.record('deferred', phase=PHASE, reasons=('group-head',))
                                return None
                            if self.pending is None:
                                saved = reader.rec_proposals.preview_work()
                                if (saved is not None and not saved['invalid'] and saved['schema_version'] == snapshot._schema
                                        and saved['at'] <= now
                                        and (saved['expires_at'] is None or now < saved['expires_at'])):
                                    from yt_playlist.rec.stored_groups import restore_preview_pages
                                    self.pending = PreviewWork(snapshot, saved['at'], {
                                        'expires_at': saved['expires_at'], 'frames': restore_preview_pages(saved['pages'])},
                                        token=saved['token'], expected_token=saved['token'],
                                        dependencies=set(saved['dependencies']))
                                    reader._snapshot_reads.include(saved['dependencies'])
                                else:
                                    self.pending = PreviewWork(snapshot, now, {
                                        'expires_at': reader.rec_model.next_suppression_change(now)},
                                        expected_token=saved['token'] if saved else None)
                            work = self.pending

                            def checkpoint(version, lane, after, delta):
                                work.guard.store._snapshot_reads.include(reader._snapshot_reads.dependencies)
                                dependencies = {(table, key) for table, key in work.guard.store._snapshot_reads.dependencies
                                                if table in store._snapshot_revisions.tables - group_preview_work.TABLES}
                                unknown = {table for table, _ in dependencies} - group_preview_work.SOURCES
                                if unknown:
                                    raise RuntimeError('Adaptive preview consumed unguarded durable inputs: ' + ', '.join(sorted(unknown)))
                                header = {'token': work.token, 'schema_version': work.guard._schema, 'at': work.at,
                                          'expires_at': work.progress.get('expires_at')}
                                if not store.rec_proposals.save_preview_page(work.guard, header, work.expected_token,
                                        (version, lane, after, delta), dependencies - work.dependencies):
                                    raise WorkBudgetExceeded('Adaptive preview inputs changed before checkpoint')
                                work.expected_token = work.token
                                work.dependencies.update(dependencies)

                            if work.candidates is None:
                                work.candidates = prepare_candidates(reader, head, work.at,
                                    progress=work.progress, checkpoint=checkpoint)
                            work.guard.store._snapshot_reads.include(reader._snapshot_reads.dependencies)
                        finally:
                            snapshot.release_reader()
                    # Allocation/report arrivals never own candidate progress.
                    # Read them in a fresh view, then validate both cohorts under
                    # the publication's writer reservation.
                    with store.read_snapshot() as publication:
                        publication.store._snapshot_reads.include(work.guard.store._snapshot_reads.dependencies)
                        result = compose(publication.store, head, work.at, work.candidates,
                                         progress=work.progress, allocation_at=now)
                        encoded = json.dumps(result)

                        def publish():
                            if not work.current(now):
                                raise WorkBudgetExceeded('Adaptive preview candidate inputs changed before publication')
                            store.rec_proposals.publish_prepared_home(encoded, work.at, surface=SURFACE,
                                                                     input_revision=publication._changes)

                        check_stopping()
                        if publication.publish(publish, allow_stale_tables=group_preview_work.TABLES):
                            work.guard, work.result = publication, result
                            self.complete, self.pending = work, None
                            work.progress.pop('frames', None)
                            work.candidates = None
                            store.maintenance_diagnostics.record('completed', phase=PHASE)
                            return result
            except WorkBudgetExceeded:
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
