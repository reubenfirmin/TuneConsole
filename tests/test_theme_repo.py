import pytest


def test_bump_discount_get_theme(store):
    assert store.get_theme("genre:jazz") is None
    assert store.bump_theme("genre:jazz", 0.7, now=1.0) == pytest.approx(0.7)
    assert store.bump_theme("genre:jazz", 0.5, now=2.0) == pytest.approx(1.2)   # running total, no decay
    store.discount_theme("genre:jazz", 1.2)
    assert store.get_theme("genre:jazz") == pytest.approx(0.0)
