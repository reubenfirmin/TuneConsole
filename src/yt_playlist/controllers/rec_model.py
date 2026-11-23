"""Taste policy and model publication over explicit SQL, settings and file owners."""
from dataclasses import replace
import json
import time
import uuid

from yt_playlist.core import embeddings, model_state, rating_feedback, rec_model_policy
from yt_playlist.core.model_state import (
    ArtistModelState, ArtistModelUpdate, RecordingModelState, RecordingModelUpdate, UNCHANGED,
)
from yt_playlist.core.rec_model_policy import WEIGHT_MIN, WEIGHT_MAX
from yt_playlist.core.recommendation_settings import LANE_NAMES


class RecommendationModel:
    def __init__(self, repository, settings, player_events, genres, vectors, recovery, guard,
                 *, active_session=lambda now: None, first_session=lambda at, until: None,
                 permanent=lambda: None):
        self.repository, self.settings, self.player_events = repository, settings, player_events
        self.genres, self.vectors, self.recovery, self.guard = genres, vectors, recovery, guard
        self.active_session = active_session
        self.first_session = first_session
        self.permanent = permanent

    def weight_records(self):
        return self.repository.weight_records()

    def parameter_values(self, keys):
        return self.repository.parameter_values(keys)

    def get_weights(self, now=None, revert_halflife_d=60., *, records=None):
        weights = self.baseline_weights(now, revert_halflife_d, records=records)
        weights.update({axis: value for axis, value in self.get_session_weights(now).items()
                        if weights.get(axis) != 0.})
        return weights

    def baseline_weights(self, now=None, revert_halflife_d=60., *, records=None):
        permanent = self.permanent()
        records = self.weight_records() if records is None else records
        if permanent is not None:
            return {**permanent['weights'], **{axis: 0. for axis, row in records.items()
                    if axis.startswith('genre:') and row['weight'] == 0.}}
        now = time.time() if now is None else now
        events = self.repository.legacy_dislike_weight_events() if records else {}
        weights = {}
        for axis, row in records.items():
            history = events.get((axis, row['weight'], row['updated_at']))
            if history:
                row = {**row, 'weight': rec_model_policy.startup_weight(history, revert_halflife_d)}
            weights[axis] = rec_model_policy.effective(axis, row, now, revert_halflife_d)
        return weights

    def get_session_weights(self, now=None):
        if not (raw := self.settings.get_setting(rec_model_policy.SESSION_WEIGHTS)):
            return {}
        now = time.time() if now is None else now
        return rec_model_policy.session_weights(raw, self.active_session(now), now, self.first_session)

    def set_session_weight(self, axis, weight, now, *, lo=WEIGHT_MIN, hi=WEIGHT_MAX):
        with self.guard():
            session = self.active_session(now)
            raw = self.settings.get_setting(rec_model_policy.SESSION_WEIGHTS)
            active = rec_model_policy.session_weights(raw, session, now, self.first_session)
            document = {key: row for key, row in json.loads(raw or '{}').items() if key in active}
            document[axis] = {'value': max(lo, min(hi, float(weight))), 'at': now,
                              'session': rec_model_policy.session_identity(session)}
            self.settings.set_setting(rec_model_policy.SESSION_WEIGHTS, json.dumps(document, sort_keys=True))

    def clear_session_weights(self):
        self.settings.delete_setting(rec_model_policy.SESSION_WEIGHTS)

    def nudge_weight(self, axis, factor, lo=WEIGHT_MIN, hi=WEIGHT_MAX, now=None, revert_halflife_d=60.):
        now = time.time() if now is None else now
        with self.guard():
            row = self.repository.weight_record(axis)
            if row and axis.startswith('genre:') and row['weight'] == 0.:
                return 0.
            base = rec_model_policy.effective(axis, row, now, revert_halflife_d) if row else 1.
            value = max(lo, min(hi, base * factor))
            self.repository.put_weight(axis, value, now)
            return value

    def set_weight(self, axis, weight, lo=WEIGHT_MIN, hi=WEIGHT_MAX, now=None):
        self.repository.put_weight(axis, max(lo, min(hi, float(weight))), time.time() if now is None else now)

    def reset_weights(self):
        return self.repository.reset_weights()

    def save_taste_settings(self, param_keys, now, replace=False):
        with self.guard(), self.recovery.retained():
            if not replace and self.repository.checkpoint_facts()['header'] is not None:
                return
            params = self.repository.parameter_values(param_keys)
            weights = list(self.weight_records().values())
            integrity = self.recovery.describe('taste-checkpoint', 1, params)
            file_id = self.repository.reserve_checkpoint_file(integrity)
            reference = self.recovery.publish_reserved(file_id, 'taste-checkpoint', 1, params, expected=integrity)
            self.repository.publish_checkpoint(uuid.uuid4().hex, now, weights, reference, file_id=file_id)

    def restore_taste_settings(self, param_keys):
        with self.guard(), self.recovery.retained():
            facts = self.repository.checkpoint_facts()
            header = facts['header']
            if header is None:
                return False
            reference = tuple(header[name] for name in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
            params = self.recovery.read(reference, kind='taste-checkpoint', owner=1)
            accepted = [row['position'] for row in facts['weights']
                        if not row['captured_axis'].startswith('lane:') or row['captured_axis'][5:] in LANE_NAMES]
            weights = self.repository.checkpoint_axes(accepted)
            if len({row[0] for row in weights}) != len(weights):
                raise ValueError('Merged checkpoint axes require an explicit choice before restoring')
            self.settings.replace_keys(param_keys, {key: value for key, value in params.items() if key in param_keys})
            self.repository.replace_weights(weights)
            self.clear_session_weights()
            return True

    def record_feedback(self, surface, item_key, kind, reason=None, scope='', until=None, now=None):
        key = self.repository.resolve_key(item_key) or item_key
        artist = key[7:] if key.startswith('artist:') else key if surface in ('artist', 'new_artists') else None
        return self.repository.insert_feedback(surface, key, kind, reason, scope or '', until, now, artist)

    def recent_recommendation_feedback(self, start, now):
        return self.repository.recent_recommendation_feedback(start, now)

    def suppressed_keys(self, surface, now, scope='', *, keys=None, records=None, check_genres=True):
        keys = list(dict.fromkeys(keys)) if keys is not None else None
        if keys == []:
            return set()
        blocked = self.repository.feedback_suppression(surface, scope or '', now, keys,
            local_kinds=('dismiss', 'mute', 'not_now'), global_kind='dislike', artist_kind='mute')
        blocked.update(self.player_events.skip_blackouts(now, keys=keys))
        if not check_genres:
            return blocked
        records = self.weight_records() if records is None else records
        muted = {axis[6:] for axis, row in records.items() if axis.startswith('genre:') and row['weight'] == 0}
        if muted:
            rows = self.repository.genre_rows(keys)
            axes = self.genres.feature_axes({row[1] for row in rows})
            blocked.update(key for key, genre in rows if muted.intersection(axes[genre]))
        return blocked

    def next_suppression_change(self, now):
        deadlines = [self.repository.feedback_expiry(now), *self.player_events.skip_blackouts(now).values()]
        return min((at for at in deadlines if at is not None), default=None)

    def apply_rating_feedback(self, states, *, dislike_seconds):
        with self.guard():
            rows = self.repository.rating_rows(list(states))
            deleted, actions, inserted, likes = rating_feedback.feedback_changes(states, rows, dislike_seconds=dislike_seconds)
            self.repository.publish_ratings(deleted, actions, inserted)
            return likes

    def reassociate_rating_feedback(self, states, prior_feedback, *, association=None):
        rows = self.repository.rating_rows(list(states))
        changes = rating_feedback.association_changes(states, rows, prior_feedback)
        self.repository.publish_association_ratings(*changes, association=association)

    def unban(self, identity_key, now):
        with self.guard():
            key, recording, latest = self.repository.unban_facts(identity_key)
            self.repository.publish_unban(key, recording, max(now, latest) if latest is not None else now)

    def clear_feedback(self):
        return self.repository.clear_feedback(retained_kinds=('like_grad', 'dislike_grad'))

    def recording_state(self) -> RecordingModelState:
        return self.repository.recording_state()

    def artist_state(self) -> ArtistModelState:
        return self.repository.artist_state()

    def _encoder(self, reference, owner):
        if reference is not None:
            return self.recovery.read(reference, kind='taste-settings', owner=owner)

    def recording_encoder(self) -> str | None:
        with self.recovery.retained():
            return self._encoder(self.recording_state().reference, 'recording')

    def artist_encoder(self) -> str | None:
        with self.recovery.retained():
            return self._encoder(self.artist_state().reference, 'artist')

    def publish_model_state(self, *, recording: RecordingModelUpdate | None = None,
                            artist: ArtistModelUpdate | None = None,
                            related_settings: dict[str, str | None] | None = None) -> None:
        for update in (recording, artist):
            if update is not None:
                model_state.validate_encoder(update.encoder)
        with self.recovery.retained():
            files = []
            recording_write = self._prepare_model_write('recording', recording, files)
            artist_write = self._prepare_model_write('artist', artist, files)
            self.repository.publish_model_state(recording_write, artist_write,
                file_writes=files, related_settings=related_settings)

    def _prepare_model_write(self, owner, update, files):
        if update is None:
            return None
        if update.encoder is UNCHANGED:
            return update, UNCHANGED
        if not update.encoder:
            return update, None
        integrity = self.recovery.describe('taste-settings', owner, update.encoder)
        identity = self.repository.reserve_encoder(owner, integrity)
        reference = self.recovery.publish_reserved(identity, 'taste-settings', owner, update.encoder,
                                                   expected=integrity)
        files.append((identity, owner, reference))
        return update, reference

    def _embedding_settings(self, recording=None, artist=None):
        with self.recovery.retained():
            recording_state, artist_state = self.recording_state(), self.artist_state()
            return {
                'rec_content_model': (recording.encoder if recording and recording.encoder is not UNCHANGED
                                      else self._encoder(recording_state.reference, 'recording')),
                'rec_artist_content_model': (artist.encoder if artist and artist.encoder is not UNCHANGED
                                             else self._encoder(artist_state.reference, 'artist')),
                'rec_fit_inputs': (recording.fit_fingerprint if recording and recording.fit_fingerprint is not UNCHANGED
                                   else recording_state.fit_fingerprint),
                'rec_artist_fit_inputs': (artist.fit_fingerprint if artist and artist.fit_fingerprint is not UNCHANGED
                                          else artist_state.fit_fingerprint),
                'rec_embed_method': self.settings.get_setting('rec_embed_method'),
                'rec_dim': self.settings.get_setting('rec_dim'),
            }

    def prepare_training_changes(self, vectors, *, recording=None, artist=None):
        self.vectors.assert_reader()
        basis, changes = self._embedding_settings(recording, artist), {}
        with self.vectors.retained():
            for table, rows in vectors.items():
                embeddings.descriptor(table)
                delta = rec_model_policy.training_delta(table, rows, self.vectors.rows(table), self.vectors.head(table), basis)
                if delta is not None:
                    changes[table] = delta
            return self.prepare_training_deltas(changes)

    def prepare_training_deltas(self, changes):
        self.vectors.assert_reader()
        with self.vectors.retained():
            return {table: replace(delta, publication=self.vectors.prepare(
                table, delta.upserts, delta.removed, replace=delta.replace, model_version=delta.model_version))
                for table, delta in changes.items()}

    def publish_training_generation(self, changes):
        for table in changes:
            embeddings.descriptor(table)
        with self.vectors.retained():
            publications = []
            for table, delta in changes.items():
                publication = delta.publication or self.vectors.prepare(table, delta.upserts, delta.removed,
                    replace=delta.replace, model_version=delta.model_version)
                if publication.name != table:
                    raise ValueError('Training publication belongs to another space')
                publications.append(publication)
            self.vectors.publish(publications)

    def collaborative_basis(self):
        return self.vectors.head('rec_vectors')

    def content_generation(self, *, keys=None):
        with self.guard(), self.vectors.retained(), self.recovery.retained():
            state = self.recording_state()
            encoder = self._encoder(state.reference, 'recording')
            result = {'generation': state.generation or '', 'model': encoder,
                'revision': state.genre_revision, 'inputs': state.input_fingerprint,
                'library_basis': self.vectors.head('rec_content_vectors'),
                'discovered_basis': self.vectors.head('rec_discovered_content_vectors'),
                'library': self.get_rec_content_vectors() if keys is None else list(self.vectors_by_keys(keys, content=True).items()),
                'discovered': self.get_rec_discovered_content_vectors() if keys is None else [], 'coherent': True}
            expected = embeddings.version('rec_content_vectors', {'rec_content_model': encoder})
            for scope in ('library', 'discovered'):
                basis = result[scope + '_basis']
                if expected and basis and basis[0] != expected:
                    result[scope], result['coherent'] = [], False
            return result

    def publish_content_generation(self, library, discovered, model, revision, inputs, expected_generation):
        library, discovered = list(library), list(discovered)
        settings = {'rec_content_model': model}
        dimension = embeddings.model_dimensions('rec_content_vectors', settings)
        if any(embeddings.dimensions(blob) != dimension for _key, blob in library + discovered):
            raise ValueError('Embedding dimensions do not match their encoder')
        with self.vectors.retained():
            publications = [self.vectors.prepare(table, rows, replace=True, model_version=embeddings.version(table, settings))
                for table, rows in (('rec_content_vectors', library), ('rec_discovered_content_vectors', discovered))]
            with self.guard():
                if (self.recording_state().generation or '') != expected_generation:
                    return False
                self.vectors.publish(publications)
                self.publish_model_state(recording=RecordingModelUpdate(
                    encoder=model, genre_revision=revision, input_fingerprint=inputs or '', generation=uuid.uuid4().hex))
                return True

    def vectors_by_keys(self, keys, *, content=False):
        if content:
            return self.content_vector_pools(keys)['library']
        keys, result = list(dict.fromkeys(keys)), {}
        for start in range(0, len(keys), 500):
            result.update(self.vectors.rows('rec_vectors', keys=keys[start:start + 500]))
        return result

    def content_vector_pools(self, keys):
        keys, pools = list(dict.fromkeys(keys)), {'library': {}, 'discovered': {}}
        with self.guard(), self.vectors.retained():
            expected = embeddings.version('rec_content_vectors', {'rec_content_model': self.recording_encoder()})
            tables = {'library': 'rec_content_vectors', 'discovered': 'rec_discovered_content_vectors'}
            heads = {scope: self.vectors.head(table) for scope, table in tables.items()}
            for scope, table in tables.items():
                basis = heads[scope]
                if not basis or (expected and basis[0] != expected):
                    continue
                if not expected and scope == 'discovered' and heads['library'] and basis != heads['library']:
                    continue
                for start in range(0, len(keys), 500):
                    pools[scope].update(self.vectors.rows(table, keys=keys[start:start + 500]))
        return pools

    def content_vectors_by_keys(self, keys):
        pools = self.content_vector_pools(keys)
        return {**pools['discovered'], **pools['library']}

    def replace_rec_vectors(self, rows):
        return self.vectors.replace('rec_vectors', rows, self._embedding_settings())

    def get_rec_vectors(self):
        return self.vectors.rows('rec_vectors')

    def rec_vectors_count(self):
        return self.vectors.count('rec_vectors')

    def replace_rec_content_vectors(self, rows):
        return self.vectors.replace('rec_content_vectors', rows, self._embedding_settings())

    def get_rec_content_vectors(self):
        return self.vectors.rows('rec_content_vectors')

    def rec_content_vectors_count(self):
        return self.vectors.count('rec_content_vectors')

    def replace_rec_discovered_content_vectors(self, rows):
        return self.vectors.replace('rec_discovered_content_vectors', rows, self._embedding_settings())

    def get_rec_discovered_content_vectors(self):
        return self.vectors.rows('rec_discovered_content_vectors')

    def rec_discovered_content_vectors_count(self):
        return self.vectors.count('rec_discovered_content_vectors')

    def replace_rec_artist_vectors(self, rows):
        return self.vectors.replace('rec_artist_vectors', rows, self._embedding_settings())

    def get_rec_artist_vectors(self):
        return self.vectors.rows('rec_artist_vectors')

    def rec_artist_vectors_count(self):
        return self.vectors.count('rec_artist_vectors')

    def replace_rec_artist_content_vectors(self, rows):
        return self.vectors.replace('rec_artist_content_vectors', rows, self._embedding_settings())

    def get_rec_artist_content_vectors(self):
        return self.vectors.rows('rec_artist_content_vectors')

    def next_feedback_expiry(self, now):
        return self.repository.next_feedback_expiry(now)

    def disliked_identity_keys(self, *, keys=None, now=None):
        return self.repository.disliked_identity_keys(keys=keys, now=now)

    def disliked_key_page(self, *, after=None, limit=128):
        return self.repository.disliked_key_page(after=after, limit=limit)

    def list_dislikes(self):
        return self.repository.list_dislikes()

    def recent_liked_keys(self, limit=None):
        return self.repository.recent_liked_keys(limit)

    def recent_liked_with_ts(self, limit=None):
        return self.repository.recent_liked_with_ts(limit)

    def like_provenance(self, identity_key):
        return self.repository.like_provenance(identity_key)

    def mark_graduated_once(self, identity_key, kind, now):
        return self.repository.mark_graduated_once(identity_key, kind, now)

    def disliked_with_ts(self):
        return self.repository.disliked_with_ts()

    def set_lean(self, axis, value, now):
        return self.repository.set_lean(axis, value, now)

    def get_leans(self):
        return self.repository.get_leans()

    def get_lean(self, axis):
        return self.repository.get_lean(axis)

    def lean_rows(self):
        return self.repository.lean_rows()

    def set_lean_graduated_day(self, axis, day):
        return self.repository.set_lean_graduated_day(axis, day)

    def clear_lean(self, axis):
        return self.repository.clear_lean(axis)

    def clear_all_leans(self):
        return self.repository.clear_all_leans()

    def hidden_facets(self):
        return self.repository.hidden_facets()

    def hide_facet(self, axis):
        return self.repository.hide_facet(axis)

    def unhide_facet(self, axis):
        return self.repository.unhide_facet(axis)

    def clear_hidden_facets(self):
        return self.repository.clear_hidden_facets()

    def muted_album_ids(self, browse_ids, now):
        return self.repository.muted_album_ids(browse_ids, now)

    def artist_mute_targets(self, now):
        return self.repository.artist_mute_targets(now)

    def muted_artists(self, now=None):
        return self.repository.muted_artists(now)

    def feedback_summary(self):
        return self.repository.feedback_summary()

    def recent_rating_inputs(self, since, until):
        return self.repository.recent_rating_inputs(since, until)
