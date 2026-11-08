"""Production access inventory and restartable mixed-format preparation."""
from pathlib import Path
import pytest

from experiments.retained_copy.fao_audit import ROOT, audit, inventory, boundary
from yt_playlist.core import fao
from yt_playlist.core.preparation.file_bodies import Conversion, managed_roots
from yt_playlist.core.store import Store


def test_inventory_finds_file_calls_inside_unrelated_calls_and_nested_receivers(tmp_path):
    source = tmp_path / 'nested.py'
    source.write_text(
        'def read(factory):\n'
        '    consume(factory().read_bytes())\n'
        '    return json.load(factory().open("rb"))\n')
    (tmp_path / 'unicode.py').write_text('def obtain(path):\n    return 𝚘𝚙𝚎𝚗(path)\n')
    assert [(row['owner'], row['operation']) for row in inventory(tmp_path)] == [
        ('read', 'factory().read_bytes'), ('read', 'json.load'), ('read', 'factory().open'),
        ('obtain', 'open')]


@pytest.mark.parametrize('area', sorted({
    path.relative_to(ROOT).parts[0] for path in ROOT.rglob('*.py')}))
def test_production_file_body_operations_have_an_explicit_boundary(area):
    # Each production package owns its complete inventory. New modules and new
    # top-level packages are included automatically, with root-relative owners.
    source = ROOT / area
    paths = [source] if source.is_file() else source.rglob('*.py')
    assert not [row for row in audit(paths=paths) if row['boundary'] is None]


def test_unowned_file_body_operations_require_a_new_boundary(tmp_path):
    source = tmp_path/'new_owner.py'
    source.write_text("from pathlib import Path\ndef read(path):\n    return Path(path).read_text()\n")
    assert boundary(inventory(tmp_path)[0]) is None
    # A schema module's bundled-resource reader does not exempt other readers
    # in that module from the managed-file boundary.
    core = tmp_path/'core'
    core.mkdir()
    for name in ('schema_admission', 'schema_definition', 'schema_transforms'):
        (core/(name+'.py')).write_text(
            "from pathlib import Path\ndef runtime_asset(path):\n    return Path(path).read_bytes()\n")
    assert all(boundary(row) is None for row in inventory(core.parent))


def test_conversion_restart_and_small_compressed_asset_overhead(tmp_path,store):
    root = tmp_path/'assets'
    root.mkdir()
    first,second = root/'first.json',root/'second.png'
    data = b'{"exact":1.000001}\n'
    first.write_bytes(data)
    second.write_bytes(b'\x89PNG\r\n\x1a\n')
    before = fao.read(first).info
    work = Conversion({'assets':root})
    try:
        assert work.step(entries=1)['converted']==1
    finally:
        work.close()
    restarted = Conversion({'assets':root})
    try:
        totals = []
        while not restarted.complete:
            totals.append(restarted.step(entries=1))
        assert sum(row['converted'] for row in totals)==1
    finally:
        restarted.close()
    assert fao.read(first).bytes==data
    assert fao.read(first).info.mtime_ns==before.mtime_ns
    assert fao.read(first).info.ctime_ns==before.ctime_ns
    assert fao.read(second).info.compressed_bytes>fao.read(second).info.logical_bytes
    disk = Store(tmp_path/'state.db')
    try:
        assert 'profiles' in managed_roots(disk.conn)
    finally:
        disk.conn.close()


def test_idle_orphans_keep_referenced_policy_and_snapshot_lease(store):
    import time
    from yt_playlist.core import policy_documents
    from yt_playlist.core.preparation.managed_files import Orphans
    store.rec_ledger.record_serving_policy('required',{'weights':[1,2,3]},1)
    root = policy_documents.directory(store.conn)
    orphan = root/'aa'/('a'*64+'.json')
    fao.write(orphan, b'{"unused":true}')
    now = time.time()+31*86400
    work = Orphans(store.conn)
    try:
        with store.read_view():
            work.step(store,now)
            assert Path(orphan).is_file()
        for _ in range(20):
            work.step(store,now)
        assert not Path(orphan).is_file()
        assert store.rec_ledger.policy_configuration('required')=={'weights':[1,2,3]}
    finally:
        work.close()


def test_crash_left_pending_collection_keeps_an_active_staged_lease(tmp_path):
    import time
    from yt_playlist.core.preparation.managed_files import Pending
    target = tmp_path/'asset.json'
    stage = fao.prepare(target, b'{"exact":true}')
    leftover = tmp_path/'.fao-pending-abandoned'
    leftover.write_bytes(b'BZh-incomplete')
    now = time.time()+8*86400
    scan = Pending([tmp_path])
    try:
        while not scan.complete:
            scan.step(now,limit=1)
        assert not leftover.exists()
        assert stage.pending.exists()
        stage.publish()
        assert fao.read(target).bytes==b'{"exact":true}'
    finally:
        scan.close()
        stage.close()
