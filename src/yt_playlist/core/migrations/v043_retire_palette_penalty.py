"""Remove the saved override for the unsupported genre familiarity control."""


def apply(conn, context):
    conn.execute("DELETE FROM settings WHERE key='rec_param:palette_absence_penalty'")
