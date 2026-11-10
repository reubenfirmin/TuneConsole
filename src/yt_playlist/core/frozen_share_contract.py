"""Future inserts must follow the immutable captured occurrence sequence."""


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
