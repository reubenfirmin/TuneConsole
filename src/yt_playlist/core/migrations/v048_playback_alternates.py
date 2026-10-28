"""Retain account-scoped, provider-witnessed playback replacements."""


def statements():
    yield """CREATE TABLE youtube_playback_alternates (
        identity_id INTEGER NOT NULL REFERENCES identities(id),
        requested_video_id TEXT NOT NULL REFERENCES youtube_items(video_id),
        video_id TEXT NOT NULL REFERENCES youtube_items(video_id),
        same_recording INTEGER NOT NULL CHECK(same_recording IN (0,1)),
        evidence_json TEXT NOT NULL,
        observed_at REAL NOT NULL,
        PRIMARY KEY(identity_id,requested_video_id),
        CHECK(requested_video_id<>video_id)
    )"""


def apply(conn, context):
    for statement in statements():
        conn.execute(statement)
