"""SQL-free ID naming and integrity at the backup file boundary."""
from pathlib import Path

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import playlist_backup_files as files
from yt_playlist.core.playlist_backup_paths import record_id

pytestmark = pytest.mark.usefixtures('no_database')


def test_id_named_capture_round_trips_and_old_recovery_files_remain_readable(tmp_path):
    port = files.PlaylistBackupFiles(tmp_path)
    payload = {'tracks': [{'videoId': 'song'}]}
    with port.write_reserved('a' * 32, payload, expected=port.describe(payload)) as reference:
        assert Path(reference[0]) == tmp_path / ('a' * 32 + '.json')
        assert record_id(reference[0]) == 'a' * 32
        assert port.load(reference[0]) == payload
    legacy = tmp_path / 'SOURCE_7_100_old.json'
    legacy.write_text('{"tracks": []}')
    assert port.load(legacy) == {'tracks': []}


@pytest.mark.parametrize('identity', ['', '../outside', 'a' * 64, 'A' * 32, 7])
def test_invalid_identity_never_creates_a_file(tmp_path, identity):
    port = files.PlaylistBackupFiles(tmp_path)
    with pytest.raises(ValueError, match='SQL capture ID'), port.write_reserved(identity, {}, expected=port.describe({})):
        pytest.fail('Invalid identity admitted')
    assert not list(tmp_path.iterdir())


def test_body_changed_after_sql_reservation_is_not_published(tmp_path):
    port = files.PlaylistBackupFiles(tmp_path)
    with pytest.raises(ValueError, match='changed after reservation'), port.write_reserved(
            'a' * 32, {'changed': True}, expected=port.describe({})):
        pytest.fail('Changed body admitted')
    assert not list(tmp_path.iterdir())
