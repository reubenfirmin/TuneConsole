"""Correct play associations without changing the already installed catalog migration."""
from functools import lru_cache

from . import v008_recordings


KEY_LOOKUP = "COALESCE((SELECT id FROM recordings WHERE identity_key=NEW.identity_key),(SELECT MIN(recording_id) FROM recording_aliases WHERE alias=NEW.identity_key HAVING COUNT(DISTINCT recording_id)=1))"
KEY_LOOKUP = f'COALESCE((SELECT recording_id FROM recording_redirects WHERE retired_id={KEY_LOOKUP}),{KEY_LOOKUP})'
PLAY_LOOKUP = f"CASE WHEN NULLIF(trim(NEW.video_id),'') IS NOT NULL THEN (SELECT recording_id FROM youtube_items WHERE video_id=NEW.video_id) ELSE {KEY_LOOKUP} END"
FALLBACK = "CASE WHEN NULLIF(trim(NEW.video_id),'') IS NOT NULL AND EXISTS(SELECT 1 FROM recordings WHERE identity_key=NEW.identity_key) THEN '@unresolved-video:'||NEW.video_id ELSE identity_key END"
PLAY_TRIGGER = f'CREATE TRIGGER play_events_subject AFTER INSERT ON play_events BEGIN UPDATE play_events SET raw_identity_key=NEW.identity_key,recording_id={PLAY_LOOKUP} WHERE rowid=NEW.rowid; UPDATE play_events SET identity_key=COALESCE((SELECT identity_key FROM recordings WHERE id=play_events.recording_id),{FALLBACK}) WHERE rowid=NEW.rowid; END'


def apply(conn, context):
    conn.execute('DROP TRIGGER play_events_subject')
    conn.execute(PLAY_TRIGGER)
    # A blank video supplies no playback target. Resolve only a unique key, while an
    # unknown nonempty video must stay unresolved even if its text matches a recording.
    keys = dict(conn.execute('SELECT identity_key,id FROM recordings'))
    aliases = {}
    for alias, rid in conn.execute('SELECT alias,recording_id FROM recording_aliases'):
        aliases.setdefault(alias, set()).add(rid)
    redirects = dict(conn.execute('SELECT retired_id,recording_id FROM recording_redirects'))
    recording_keys = dict(conn.execute('SELECT id,identity_key FROM recordings'))
    videos = dict(conn.execute('SELECT video_id,recording_id FROM youtube_items'))
    changed = False
    for event_id, key, raw, video in conn.execute('SELECT id,identity_key,raw_identity_key,video_id FROM play_events WHERE recording_id IS NULL').fetchall():
        has_video = bool(video and video.strip())
        rid = videos.get(video) if has_video else keys.get(raw)
        if not has_video and rid is None and len(aliases.get(raw, ())) == 1:
            rid = next(iter(aliases[raw]))
        rid = redirects.get(rid, rid)
        effective = recording_keys[rid] if rid is not None else key
        if rid is None and has_video and key in keys:
            effective = '@unresolved-video:' + video
        if rid is not None or effective != key:
            conn.execute('UPDATE play_events SET identity_key=?,recording_id=? WHERE id=?', (effective, rid, event_id))
            changed = True
    if changed:
        # Rebuild predictions, retaining original observations, weights, feedback and
        # selection history. Snapshots made with the old associations cannot roll back.
        for table in ('rec_vectors', 'rec_content_vectors', 'rec_discovered_content_vectors',
                      'rec_artist_vectors', 'rec_artist_content_vectors', 'rec_proposals',
                      'trend_first_play'):
            conn.execute(f'DELETE FROM {table}')
        conn.execute("DELETE FROM settings WHERE key IN ('rec_content_model','rec_content_cov_bucket','rec_content_genre_revision')")
        conn.execute('UPDATE rec_model_selection_runs SET identity_version=0 WHERE identity_version=2')


@lru_cache(maxsize=1)
def expected_schema():
    return [(name, kind, PLAY_TRIGGER if name == 'play_events_subject' else sql)
            for name, kind, sql in v008_recordings.expected_schema()]


def validate(conn):
    v008_recordings.validate(conn, schema=expected_schema())
