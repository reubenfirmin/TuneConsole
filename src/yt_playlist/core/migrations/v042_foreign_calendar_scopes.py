"""Index affected foreign-calendar dates without changing retained observations."""


def statements():
    yield """CREATE INDEX ix_listening_observation_source_scope ON listening_observations(
        identity_id,identity_key,source_day,timezone) WHERE precision='calendar_day'"""


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
