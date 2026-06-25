from yt_playlist import egress


def test_deezer_and_acousticbrainz_are_allowed():
    assert egress.host_allowed("api.deezer.com")
    assert egress.host_allowed("acousticbrainz.org")


def test_unrelated_host_still_blocked():
    assert not egress.host_allowed("evil.example.com")
