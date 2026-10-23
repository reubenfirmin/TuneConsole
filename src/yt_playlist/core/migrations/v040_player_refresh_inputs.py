"""Keep player telemetry out of the durable recommendation refresh queue."""


def statements():
    relevant = "{side}.kind IN ('track_exit','bye','ended')"
    for event, side in (('INSERT', 'NEW'), ('DELETE', 'OLD'), ('UPDATE', 'NEW')):
        name = f'rec_capture_player_events_{event.lower()}'
        yield f'DROP TRIGGER {name}'
        condition = relevant.format(side=side)
        if event == 'UPDATE':
            changed = ' OR '.join(f'OLD.{column} IS NOT NEW.{column}' for column in
                                 ('video_id', 'position', 'duration', 'kind', 'at'))
            condition = f'({condition} OR {relevant.format(side="OLD")}) AND ({changed})'
        yield (f'CREATE TRIGGER {name} AFTER {event} ON player_events WHEN {condition} BEGIN '
               "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
               'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
