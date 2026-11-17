"""Account and provider identifiers retain the external backup format."""
import json


def test_backup_v2_still_encodes_account_and_remote_using_historical_fields(store,tmp_path,monkeypatch):
    from yt_playlist.library.playlist_backups import backup_remote_playlist
    from yt_playlist.core import paths
    account = store.accounts.upsert_account('Main','credential',None,True)
    path = backup_remote_playlist(store,account,'PL','Playlist',1,
                                 {'title':'Playlist','tracks':[],'trackCount':0})
    from yt_playlist.core import fao
    saved = json.loads(fao.read(path).bytes.decode())
    assert saved['snapshot_version']==2
    assert saved['identity_id']==account and saved['ytm_playlist_id']=='PL'
    assert 'account_id' not in saved and 'provider_playlist_id' not in saved
