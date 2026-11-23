"""Recommendation publication workflows and current-catalog projections."""
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
import hashlib

from yt_playlist.core import artist_policy, bundle_plan, clicked_pool_policy, recommendation_plan
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec.vibe_score_policy import score_patterns


def prepare_bundle(repository, bundle, fresh, *, previous=None, reuse_existing=False, **options):
    """Resolve SQL identities, build the plan, then compare bounded reuse inputs."""
    keys, versions = bundle_plan.requirements(bundle, fresh, options['policy_version'])
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
        self._complete_rank_reads = None

    @contextmanager
    def planning_read(self):
        """Share current catalog facts only for this private planning reader."""
        previous = self._facts_cache
        self._facts_cache = OrderedDict()
        try:
            yield
        finally:
            self._facts_cache = previous

    @contextmanager
    def reuse_complete_ranks(self):
        """Share immutable ranked inputs across rotations on one pinned reader."""
        previous = self._complete_rank_reads
        self._complete_rank_reads = {}
        try:
            yield
        finally:
            self._complete_rank_reads = previous

    def prepare_clicked_pool(self, surface, value, at, **options):
        inputs = clicked_pool_policy.publication_inputs(surface, value, at, **options)
        if inputs is None:
            return None
        bundle, options = inputs
        return prepare_bundle(self.repository, bundle, None, **options)

    def prepare_bundle(self, bundle, fresh, **options):
        return prepare_bundle(self.repository, bundle, fresh, **options)

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
                    integrity = self.files.describe(output.encoded)
                    identity = self.repository.reserve_file(build, output.surface, output.chunk_key, integrity)
                    reference = self.files.publish_reserved(identity, output.encoded, expected=integrity)
                    files.append((output, reference, identity))
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

    def mix_head_ids(self):
        return self.repository.mix_head_ids()

    def heads(self):
        return self.repository.heads()

    def radio_group_pools_ready(self, surfaces, home_lanes):
        return self.repository.radio_group_pools_ready(surfaces, home_lanes)

    def pool(self, surface, mode_key, purpose, **options):
        return self.repository.pool(surface, mode_key, purpose, **options)

    def page(self, surface, mode_key, purpose, **options):
        return self.repository.page(surface, mode_key, purpose, **options)

    def complete_ranks(self, slots, *, group_version=None, after=-1, include_metadata=False, fresh_start=None,
                       vibe=None, matching_vibe=False):
        cache = self._complete_rank_reads
        options = dict(group_version=group_version, after=after, include_metadata=include_metadata)
        if fresh_start is not None:
            options['fresh_start'] = fresh_start
        if vibe:
            options['vibe'] = (vibe, *score_patterns())
            options['matching_vibe'] = matching_vibe
        if cache is None or include_metadata:
            return self.repository.complete_rank_rows(slots, **options)
        slots = tuple(slots)
        key = slots, group_version, after, include_metadata, fresh_start
        if key not in cache:
            cache[key] = self.repository.complete_rank_rows(slots, **options)
        return cache[key]

    def complete_candidates(self, slots, *, group_version=None, after=-1):
        rows = self.complete_ranks(slots, group_version=group_version, after=after)
        sources = defaultdict(list)
        for row in rows:
            surface = slots[row['slot']][0]
            sources[surface, row['generation_id'], row['mode_key'], row['purpose']].append(row)
        facts = self._candidate_facts(list(dict.fromkeys(row['key'] for row in rows))) if rows else {}
        tracks = {}
        for source, ranked in sources.items():
            _, generation, _, purpose = source
            tracks[source] = [self._ranked_track(facts[row['key']], row, generation, row['pool_id'], purpose, {})
                              for row in ranked if facts[row['key']] is not None]
        return tracks, rows[-1]['rank'] if rows else after

    def mix_candidates(self, library_slots, fresh_slots, *, vibe=None, matching_vibe=False):
        """Capture the published ranks and current selection facts for one Mix Tape batch."""
        library_slots, fresh_slots = tuple(library_slots), tuple(fresh_slots)
        slots = library_slots + fresh_slots
        ranks, facts = self.complete_ranks(
            slots, include_metadata=True, fresh_start=len(library_slots) if fresh_slots else None, vibe=vibe,
            matching_vibe=matching_vibe)
        artist_tokens = {}

        def token(identity):
            value = artist_tokens.get(identity)
            if value is None:
                value = artist_tokens[identity] = artist_policy.token(identity)
            return value

        for track in facts.values():
            primary = track.pop('primary_credit')
            additional = track.pop('additional_credits', None)
            artists = [] if primary is None else [token(primary)]
            if additional:
                artists.extend(map(token, additional))
                artists.sort()
            track.update(_radio_fresh=bool(track['_radio_fresh']), year=self._candidate_year(track['year']),
                         artist_ids=artists)
        return MixCandidates(self.repository, dict(zip(slots, ranks, strict=True)), facts)

    def catalog_read_version(self):
        return self.repository.catalog_read_version()

    def mix_playback_details(self, recordings):
        return self.repository.mix_playback_details(recordings)

    def vibe_fresh_candidates(self, surface, generation, *, include, avoid, limit):
        rows = self.repository.vibe_fresh_candidates(surface, generation, include=include, avoid=avoid, limit=limit)
        identities = self.query.artist_memberships([row['key'] for row in rows]) if rows else {}
        tracks = []
        for row in rows:
            pool_id, rank, score, policy = (row.pop(field) for field in ('pool_id', 'rank', 'score', 'pool_policy'))
            key = row['key']
            row.update(artist_ids=sorted(identities.get(key, ())), year=self._candidate_year(row['year']),
                       _radio_fresh=True, plays=0, _mix_selection=surface)
            tracks.append(_track_at_rank(row, key, row['recording_id'], rank, score, policy,
                                         generation, pool_id, 'fresh', {}))
        return tracks

    def explain_candidates(self, source, tracks):
        surface, generation, mode, purpose = source
        display = self.display(surface, mode, purpose, [track['pool_rank'] for track in tracks],
                               generation=generation) if tracks else {}
        for track in tracks:
            explanation = display.get(track['pool_rank'], {})
            track['reason'] = explanation.get('reason', '')
            track['lane'] = explanation.get('lane', purpose)

    def explain_mix(self, tracks):
        """Read explanations for the selected Mix Tape tracks together."""
        selected = defaultdict(list)
        for track in tracks:
            if isinstance(track, dict) and (surface := track.get('_mix_selection')):
                context = track['serving_context']
                selected[surface, context['recommendation_generation_id'],
                         context['recommendation_pool_id']].append(track)
        ranks = {source: {track['pool_rank'] for track in items}
                 for source, items in selected.items()}
        chunks = defaultdict(list)
        for row in self.repository.mix_display_references(ranks):
            chunks[row['surface'], row['generation_id'], row['pool_id']].append(row)
        for source, files in chunks.items():
            explanations = self.files.display(files, ranks[source], source[0])
            for track in selected[source]:
                if explanation := explanations.get(track['pool_rank']):
                    track['reason'] = explanation.get('reason', '')
                    track['lane'] = explanation.get('lane', track['lane'])

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

    def intensity_headings(self, surface, **options):
        return self.repository.intensity_headings(surface, **options)

    def home_genre_candidates(self):
        return self.repository.home_genre_candidates()

    def recording_metadata_page(self, surface, *, generation=None, after=0, limit=128, mode_key=None):
        generation, rows = self.repository.recording_metadata_rows(surface, generation=generation,
            after=after, limit=limit, mode_key=mode_key)
        keys = [row['identity_key'] for row in rows]
        owned = self.query.group_candidates(keys) if keys else {}
        discovered = self.discovery.discovered_tracks_by_keys(keys, available_only=True) if keys else {}
        identities = self.query.artist_memberships([key for key in discovered if key not in owned]) if discovered else {}
        tracks = []
        for row in rows:
            key = row['identity_key']
            current = owned.get(key) or discovered.get(key)
            if current and current.get('video_id'):
                tracks.append({**current, 'key': key, 'recording_id': row['id'],
                    'artist_ids': current.get('artist_ids') or sorted(identities.get(key, ())),
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

    def track_play_counts(self, keys):
        return self.repository.candidate_play_counts(keys)

    @staticmethod
    def _candidate_year(year):
        if isinstance(year, str):
            return int(year[:4]) if len(year) >= 4 and year[:4].isascii() and year[:4].isdigit() else None
        return year

    def _candidate_facts(self, keys):
        cache = self._facts_cache
        missing = [key for key in keys if cache is None or key not in cache]
        owned = self.query.group_candidates(missing) if missing else {}
        discovered = self.discovery.candidate_tracks_by_keys(missing) if missing else {}
        identities = self.query.artist_memberships(missing) if discovered else {}
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
                year = self._candidate_year(years.get(key) if key in owned else current.get('year'))
                track = {name: current.get(name) for name in ('title', 'artist', 'album', 'video_id', 'thumbnail', 'genre')}
                track['artist_ids'] = current.get('artist_ids') or sorted(identities.get(key, ()))
                track.update(details.get(current['video_id'], {}))
                track.update(key=key, year=year, plays=plays.get(key, 0), _radio_fresh=key not in owned)
            facts[key] = track
            if cache is not None:
                cache[key] = track
                if len(cache) > 8192:
                    cache.popitem(last=False)
        return facts

    def hydrate_radio_ranks(self, rows, generation):
        """Complete a shortlist whose current playable uploads are already known."""
        if not rows:
            return []
        keys = list(dict.fromkeys(row['key'] for row in rows))
        details = self.repository.candidate_details([row['video_id'] for row in rows])
        plays = self.repository.candidate_play_counts(keys)
        artists = self.query.artist_memberships(keys)
        result = []
        for row in rows:
            key, video = row['key'], row['video_id']
            current = {field: row[field] for field in ('title', 'artist', 'album', 'video_id',
                                                       'thumbnail', 'genre', '_radio_fresh')}
            current.update(details.get(video, {}), duration=row['duration'], year=self._candidate_year(row['year']),
                           plays=plays.get(key, 0), artist_ids=sorted(artists.get(key, ())))
            if row['purpose'] == 'catalog':
                result.append(dict(current, key=key, recording_id=row['recording_id']))
            else:
                result.append(self._ranked_track(current, row, generation, row['pool_id'], row['purpose'], {}))
        return result

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
                result['tracks'].append(self._ranked_track(current, row, generation, pool['id'], purpose,
                                                          display.get(row['rank'], {})))

    @staticmethod
    def _ranked_track(current, row, generation, pool_id, purpose, explanation):
        return _track_at_rank(current, row['key'], row['recording_id'], row['rank'], row['score'],
                              row['pool_policy'], generation, pool_id, purpose, explanation)

    def retain(self, kind, external_key, generation_id):
        return self.repository.retain(kind, external_key, generation_id)

    def release(self, kind, external_key):
        return self.repository.release(kind, external_key)


def _track_at_rank(current, key, recording_id, rank, score, pool_policy, generation, pool_id, purpose, explanation):
    return {**current, 'key': key, 'recording_id': recording_id,
            'pool_rank': rank, 'score': score, 'pool_policy': pool_policy,
            'reason': explanation.get('reason', ''), 'lane': explanation.get('lane', purpose),
            'serving_context': {'recommendation_generation_id': generation,
                                'recommendation_pool_id': pool_id, 'recommendation_pool_rank': rank}}


class MixCandidates:
    """Reuse catalog facts across rotations on the worker's private reader."""

    def __init__(self, repository, rows_by_slot, facts):
        self.repository = repository
        self.rows_by_slot = rows_by_slot
        self.facts = facts
        self.details = {}

    def candidates(self, slots):
        tracks = defaultdict(list)
        for slot in slots:
            surface, _, _, purpose = slot
            for generation, mode, pool_id, policy, rank, score, recording_id in self.rows_by_slot.get(slot, ()):
                facts = self.facts[recording_id]
                track = _track_at_rank(
                    facts, facts['key'], recording_id, rank, score, policy, generation, pool_id, purpose, {})
                track['_mix_selection'] = surface
                tracks[surface, generation, mode, purpose].append(track)
        return tracks

    def load_display(self, tracks):
        """Hydrate chosen uploads and report any removed since selection."""
        selected = [track for track in tracks if isinstance(track, dict) and track.pop('_mix_selection', False)]
        missing = [track['recording_id'] for track in selected if track['recording_id'] not in self.details]
        if missing:
            self.details.update(self.repository.mix_playback_details(missing))
        unavailable = set()
        for track in selected:
            recording = track['recording_id']
            if details := self.details.get(recording):
                track.update(details)
            else:
                unavailable.add(recording)
        return unavailable
