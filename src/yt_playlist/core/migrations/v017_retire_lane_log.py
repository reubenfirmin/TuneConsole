"""Retire unversioned lane-only evidence; actual exposures now use the shared ledger."""


def apply(conn, context):
    # Keep the empty historical schema for frozen catalog validators and upgrade paths.
    # No runtime writer or reader uses it after this migration.
    conn.execute("DELETE FROM rec_lane_impressions")
