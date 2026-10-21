from .v001_baseline import setting, stamp


def apply(conn, context):
    if not setting(conn, 'playlist_created_at_backfilled'):
        conn.execute("WITH creations AS (SELECT json_extract(CASE WHEN json_valid(undo_json) THEN undo_json ELSE '{}' END,'$.new_ytm') ytm,MIN(COALESCE(executed_at,created_at)) at FROM actions WHERE kind='copy_playlist' AND status='executed' GROUP BY ytm) UPDATE playlists SET created_at=COALESCE((SELECT at FROM creations WHERE ytm=playlists.ytm_playlist_id),(SELECT created_at FROM rec_recipes WHERE playlist_ytm=playlists.ytm_playlist_id)) WHERE created_at IS NULL")
        stamp(conn, 'playlist_created_at_backfilled')
    conn.execute("UPDATE playlists SET created_at_source='local' WHERE created_at IS NOT NULL AND created_at_source IS NULL")
