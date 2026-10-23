"""Indexed, retractable recent evidence and bounded feedback lookups."""


def statements():
    yield 'CREATE INDEX ix_rec_feedback_key ON rec_feedback(item_key,kind)'
    yield 'CREATE INDEX ix_rec_feedback_recent ON rec_feedback(kind,created_at DESC)'
    yield 'CREATE INDEX ix_player_event_recent ON player_events(at DESC,id DESC)'
    yield ('CREATE TABLE rec_recent_signals (kind TEXT NOT NULL,item TEXT NOT NULL,'
           'identity_key TEXT NOT NULL,at REAL NOT NULL,weight REAL NOT NULL,'
           'PRIMARY KEY(kind,item,identity_key))')
    yield ('CREATE INDEX ix_rec_recent_signal_time ON rec_recent_signals'
           '(kind,at DESC,item,identity_key)')
    for event, prefix in (('INSERT', 'NEW'), ('DELETE', 'OLD'), ('UPDATE', 'NEW')):
        when = (' WHEN OLD.video_id IS NOT NEW.video_id OR OLD.position IS NOT NEW.position '
                'OR OLD.duration IS NOT NEW.duration OR OLD.kind IS NOT NEW.kind '
                'OR OLD.at IS NOT NEW.at') if event == 'UPDATE' else ''
        yield (f'CREATE TRIGGER rec_capture_player_events_{event.lower()} AFTER {event} ON player_events{when} '
               f"BEGIN INSERT INTO rec_change_queue(kind,item) VALUES ('player',CAST({prefix}.id AS TEXT)) "
               'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END')
    yield ('CREATE TRIGGER rec_capture_feedback_provenance AFTER UPDATE ON rec_feedback '
           'WHEN OLD.reason IS NOT NEW.reason OR OLD.created_at IS NOT NEW.created_at '
           'OR OLD.artist_id IS NOT NEW.artist_id BEGIN '
           "INSERT INTO rec_change_queue(kind,item) VALUES ('feedback',json_array(NEW.surface,NEW.item_key,NEW.scope)) "
           'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
