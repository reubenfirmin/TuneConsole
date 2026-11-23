"""Checkpointed playlist transactions and reconciliation, independent of Radio Session policy."""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

from yt_playlist.library.playlist_catalog import fetch_song_duration
from yt_playlist.rec import radio_rotation
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.matching import identity_key, track_album, track_artist
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.web.radio_edits import AppendOperation, PlaylistOperation, RadioEdits
from yt_playlist.web.radio_playlist_io import (
    PendingWrite, PlaylistChanged, PlaylistEdit, PlaylistPending, append_receipt, matches_substitution,
    duration_seconds, readback_durations,
)
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail, Substitution, TailView

log = logging.getLogger(__name__)
CONFIRM_TIMEOUT = 30


@dataclass(frozen=True)
class PlaylistTarget:
    client: object
    playlist: str
    local_id: int | None
    title: str
    episode_id: str
    stop: threading.Event
    account_id: int | None = None


@dataclass(frozen=True)
class UploadSubstitution:
    requested: dict
    accepted: dict


@dataclass(frozen=True)
class MutationResult:
    status: Literal['waiting', 'confirmed', 'rejected', 'discarded'] = 'waiting'
    tracks: tuple[dict, ...] | None = None
    remote: tuple[dict, ...] = ()
    rejected: dict | None = None
    substitutions: tuple[UploadSubstitution, ...] = ()
    snapshot: dict | None = None


class RadioMutations:
    def __init__(self, playlists, persistence, catalog, now_fn, *, paused=lambda: False, rotation_facts=None):
        self.playlists, self.persistence = playlists, persistence
        self.catalog, self.now_fn = catalog, now_fn
        self.paused = paused
        self.rotation_facts = rotation_facts

    def _journal(self, target: PlaylistTarget, operation: PlaylistOperation):
        self.persistence.record_edit(target.episode_id, operation)

    def _send(self, target: PlaylistTarget, edits: RadioEdits, operation: PlaylistOperation, edit: PlaylistEdit,
              *, prior_write: PendingWrite | None = None):
        """Journal a new edit before sending it. An unknown reply is never replayed."""
        if target.stop.is_set():
            return False
        if edits.active is not operation:
            raise ValueError('Cannot send a superseded playlist operation')
        if operation.write is not prior_write:
            raise ValueError('Reconcile the outstanding write before sending another')
        if isinstance(operation, RadioPlaylistTail):
            operation.begin(edit, self.now_fn())
        else:
            operation.write = PendingWrite(edit, self.now_fn())
        try:
            self._journal(target, operation)
        except Exception:
            if prior_write is not None:
                operation.write = prior_write
            raise
        if target.stop.is_set():
            return False
        window = [self.now_fn(), float('inf')]
        edits.windows.append(window)
        try:
            result = self.playlists.write(target.client, target.playlist, edit)
            operation.write.receipt = append_receipt(result)
            self._journal(target, operation)
        except Exception:
            log.warning('Radio playlist write response failed; checking saved contents', exc_info=True)
        finally:
            window[1] = self.now_fn() + 3
        return True

    def substitute(self, requested, row, known, *, playlist=None):
        """Retain the actual upload and prefer it when its recording matches."""
        video = row['videoId']
        previous = known.get(video) or {}
        accepted = {**requested, **previous, 'video_id': video,
            'title': row.get('title') or previous.get('title') or '',
            'artist': track_artist(row) or previous.get('artist') or '',
            'album': track_album(row) or previous.get('album') or '',
            'thumbnail': best_thumb(row.get('thumbnails')) or previous.get('thumbnail'),
            'duration': duration_seconds(row.get('duration_seconds'))
                        or duration_seconds(parse_duration(row.get('duration')))
                        or duration_seconds(previous.get('duration'))}
        if self.paused():
            accepted['key'] = previous.get('key') or identity_key(accepted['title'], accepted['artist'])
        else:
            tid = self.catalog.upsert_track(video, accepted['title'], accepted['artist'],
                accepted['album'], accepted['duration'], thumbnail=accepted.get('thumbnail'))
            canonical = self.catalog.remember_playback_upload(requested, accepted,
                evidence={'playlist': playlist, 'set_video_id': row.get('setVideoId'),
                          'source': 'youtube-music-playlist-occurrence'}, now=self.now_fn())
            accepted['key'] = canonical or self.catalog.catalog_subject(tid)['identity_key']
        # An older offer for this upload does not own the new allocation.
        accepted['_radio_rotation_slot'] = (requested.get('_radio_rotation_slot')
                                            if accepted['key'] == requested['key'] else None)
        if accepted['key'] != requested['key'] and self.rotation_facts is not None:
            accepted.update(radio_rotation.metadata(self.rotation_facts([accepted['key']]).get(accepted['key'])))
            radio_rotation.issue(accepted)
        log.info('Radio accepted upload substitution: requested=%s actual=%s', requested['video_id'], video)
        return UploadSubstitution(requested, accepted)

    def _publish(self, target, view, tracks, remote):
        tracks = readback_durations(tracks, remote, previous=view.tracks)
        previous = {track['video_id'] for track in view.tracks}
        # One best-effort lookup when a new song is confirmed, never on status
        # polls or repeatedly for unknown durations already in the playlist.
        for track in tracks:
            if (not track.get('duration') and track['video_id'] not in previous
                    and hasattr(target.client, 'get_song') and not target.stop.is_set()):
                track['duration'] = duration_seconds(fetch_song_duration(target.client, track['video_id']))
        if list(tracks) != list(view.tracks):
            self.playlists.publish(target.local_id, target.title, list(view.tracks), list(tracks), self.now_fn())
        return tracks

    @staticmethod
    def append_substitution(operation: AppendOperation, tracks, remote):
        if not operation.write or len(remote) != len(tracks) + 1:
            return False
        expected = [t['video_id'] for t in tracks]
        row = remote[-1]
        return ([t['videoId'] for t in remote[:-1]] == expected
                and row['videoId'] not in expected and row['videoId'] != operation.track['video_id']
                and matches_substitution(operation.track, row, operation.write.receipt or {}))

    @staticmethod
    def _owned_duplicate(expected, remote, handle):
        # A repeat of a playable ID alone could also be an unrelated playlist edit.
        return bool(handle and len(remote) == len(expected) + 1
                    and remote[-1]['videoId'] in expected and remote[-1].get('setVideoId') == handle
                    and all(row['videoId'] == video and row.get('setVideoId') != handle
                            for row, video in zip(remote[:-1], expected)))

    def _duplicate_removal_row(self, expected, remote, receipt):
        handle = receipt.get('setVideoId')
        if receipt.get('status') != 'STATUS_SUCCEEDED' or not self._owned_duplicate(expected, remote, handle):
            return None
        saved_id, receipt_id = remote[-1].get('playlistVideoId'), receipt.get('videoId')
        # The provider may expose a different upload in the playback button.
        # Use a saved identity tied to the acknowledged occurrence handle.
        if saved_id and receipt_id and saved_id != receipt_id:
            return None
        if video_id := saved_id or receipt_id:
            return {'videoId': video_id, 'setVideoId': handle}
        return None

    def _append_conflict(self, target, edits, expected, remote):
        # Settle complete readbacks after reconnect before treating them as external edits.
        fields = ('videoId', 'playlistVideoId', 'setVideoId')
        received = tuple(tuple(row.get(field) for field in fields) for row in remote)
        if edits.conflict != received:
            edits.conflict, edits.conflict_at = received, time.monotonic()
            log.info('Radio readback settling: playlist=%s expected=%s receipt=%s received_fields=%s received=%s',
                     target.playlist, expected, edits.append.write.receipt if edits.append.write else None,
                     fields, received)
        if time.monotonic() - edits.conflict_at < CONFIRM_TIMEOUT:
            raise PlaylistPending()
        raise PlaylistChanged('The playlist was changed elsewhere. Radio stopped to preserve those changes.')

    def _confirm_duplicate_removal(self, target, edits, expected, remote):
        operation = edits.append
        write = operation.write
        handle = write.edit.row['setVideoId']
        if ([row['videoId'] for row in remote] == expected
                and all(row.get('setVideoId') != handle for row in remote)):
            return MutationResult('rejected', rejected=operation.track)
        if not self._owned_duplicate(expected, remote, handle):
            return self._append_conflict(target, edits, expected, remote)
        edits.conflict = None
        status = (write.receipt or {}).get('status')
        if (status and status != 'STATUS_SUCCEEDED'
                or self.now_fn() - write.started_at >= CONFIRM_TIMEOUT):
            log.warning('Radio duplicate removal unconfirmed: playlist=%s row=%s receipt=%s',
                        target.playlist, write.edit.row, write.receipt)
            raise PlaylistChanged('Radio stopped because the saved queue could not be confirmed.')
        return MutationResult()

    def append(self, target: PlaylistTarget, edits: RadioEdits, view: TailView) -> MutationResult:
        operation = edits.append
        expected = [t['video_id'] for t in view.tracks]
        desired = expected + [operation.track['video_id']]
        snapshot = self.playlists.read_snapshot(target.client, target.playlist)
        remote = snapshot['tracks']
        actual = [t['videoId'] for t in remote]
        if actual == expected and operation.write is None:
            if operation.track['video_id'] in view.blocked:
                return MutationResult('discarded')
            if not self._send(target, edits, operation, PlaylistEdit('add', tuple(desired), track=operation.track)):
                return MutationResult()
            snapshot = self.playlists.read_snapshot(target.client, target.playlist)
            remote = snapshot['tracks']
            actual = [t['videoId'] for t in remote]
        if write := operation.write:
            if write.edit.kind == 'remove':
                return self._confirm_duplicate_removal(target, edits, expected, remote)
            if row := self._duplicate_removal_row(expected, remote, write.receipt or {}):
                log.info('Radio removing owned duplicate append: playlist=%s requested=%s playable=%s saved=%s handle=%s',
                         target.playlist, operation.track['video_id'], remote[-1]['videoId'],
                         row['videoId'], row['setVideoId'])
                if not self._send(target, edits, operation, PlaylistEdit('remove', tuple(expected), row=row),
                                  prior_write=write):
                    return MutationResult()
                remote = self.playlists.read(target.client, target.playlist)
                return self._confirm_duplicate_removal(target, edits, expected, remote)
        accepted, substitutions = operation.track, ()
        if self.append_substitution(operation, view.tracks, remote):
            substitution = self.substitute(operation.track, remote[-1], view.known, playlist=target.playlist)
            accepted, substitutions = substitution.accepted, (substitution,)
            desired = expected + [accepted['video_id']]
        if actual == desired:
            tracks = (*view.tracks, accepted)
            tracks = self._publish(target, view, tracks, remote)
            return MutationResult('confirmed', tracks, tuple(remote), substitutions=substitutions, snapshot=snapshot)
        if actual == expected:
            edits.conflict = None
            if operation.write:
                receipt = operation.write.receipt or {}
                if receipt.get('status') and receipt['status'] != 'STATUS_SUCCEEDED':
                    return MutationResult('rejected', rejected=operation.track)
                if operation.write.started_at is None:
                    operation.write.started_at = self.now_fn()
                    self._journal(target, operation)
                if self.now_fn() - operation.write.started_at >= CONFIRM_TIMEOUT:
                    return MutationResult('rejected', rejected=operation.track)
            return MutationResult()
        if tuple(actual) in view.stale_states:
            edits.conflict = None
            return MutationResult()
        return self._append_conflict(target, edits, desired, remote)

    def _accept_tail(self, target: PlaylistTarget, tail: RadioPlaylistTail, view: TailView, remote):
        accepted = tail.accept(remote, view)
        substitutions = ()
        known = dict(view.known)
        if isinstance(accepted, Substitution):
            substitution = self.substitute(accepted.requested, accepted.row, known, playlist=target.playlist)
            known[substitution.accepted['video_id']] = substitution.accepted
            substitutions = (substitution,)
            tail.adopt(tuple(row['videoId'] for row in remote), accepted.requested, substitution.accepted)
            accepted = True
        self._journal(target, tail)
        return accepted, substitutions, known

    def recover_tail(self, target: PlaylistTarget, tail: RadioPlaylistTail, view: TailView, remote) -> MutationResult:
        # Conflict recovery establishes ownership only. The normal transaction
        # pass still owns timeout rejection, mirror publication and the next edit.
        accepted, substitutions, _ = self._accept_tail(target, tail, view, remote)
        return MutationResult('confirmed' if accepted else 'waiting', substitutions=substitutions)

    def recover(self, target: PlaylistTarget, edits: RadioEdits, view: TailView) -> MutationResult:
        """Recognize an owned readback without accepting or overwriting an unrelated edit."""
        remote = self.playlists.read(target.client, target.playlist)
        actual = tuple(t['videoId'] for t in remote)
        expected = tuple(t['video_id'] for t in view.tracks)
        allowed, substitutions = {expected}, ()
        if operation := edits.append:
            write = operation.write
            if write and write.edit.kind == 'remove':
                handle = write.edit.row['setVideoId']
                if actual != expected or any(row.get('setVideoId') == handle for row in remote):
                    return MutationResult()
            else:
                allowed.add((*expected, operation.track['video_id']))
                receipt = (write.receipt if write else None) or {}
                if self._duplicate_removal_row(expected, remote, receipt):
                    allowed.add(actual)
        if edits.replacement:
            try:
                result = self.recover_tail(target, edits.replacement, view, remote)
            except PlaylistChanged:
                return MutationResult()
            substitutions = result.substitutions
            if result.status != 'confirmed':
                return result
            allowed.update(edits.replacement.allowed)
        if actual not in allowed and not (edits.append and self.append_substitution(edits.append, view.tracks, remote)):
            return MutationResult(substitutions=substitutions)
        edits.conflict = None
        log.info('Radio saved-playlist readback recovered: playlist=%s', target.playlist)
        return MutationResult('confirmed', substitutions=substitutions)

    def inspect_tail(self, target: PlaylistTarget, tail: RadioPlaylistTail, view: TailView, remote) -> MutationResult:
        accepted, substitutions, known = self._accept_tail(target, tail, view, remote)
        if not accepted:
            return MutationResult(substitutions=substitutions)
        ids = tuple(row['videoId'] for row in remote)
        pending = tail.pending(ids, self.now_fn())
        self._journal(target, tail)
        if pending is not None:
            if pending.rejected:
                tail.clear_wait()
                self._journal(target, tail)
                return MutationResult('rejected', rejected=pending.rejected, substitutions=substitutions)
            return MutationResult(substitutions=substitutions)
        tracks = tuple(known[video] for video in ids)
        tracks = self._publish(target, view, tracks, remote)
        return MutationResult('confirmed', tracks, tuple(remote), substitutions=substitutions)

    def read_tail(self, target: PlaylistTarget, tail: RadioPlaylistTail, view: TailView) -> MutationResult:
        return self.inspect_tail(target, tail, view, self.playlists.read(target.client, target.playlist))

    def edit_tail(self, target: PlaylistTarget, edits: RadioEdits, view: TailView,
                  edit: PlaylistEdit, previous: tuple[dict, ...]) -> MutationResult:
        tail = edits.replacement
        if not self._send(target, edits, tail, edit):
            return MutationResult()
        remote = self.playlists.read(target.client, target.playlist)
        receipt = tail.write.receipt or {}
        if (receipt.get('status') and receipt['status'] != 'STATUS_SUCCEEDED'
                and [t['videoId'] for t in remote] == [t['videoId'] for t in previous]):
            tail.clear_wait()
            self._journal(target, tail)
            return MutationResult('rejected', rejected=edit.track)
        return self.inspect_tail(target, tail, view, remote)
