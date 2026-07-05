"""#91 read-time classification of raw exit observations. Pure; thresholds are module constants."""
import pytest

from yt_playlist.library.listen_derive import classify_exit


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
