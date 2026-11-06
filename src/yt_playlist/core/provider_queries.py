"""Local query prerequisites, separate from provider absence and network retry state."""


def lastfm_ready(title, artist):
    return bool((title or '').strip() and (artist or '').strip())


def deferred_reason(provider, track):
    if provider == 'lastfm' and not lastfm_ready(track.get('title'), track.get('artist')):
        return 'Waiting for title and artist metadata.'
    return ''


def lastfm_ready_sql(title='t.title', artist='t.artist'):
    # SQLite's default TRIM removes only ASCII spaces. Match Python str.strip,
    # including tabs, newlines and non-breaking/Unicode spaces in imported tags.
    whitespace = 'char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)'
    return ' AND '.join(f"trim(COALESCE({column},''),{whitespace})<>''" for column in (title, artist))


def identity(conn, entry_id, field):
    if field not in ('title', 'artist'):
        return None
    row = conn.execute('SELECT recording_id,title,artist FROM catalog_tracks WHERE id=?',
                       (entry_id,)).fetchone()
    return tuple(row) if row else None


def resume_changed(conn, entry_id, field, previous):
    """A changed query can run now; retain findings, attempts and unrelated retries.

    Metadata callers own the surrounding transaction. This changes admission only,
    and does not assert that a provider request has succeeded or established absence.
    """
    current = identity(conn, entry_id, field) if previous is not None else None
    if current is not None and current != previous:
        conn.execute("UPDATE enrichment_provider_state SET failures=0,retry_at=0 "
                     "WHERE recording_id=? AND provider='lastfm' AND status='retry'",
                     (current[0],))
