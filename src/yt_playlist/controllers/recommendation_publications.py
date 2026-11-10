"""Recommendation publication workflows and current-catalog projections."""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib

from yt_playlist.core import bundle_plan, clicked_pool_policy, recommendation_plan
from yt_playlist.core.activity import check_cancellable_work


def prepare_bundle(repository, bundle, fresh, *, previous=None, reuse_existing=False, **options):
    """Resolve SQL identities, build the plan, then compare bounded reuse inputs."""
    keys, versions = bundle_plan.requirements(bundle, fresh, options['policy_version'])
    reuse_existing = bool(reuse_existing and repository.supports_pool_revalidation())
    plan = bundle_plan.prepare(bundle, fresh, recording_ids=repository.recording_ids(keys),
        policy_families=repository.policy_families(versions), reuse_existing=reuse_existing, **options)
    if not reuse_existing:
        return plan
    previous = repository if previous is None else previous
    recent = previous.recent_pools(generations=bundle_plan.REUSE_GENERATIONS,
                                  limit=bundle_plan.REUSE_GENERATIONS * recommendation_plan.MAX_SLOTS)
    reused = {}
    for digest, candidates in bundle_plan.reuse_options(plan, recent):
        for identity, candidate in candidates:
            check_cancellable_work()
            if previous.pool_matches(identity, candidate):
                reused[digest] = candidate
                break
    return bundle_plan.revalidate(plan, reused, allow_unobserved=options.get('allow_unobserved', False))


class RecommendationPublications:
    def __init__(self, repository, query, discovery, modes, files):
        self.repository = repository
        self.query = query
        self.discovery = discovery
        self.modes = modes
        self.files = files
        self._facts_cache = None

    @contextmanager
    def planning_read(self):
        """Share current catalog facts only for this private planning reader."""
        previous = self._facts_cache
        self._facts_cache = OrderedDict()
        try:
            yield
        finally:
            self._facts_cache = previous

    def prepare_clicked_pool(self, surface, value, at, **options):
        inputs = clicked_pool_policy.publication_inputs(surface, value, at, **options)
        if inputs is None:
            return None
        bundle, options = inputs
        return prepare_bundle(self.repository, bundle, None, **options)

    def prepare_bundle(self, bundle, fresh, **options):
        return prepare_bundle(self.repository, bundle, fresh, **options)

    def native_clicked_pools(self):
        return self.repository.native_clicked_pools()

    def put_clicked_pool(self, surface, raw, at, *, input_revision=None):
        value = clicked_pool_policy.decode(surface, raw)
        plan = self.prepare_clicked_pool(surface, value, at, input_revision=input_revision)
        if plan is None:
            return self.repository.clear_surface(surface)
        build = self.claim(plan)
        self.remember_clicked_pool_parameters(build.generation_id, value['parameters'], at)
        self.stage(plan, build)
        return self.publish(build, plan.created_at)

    def clicked_pool_present(self, surface):
        return self.repository.clicked_pool_header(surface) is not None

    def clicked_pool(self, surface):
        header = self.repository.clicked_pool_header(surface)
        if header is None:
            return None
        parameters = header['parameters']
        tracks, cursor = [], -1
        while True:
            page = self.candidate_page(surface, 'all', parameters['lane'],
                                       generation=header['generation'], after=cursor, limit=512)
            tracks.extend(page['tracks'])
            if page['exhausted']:
                return {'parameters': parameters, 'tracks': tracks}
            cursor = page['after']

    def remember_clicked_pool_parameters(self, generation, parameters, at):
        return self.repository.remember_clicked_pool_parameters(generation, parameters, at)

    def prepare_bundles(self, bundle, fresh, *, previous, **options):
        if not self.repository.is_private_reader() or not previous.repository.is_private_reader():
            raise ValueError('Bundle preparation requires private readers')
        return prepare_bundle(self.repository, bundle, fresh, previous=previous.repository,
                              reuse_existing=True, **options)

    def claim(self, plan, *, owner=None):
        return self.repository.claim(plan, recommendation_plan.validate(plan), owner=owner)

    def stage(self, plan, build, *, still_current=None):
        if recommendation_plan.validate(plan) != build.plan_digest:
            raise ValueError('Recommendation staged plan changed')
        staged_pools = {}
        for start in range(0, len(plan.slots), recommendation_plan.SLOT_BATCH_ROWS):
            if still_current is not None and not still_current():
                return False
            slots = plan.slots[start:start + recommendation_plan.SLOT_BATCH_ROWS]
            existing = self.repository.staged_slots(build, slots)
            for slot in slots:
                check_cancellable_work()
                digest = existing.get((slot.surface, slot.mode_key, slot.purpose))
                if digest is not None:
                    if digest != slot.pool.content_digest:
                        raise ValueError('Recommendation staged pool selection changed')
                    continue
                identity = staged_pools.get(slot.pool.content_digest)
                if identity is None:
                    identity = self._stage_pool(build, slot.pool, still_current=still_current)
                if identity is None:
                    return False
                self.repository.attach_pool(build, slot, identity)
                # Attaching seals the immutable pool. Other slots in this call
                # can reuse its verification; retries verify their inputs anew.
                staged_pools[slot.pool.content_digest] = identity
        for start in range(0, len(plan.outputs), recommendation_plan.OUTPUT_BATCH_ROWS):
            if still_current is not None and not still_current():
                return False
            outputs = plan.outputs[start:start + recommendation_plan.OUTPUT_BATCH_ROWS]
            existing = self.repository.staged_outputs(build, outputs)
            pending = []
            for output in outputs:
                check_cancellable_work()
                digest = existing.get((output.surface, output.chunk_key))
                if digest is not None:
                    if digest != hashlib.sha256(output.encoded).hexdigest():
                        raise ValueError('Recommendation staged output changed')
                else:
                    pending.append(output)
            if not pending:
                continue
            # Files become durable before their bounded SQL manifest. The file
            # lifetime spans that gap, without holding a database transaction.
            with self.files.retained():
                files = []
                for output in pending:
                    check_cancellable_work()
                    files.append((output, self.files.publish(output.encoded)))
                self.repository.record_outputs(plan, build, files)
        if still_current is not None and not still_current():
            return False
        self.repository.ready(plan, build)
        return True

    def _stage_pool(self, build, prepared, *, still_current=None):
        identity, sealed, count = self.repository.begin_pool(build, prepared)
        if not sealed:
            if count is None:
                count = self.repository.pool_size(identity)
            if count > len(prepared.candidates):
                raise ValueError('Recommendation candidate prefix exceeds its plan')
            for start in range(count, len(prepared.candidates), recommendation_plan.BATCH_ROWS):
                if still_current is not None and not still_current():
                    return None
                self.repository.append_pool(build, identity,
                    prepared.candidates[start:start + recommendation_plan.BATCH_ROWS], start)
        self.repository.verify_pool(identity, prepared)
        return identity

    def publish(self, build, at, *, retire_surfaces=()):
        return self.repository.publish(build, at, retire_surfaces=retire_surfaces)

    def cleanup(self):
        return self.repository.cleanup()

    def cleanup_generations(self, **options):
        return self.repository.cleanup_generations(**options)

    def cleanup_pools(self, **options):
        return self.repository.cleanup_pools(**options)

    def retirement_pending(self):
        return self.repository.retirement_pending()

    def checkpoint_retirement(self):
        return self.repository.checkpoint_retirement()

    def head(self, surface):
        return self.repository.head(surface)

    def heads(self):
        return self.repository.heads()

    def pool(self, surface, mode_key, purpose, **options):
        return self.repository.pool(surface, mode_key, purpose, **options)

    def page(self, surface, mode_key, purpose, **options):
        return self.repository.page(surface, mode_key, purpose, **options)

    def output(self, surface, chunk_key, *, generation=None):
        reference = self.repository.output_reference(surface, chunk_key, generation=generation)
        return self.files.read(reference, surface=surface, chunk_key=chunk_key) if reference is not None else None

    def display(self, surface, mode_key, purpose, ranks, *, generation=None):
        references = self.display_references(surface, mode_key, purpose, ranks, generation=generation)
        return self.files.display(references, ranks, surface)

    def display_references(self, surface, mode_key, purpose, ranks, **options):
        return self.repository.display_references(surface, mode_key, purpose, ranks, **options)

    def framing(self, surface, **options):
        return self.repository.framing(surface, **options)

    def selections(self, surface, purpose, **options):
        return self.repository.selections(surface, purpose, **options)

    def ranks(self, surface, mode_key, purpose, keys, **options):
        return self.repository.ranks(surface, mode_key, purpose, keys, **options)

    def band_page(self, surface, mode_key, purpose, **options):
        return self.repository.band_page(surface, mode_key, purpose, **options)

    def intensity_bands(self, surface, mode_key, purpose, **options):
        return self.repository.intensity_bands(surface, mode_key, purpose, **options)

    def recording_metadata_page(self, surface, *, generation=None, after=0, limit=128, mode_key=None):
        generation, rows = self.repository.recording_metadata_rows(surface, generation=generation,
            after=after, limit=limit, mode_key=mode_key)
        keys = [row['identity_key'] for row in rows]
        owned = self.query.group_candidates(keys) if keys else {}
        discovered = self.discovery.discovered_tracks_by_keys(keys, available_only=True) if keys else {}
        tracks = []
        for row in rows:
            key = row['identity_key']
            current = owned.get(key) or discovered.get(key)
            if current and current.get('video_id'):
                tracks.append({**current, 'key': key, 'recording_id': row['id'],
                    'serving_context': {'recommendation_generation_id': generation}})
        return {'generation': generation, 'after': rows[-1]['id'] if rows else after,
                'exhausted': generation is None or len(rows) < limit, 'tracks': tracks}

    def candidate_page(self, surface, mode_key, purpose, *, generation=None, after=-1, limit=128,
                       band=None, genres=None, group_version=None, include_display=True):
        return self.candidate_pages(surface, [(mode_key, purpose, after)], generation=generation,
            limit=limit, band=band, genres=genres, group_version=group_version,
            include_display=include_display)[0]

    def candidate_pages(self, surface, selections, *, generation=None, limit=128,
                        band=None, genres=None, group_version=None, include_display=True):
        if len(selections) > 16:
            raise ValueError('Candidate batch exceeds 16 pages')
        generation, captured = self.repository.candidate_rows(surface, selections, generation=generation,
            limit=limit, band=band, genres=genres, group_version=group_version)
        pages = []
        for (mode, purpose, pool, rows), (_, _, after) in zip(captured, selections):
            cursor = rows[-1]['rank'] if rows else after
            result = {'generation': generation, 'after': cursor,
                'exhausted': not pool or len(rows) < limit or cursor >= pool['eligible_count'] - 1, 'tracks': []}
            pages.append((mode, purpose, pool, rows, result))
        self._hydrate_pages(surface, generation, pages, include_display)
        return [page[-1] for page in pages]

    def selected_candidates(self, surface, selections, *, generation=None):
        if len(selections) > 16 or any(len(keys) > 512 for _, _, keys in selections):
            raise ValueError('Candidate selection exceeds its read budget')
        generation, captured = self.repository.selected_rows(surface, selections, generation=generation)
        pages = [(*page, {'tracks': []}) for page in captured]
        self._hydrate_pages(surface, generation, pages, True)
        return [page[-1]['tracks'] for page in pages]

    def _candidate_facts(self, keys):
        cache = self._facts_cache
        missing = [key for key in keys if cache is None or key not in cache]
        owned = self.query.group_candidates(missing) if missing else {}
        discovered = self.discovery.candidate_tracks_by_keys(missing) if missing else {}
        videos = list(dict.fromkeys(current['video_id'] for key in missing
            if (current := owned.get(key) or discovered.get(key)) and current.get('video_id')))
        details = self.repository.candidate_details(videos) if videos else {}
        years = self.modes.years_for(missing) if missing else {}
        plays = self.repository.candidate_play_counts(missing) if missing else {}
        facts = {key: cache[key] for key in keys if cache is not None and key in cache}
        for key in missing:
            current = owned.get(key) or discovered.get(key)
            track = None
            if current and current.get('video_id'):
                year = years.get(key) if key in owned else current.get('year')
                if isinstance(year, str):
                    year = int(year[:4]) if len(year) >= 4 and year[:4].isascii() and year[:4].isdigit() else None
                track = {name: current.get(name) for name in ('title', 'artist', 'album', 'video_id', 'thumbnail', 'genre')}
                track.update(details.get(current['video_id'], {}))
                track.update(key=key, year=year, plays=plays.get(key, 0), _radio_fresh=key not in owned)
            facts[key] = track
            if cache is not None:
                cache[key] = track
                if len(cache) > 8192:
                    cache.popitem(last=False)
        return facts

    def _hydrate_pages(self, surface, generation, pages, include_display):
        keys = list(dict.fromkeys(row['key'] for _, _, _, rows, _ in pages for row in rows))
        if not keys:
            return
        facts = self._candidate_facts(keys)
        for mode, purpose, pool, rows, result in pages:
            display = (self.display(surface, mode, purpose, [row['rank'] for row in rows], generation=generation)
                       if include_display else {})
            for row in rows:
                current = facts.get(row['key'])
                if current is None:
                    continue
                explanation = display.get(row['rank'], {})
                result['tracks'].append({**current, 'key': row['key'], 'recording_id': row['recording_id'],
                    'pool_rank': row['rank'], 'score': row['score'], 'pool_policy': row['pool_policy'],
                    'reason': explanation.get('reason', ''), 'lane': explanation.get('lane', purpose),
                    'serving_context': {'recommendation_generation_id': generation,
                        'recommendation_pool_id': pool['id'], 'recommendation_pool_rank': row['rank']}})

    def retain(self, kind, external_key, generation_id):
        return self.repository.retain(kind, external_key, generation_id)

    def release(self, kind, external_key):
        return self.repository.release(kind, external_key)
