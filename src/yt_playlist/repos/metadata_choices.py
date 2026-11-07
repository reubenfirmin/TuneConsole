"""SQL representation of explicit catalog metadata choices."""
from contextlib import contextmanager
import json

from yt_playlist.core import catalog, genre_classifications, genre_identity, metadata, metadata_evidence, metadata_runtime
from yt_playlist.core.metadata_values import ITEM_FIELDS, RECORDING_FIELDS
from yt_playlist.repos.base import Repo, synchronized


class MetadataChoiceRepo(Repo):
    @contextmanager
    def transaction(self):
        # Reserve this metadata writer before assigning a file revision. Other
        # writers cannot publish different content at the same revision.
        with self._lock:
            with self.conn.atomic():
                yield

    @synchronized
    def input(self, entry, field):
        if field not in (*ITEM_FIELDS, *RECORDING_FIELDS):
            raise ValueError('Unknown metadata field')
        entry = catalog.resolve_entry(self.conn, entry)
        row = self.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata catalog input')
        layout = ('runtime' if metadata_runtime.active(self.conn) else
                  'evidence' if metadata_evidence.available(self.conn) else 'legacy')
        choice = self.conn.execute(
            'SELECT id,revision FROM metadata_runtime_choices WHERE entry_id=? AND field=?',
            (entry, metadata_runtime.code(field))).fetchone() if layout == 'runtime' else None
        return {'track': dict(row), 'layout': layout, 'choice': dict(choice) if choice else None}

    @synchronized
    def genre_matches(self, label):
        return genre_identity.axis_matches(self.conn, [label]).get(label, {})

    @synchronized
    def reserve_identity(self):
        return metadata_runtime.next_identity(self.conn, 'metadata_runtime_choices')

    @synchronized
    def store_observations(self, delivery, prepared):
        with self.conn.atomic():
            return metadata_evidence.record_prepared(self.conn, delivery, prepared)

    @synchronized
    def transfer_recording_observation(self, entry, observation, at):
        with self.conn.atomic():
            return metadata_evidence.reuse_recording_observation(self.conn, entry, observation, now=at)

    @synchronized
    def store(self, inputs, field, value, prepared, *, withdraw, source, at, identity=None, revision=None):
        track, layout = inputs['track'], inputs['layout']
        entry, recording, video = track['id'], track['recording_id'], track['video_id']
        with self.conn.atomic(join=True):
            current = self.input(entry, field)
            if current['layout'] != layout or current['track']['recording_id'] != recording or current['choice'] != inputs['choice']:
                raise ValueError('Metadata decision input changed')
            if layout == 'runtime':
                genre = metadata_runtime.genre_id(self.conn, prepared.genre_key)
                kind = (0 if withdraw else {'null': 1, 'blank': 2, 'value':
                    4 if prepared.number is not None else 5 if genre is not None else 3,
                    'unresolved': 6}[prepared.kind])
                self.conn.execute('INSERT INTO metadata_runtime_choices VALUES (?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(entry_id,field) DO UPDATE SET provider_id=excluded.provider_id,'
                    'revision=excluded.revision,kind=excluded.kind,text_value=excluded.text_value,'
                    'number_value=excluded.number_value,genre_id=excluded.genre_id',
                    (identity, entry, metadata_runtime.code(field), metadata_runtime.provider_id(self.conn, source),
                     revision, kind, None if kind in (0, 1, 5) else prepared.text,
                     None if withdraw else prepared.number, None if withdraw else genre))
                self.conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                                  'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1')
                self.conn.execute('INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings '
                    'WHERE id=? ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1', (recording,))
            elif layout == 'evidence':
                identity = self.conn.execute('INSERT INTO metadata_observations(entry_id,recording_id,input_video_id,'
                    'field_scope,field,value_kind,text_value,number_value,genre_key,unit,resolution_reason,'
                    "observed_at,origin,legacy_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'user',?)",
                    (entry, recording, video, 'recording' if field in RECORDING_FIELDS else
                     'release' if field in ('mb_year', 'label') else 'item', field, prepared.kind,
                     prepared.text, prepared.number, prepared.genre_key, prepared.unit, prepared.resolution_reason, at, source)).lastrowid
                self.conn.execute('INSERT INTO metadata_choice_heads(entry_id,field,observation_id) VALUES (?,?,?) '
                    'ON CONFLICT(entry_id,field) DO UPDATE SET observation_id=excluded.observation_id',
                    (entry, field, identity))
            elif withdraw:
                self.conn.execute('DELETE FROM entry_field_choices WHERE entry_id=? AND field=?', (entry, field))
            else:
                identity = self.conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source,chosen_at) '
                    'VALUES (?,?,?,?,?)', (entry, field, json.dumps(value), source, at)).lastrowid
                self.conn.execute('INSERT INTO entry_field_choices VALUES (?,?,?,?) '
                    'ON CONFLICT(entry_id,field) DO UPDATE SET value_json=excluded.value_json,source=excluded.source',
                    (entry, field, json.dumps(value), source))
            return identity

    @synchronized
    def query_identity(self, entry):
        row = self.conn.execute('SELECT recording_id,title,artist FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        return tuple(row) if row is not None else None

    @synchronized
    def store_base(self, inputs, field, value, prepared, source, at, *, delivery_id=None, observation_id=None):
        track, layout = inputs['track'], inputs['layout']
        entry, recording, video = track['id'], track['recording_id'], track['video_id']
        current = self.input(entry, field)
        if current['layout'] != layout or current['track']['recording_id'] != recording:
            raise ValueError('Metadata selection input changed')
        if layout == 'evidence':
            if observation_id is None:
                observation_id = metadata_evidence.record_prepared(self.conn, delivery_id, {field: prepared})[field]
            row = self.conn.execute('SELECT o.*,COALESCE(d.provider,o.legacy_source) provider '
                'FROM metadata_observations o LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?',
                (observation_id,)).fetchone()
            if row is None or row['provider'] != source or metadata_evidence.value_from_row(row).selected() != value:
                raise ValueError('Selected metadata value does not match its supplying observation')
            return metadata_evidence.select(self.conn, entry, field, observation_id, now=at)
        if field in RECORDING_FIELDS:
            scope, identity = 'recording', recording
            if field == 'genre' and layout == 'runtime':
                genre_classifications.write_evidence(self.conn, 'recordings', recording, prepared)
                genre = metadata_runtime.genre_id(self.conn, prepared.genre_key)
                self.conn.execute('INSERT INTO metadata_runtime_recording_genres VALUES (?,?,?) '
                    'ON CONFLICT(recording_id) DO UPDATE SET genre_id=excluded.genre_id,unresolved=excluded.unresolved',
                    (recording, genre, value if genre is None else None))
            else:
                self.conn.execute(f'UPDATE recordings SET {field}=? WHERE id=? AND {field} IS NOT ?',
                                  (value, recording, value))
        else:
            scope, identity = 'entry', entry
            table, key, target = (('youtube_items', 'video_id', video) if video is not None else
                                  ('unresolved_items', 'entry_id', entry))
            self.conn.execute(f'UPDATE {table} SET {field}=? WHERE {key}=? AND {field} IS NOT ?', (value, target, value))
        if layout == 'runtime':
            self.conn.execute('INSERT INTO metadata_runtime_'+scope+'_sources VALUES (?,?,?) ON CONFLICT('+scope+
                '_id,field) DO UPDATE SET provider_id=excluded.provider_id',
                (identity, metadata_runtime.code(field), metadata_runtime.provider_id(self.conn, source)))
        else:
            table, column, subject = metadata.source_location(self.conn, entry, field)
            self.conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?) ON CONFLICT({column},field) DO UPDATE '
                'SET source=excluded.source,selected_at=excluded.selected_at', (subject, field, source, at))

    @synchronized
    def resume_provider_query(self, recording, provider, *, status):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE enrichment_provider_state SET failures=0,retry_at=0 '
                              'WHERE recording_id=? AND provider=? AND status=?', (recording, provider, status))
