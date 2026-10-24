"""Remove overrides for unsupported onboarding coverage and library thresholds."""


def apply(conn, context):
    conn.execute("DELETE FROM settings WHERE key IN "
                 "('rec_param:onboard_coverage_min','rec_param:onboard_library_min')")
