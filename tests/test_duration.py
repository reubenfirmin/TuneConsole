"""parse_duration: 'M:SS' / 'H:MM:SS' strings -> seconds."""
import pytest

from yt_playlist.util.duration import parse_duration


@pytest.mark.parametrize("text,secs", [
    ("3:01", 181),
    ("2:40", 160),
    ("1:02:03", 3723),
    ("0:09", 9),
    (None, None),
    ("", None),
    ("not a time", None),
])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs
