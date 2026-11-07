"""Split release evidence, publication occurrences and frozen share membership."""
import json
from pathlib import Path
import re
import sqlite3

from yt_playlist.core import album_tracklists
from .runner import MigrationError


RETIRED = {'album_tracks','album_fetch_state','album_share_snapshots','album_tracklists','album_track_links',
           'ix_album_track_entry','ix_album_track_recording','ix_album_track_incidental','ix_rec_album_cursor','album_track_links_delete'}


def transform(sql):
    match = re.match(r'CREATE (?:TABLE|VIEW|(?:UNIQUE )?INDEX|TRIGGER)\s+"?(\w+)"?',sql.lstrip(),re.I)
    if match and (match[1] in RETIRED or re.match(r'rec_(?:capture|evidence)_album_tracks_',match[1])):
        return ''
    if match and match[1]=='albums' and re.match(r'CREATE TABLE\b',sql.lstrip(),re.I):
        end = sql.rindex(')')
        return sql[:end]+',current_tracklist_id INTEGER,FOREIGN KEY(current_tracklist_id,id) REFERENCES album_tracklists(id,album_id) DEFERRABLE INITIALLY DEFERRED'+sql[end:]
    return sql


def definitions():
    pending = ''
    for line in Path(__file__).with_name('schema_album_occurrences.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete album occurrence schema')


def fences():
    useful = ("EXISTS(SELECT 1 FROM album_tracklists t WHERE t.id=NEW.current_tracklist_id AND t.album_id=NEW.id "
        "AND t.status='complete' AND t.expected_count>0 AND t.expected_count="
        '(SELECT count(*) FROM album_track_occurrences o WHERE o.tracklist_id=t.id))')
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER album_current_tracklist_{event.lower()} BEFORE {event} ON albums '
               f'WHEN NEW.current_tracklist_id IS NOT NULL AND NOT ({useful}) '
               "BEGIN SELECT RAISE(ABORT,'Album current tracklist is not a complete owned publication'); END")
        yield (f'CREATE TRIGGER album_source_agreement_{event.lower()} BEFORE {event} ON album_occurrence_sources '
            'WHEN NOT EXISTS(SELECT 1 FROM album_track_occurrences o JOIN catalog_entries e ON e.id=NEW.entry_id '
            'WHERE o.id=NEW.occurrence_id AND o.recording_id=e.recording_id AND '
            "((NEW.provider='youtube' AND e.video_id IS NOT NULL) OR (NEW.provider='local' AND e.source_ref LIKE 'local:%') OR "
            "(NEW.provider='unresolved' AND e.source_ref IS NOT NULL AND e.source_ref NOT LIKE 'local:%'))) "
            "BEGIN SELECT RAISE(ABORT,'Album occurrence source disagrees with its subject or provider'); END")
        yield (f'CREATE TRIGGER album_tracklist_provider_{event.lower()} BEFORE {event} ON album_tracklists '
            'WHEN NOT EXISTS(SELECT 1 FROM album_provider_ids p WHERE p.provider=NEW.provider AND p.provider_id=NEW.provider_id AND p.album_id=NEW.album_id) '
            "BEGIN SELECT RAISE(ABORT,'Album tracklist belongs to another provider representation'); END")
        yield (f'CREATE TRIGGER album_share_source_{event.lower()} BEFORE {event} ON album_share_items '
            'WHEN NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM album_occurrence_sources s '
            'WHERE s.occurrence_id=NEW.occurrence_id AND s.entry_id=NEW.entry_id) '
            "BEGIN SELECT RAISE(ABORT,'Frozen album source does not belong to its occurrence'); END")
    yield ("CREATE TRIGGER album_tracklist_finish BEFORE UPDATE ON album_tracklists WHEN NEW.status IN ('complete','snapshot') AND ("
        'NEW.expected_count IS NULL OR NEW.expected_count<>(SELECT count(*) FROM album_track_occurrences WHERE tracklist_id=NEW.id) OR '
        "(NEW.status='complete' AND NEW.expected_count=0) OR (NEW.order_status='known' AND EXISTS(SELECT 1 FROM album_track_occurrences WHERE tracklist_id=NEW.id AND position IS NULL)) OR "
        "(NEW.order_status='unknown' AND EXISTS(SELECT 1 FROM album_track_occurrences WHERE tracklist_id=NEW.id AND position IS NOT NULL)) OR "
        "(NEW.order_status='known' AND NEW.expected_count>0 AND ((SELECT min(position) FROM album_track_occurrences WHERE tracklist_id=NEW.id)<>0 OR "
        '(SELECT max(position) FROM album_track_occurrences WHERE tracklist_id=NEW.id)<>NEW.expected_count-1))) '
        "BEGIN SELECT RAISE(ABORT,'Album tracklist count or captured order is incomplete'); END")
    yield ("CREATE TRIGGER album_tracklist_initial BEFORE INSERT ON album_tracklists WHEN NEW.status<>'building' "
           "BEGIN SELECT RAISE(ABORT,'Album tracklists must be built before publication'); END")
    yield ("CREATE TRIGGER album_tracklist_frozen BEFORE UPDATE ON album_tracklists WHEN OLD.status<>'building' AND "
        '(NEW.id<>OLD.id OR NEW.provider<>OLD.provider OR NEW.provider_id<>OLD.provider_id OR NEW.revision<>OLD.revision OR NEW.capture_kind<>OLD.capture_kind OR '
        'NEW.status<>OLD.status OR NEW.order_status<>OLD.order_status OR NEW.expected_count IS NOT OLD.expected_count OR NEW.fetched_at IS NOT OLD.fetched_at) '
        "BEGIN SELECT RAISE(ABORT,'Published album capture cannot change'); END")
    for event,side in (('INSERT','NEW'),('DELETE','OLD')):
        retained = ('' if event=='INSERT' else " AND (capture_kind<>'provider' OR EXISTS(SELECT 1 FROM albums a WHERE a.current_tracklist_id=album_tracklists.id) "
            'OR EXISTS(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=album_tracklists.id))')
        yield (f'CREATE TRIGGER album_occurrence_frozen_{event.lower()} BEFORE {event} ON album_track_occurrences '
            f"WHEN EXISTS(SELECT 1 FROM album_tracklists WHERE id={side}.tracklist_id AND status<>'building'{retained}) "
            "BEGIN SELECT RAISE(ABORT,'Published album occurrences are retained'); END")
    yield ("CREATE TRIGGER album_occurrence_frozen_update BEFORE UPDATE ON album_track_occurrences WHEN "
        "EXISTS(SELECT 1 FROM album_tracklists WHERE id=OLD.tracklist_id AND status<>'building') AND "
        '(NEW.id<>OLD.id OR NEW.tracklist_id<>OLD.tracklist_id OR NEW.position IS NOT OLD.position OR NEW.disc_number IS NOT OLD.disc_number OR '
        'NEW.track_number IS NOT OLD.track_number OR NEW.captured_title IS NOT OLD.captured_title OR NEW.captured_credit IS NOT OLD.captured_credit OR '
        'NEW.raw_identity_key IS NOT OLD.raw_identity_key OR NEW.raw_source_ref IS NOT OLD.raw_source_ref OR NEW.captured_source IS NOT OLD.captured_source OR NEW.legacy_row_id IS NOT OLD.legacy_row_id) '
        "BEGIN SELECT RAISE(ABORT,'Published album occurrence capture cannot change'); END")
    yield ("CREATE TRIGGER album_occurrence_resolution BEFORE UPDATE ON album_track_occurrences WHEN EXISTS("
        'SELECT 1 FROM album_occurrence_sources s JOIN catalog_entries e ON e.id=s.entry_id '
        'WHERE s.occurrence_id=NEW.id AND e.recording_id IS NOT NEW.recording_id) '
        "BEGIN SELECT RAISE(ABORT,'Album occurrence resolution disagrees with its sources'); END")
    yield ("CREATE TRIGGER album_source_reassociation AFTER UPDATE OF recording_id ON catalog_entries WHEN NEW.recording_id<>OLD.recording_id BEGIN "
        'UPDATE album_track_occurrences SET recording_id=NEW.recording_id WHERE id IN '
        '(SELECT occurrence_id FROM album_occurrence_sources WHERE entry_id=NEW.id); END')
    for event in ('UPDATE','DELETE'):
        retained = ('' if event=='UPDATE' else " AND (t.capture_kind<>'provider' OR EXISTS(SELECT 1 FROM albums a WHERE a.current_tracklist_id=t.id) "
            'OR EXISTS(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=t.id))')
        yield (f'CREATE TRIGGER album_source_frozen_{event.lower()} BEFORE {event} ON album_occurrence_sources '
            f"WHEN EXISTS(SELECT 1 FROM album_track_occurrences o JOIN album_tracklists t ON t.id=o.tracklist_id WHERE o.id=OLD.occurrence_id AND t.status<>'building'{retained}) "
            "BEGIN SELECT RAISE(ABORT,'Published album source evidence is retained'); END")
    for table in ('album_share_snapshots','album_share_items'):
        # Alias consolidation may move the release owner, but cannot edit frozen
        # source choices. The composite FK continues to check tracklist ownership.
        condition = ' WHEN NEW.id<>OLD.id OR NEW.tracklist_id<>OLD.tracklist_id OR NEW.captured_provider_id<>OLD.captured_provider_id OR NEW.title<>OLD.title OR NEW.created_at IS NOT OLD.created_at' if table=='album_share_snapshots' else ''
        yield (f'CREATE TRIGGER {table}_frozen BEFORE UPDATE ON {table}{condition} '
               "BEGIN SELECT RAISE(ABORT,'Frozen album share cannot change'); END")
        yield (f'CREATE TRIGGER {table}_retained BEFORE DELETE ON {table} '
               "BEGIN SELECT RAISE(ABORT,'Frozen album share is retained'); END")
    for table in ('album_item_observations','album_track_occurrences','album_occurrence_sources'):
        for event in ('INSERT','UPDATE','DELETE'):
            yield (f'CREATE TRIGGER rec_capture_{table}_{event.lower()} AFTER {event} ON {table} BEGIN '
                "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; "
                'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')
    yield ("CREATE TRIGGER rec_capture_album_publication AFTER UPDATE OF current_tracklist_id ON albums WHEN OLD.current_tracklist_id IS NOT NEW.current_tracklist_id BEGIN "
        "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; "
        'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')


def statements():
    yield from definitions()
    yield from fences()


def apply(conn,context):
    original = list(conn.execute('SELECT * FROM album_tracks ORDER BY id'))
    states = {r['album_id']:r for r in conn.execute('SELECT * FROM album_fetch_state')}
    shares = [(row['browse_id'],row['title'],json.loads(row['video_ids_json'])) for row in conn.execute('SELECT * FROM album_share_snapshots')]
    albums = list(conn.execute('SELECT * FROM albums'))
    highwater = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='albums'").fetchone()
    definition = conn.execute("SELECT sql FROM sqlite_schema WHERE name='albums'").fetchone()[0]
    attached = [r[0] for r in conn.execute("SELECT sql FROM sqlite_schema WHERE tbl_name='albums' AND type IN ('index','trigger') AND sql IS NOT NULL")]
    for name in ('album_tracklists','album_track_links'):
        conn.execute('DROP VIEW '+name)
    for name in ('album_tracks','album_fetch_state','album_share_snapshots','albums'):
        conn.execute('DROP TABLE '+name)
    conn.execute(transform(definition))
    columns = tuple(albums[0].keys()) if albums else ()
    if columns:
        conn.executemany(f'INSERT INTO albums({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                         [tuple(r) for r in albums])
    if highwater:
        conn.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 'albums',? WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='albums')",(highwater[0],))
        conn.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='albums'",(highwater[0],))
    for sql in definitions():
        conn.execute(sql)
    for sql in attached:
        conn.execute(sql)
    # Install guards before converting data so backfill uses the same contract.
    for sql in fences():
        conn.execute(sql)
    grouped = {}
    for row in original:
        if row['is_full']:
            grouped.setdefault(row['album_id'],[]).append(row)
        else:
            conn.execute('INSERT INTO album_item_observations(album_id,entry_id,provider,recording_id,raw_identity_key,legacy_row_id,captured_position,captured_disc_number,captured_track_number) VALUES (?,?,?,?,?,?,?,?,?)',
                (row['album_id'],row['entry_id'],row['source'],row['recording_id'],row['legacy_track_key'],row['id'],row['position'],row['disc_number'],row['track_number']))
    for aid in states:
        grouped.setdefault(aid,[])
    for aid,rows in grouped.items():
        provider = conn.execute("SELECT provider_id FROM album_provider_ids WHERE album_id=? AND provider='youtube' ORDER BY is_primary DESC,provider_id LIMIT 1",(aid,)).fetchone()
        if provider is None:
            raise MigrationError(f'Album {aid} has no retained provider representation')
        state = states.get(aid)
        known = sum(r['position'] is not None for r in rows)
        tracklist = album_tracklists.begin(conn,aid,provider[0],capture_kind='legacy_full',
            order_status='known' if known==len(rows) else 'unknown' if not known else 'mixed',
            expected_count=state['expected_track_count'] if state else None,fetched_at=state['fetched_at'] if state else None)
        for row in rows:
            album_tracklists.occurrence(conn,tracklist,entry_id=row['entry_id'],recording_id=row['recording_id'],
                raw_identity_key=row['legacy_track_key'],position=row['position'],disc_number=row['disc_number'],track_number=row['track_number'],captured_source=row['source'],legacy_row_id=row['id'])
        album_tracklists.finish(conn,tracklist,complete=bool(rows and state and state['complete'] and state['expected_track_count']==len(rows)))
    for browse,title,videos in shares:
        owner = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?",(browse,)).fetchone()
        if owner is None:
            raise MigrationError(f'Album share {browse} has no retained release')
        album_tracklists.freeze(conn,owner[0],browse,title,videos,legacy=True)
