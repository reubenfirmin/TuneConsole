"""Library rating policy shared by catalog and recommendation projections.

The active master account's recorded rating, including confirmed writes not yet
synced, takes precedence over cached Liked Music membership. Queries alias tracks
as ``t`` when executing this domain-owned projection.
"""
LIKED_EXISTS = ("COALESCE((SELECT r.status='LIKE' FROM item_ratings r "
                "JOIN accounts i ON i.id=r.account_id WHERE i.active=1 AND i.is_master=1 "
                "AND r.video_id=t.video_id), EXISTS(SELECT 1 FROM playlist_tracks lpt "
                "JOIN playlists lpl ON lpl.id = lpt.playlist_id "
                "JOIN tracks lt ON lt.id = lpt.track_id "
                "WHERE lpl.provider_playlist_id = 'LM' AND lt.video_id = t.video_id))")
