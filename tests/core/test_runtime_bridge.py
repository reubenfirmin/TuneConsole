from types import SimpleNamespace
from yt_playlist.core.bridge import Bridge


def test_provider_builds_bridge_clients(tmp_path, monkeypatch):
    # Minimal: a config with one identity, no credential file needed.
    from yt_playlist.core.runtime import Runtime
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[identity]]\nlabel = "me"\ncredential_ref = "browser"\nis_master = true\n')

    class FakeStore:
        def upsert_identity(self, *a): return 1
        def get_setting(self, k): return None
        def set_setting(self, k, v): pass

    rt = Runtime(FakeStore(), cfg, tmp_path)
    rt.bridge = Bridge()
    rt.load()
    assert rt.configured
    clients = rt.clients()
    assert set(clients) == {1}
