"""DAO suite for RediscoverRepo (stale snooze/dismiss state)."""


def test_dismiss_forever_and_restore(store):
    store.rediscover.dismiss_stale("PLZ")                       # until=None -> dismissed forever
    assert store.rediscover.get_stale_hidden_ytm(now=1.0) == {"PLZ"}
    store.rediscover.restore_stale("PLZ")
    assert store.rediscover.get_stale_hidden_ytm(now=1.0) == set()


def test_snooze_expires(store):
    store.rediscover.dismiss_stale("PLZ", until=1.0 + 30 * 86400)
    assert store.rediscover.get_stale_hidden_ytm(now=1.0) == {"PLZ"}            # still snoozed
    assert store.rediscover.get_stale_hidden_ytm(now=1.0 + 31 * 86400) == set()  # snooze expired
    assert store.rediscover.get_stale_dismissed(now=1.0) == [("PLZ", 1.0 + 30 * 86400)]


def test_facade_delegates(store):
    store.dismiss_stale("PLZ")                                  # legacy store.x() call site
    assert store.get_stale_hidden_ytm(now=1.0) == {"PLZ"}
