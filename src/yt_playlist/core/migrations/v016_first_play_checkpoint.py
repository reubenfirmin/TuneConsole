"""Catalog conversion retired cached keys; rebuild them and their checkpoint together."""


def apply(conn, context):
    conn.execute("DELETE FROM trend_first_play")
    conn.execute("DELETE FROM settings WHERE key IN "
                 "('trend_first_play_watermark','trend_first_play_source')")
