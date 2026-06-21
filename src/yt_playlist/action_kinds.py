"""Central registry of action kinds recorded in the actions log.

Every mutation records an Action with one of these kinds. Single-sourcing the
constants and the undoable set here — rather than repeating string literals across
the executor, the web routes, and the actions template — keeps the taxonomy in one
place and lets the undoable check evolve without hunting down duplicated lists.
"""

PLAN = "plan"                    # a planned (then executed) delete/move/merge of one playlist into another
APPLY_MERGE = "apply_merge"      # N-way merge editor applied: keepers set to a result, droppers deleted
MOVE_IDENTITY = "move_identity"  # playlist copied/moved to another identity
DELETE_EMPTY = "delete_empty"    # an empty playlist deleted
DELETE_PLAYLIST = "delete_playlist"  # a playlist deleted outright (e.g. from the Playlists tab)
COPY_PLAYLIST = "copy_playlist"  # a playlist duplicated into a new one (same identity)
ADD_TRACKS = "add_tracks"        # one or more tracks added to an existing playlist (e.g. alternate versions)
REMOVE_TRACK = "remove_track"    # a single track removed from a playlist
UNDO = "undo"                    # an undo of a previous action (itself not undoable)

# Kinds whose effects can be reversed from the Actions page.
UNDOABLE_KINDS = (PLAN, APPLY_MERGE, MOVE_IDENTITY, DELETE_EMPTY, DELETE_PLAYLIST, COPY_PLAYLIST)


def is_undoable(kind) -> bool:
    return kind in UNDOABLE_KINDS
