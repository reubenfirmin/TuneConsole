"""Production access inventory and restartable mixed-format preparation."""
from pathlib import Path
import pytest

from experiments.retained_copy.fao_audit import audit, inventory, boundary
from yt_playlist.core import fao
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """File inventory and recovery do not construct an HTTP application."""


def test_inventory_finds_file_calls_inside_unrelated_calls_and_nested_receivers(tmp_path):
    source = tmp_path / 'nested.py'
    source.write_text(
        'def read(factory):\n'
        '    consume(factory().read_bytes())\n'
        '    return json.load(factory().open("rb"))\n'
        'def wrapped(path):\n'
        '    (path.read_text # A parenthesized attribute.\n'
        '    )()\n'
        '    path.write_bytes \\\n'
        '        (b"body")\n')
    (tmp_path / 'unicode.py').write_text(
        'def obtain(path):\n'
        '    return (𝚘𝚙𝚎𝚗 # A parenthesized Unicode identifier.\n'
        '            )(path)\n')
    assert [(row['owner'], row['operation']) for row in inventory(tmp_path)] == [
        ('read', 'factory().read_bytes'), ('read', 'json.load'), ('read', 'factory().open'),
        ('wrapped', 'path.read_text'), ('wrapped', 'path.write_bytes'),
        ('obtain', 'open')]


def test_production_file_body_operations_have_an_explicit_boundary(tmp_path):
    assert not [row for row in audit() if row['boundary'] is None]
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


def test_crash_left_pending_collection_keeps_an_active_staged_lease(tmp_path):
    import time
    from yt_playlist.core.managed_file_candidates import Pending
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
