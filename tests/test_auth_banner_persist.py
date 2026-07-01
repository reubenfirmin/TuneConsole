"""The re-auth banner state must survive a server restart: it is persisted to the store and reseeded
when a fresh Ctx is built (e.g. uvicorn --reload re-spawning the worker)."""
from yt_playlist.core.store import Store
from yt_playlist.web.context import Ctx, AUTH_EXPIRED_KEY


def _ctx(store):
    return Ctx(store=store, client_provider=lambda: {}, now_fn=lambda: 1.0, templates=None, jobs=None)


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_flag_persists_and_reseeds_across_restart():
    store = _store()
    ctx = _ctx(store)
    ctx.flag_auth_expired(7, "flavor8100")
    assert ctx.auth_expired == {7: "flavor8100"}
    assert store.get_setting(AUTH_EXPIRED_KEY)                 # written through

    fresh = _ctx(store)                                        # simulate a restart: brand-new Ctx, same store
    assert fresh.auth_expired == {7: "flavor8100"}            # int key, so on_auth_ok's pop(iid) matches


def test_flag_defaults_label_to_id():
    store = _store()
    ctx = _ctx(store)
    ctx.flag_auth_expired(3, None)
    assert ctx.auth_expired == {3: "3"}


def test_clear_one_removes_and_persists():
    store = _store()
    ctx = _ctx(store)
    ctx.flag_auth_expired(1, "a")
    ctx.flag_auth_expired(2, "b")
    ctx.clear_auth_expired(1)
    assert ctx.auth_expired == {2: "b"}
    assert _ctx(store).auth_expired == {2: "b"}               # persisted partial clear

    ctx.clear_auth_expired(2)
    assert ctx.auth_expired == {}
    assert store.get_setting(AUTH_EXPIRED_KEY) is None         # setting deleted when empty


def test_clear_all_wipes_banner():
    store = _store()
    ctx = _ctx(store)
    ctx.flag_auth_expired(1, "a")
    ctx.flag_auth_expired(2, "b")
    ctx.clear_all_auth_expired()
    assert ctx.auth_expired == {}
    assert _ctx(store).auth_expired == {}                      # re-auth/sign-out clears it durably


def test_corrupt_setting_is_ignored():
    store = _store()
    store.set_setting(AUTH_EXPIRED_KEY, "not json{")
    assert _ctx(store).auth_expired == {}                      # tolerated, not a crash
