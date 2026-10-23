"""Shared retrieval rule for playlists that have not been explicitly adopted.

The view owns provenance: moving a generated playlist to another display group
does not make it independent taste evidence. Bind the quarantine group parameter.
"""

QUARANTINED_PLAYLIST_IDS_SQL = (
    "SELECT p.id FROM playlists p "
    "JOIN quarantined_playlists g ON g.ytm=p.scope_ref WHERE g.name=?"
)
