"""Transport-neutral enrichment queries and commands shared by resources."""
from contextlib import nullcontext

from yt_playlist.providers import waterfall
from yt_playlist.core import enrichment_conflict_policy as policy
from yt_playlist.core import enrichment_selection_policy as selections
from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import clock, ALTERNATIVE_SECONDS
from yt_playlist.core.metadata_values import scope
from yt_playlist.core.metadata_file_contract import describe
from yt_playlist.core.metadata_observations import SubjectChanged


def worker_state(*, enabled, activity, busy, retries, remaining):
    if not enabled:
        return 'paused'
    if activity.get('state') in ('blocked', 'error'):
        return activity['state']
    if busy:
        return 'waiting' if activity.get('state') == 'waiting' else 'running'
    return 'retry' if retries else 'queued' if remaining else 'idle'


class EnrichmentCommands:
    def __init__(self, reports, queue, state, metadata, tracks, settings, *, now,
                 worker=None, refresh=lambda **kwargs: None, jobs=None, logger=None, start_thread=None,
                 start_enrichment=None, playlists=None):
        self.reports, self.queue, self.state = reports, queue, state
        self.metadata, self.tracks, self.settings = metadata, tracks, settings
        self.now, self.worker, self.refresh = now, worker, refresh
        self.jobs, self.logger, self.start_thread = jobs, logger, start_thread
        self.start_enrichment = start_enrichment
        self.playlists = playlists

    def start(self, *, playlist_id=None, album_browse=None):
        if playlist_id is not None and self.playlists is not None and self.playlists.get_playlist(playlist_id) is None:
            raise LookupError('playlist not found')
        return self.start_enrichment(playlist_id=playlist_id, album_browse=album_browse)

    def conflicts(self, *, playlist_id=None, album_browse=None):
        if album_browse is not None:
            return dict(conflicts=self.reports.unresolved_conflicts_for_album(album_browse),
                        count_left=self.reports.conflict_count_for_album(album_browse))
        return dict(conflicts=self.reports.unresolved_conflicts_for_playlist(playlist_id),
                    count_left=self.reports.conflict_count_for_playlist(playlist_id))

    def resolve_many(self, selections):
        for key, value in selections:
            tid, separator, field = key.partition(':')
            if separator and tid.isdigit() and value:
                try:
                    self.metadata.resolve_conflict(int(tid), field, value, now=self.now())
                except ValueError:
                    pass

    def query(self):
        now = self.now()
        coverage = self.reports.coverage_stats(include_discovery=True)
        queue = self.reports.queue_stats()
        activity = self.worker.activity if self.worker else {}
        retry = self.state.enrichment_retry_status()
        enabled = self.settings.get_setting('enrich_worker_enabled', '1') == '1'
        return dict(cov=coverage, queue=queue, remaining=queue['total'], activity=activity,
                    retry=retry, art_retry=self.state.artwork_retry_status(), enabled=enabled,
                    state=worker_state(enabled=enabled, activity=activity,
                        busy=bool(self.worker and self.worker.busy), retries=retry['count'], remaining=queue['total']),
                    timeline=self.reports.processed_timeline(),
                    progress=self.reports.enrichment_activity_stats(now),
                    conflicts=self.reports.outstanding_conflicts())

    def detail(self, view, page):
        total = (self.reports.queue_remaining() if view == 'queue' else
                 self.reports.coverage_stats(include_discovery=True)['processed'] if view == 'processed' else
                 self.reports.outstanding_conflicts())
        size = 20 if view == 'conflicts' else 40
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        offset = (page - 1) * size
        fetch = {'queue': self.queue.next_enrich_batch, 'processed': self.reports.enriched_tracks,
                 'conflicts': self.reports.unresolved_enrichment_conflicts}[view]
        return dict(view=view, rows=fetch(size, offset), total=total, page=page, pages=pages,
                    start=offset + 1 if total else 0, end=min(offset + size, total))

    def resolve_conflict(self, track_id, field, value):
        if not isinstance(value, str) or not self.metadata.resolve_enrichment_candidate(
                track_id, field, value, now=self.now()):
            raise ValueError('Choose a current provider candidate.')
        return self.reports.enrichment_conflict_detail(track_id, field)

    def toggle(self):
        enabled = self.settings.get_setting('enrich_worker_enabled', '1') != '1'
        self.settings.set_setting('enrich_worker_enabled', '1' if enabled else '0')
        if enabled and self.worker:
            self.worker.trigger()

    def lookup_genre(self, store, track_id):
        track = self.tracks.track_for_waterfall(track_id)
        if track is None:
            return None
        job = self.jobs.create()
        job.source = 'genre-lookup'
        return waterfall.start_enrichment_job(store, self.logger, self.start_thread,
                                              job, True, lambda: [track])


class Enrichment:
    def __init__(self, repository, choices, files, artists=None):
        self.repository, self.choices, self.files = repository, choices, files
        self.artists = artists

    def refresh_genre(self, recording_id, answers, providers, *, now=None):
        inputs = self.repository.genre_refresh_input(recording_id)
        if inputs is None:
            return
        replacement = selections.refreshed_genre(inputs['source'], answers, providers)
        if replacement is None:
            return
        genre, source = replacement
        observations = self.reuse_provider_fields(inputs['entry_id'], source, {'genre': genre}, now=now, select=False) if inputs['attempts'] else {}
        if inputs['attempts'] and 'genre' not in observations:
            return
        self.choices.select(inputs['entry_id'], 'genre', genre, source, now=now, observation_id=observations.get('genre'))

    def apply_provider_fields(self, track_id, provider, fields, *, providers=(), delivery_id=None, now=None):
        at = clock(now)
        facts = self.repository.provider_inputs(track_id, provider)
        observations = {}
        if facts['layout'] == 'runtime' and delivery_id is not None:
            selections.validate_capture(facts, provider, fields, delivery_id)
            if facts['provider_at'] is not None and facts['provider_at'] >= delivery_id.at:
                return {}
        captured = selections.canonical_fields(fields)
        if facts['layout'] == 'evidence':
            if delivery_id is not None:
                observations = self.choices.result_evidence(facts['entry_id'], provider, delivery_id, fields)
            elif captured:
                _, observations = self.choices.capture_fields(facts['entry_id'], provider, captured, purpose='provider-fields',
                    body={'format': 'catalog-provider-fields-1', 'fields': captured}, now=at)
        self._select_provider_fields(facts, provider, captured, providers, observations, at)
        if self.artists is not None and fields.get('artist_credits'):
            import hashlib
            from yt_playlist.core.metadata_contract import Capture
            self.artists.enrich_credit(facts['entry_id'], fields['artist_credits'], provider, now=at,
                delivery_key=('enrichment:' + str(delivery_id.at) + ':' + hashlib.sha256(delivery_id.encoded).hexdigest()
                              if isinstance(delivery_id, Capture) else
                              None),
                metadata_delivery_id=delivery_id if type(delivery_id) is int else None)
        return observations

    def _select_provider_fields(self, facts, provider, fields, providers, observations, at):
        for field, value in fields.items():
            if value in (None, ''):
                continue
            if facts['layout'] == 'legacy':
                self.repository.store_finding(facts['entry_id'], facts['recording_id'], field, provider, value, at)
            if not selections.provider_wins(field, value, provider, facts['sources'].get(field), facts['genre'], providers):
                continue
            self.choices.select(facts['entry_id'], field, value, provider, now=at, observation_id=observations.get(field))
            if facts['layout'] != 'runtime':
                self.repository.retire_entry_hints(facts['layout'], facts['entry_id'], field)

    def reuse_provider_fields(self, entry_id, provider, fields, *, providers=(), now=None, select=True):
        # Credits are selected with their original delivery. Reusing completed
        # scalar coverage must not replay an older artist choice over a newer one.
        fields = {key: value for key, value in fields.items() if key != 'artist_credits'}
        at = clock(now)
        facts = self.repository.provider_inputs(entry_id, provider)
        if facts['layout'] == 'runtime':
            data = {}
            reference = self.repository.alternative_references(facts['recording_id'], [provider]).get(provider)
            if reference and reference['at'] > at - ALTERNATIVE_SECONDS:
                data = self.files.read('alternatives', (facts['recording_id'], reference['provider_id']),
                    float(reference['at']), missing=True, **({'reference': reference['file']} if reference.get('file') else {})) or {}
            scoped = selections.scoped_fields(facts, data)
            fields = {field: value for field, value in fields.items() if field in scoped and scoped[field] == value}
        elif facts['attempts']:
            observations = {}
            rows = self.repository.reuse_inputs(facts['recording_id'], provider, fields)
            for concept, value in fields.items():
                field = 'mb_year' if concept == 'year' else concept
                if not selections.canonical_fields({concept: value}):
                    continue
                row = rows.get(concept)
                reuse = selections.reusable_observation(facts, field, value, row)
                if reuse:
                    observations[field] = (self.choices.reuse_recording_observation(facts['entry_id'], row['id'], now=at)
                                           if reuse == 'transfer' else row['id'])
            if select:
                eligible = {field: value for field, value in selections.canonical_fields(fields).items() if field in observations}
                self._select_provider_fields(facts, provider, eligible, providers, observations, at)
            return observations
        if select:
            self.apply_provider_fields(facts['entry_id'], provider, fields, providers=providers, now=at)
        return None

    def replace_catalog_hints(self, track_id, fields):
        facts = self.repository.hint_inputs(track_id, fields)
        if facts is None:
            return
        for field, value in fields.items():
            if field not in facts['fields'] or value is None:
                continue
            previous = facts['fields'][field]
            if selections.replaces_hint(facts['layout'], previous['source'], previous['current'], previous['hints']):
                self.choices.select(facts['entry_id'], field, value, 'catalog_hint_replacement')
                self.repository.retire_hints(facts['layout'], field,
                    [(hint['entry_id'], hint['source']) for hint in previous['hints']])

    def upsert_conflict(self, track_id, field, candidates, now=None, *, refreshed_providers=None):
        field, prepared = policy.prepare_candidates(field, candidates)
        at = clock(now)
        facts = self.repository.conflict_inputs(track_id, field)
        if facts is None:
            raise ValueError('Unknown metadata conflict input')
        if facts['layout'] == 'evidence':
            for candidate in prepared:
                if candidate.get('observation_id') is None:
                    _, observations = self.choices.capture_fields(facts['entry_id'], candidate['provider'],
                        {field: candidate['value']}, purpose='conflict-offer:' + field,
                        body={'format': 'metadata-conflict-offer-1', 'field': field, 'value': candidate['value']}, now=at)
                    candidate['observation_id'] = observations[field]
        with self.files.retained():
            facts = self.repository.conflict_inputs(track_id, field)
            if facts is None:
                raise SubjectChanged('Metadata conflict input disappeared')
            previous = next((row for row in facts['conflicts'] if row['entry_id'] == facts['entry_id']),
                            facts['conflicts'][0] if facts['conflicts'] else None)
            old = self._candidates(previous, at) if previous else []
            current = prepared
            references = None
            providers = sorted({candidate['provider'] for candidate in prepared})
            if facts['layout'] == 'runtime':
                references = self.repository.alternative_references(facts['recording_id'], providers)
                current = []
                for candidate in prepared:
                    reference = references.get(candidate['provider'])
                    supplied = {}
                    if reference and reference['at'] > at - ALTERNATIVE_SECONDS:
                        supplied = self.files.read('alternatives', (facts['recording_id'], reference['provider_id']),
                            float(reference['at']), missing=True,
                            **({'reference': reference['file']} if reference.get('file') else {})) or {}
                    captured_at = candidate.get('at', supplied.get('at', at))
                    if captured_at > at - ALTERNATIVE_SECONDS:
                        current.append({'provider': candidate['provider'], 'value': candidate['value'], 'at': captured_at})
            current = [*current, *policy.retain_candidates(old, prepared, refreshed_providers)]
            status = policy.refreshed_status(previous['status'] if previous else 0, old, current)
            identity = previous['id'] if previous else None
            revision = None
            record = None
            if facts['layout'] == 'runtime':
                with self.repository.file_reservation():
                    identity = identity if identity is not None else self.repository.reserve_conflict_identity()
                    revision = previous['revision'] + 1 if previous else 1
                    record = describe('conflicts', (identity,), revision, {'candidates': current})
                    record['file_id'] = self.repository.reserve_file(record)
            with self.files.write_reserved([record]) if record else nullcontext():
                with self.repository.conflict_transaction():
                    if self.repository.conflict_inputs(track_id, field) != facts or (
                            references is not None and self.repository.alternative_references(
                                facts['recording_id'], providers) != references):
                        raise SubjectChanged('Metadata conflict changed during file publication')
                    self.repository.store_conflict(facts, field, current, status, at, scope=scope(field),
                        identity=identity, revision=revision, **({'file_record': record} if record else {}))

    def retire_agreed_conflicts(self, track_id, evidence, now=None):
        at = clock(now)
        with self.files.retained():
            facts = self.repository.conflict_inputs(track_id)
            if facts is None:
                return
            identities = []
            for row in facts['conflicts']:
                concept = 'year' if row['field'] == 'mb_year' else row['field']
                if row['status'] == 0 and policy.agreed(self._candidates(row, at), evidence.get(concept, {})):
                    identities.append(row['id'])
            with self.repository.conflict_transaction():
                if self.repository.conflict_inputs(track_id) != facts:
                    raise SubjectChanged('Metadata conflict changed while checking convergence')
                self.repository.mark_converged(facts['layout'], identities, at)

    def _candidates(self, row, now):
        evidence = row['evidence']
        if 'candidates' in evidence:
            return evidence['candidates']
        identity, revision = evidence['reference']
        document = self.files.read('conflicts', (identity,), revision,
            **({'reference': evidence['file']} if evidence.get('file') else {}))
        return policy.current_candidates(document, evidence['updated_at'], now)

    def resolve_conflict(self, track_id, field, value, now=None):
        column, prepared = policy.selected_value(field, value)
        at = clock(now)
        observation = self.choices.choose(track_id, column, prepared, source='conflict_resolution', now=at)
        self.repository.mark_resolved(track_id, field, value, observation, at)

    def resolve_enrichment_candidate(self, track_id, field, value, now=None):
        spec = FIELD_COLUMN.get(field)
        if spec is None:
            return False
        at = clock(now)
        facts = self.repository.resolution_inputs(track_id, field, column=spec[0])
        if facts is None:
            return False
        candidates = self._candidates(facts, at)
        if not policy.offered_value(facts['current'], candidates, value):
            return False
        self.resolve_conflict(facts['entry_id'], field, value, now=at)
        return True
