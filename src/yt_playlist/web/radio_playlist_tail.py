"""Change only the unplayed suffix of a saved YouTube playlist.

The caller serializes this with appends. Keep each in-flight write until a
complete read sees its result; YouTube can briefly serve an older playlist.
"""
import logging

log = logging.getLogger(__name__)
LOOKAHEAD = 1


class PlaylistChanged(ValueError):
    pass


class PlaylistPending(Exception):
    """A temporary readback condition, never a user-facing error."""


class RadioPlaylistTail:
    def __init__(self, tracks, proposed, slot, *, choice_id=None):
        self.allowed = {tuple(t["video_id"] for t in tracks)}
        self.proposed = list(proposed)
        self.slot = slot
        self.choice_id = choice_id
        self.confirmed = next(iter(self.allowed))
        self.waiting = None
        self.saved = False

    def _accept(self, radio, s, ids):
        state = tuple(ids)
        if state in self.allowed:
            return True
        if radio._stale_read(s, ids):
            return False
        # A direction click owns the upcoming suffix. YouTube can expose a
        # combination/order of our old and new future rows while edits settle.
        # Rebase that suffix, preserving every historical and playing row.
        history = [t["video_id"] for t in s.tracks[:max(s.position, s.furthest) + 1]]
        owned = {video for previous in self.allowed for video in previous}
        owned.update(t["video_id"] for t in self.proposed)
        if (ids[:len(history)] == history and len(ids) == len(set(ids))
                and set(ids) <= owned and all(video in s.candidates.by_video for video in ids)):
            log.info("Radio direction readback rebased: previous=%s actual=%s", self.confirmed, ids)
            self.allowed.add(state)
            self.confirmed = state
            self.waiting = None
            radio._persist(s)
            return True
        log.warning("Radio playlist conflict: history=%s known_states=%s actual=%s",
                    history, sorted(self.allowed), ids)
        raise PlaylistChanged("Saved playlist contains an unrecognized edit")

    def apply(self, radio, session):
        """Return True only after confirming the requested suffix in its exact order."""
        s = session
        self.saved = False
        # Bound each pass so the observer can keep processing feedback and Stop,
        # including when shrinking a session's initial three-song playlist.
        for _ in range(7):
            if s.stop.is_set():
                return False
            remote = radio._remote_tracks(s)
            ids = [t["videoId"] for t in remote]
            if not self._accept(radio, s, ids):
                return False
            if self.waiting is not None:
                if tuple(ids) != self.waiting:
                    return False
                self.confirmed = self.waiting
                self.waiting = None
            elif tuple(ids) != self.confirmed:
                return False
            radio._publish(s, [s.candidates.by_video[v] for v in ids])
            radio._browser(s)
            player = radio._player(s)
            if player is None or player.get("video_id") not in ids:
                return False
            position = ids.index(player["video_id"])
            radio._observe(s, position)
            if position < s.furthest:
                return False
            history = ids[:position + 1]
            proposed = [t for t in self.proposed if t["video_id"] not in history
                        and t["video_id"] not in s.directions.blocked]
            target = proposed[:LOOKAHEAD]
            desired = history + [t["video_id"] for t in target]
            if ids == desired:
                self.proposed = proposed
                return True
            duration = player.get("duration") or s.tracks[position].get("duration")
            progress = player.get("position")
            if not duration or progress is None or duration - progress < 8:
                return False

            missing = next((t for t in target if t["video_id"] not in ids), None)
            extra = next((t for t in remote[position + 1:]
                          if t["videoId"] not in desired), None)
            after = list(ids)
            if missing:
                after.append(missing["video_id"])
                write = lambda: s.client.add_playlist_items(s.playlist, [missing["video_id"]])
            elif extra:
                if not extra.get("setVideoId"):
                    return False
                after.remove(extra["videoId"])
                write = lambda: s.client.remove_playlist_items(s.playlist, [extra])
            else:
                # Only future rows can move; never touch the playing or historical prefix.
                index = next(i for i in range(position + 1, len(ids)) if ids[i] != desired[i])
                moved = next(t for t in remote if t["videoId"] == desired[index])
                successor = remote[index]
                if not moved.get("setVideoId") or not successor.get("setVideoId"):
                    return False
                after.remove(moved["videoId"])
                after.insert(index, moved["videoId"])
                write = lambda: s.client.edit_playlist(s.playlist,
                    moveItem=(moved["setVideoId"], successor["setVideoId"]))
            # A read of the old state does not mean the write failed. Keep the
            # expected result and do not send another mutation until it arrives.
            self.allowed.add(tuple(after))
            self.waiting = tuple(after)
            radio._persist(s)
            s.mutation_windows.append([radio.ctx.now_fn(), float("inf")])
            try:
                result = write()
                status = result.get("status") if isinstance(result, dict) else result
                if status and status != "STATUS_SUCCEEDED":
                    self.waiting = None
                    radio._persist(s)
                    return False
            except Exception:
                log.warning("Radio playlist write response failed; checking saved contents", exc_info=True)
            finally:
                s.mutation_windows[-1][1] = radio.ctx.now_fn() + 3
            confirmed = radio._remote_tracks(s)
            actual = [t["videoId"] for t in confirmed]
            if not self._accept(radio, s, actual):
                return False
            if self.waiting is not None and tuple(actual) != self.waiting:
                return False
            self.confirmed = tuple(actual)
            self.waiting = None
            radio._publish(s, [s.candidates.by_video[v] for v in actual])
            s.refresh_needed = True
            radio._persist(s)
        return False
