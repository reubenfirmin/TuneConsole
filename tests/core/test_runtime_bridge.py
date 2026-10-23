from yt_playlist.core.bridge import Bridge
from yt_playlist.core.bridge_session import BridgeSession


def test_provider_builds_bridge_clients(store, tmp_path):
    # Minimal: a config with one identity, no credential file needed.
    from yt_playlist.core.runtime import Runtime
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[identity]]\nlabel = "me"\ncredential_ref = "browser"\nis_master = true\n')

    rt = Runtime(store, cfg, tmp_path)
    rt.bridge = Bridge()
    rt.load()
    assert rt.configured
    clients = rt.clients()
    assert set(clients) == {store.identities.get_master_identity().id}
    assert all(isinstance(client._session, BridgeSession) for client in clients.values())
