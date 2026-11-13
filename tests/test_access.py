"""Transport-neutral access dispatch: which fact-gathering path is used, not
the underlying journey math (covered by test_account_growth.py and friends).
"""
import pytest

from yt_playlist.rec import access, account_growth


def test_cached_growth_skips_fact_gathering_entirely(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access',
                        lambda *a, **k: pytest.fail('must not gather facts when growth is supplied'))
    monkeypatch.setattr(account_growth, 'radio_access',
                        lambda *a, **k: pytest.fail('must not gather facts when growth is supplied'))
    monkeypatch.setattr(account_growth, 'vibe_access',
                        lambda *a, **k: pytest.fail('must not gather facts when growth is supplied'))
    monkeypatch.setattr(account_growth, 'has_access', lambda growth, area: growth == 'GROWTH' and area == 'tv')
    assert access.check('STORE', 'tv', now=1000, growth='GROWTH') is True
    assert access.check('STORE', 'other', now=1000, growth='GROWTH') is False


def test_retained_access_short_circuits_before_any_area_fast_path(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access', lambda store, area: True)
    monkeypatch.setattr(account_growth, 'radio_access', lambda *a, **k: pytest.fail('must not reach radio_access'))
    assert access.check('STORE', 'radio', now=1000) is True


def test_radio_area_uses_its_fast_path_without_building_the_whole_journey(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access', lambda store, area: False)
    monkeypatch.setattr(account_growth, 'radio_access',
                        lambda store, now, *, radio: (store, now, radio) == ('STORE', 1000, 'BOOT'))
    monkeypatch.setattr(account_growth, 'view', lambda *a, **k: pytest.fail('must not build the whole journey'))
    assert access.check('STORE', 'radio', now=1000, radio='BOOT') is True


def test_vibes_area_uses_its_fast_path_without_building_the_whole_journey(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access', lambda store, area: False)
    monkeypatch.setattr(account_growth, 'vibe_access',
                        lambda store, now, radio: (store, now, radio) == ('STORE', 1000, 'BOOT'))
    monkeypatch.setattr(account_growth, 'view', lambda *a, **k: pytest.fail('must not build the whole journey'))
    assert access.check('STORE', 'vibes', now=1000, radio='BOOT') is True


def test_other_areas_fall_back_to_the_full_journey(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access', lambda store, area: False)
    monkeypatch.setattr(account_growth, 'view', lambda store, now, *, radio: 'GROWTH')
    monkeypatch.setattr(account_growth, 'has_access', lambda growth, area: growth == 'GROWTH' and area == 'discover')
    assert access.check('STORE', 'discover', now=1000) is True


def test_require_raises_access_denied_carrying_the_area(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access', lambda store, area: False)
    monkeypatch.setattr(account_growth, 'radio_access', lambda store, now, *, radio: False)
    with pytest.raises(access.AccessDenied) as excinfo:
        access.require('STORE', 'radio', now=1000, radio='BOOT')
    assert excinfo.value.area == 'radio'
    assert str(excinfo.value) == access.DENIED_MESSAGE


def test_require_is_silent_when_access_is_granted(monkeypatch):
    monkeypatch.setattr(account_growth, 'retained_access', lambda store, area: True)
    access.require('STORE', 'radio', now=1000)
