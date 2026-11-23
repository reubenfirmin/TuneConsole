"""SQL representation of explicit catalog metadata choices."""
from contextlib import contextmanager
from yt_playlist.core import content_work, genre_classifications, genre_identity, metadata_choice_selections
from yt_playlist.core import metadata_evidence, metadata_hints, metadata_runtime, recording_metadata, runtime_metadata_choices
from yt_playlist.core.metadata_values import ITEM_FIELDS, RECORDING_FIELDS
from yt_playlist.core.recommendation_invalidation import queue_refresh
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import metadata_file_writes


class MetadataChoiceRepo(Repo):
    @contextmanager
    def reservation(self):
        with self._lock:
            if self.conn.in_transaction:
                raise ValueError('Metadata file reservation requires its own transaction')
            with self.conn.atomic():
                yield

    @contextmanager
    def transaction(self):
        # Recheck the selected revision and install the completed file together.
        with self._lock:
            with self.conn.atomic(join=True):
                yield

    @synchronized
    def input(self, entry, field):
        if field not in (*ITEM_FIELDS, *RECORDING_FIELDS):
            raise ValueError('Unknown metadata field')
        row = self.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata catalog input')
        layout = 'runtime' if metadata_runtime.active(self.conn) else 'evidence'
        choice = runtime_metadata_choices.head(self.conn, entry, metadata_runtime.code(field)) if layout == 'runtime' else None
        return {'track': dict(row), 'layout': layout, 'choice': dict(choice) if choice else None}

    @synchronized
    def selection_input(self, entry):
        row = self.conn.execute('SELECT id,recording_id,video_id,title,artist FROM catalog_tracks WHERE id=?',
                                (entry,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata catalog input')
        layout = 'runtime' if metadata_runtime.active(self.conn) else 'evidence'
        return {'track': dict(row), 'layout': layout}

    @synchronized
    def genre_matches(self, label, normalized):
        return genre_identity.axis_match_rows(self.conn, [(label, normalized)]).get(label, {})

    @synchronized
    def reserve_identity(self):
        return runtime_metadata_choices.next_identity(self.conn)

    @synchronized
    def reserve_file(self, record):
        return metadata_file_writes.reserve(self.conn, record)

    @synchronized
    def import_inputs(self, entry, source):
        inputs = self.input(entry, 'title')
        hints = set() if inputs['layout'] == 'runtime' else metadata_hints.sources(self.conn, inputs['track']['id'], source)
        return {**inputs, 'hints': hints}

    @synchronized
    def store_hint(self, entry, field, source, observation):
        with self.conn.atomic(join=True):
            metadata_hints.store(self.conn, entry, field, source, observation)

    @synchronized
    def store_observations(self, delivery, prepared, *, source_run_id=None):
        with self.conn.atomic(join=True):
            return metadata_evidence.record_prepared(self.conn, delivery, prepared, source_run_id=source_run_id)

    @synchronized
    def transfer_recording_observation(self, entry, observation, at):
        with self.conn.atomic():
            return metadata_evidence.reuse_recording_observation(self.conn, entry, observation, now=at)

    @synchronized
    def recording_reuse_inputs(self, previous, target):
        layout = 'runtime' if metadata_runtime.active(self.conn) else 'evidence'
        before = dict(self.conn.execute('SELECT * FROM recordings WHERE id=?', (previous,)).fetchone())
        after = dict(self.conn.execute('SELECT * FROM recordings WHERE id=?', (target,)).fetchone())
        before['genre'] = genre_classifications.read(self.conn, 'recordings', previous)
        after['genre'] = genre_classifications.read(self.conn, 'recordings', target)
        entry = self.conn.execute('SELECT id FROM track_sources WHERE recording_id=? ORDER BY id LIMIT 1', (target,)).fetchone()
        if layout == 'runtime':
            rows = self.conn.execute('SELECT recording_id,field,source FROM recording_field_sources '
                                     'WHERE recording_id IN (?,?)', (previous, target)).fetchall()
            selected = {row['field'] for row in rows if row['recording_id'] == target}
            sources = {row['field']: row['source'] for row in rows if row['recording_id'] == previous}
        else:
            selected = {row[0] for row in self.conn.execute(
                'SELECT field FROM recording_field_sources WHERE recording_id=?', (target,))}
            sources = {}
        return dict(layout=layout, before=before, after=after, sources=sources, selected=selected, target_entry=entry[0] if entry else None)

    @synchronized
    def copy_recording_selection(self, previous, target, field):
        if field not in RECORDING_FIELDS:
            raise ValueError('Only recording selections can be copied')
        with self.conn.atomic(join=True):
            metadata_evidence.transfer_recording_selection(self.conn, previous, target, field)

    @synchronized
    def delivery_evidence(self, identity):
        row = self.conn.execute('SELECT provider,input_entry_id,input_recording_id,input_video_id,key_contract,delivery_key '
                                'FROM metadata_deliveries WHERE id=?', (identity,)).fetchone()
        if row is None:
            return None
        observations = {row['field']: row['id'] for row in self.conn.execute(
            "SELECT field,id FROM metadata_observations WHERE delivery_id=? AND origin='provider'", (identity,))}
        return {**dict(row), 'observations': observations}

    @synchronized
    def store(self, inputs, field, value, prepared, *, withdraw, source, at, identity=None, revision=None, file_record=None):
        track, layout = inputs['track'], inputs['layout']
        entry, recording, video = track['id'], track['recording_id'], track['video_id']
        with self.conn.atomic(join=True):
            current = self.input(entry, field)
            if current['layout'] != layout or current['track']['recording_id'] != recording or current['choice'] != inputs['choice']:
                raise ValueError('Metadata decision input changed')
            if layout == 'runtime':
                if file_record is None:
                    raise ValueError('Runtime metadata choice requires its reserved file')
                metadata_file_writes.complete(self.conn, file_record, kind='choices', owner=(identity,), revision=revision)
                genre = prepared.genre_key
                kind = (0 if withdraw else {'null': 1, 'blank': 2, 'value':
                    4 if prepared.number is not None else 5 if genre is not None else 3,
                    'unresolved': 6}[prepared.kind])
                runtime_metadata_choices.save(self.conn, entry, metadata_runtime.code(field), identity,
                    metadata_runtime.provider_id(self.conn, source), revision, kind,
                    None if kind in (0, 1, 5) else prepared.text, None if withdraw else prepared.number,
                    None if withdraw else genre)
                queue_refresh(self.conn)
                content_work.for_recording(self.conn, recording)
            else:
                identity = self.conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,'
                    'field_scope,field,value_kind,text_value,number_value,genre_key,unit,resolution_reason,'
                    "observed_at,origin,legacy_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'user',?)",
                    (entry, recording, video, 'recording' if field in RECORDING_FIELDS else
                     'release' if field in ('mb_year', 'label') else 'item', field, prepared.kind,
                     prepared.text, prepared.number, prepared.genre_key, prepared.unit, prepared.resolution_reason, at, source)).lastrowid
                metadata_choice_selections.select(self.conn, entry, field, identity)
            recording_metadata.audit_choice(self.conn, entry, recording, field, source, prepared, at)
            return identity

    @synchronized
    def query_identity(self, entry):
        row = self.conn.execute('SELECT recording_id,title,artist FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        return tuple(row) if row is not None else None

    @synchronized
    def store_base(self, inputs, field, value, prepared, source, at, *, delivery_id=None, observation_id=None):
        self.store_bases(inputs, {field: (value, prepared)}, source, at,
                         deliveries={field: delivery_id}, observations={field: observation_id})

    @synchronized
    def store_bases(self, inputs, selections, source, at, *, deliveries, observations):
        current = self.selection_input(inputs['track']['id'])
        if current['layout'] != inputs['layout'] or current['track']['recording_id'] != inputs['track']['recording_id']:
            raise ValueError('Metadata selection input changed')
        for field, (value, prepared) in selections.items():
            self._store_base(inputs, field, value, prepared, source, at,
                             delivery_id=deliveries.get(field), observation_id=observations.get(field))

    def _store_base(self, inputs, field, value, prepared, source, at, *, delivery_id=None, observation_id=None):
        track, layout = inputs['track'], inputs['layout']
        entry = track['id']
        if layout == 'evidence':
            if observation_id is None:
                observation_id = metadata_evidence.record_prepared(self.conn, delivery_id, {field: prepared})[field]
            row = self.conn.execute('SELECT o.*,COALESCE(d.provider,o.legacy_source) provider '
                'FROM metadata_observations o LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?',
                (observation_id,)).fetchone()
            if row is None or row['provider'] != source or metadata_evidence.value_from_row(row).selected() != value:
                raise ValueError('Selected metadata value does not match its supplying observation')
            return metadata_evidence.select(self.conn, entry, field, observation_id, now=at)
        return metadata_runtime.select(self.conn, entry, field, value, source, now=at, prepared=prepared)

    @synchronized
    def resume_provider_query(self, recording, provider, *, status):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE enrichment_provider_state SET failures=0,retry_at=0 '
                              'WHERE recording_id=? AND provider=? AND status=?', (recording, provider, status))
