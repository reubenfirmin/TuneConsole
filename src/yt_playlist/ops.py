"""PlaylistOps — a service facade over the executor.

Route handlers shouldn't thread the store, the per-identity client provider, and the clock
through every executor call. PlaylistOps binds those three once and exposes the high-level
playlist operations (merge, dupe-delete, keep-one, delete, move, undo) so the routes stay thin
and the orchestration lives in one place.

Methods raise ValueError for expected/user-facing problems (missing playlist, no client, etc.);
callers turn those into messages and let unexpected exceptions bubble to their own handlers.
"""
import logging

from yt_playlist import analysis
from yt_playlist import sync as sync_mod
from yt_playlist.executor import (
    MergePlan, add_tracks_to_playlist, apply_result, copy_into_playlist, copy_or_move_playlist,
    copy_playlist, create_playlist_from_album, delete_empty_playlist, delete_playlist,
    deserialize_plan, execute_planned, remove_track, rename_playlist,
    reorder_track, search_versions, store_plan, undo_action)
from yt_playlist.liked_music import LikedMusic

logger = logging.getLogger("yt_playlist.ops")


class PlaylistOps:
    def __init__(self, store, client_provider, now_fn):
        self.store = store
        self._client_provider = client_provider
        self.now_fn = now_fn

    def _clients(self):
        return self._client_provider()

    def _client_for(self, playlist):
        return self._clients().get(playlist.identity_id)

    # --- merge / duplicate resolution ---------------------------------------
    def apply_merge(self, playlist_ids, result_video_ids, keep) -> dict:
        return apply_result(self.store, self._clients(), playlist_ids, result_video_ids, keep, self.now_fn())

    def delete_dupe(self, source, target) -> str:
        """Plan + remote-verified delete of `source` (keeping `target`). Returns the deleted title."""
        if source == target:
            raise ValueError("source and target must differ")
        src, tgt = self.store.get_playlist(source), self.store.get_playlist(target)
        if src is None or tgt is None:
            raise ValueError("playlist no longer exists (already deleted?)")
        aid = store_plan(self.store, MergePlan(source, target, [], []), "delete",
                         src.ytm_playlist_id, self.now_fn())
        execute_planned(self.store, aid, self._clients(), self.now_fn())
        return src.title

    def keep_one(self, keep) -> tuple:
        """Delete every non-system sibling with the same track set as `keep`. Returns (deleted, errors)."""
        kept = self.store.get_playlist(keep)
        if kept is None:
            raise ValueError("kept playlist no longer exists")
        keep_keys = self.store.get_playlist_track_keys(keep)
        siblings = [p for p in self.store.get_playlists()
                    if p.id != keep and p.ytm_playlist_id not in analysis.SYSTEM_PLAYLIST_IDS
                    and self.store.get_playlist_track_keys(p.id) == keep_keys]
        deleted, errors = 0, []
        for sib in siblings:
            try:
                aid = store_plan(self.store, MergePlan(sib.id, keep, [], []), "delete",
                                 sib.ytm_playlist_id, self.now_fn())
                execute_planned(self.store, aid, self._clients(), self.now_fn())
                deleted += 1
            except ValueError as e:
                errors.append(str(e))
            except Exception:  # noqa: BLE001
                logger.exception("keep-one delete of %s failed", sib.ytm_playlist_id)
                errors.append(f"{sib.ytm_playlist_id}: unexpected error")
        return deleted, errors

    # --- deletes ------------------------------------------------------------
    def delete_empty(self, playlist_id) -> None:
        delete_empty_playlist(self.store, playlist_id, self._require_client(playlist_id), self.now_fn())

    def delete(self, playlist_id) -> None:
        delete_playlist(self.store, playlist_id, self._require_client(playlist_id), self.now_fn())

    def copy(self, playlist_ids, new_name) -> dict:
        if not playlist_ids:
            raise ValueError("no playlists to copy")
        # the new playlist is created under the first selected playlist's identity
        client = self._require_client(playlist_ids[0])
        return copy_playlist(self.store, playlist_ids, new_name, client, self.now_fn())

    def copy_into(self, source_ids, target_id) -> dict:
        """Copy the selected playlists' songs into an existing target playlist (the target's client)."""
        if not source_ids:
            raise ValueError("no playlists to copy")
        client = self._require_client(target_id)
        return copy_into_playlist(self.store, source_ids, target_id, client, self.now_fn())

    def create_playlist_from_album(self, browse_id, name="") -> dict:
        """Create a new playlist from an album's tracks, under the master account."""
        if not browse_id:
            raise ValueError("no album given")
        identity = self.store.get_master_identity()
        client = self._clients().get(identity.id)
        if client is None:
            raise ValueError("the master account isn't connected")
        return create_playlist_from_album(self.store, browse_id, name, client, self.now_fn(), identity.id)

    # --- alternate versions -------------------------------------------------
    def find_alternates(self, playlist_id, video_id) -> list:
        """Search YouTube for alternate versions of a track already in the playlist."""
        track = next((t for t in self.store.playlist_tracks_detail(playlist_id)
                      if t["video_id"] == video_id), None)
        if track is None:
            raise ValueError("track is not in this playlist")
        client = self._require_client(playlist_id)
        return search_versions(client, track["title"], track["artist"], exclude=video_id)

    def add_tracks(self, playlist_id, tracks, after_video_id=None) -> dict:
        pl = self.store.get_playlist(playlist_id)
        if pl is None:
            raise ValueError("playlist no longer exists")
        client = self._client_for(pl)
        if client is None:
            raise ValueError("no client for that identity")
        # Liked Music is YouTube-managed and won't accept directly-added tracks, so "Add" (from an
        # alternate version or a 'complete this playlist' suggestion) becomes a *like* — the only thing
        # that actually lands a song in Liked Music. Every other playlist adds normally. (Ordering is
        # meaningless for likes, so after_video_id is ignored on that path.)
        if LikedMusic.is_lm(pl):
            return LikedMusic(self.store).add(playlist_id, tracks, client, self.now_fn())
        return add_tracks_to_playlist(self.store, playlist_id, tracks, client, self.now_fn(),
                                      after_video_id)

    def remove_track(self, playlist_id, video_id) -> dict:
        pl = self.store.get_playlist(playlist_id)
        if pl is None:
            raise ValueError("playlist no longer exists")
        client = self._client_for(pl)
        if client is None:
            raise ValueError("no client for that identity")
        # Liked Music has no removable playlist item — "remove" unlikes the song instead.
        if LikedMusic.is_lm(pl):
            return LikedMusic(self.store).remove(playlist_id, video_id, client, self.now_fn())
        return remove_track(self.store, playlist_id, video_id, client, self.now_fn())

    def rename(self, playlist_id, title) -> dict:
        client = self._require_client(playlist_id)
        return rename_playlist(self.store, playlist_id, title, client, self.now_fn())

    def reorder_track(self, playlist_id, video_id, before_video_id) -> dict:
        client = self._require_client(playlist_id)
        return reorder_track(self.store, playlist_id, video_id, before_video_id, client, self.now_fn())

    def set_liked(self, video_id, on) -> dict:
        # The like-button path: like/unlike a song in your main account's Liked Music (LM is
        # per-account; we target master). Resolve the master client here, then hand the rate+mirror
        # to LikedMusic so all LM membership changes go through one primitive.
        master = next((i for i in self.store.get_identities() if i.is_master), None)
        if master is None:
            raise ValueError("no main account configured")
        client = self._clients().get(master.id)
        if client is None:
            raise ValueError("your main account isn’t connected")
        LikedMusic(self.store).set_rating(master.id, video_id, on, client)
        return {"liked": bool(on)}

    def _require_client(self, playlist_id):
        pl = self.store.get_playlist(playlist_id)
        if pl is None:
            raise ValueError("playlist no longer exists")
        client = self._client_for(pl)
        if client is None:
            raise ValueError("no client for that identity")
        return client

    # --- move between identities --------------------------------------------
    def move(self, playlist_id, target_identity, *, copy_only) -> dict:
        clients = self._clients()
        pl = self.store.get_playlist(playlist_id)
        if pl is None:
            raise ValueError("playlist no longer exists")
        src_client, tgt_client = clients.get(pl.identity_id), clients.get(target_identity)
        if src_client is None or tgt_client is None:
            raise ValueError("missing client for an identity")
        return copy_or_move_playlist(self.store, playlist_id, target_identity, src_client, tgt_client,
                                     self.now_fn(), delete_source=not copy_only)

    # --- undo ---------------------------------------------------------------
    def undo(self, action_id) -> None:
        clients = self._clients()
        undo_action(self.store, action_id, clients, self.now_fn())
        # best-effort targeted refresh of the undo target (only meaningful for planned deletes);
        # avoids a slow full re-sync. Non-plan kinds make deserialize_plan raise -> harmless skip.
        try:
            tgt = self.store.get_playlist(
                deserialize_plan(self.store.get_action(action_id)).plan.target_playlist_id)
            if tgt is not None and tgt.identity_id in clients:
                sync_mod.refresh_playlist(self.store, tgt.identity_id, clients[tgt.identity_id],
                                          tgt.ytm_playlist_id, tgt.title, self.now_fn())
        except Exception:  # noqa: BLE001 - best-effort refresh
            logger.exception("post-undo refresh failed (non-fatal)")
