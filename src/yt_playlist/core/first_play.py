"""Invalidate the derived first-play index in its caller's catalog transaction."""


def invalidate(conn):
    conn.execute("DELETE FROM trend_first_play")
    conn.execute("DELETE FROM settings WHERE key IN "
                 "('trend_first_play_watermark','trend_first_play_source')")
