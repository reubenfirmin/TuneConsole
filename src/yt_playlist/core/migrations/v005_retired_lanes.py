def apply(conn, context):
    # Historical retirement, deliberately independent of the runtime lane registry.
    conn.execute("DELETE FROM rec_weights WHERE axis LIKE 'lane:%' AND axis NOT IN ('lane:neighbourhood','lane:deep_cut','lane:rotation')")
