"""Qualify #866 on a private, relocated actual-library snapshot.

The donor is opened mode=ro. Existing lifetime/budget lock files are opened rb;
no source directory, lock file, cache, migration, Store or worker is created.
Only the report survives the TemporaryDirectory on success or failure.
"""
from tests.file_content_support import read_bytes as original_bytes
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

from tests.file_custody_support import snapshot_library
from yt_playlist.core import fao, playback_feedback, policy_documents
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.evidence_inventory import Inventory
from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence
from yt_playlist.web.app import create_app


def qualify(source, report_path):
    source = source.resolve(strict=True)
    report = {'source': str(source), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'success': False, 'turns': [], 'controlled_inventory_cutoff': 40}
    reader, budget, publication = policy_documents.retained_reader, EvidenceLedger._budget, fao._publication

    @contextmanager
    def read_only_lifetime(root):
        root = Path(root)
        if not root.is_relative_to(source.parent):
            with reader(root):
                yield
            return
        if not root.exists():
            yield
            return
        with (root / '.readers.lock').open('rb') as stream:
            unlock = fao._try_file_lock(stream, exclusive=False)
            if unlock is None:
                raise RuntimeError('Donor collection owns its reader fence; retry qualification')
            try:
                yield
            finally:
                unlock()

    @contextmanager
    def read_only_budget(ledger):
        if not ledger.directory.is_relative_to(source.parent):
            with budget(ledger) as reservation:
                yield reservation
            return
        with (ledger.directory / '.budget').open('rb') as stream:
            unlock = fao._try_file_lock(stream, exclusive=True)
            if unlock is None:
                raise RuntimeError('Donor evidence publication is busy; retry qualification')
            try:
                def forbidden_save(*args):
                    raise AssertionError('Qualification tried to write the donor budget')
                yield int(stream.read()), forbidden_save
            finally:
                unlock()

    @contextmanager
    def private_publication(path):
        assert not Path(path).is_relative_to(source.parent), 'Qualification tried to mutate the donor'
        with publication(path):
            yield

    store = worker = None
    def close():
        if worker is not None:
            worker.shutdown()
        if store is not None:
            store.conn.close()

    try:
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='tuneconsole-866-'))
            cleanup.callback(close)
            began = time.monotonic()
            print('Copying and relocating actual-library owners (donor read-only).', flush=True)
            with (patch.object(policy_documents, 'retained_reader', read_only_lifetime),
                  patch.object(EvidenceLedger, '_budget', read_only_budget),
                  patch.object(fao, '_publication', private_publication),
                  patch.object(fao.os, 'fsync', lambda fd: None)):
                # This is disposable fixture construction, not durability
                # qualification. Restore real synchronization before testing
                # retention, checkpoints, continuing writes and restart.
                copied = snapshot_library(source, Path(directory) / 'copy')
            report['copy_seconds'] = time.monotonic() - began
            report['fixture_setup'] = 'Domain exporters/restorers; fsync skipped only during disposable setup'
            print('Private library ready; qualifying retention with real filesystem synchronization.', flush=True)
            path = Path(copied['restored'])
            # After relocation, even reads must use the private files. Guard
            # every managed read/publication to catch leaked source coordinates.
            open_reader = fao.read

            def private_reader(path, **kwargs):
                assert not Path(path).is_relative_to(source.parent), 'Leaked donor file coordinate'
                return open_reader(path, **kwargs)

            with (patch.object(fao, 'read', private_reader),
                  patch.object(fao, '_publication', private_publication)):
                def reopen():
                    current = Store(path)
                    current.init_schema()
                    app = create_app(current, lambda: {}, background_work=True)
                    return current, app.state.ctx.rec_worker, app.state.ctx.storage_maintenance

                store, worker, maintenance = reopen()
                ledger = store.rec_ledger.evidence()
                report['before'] = {kind: len(ledger.records(kind)) for kind in EvidenceLedger.KINDS}
                offers = {row['id']: row for row in ledger.records('opportunity')}
                durable = [row for row in ledger.records('response') if row['kind'] not in {'visible', 'open'}]
                originals = {(kind, identity): hashlib.sha256(fao.read(ledger._path(kind, identity)).bytes).hexdigest()
                    for kind, identity in [('response', row['id']) for row in durable] +
                    [('opportunity', identity) for identity in {row['opportunity_id'] for row in durable} & offers.keys()]}
                scan, read = Inventory.scan, original_bytes
                cut = [False]

                def first_cutoff(inventory):
                    if cut[0]:
                        return scan(inventory)
                    count = [0]
                    def bounded_read(target, **kwargs):
                        if target.parent.parent.name in EvidenceLedger.KINDS:
                            count[0] += 1
                            if count[0] > 40:
                                cut[0] = True
                                raise WorkBudgetExceeded('Controlled inventory cutoff after forty reads')
                        return read(target, **kwargs)
                    with patch.object(fao.Content, 'bytes', property(lambda content: bounded_read(content.path, max_bytes=content.max_bytes))):
                        return scan(inventory)

                with patch.object(Inventory, 'scan', first_cutoff):
                    for turn in range(30):
                        began = time.monotonic()
                        result = choice_evidence.maintain_storage(store, time.time())
                        status = maintenance.status()
                        report['turns'].append(dict(result=result, elapsed=time.monotonic() - began,
                            phase=store.settings.get_setting(choice_evidence.STORAGE_PHASE),
                            evidence=status['evidence_maintenance']))
                        print(json.dumps(report['turns'][-1]), flush=True)
                        assert not status['active'] and status['recording'] and not store.activity_paused
                        store.settings.set_setting('866-private-progress-probe', str(turn))
                        identity = store.accounts.get_master_account().id
                        playback_feedback.capture(store, 'player', identity, {'kind': 'state', 'state': 'paused'}, time.time())
                        assert playback_feedback.drain(store) == 1
                        if not result.get('deferred'):
                            break
                        if cut[0] and not report.get('restarted'):
                            worker.shutdown()
                            store.conn.close()
                            store, worker, maintenance = reopen()
                            report['restarted'] = True
                    else:
                        raise AssertionError('Actual-library retention did not finish in thirty turns')
                ledger = store.rec_ledger.evidence()
                for (kind, identity), digest in originals.items():
                    assert hashlib.sha256(fao.read(ledger._path(kind, identity)).bytes).hexdigest() == digest
                assert store.settings.get_setting(choice_evidence.STORAGE_PENDING) == '0'
                assert store.conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
                report.update(success=True, preserved_records=len(originals),
                    after={kind: len(ledger.records(kind)) for kind in EvidenceLedger.KINDS},
                    final_status=maintenance.status())
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    qualify(args.source, args.report)
