"""Make a disposable copy of a local library for testing."""
from contextlib import closing, ExitStack
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import time


def copy_database(source, target):
    uri = source.as_uri() + '?mode=ro'
    wal = source.with_name(source.name + '-wal')
    with ExitStack() as resources:
        donor = resources.enter_context(closing(sqlite3.connect(uri, uri=True, timeout=2)))
        unchanged_since = None
        try:
            donor.execute('BEGIN')
            donor.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone()
        except sqlite3.OperationalError as error:
            if (error.sqlite_errorcode & 0xff) not in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY) or wal.exists():
                raise
            # SQLite cannot create WAL bookkeeping in the read-only mount
            # after the application closes and removes its WAL sidecars.
            donor.close()
            unchanged_since = source.stat().st_mtime_ns
            donor = resources.enter_context(closing(sqlite3.connect(uri + '&immutable=1', uri=True)))

        copy = resources.enter_context(closing(sqlite3.connect(target)))
        deadline = time.monotonic() + 2

        def progress(status, _remaining, _total):
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
                raise RuntimeError('SQLite source is busy; retry the test-library refresh')

        copy.execute('PRAGMA synchronous=OFF')
        donor.backup(copy, progress=progress)
        if unchanged_since is not None and (wal.exists() or source.stat().st_mtime_ns != unchanged_since):
            raise RuntimeError('SQLite source changed during a closed-library refresh; retry the test-library refresh')
        copy.execute('PRAGMA journal_mode=DELETE')


def rebase(connection, source, logical):
    from tests.library_image.file_references import (action_backup_references, description_access_in_provider_row, description_access_receipts,
                                                     publication_file_references, quote, setting_file_references)
    from yt_playlist.core import action_backup_inventory, paid_description_access, publication_files
    from yt_playlist.core.policy_documents import coordinate_updates

    fields = [(table, column) for table, column in connection.execute(
        "SELECT s.name,p.name FROM sqlite_schema s JOIN pragma_table_info(s.name) p "
        "WHERE s.type='table' AND (p.name='filename' OR substr(p.name,-9)='_filename')")
        if (table, column) != ('action_backups', 'filename')]
    old = str(source.parent) + '/'
    new = str(logical.parent) + '/'
    prefixes = dict(old=old, new=new, database=str(source) + '.', logical=str(logical) + '.')
    paths = set()
    connection.execute('BEGIN')
    publications = {}
    for table, identity, path in publication_file_references(connection):
        if path.is_relative_to(source.parent):
            paths.add(path)
            publications.setdefault(table, {})[identity] = str(relocate(path, source, logical))
    with connection, coordinate_updates(connection, {*publications, *(table for table, _ in fields)}):
        for table, column in fields:
            table, column = quote(table), quote(column)
            condition = f'substr({column},1,length(:old))=:old'
            paths.update(Path(value) for value, in connection.execute(
                f'SELECT DISTINCT {column} FROM {table} WHERE {condition}', prefixes))
            connection.execute(
                f'UPDATE {table} SET {column}=CASE WHEN substr({column},1,length(:database))=:database '
                f'THEN :logical || substr({column},length(:database)+1) '
                f'ELSE :new || substr({column},length(:old)+1) END WHERE {condition}', prefixes)
        provider_row = description_access_in_provider_row(connection)
        for key, filename, _ in list(description_access_receipts(connection, provider_row=provider_row)):
            path = Path(filename)
            if path.is_relative_to(source.parent):
                paths.add(path)
                replacement = str(relocate(path, source, logical))
                if provider_row:
                    paid_description_access.rebase_receipt(connection, key, replacement)
                else:
                    connection.execute("UPDATE settings SET value=json_set(value,'$.evidence_filename',?) "
                                       "WHERE key=?", (replacement, key))
        for key, field, path in list(setting_file_references(connection)):
            if path.is_relative_to(source.parent):
                paths.add(path)
                connection.execute('UPDATE settings SET value=json_set(value,?,?) WHERE key=?',
                                   (field, str(relocate(path, source, logical)), key))
        for action, position, path in list(action_backup_references(connection)):
            if path.is_relative_to(source.parent):
                paths.add(path)
                action_backup_inventory.relocate(connection, action, position, str(relocate(path, source, logical)))
        for table, replacements in publications.items():
            publication_files.relocate(connection, table, replacements)
    return paths


def expire_superseded_vibe_models(connection):
    from tests.library_image.file_references import tables
    from yt_playlist.core import vibe_models

    if not vibe_models.available(connection):
        return 0
    if 'prediction_model_generations' in tables(connection):
        update = ("UPDATE prediction_provenance SET state='expired' WHERE state='ready' "
                  "AND prediction_id IN (SELECT prediction_id FROM prediction_model_generations) "
                  "AND prediction_id NOT IN (SELECT prediction_id FROM recording_vibes WHERE prediction_id IS NOT NULL)")
    else:
        update = ("UPDATE predictions SET provenance_state='expired' "
                  "WHERE provenance_state='ready' AND model_generation IS NOT NULL "
                  "AND id NOT IN (SELECT prediction_id FROM recording_vibes WHERE prediction_id IS NOT NULL)")

    # The expiry triggers look up each updated row by prediction_id. Give those
    # checks an index for this one bulk update on the private copy.
    connection.execute('BEGIN')
    with connection:
        connection.execute('CREATE INDEX capture_selected_predictions ON recording_vibes(prediction_id)')
        expired = connection.execute(update).rowcount
        connection.execute('DROP INDEX capture_selected_predictions')
    return expired


def capture(source, target):
    from tests.library_image import file_references as references
    from tests.library_image.schema_coordinates import read_schema
    from yt_playlist.core import fao, file_paths, playback_inbox_file, wiki_cache
    from yt_playlist.core.evidence_paths import record_id
    from yt_playlist.core.playback_inbox_upgrade import legacy_path

    started = time.monotonic()
    database = target / 'state.db'
    logical = Path('/library') / database.name
    inbox = playback_inbox_file.inbox_path(source)
    copied_inbox = playback_inbox_file.inbox_path(database)
    predecessor = legacy_path(source)
    inbox_rows = None
    paths = set()
    # Copy the queue before SQL. A capture acknowledged in between can be
    # present in both snapshots, where its SQL receipt makes replay safe.
    try:
        fao.write(copied_inbox, fao.read(inbox))
    except FileNotFoundError:
        if predecessor.is_file():
            old_copy = legacy_path(database)
            copy_database(predecessor, old_copy)
            with closing(sqlite3.connect(old_copy)) as connection:
                inbox_rows = connection.execute('SELECT count(*) FROM playback_feedback_inbox').fetchone()[0]
                paths.update(rebase(connection, source, logical))
    else:
        inbox_rows = len(playback_inbox_file.read(copied_inbox)['captures'])

    copy_database(source, database)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA synchronous=OFF')
        schema = read_schema(connection)
        expired_predictions = expire_superseded_vibe_models(connection)
        # Owner lookups derive their roots from the copy's filename. Translate
        # those relative paths back to the donor after reading their SQL indexes.
        for _, _, selected in references.owners(connection):
            for item in selected:
                if isinstance(item, references.MapReference):
                    # Copy the small archive tree without opening map pages.
                    root = relocate(item.root, database, source)
                    paths.update(path for path in root.glob('**/*') if not path.name.startswith('.'))
                elif isinstance(item, references.EvidenceReferences):
                    # A numeric file published after the SQL copy can collide
                    # with the first new receipt issued by the private database.
                    root = relocate(item.root, database, source)
                    for path in root.glob('*/*/*.json'):
                        identity = record_id(path)
                        if identity is None or identity in item.owned:
                            paths.add(path)
                    paths.update(root.glob('*/*/*.bin'))
                    paths.add(root / '.budget')
                else:
                    paths.add(relocate(item.path, database, source))
        archive = file_paths.root_for_database(source, None, 'archives')
        paths.update(path for path in archive.glob('**/*') if not path.name.startswith('.'))
        paths.update(rebase(connection, source, logical))
    wiki_cards = wiki_cache.path_for(source)
    if wiki_cards.is_file():
        paths.add(wiki_cards)
    sql_seconds = time.monotonic() - started

    copied = skipped = 0
    directories = {}
    for path in sorted(paths):
        if not path.is_relative_to(source.parent) or '..' in path.parts:
            skipped += 1
            continue
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            skipped += 1
            continue
        if stat.S_ISDIR(mode):
            continue
        parent = path.parent
        if parent not in directories:
            destination = relocate(parent, source, database) if parent.resolve() == parent else None
            if destination is not None:
                destination.mkdir(parents=True, exist_ok=True)
            directories[parent] = destination
        destination = directories[parent]
        if destination is None or not stat.S_ISREG(mode):
            skipped += 1
            continue
        private_file = destination / path.name
        try:
            shutil.copy2(path, private_file, follow_symlinks=False)
        except FileNotFoundError:
            private_file.unlink(missing_ok=True)
            skipped += 1
            continue
        if private_file.is_symlink():
            private_file.unlink()
            skipped += 1
            continue
        copied += 1
    return dict(**schema, capture_format=2, files=copied, skipped_files=skipped,
                expired_vibe_predictions=expired_predictions, playback_inbox_rows=inbox_rows,
                sql_seconds=sql_seconds,
                file_seconds=time.monotonic() - started - sql_seconds)


def relocate(path, old, new):
    relative = path.relative_to(old.parent)
    prefix = old.name + '.'
    if relative.parts and relative.parts[0].startswith(prefix):
        return new.parent / (new.name + relative.parts[0][len(old.name):]) / Path(*relative.parts[1:])
    return new.parent / relative


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    target = Path('/library')
    report = capture(args.source.resolve(strict=True), target)
    (target / '.capture.json').write_text(json.dumps(report) + '\n')
