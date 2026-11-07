"""Taste route client with a deterministic identity and clock."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store, *, now=1.0):
    masters = [identity for identity in store.accounts.get_accounts() if identity.is_master]
    iid = masters[0].id if masters else store.accounts.upsert_account("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")
