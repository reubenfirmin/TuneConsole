"""The re-auth banner state must survive a server restart: it is persisted to the store and reseeded
when a fresh Ctx is built (e.g. uvicorn --reload re-spawning the worker)."""
import sqlite3

import pytest

from yt_playlist.web.context import Ctx


def _ctx(store):
    return Ctx(store=store, client_provider=lambda: {}, now_fn=lambda: 1.0, templates=None, jobs=None)


def _store(s):
    for number in range(1,8):
        s.accounts.upsert_account(f'Account {number}',None,number==1)
    return s


def test_flag_persists_and_reseeds_across_restart(store):
    store = _store(store)
    ctx = _ctx(store)
    ctx.flag_auth_expired(7, "flavor8100")
    assert ctx.auth_expired == {7: "Account 7"}
    assert store.accounts.auth_expirations() == {7: 'Account 7'}

    fresh = _ctx(store)                                        # simulate a restart: brand-new Ctx, same store
    assert fresh.auth_expired == {7: "Account 7"}


def test_flag_uses_account_label(store):
    store = _store(store)
    ctx = _ctx(store)
    ctx.flag_auth_expired(3, None)
    assert ctx.auth_expired == {3: "Account 3"}


def test_clear_one_removes_and_persists(store):
    store = _store(store)
    ctx = _ctx(store)
    ctx.flag_auth_expired(1, "a")
    ctx.flag_auth_expired(2, "b")
    ctx.clear_auth_expired(1)
    assert ctx.auth_expired == {2: "Account 2"}
    assert _ctx(store).auth_expired == {2: "Account 2"}

    ctx.clear_auth_expired(2)
    assert ctx.auth_expired == {}
    assert store.accounts.auth_expirations() == {}


def test_clear_all_wipes_banner(store):
    store = _store(store)
    ctx = _ctx(store)
    ctx.flag_auth_expired(1, "a")
    ctx.flag_auth_expired(2, "b")
    ctx.clear_all_auth_expired()
    assert ctx.auth_expired == {}
    assert _ctx(store).auth_expired == {}                      # re-auth/sign-out clears it durably


def test_healthy_extension_clears_warning_in_memory_during_repair_then_on_disk(store):
    store = _store(store)
    ctx = _ctx(store)
    ctx.flag_auth_expired(1, 'Main')
    store.activity_paused = True
    store.conn.execute('PRAGMA query_only=ON')
    ctx.clear_all_auth_expired()
    assert ctx.auth_expired == {}
    assert store.accounts.auth_expirations() == {1: 'Account 1'}
    store.conn.execute('PRAGMA query_only=OFF')
    store.activity_paused = False
    # The still-connected extension is freshly confirmed when background work
    # resumes; an empty RAM dictionary must not leave a stale warning on disk.
    ctx.clear_all_auth_expired()
    assert _ctx(store).auth_expired == {}
    assert store.accounts.auth_expirations() == {}


def test_retired_auth_map_cannot_replace_owned_account_state(store):
    store = _store(store)
    with pytest.raises(sqlite3.IntegrityError,match='domain owner'):
        store.settings.set_setting('auth_expired_identities', "not json{")
    store.conn.rollback()
    assert _ctx(store).auth_expired == {}
