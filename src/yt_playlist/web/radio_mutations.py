"""Checkpointed playlist transactions and reconciliation, independent of Radio Session policy."""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

from yt_playlist.util.duration import parse_duration
from yt_playlist.util.matching import track_album, track_artist
from yt_playlist.web.radio_edits import AppendOperation, PlaylistOperation, RadioEdits
from yt_playlist.web.radio_playlist_io import (
    PendingWrite, PlaylistChanged, PlaylistEdit, PlaylistPending, append_receipt, matches_substitution,
)
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail, Substitution, TailView

log = logging.getLogger(__name__)
CONFIRM_TIMEOUT = 30


@dataclass(frozen=True)
class PlaylistTarget:
    client: object
    playlist: str
    local_id: int
    title: str
    episode_id: str
    stop: threading.Event


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


class RadioMutations:
    def __init__(self, playlists, persistence, catalog, now_fn):
        self.playlists, self.persistence = playlists, persistence
        self.catalog, self.now_fn = catalog, now_fn

    def _journal(self, target: PlaylistTarget, operation: PlaylistOperation):
        self.persistence.record_edit(target.episode_id, operation)

    def _send(self, target: PlaylistTarget, edits: RadioEdits, operation: PlaylistOperation, edit: PlaylistEdit):
        """One intent/receipt protocol for append and replacement; an unknown reply is never replayed."""
        if target.stop.is_set():
            return False
        if edits.active is not operation:
            raise ValueError('Cannot send a superseded playlist operation')
        if operation.write is not None:
            raise ValueError('Reconcile the outstanding write before sending another')
        if isinstance(operation, RadioPlaylistTail):
            operation.begin(edit, self.now_fn())
        else:
            operation.write = PendingWrite(edit, self.now_fn())
        self._journal(target, operation)
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

    def _substitute(self, requested, row, known):
        """Retain the actual upload without declaring two recordings equivalent."""
        video = row['videoId']
        accepted = known.get(video)
        if accepted is None:
            accepted = {**requested, 'video_id': video,
                'title': row.get('title') or requested.get('title') or '',
                'artist': track_artist(row) or requested.get('artist') or '',
                'album': track_album(row) or requested.get('album') or '',
                'duration': row.get('duration_seconds') or parse_duration(row.get('duration')) or requested.get('duration')}
            tid = self.catalog.upsert_track(video, accepted['title'], accepted['artist'],
                accepted['album'], accepted['duration'], thumbnail=accepted.get('thumbnail'))
            accepted['key'] = self.catalog.catalog_subject(tid)['identity_key']
        log.info('Radio accepted upload substitution: requested=%s actual=%s', requested['video_id'], video)
        return UploadSubstitution(requested, accepted)

    def _publish(self, target, view, tracks):
        if list(tracks) != list(view.tracks):
            self.playlists.publish(target.local_id, target.title, list(view.tracks), list(tracks), self.now_fn())

    @staticmethod
    def append_substitution(operation: AppendOperation, tracks, remote):
        if not operation.write or len(remote) != len(tracks) + 1:
            return False
        expected = [t['video_id'] for t in tracks]
        row = remote[-1]
        return ([t['videoId'] for t in remote[:-1]] == expected
                and row['videoId'] not in expected and row['videoId'] != operation.track['video_id']
                and matches_substitution(operation.track, row, operation.write.receipt or {}))

    def append(self, target: PlaylistTarget, edits: RadioEdits, view: TailView) -> MutationResult:
        operation = edits.append
        expected = [t['video_id'] for t in view.tracks]
        desired = expected + [operation.track['video_id']]
        remote = self.playlists.read(target.client, target.playlist)
        actual = [t['videoId'] for t in remote]
        if actual == expected and operation.write is None:
            if operation.track['video_id'] in view.blocked:
                return MutationResult('discarded')
            if not self._send(target, edits, operation, PlaylistEdit('add', tuple(desired), track=operation.track)):
                return MutationResult()
            remote = self.playlists.read(target.client, target.playlist)
            actual = [t['videoId'] for t in remote]
        accepted, substitutions = operation.track, ()
        if self.append_substitution(operation, view.tracks, remote):
            substitution = self._substitute(operation.track, remote[-1], view.known)
            accepted, substitutions = substitution.accepted, (substitution,)
            desired = expected + [accepted['video_id']]
        if actual == desired:
            tracks = (*view.tracks, accepted)
            self._publish(target, view, tracks)
            return MutationResult('confirmed', tracks, tuple(remote), substitutions=substitutions)
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
        # A complete stale read after reconnect is not enough to establish an external edit.
        if edits.conflict != tuple(actual):
            edits.conflict, edits.conflict_at = tuple(actual), time.monotonic()
            log.info('Radio readback settling: playlist=%s expected=%s received=%s', target.playlist, desired, actual)
        if time.monotonic() - edits.conflict_at < CONFIRM_TIMEOUT:
            raise PlaylistPending()
        raise PlaylistChanged('The playlist was changed elsewhere. Radio stopped to preserve those changes.')

    def _accept_tail(self, target: PlaylistTarget, tail: RadioPlaylistTail, view: TailView, remote):
        accepted = tail.accept(remote, view)
        substitutions = ()
        known = dict(view.known)
        if isinstance(accepted, Substitution):
            substitution = self._substitute(accepted.requested, accepted.row, known)
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
        if edits.append:
            allowed.add((*expected, edits.append.track['video_id']))
            receipt = (edits.append.write.receipt if edits.append.write else None) or {}
            if receipt.get('videoId'):
                allowed.add((*expected, receipt['videoId']))
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
        self._publish(target, view, tracks)
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
