"""#91 read-time classification of raw exit observations. Pure; thresholds are module constants."""
import pytest

from yt_playlist.library.listen_derive import classify_exit, skip_blackouts


@pytest.mark.parametrize("position,duration,want", [
    (390.0, 400.0, "completion"),    # >= 85% listened
    (340.0, 400.0, "completion"),    # exactly 85%
    (1.5, 400.0, "bounce"),          # < 3s: mis-click, not a taste signal
    (20.0, 400.0, "skip"),           # <= 30% and <= 120s listened
    (110.0, 400.0, "skip"),
    (130.0, 400.0, "partial"),       # 32%: left late, over the 120s floor
    (200.0, 400.0, "partial"),
    (30.0, None, "unknown"),
    (None, 400.0, "unknown"),
    (30.0, 0, "unknown"),
])
def test_classify_exit(position, duration, want):
    assert classify_exit(position, duration) == want


@pytest.mark.parametrize('gap,days', [(30 * 86400, 2), (30 * 86400 + 1, 1)])
def test_skip_repetition_window_is_relative_to_each_skip(gap, days):
    first = dict(key='recording', identity_id=1, kind='next', position=20, duration=200, at=86400)
    repeated = {**first, 'at': first['at'] + gap}
    until = repeated['at'] + days * 86400
    assert skip_blackouts([repeated, first], repeated['at']) == {'recording': until}
    assert skip_blackouts([repeated, first], until - 1) == {'recording': until}
    assert skip_blackouts([repeated, first], until) == {}
