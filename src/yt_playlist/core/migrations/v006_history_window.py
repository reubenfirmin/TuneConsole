def apply(conn, context):
    conn.execute('DROP TABLE IF EXISTS history_window')
