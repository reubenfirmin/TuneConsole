"""Bound frozen source choices to the immutable captured occurrence sequence."""
from .runner import MigrationError


def fences():
    yield ("CREATE TRIGGER album_share_membership_insert BEFORE INSERT ON album_share_items "
        "WHEN EXISTS(SELECT 1 FROM album_share_items i WHERE i.snapshot_id=NEW.snapshot_id AND i.position=NEW.position) "
        "OR NOT EXISTS(SELECT 1 FROM album_share_snapshots s "
        "JOIN album_tracklists t ON t.id=s.tracklist_id "
        "JOIN album_track_occurrences o ON o.tracklist_id=t.id AND o.id=NEW.occurrence_id "
        "WHERE s.id=NEW.snapshot_id AND t.id=NEW.tracklist_id "
        "AND t.status IN ('complete','snapshot') AND t.order_status='known' "
        "AND NEW.position<t.expected_count AND o.position=NEW.position) "
        "BEGIN SELECT RAISE(ABORT,'Frozen album share membership cannot extend or reorder its capture'); END")


def apply(conn, context):
    # Do not silently reinterpret a snapshot already changed by an older writer.
    invalid = conn.execute(
        "SELECT s.id FROM album_share_snapshots s JOIN album_tracklists t ON t.id=s.tracklist_id "
        "WHERE t.status NOT IN ('complete','snapshot') OR t.order_status<>'known' "
        "OR t.expected_count IS NULL OR t.expected_count<>"
        "(SELECT count(*) FROM album_share_items i WHERE i.snapshot_id=s.id) "
        "OR EXISTS(SELECT 1 FROM album_share_items i JOIN album_track_occurrences o ON o.id=i.occurrence_id "
        "WHERE i.snapshot_id=s.id AND (i.position>=t.expected_count OR o.position IS NOT i.position)) "
        "LIMIT 1").fetchone()
    if invalid:
        raise MigrationError(f'Frozen album share {invalid[0]} has changed membership; original rows retained')
    for sql in fences():
        conn.execute(sql)
