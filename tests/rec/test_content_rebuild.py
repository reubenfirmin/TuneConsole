from yt_playlist.rec import embed


class CovStore:
    """Stub: controllable coverage + persisted vectors/settings; counts rebuilds."""
    def __init__(self, tagged, total):
        self._tagged, self._total = tagged, total
        self._settings = {}
        self.builds = 0
        self._vecs = []

    # RecDao(store) is monkeypatched to read these:
    def track_content(self):
        return {f"k{i}|a": ("Techno", "1990") for i in range(self._tagged)}

    def track_audio_features(self):
        return {}

    def library_keys(self):
        return {f"k{i}|a" for i in range(self._total)}

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value

    def replace_rec_content_vectors(self, rows):
        self._vecs = list(rows); self.builds += 1

    def get_discovered_tracks(self):
        return []

    def replace_rec_discovered_content_vectors(self, rows):
        pass


def _patch(monkeypatch, store):
    monkeypatch.setattr(embed, "RecDao", lambda s: store)


def test_first_call_builds(monkeypatch):
    s = CovStore(tagged=10, total=100)      # 10% coverage
    _patch(monkeypatch, s)
    assert embed.maybe_rebuild_content_vectors(s) is True
    assert s.builds == 1
    assert s.get_setting("rec_content_cov_bucket") == "2"   # floor(0.10/0.05)


def test_within_bucket_no_rebuild(monkeypatch):
    s = CovStore(tagged=10, total=100)
    _patch(monkeypatch, s)
    embed.maybe_rebuild_content_vectors(s)                   # bucket 2
    s._tagged = 12                                           # 12%, still bucket 2
    assert embed.maybe_rebuild_content_vectors(s) is False
    assert s.builds == 1


def test_crossing_boundary_rebuilds_once(monkeypatch):
    s = CovStore(tagged=10, total=100)
    _patch(monkeypatch, s)
    embed.maybe_rebuild_content_vectors(s)                   # bucket 2
    s._tagged = 16                                           # 16%, bucket 3
    assert embed.maybe_rebuild_content_vectors(s) is True
    assert s.builds == 2
    assert s.get_setting("rec_content_cov_bucket") == "3"


def test_multi_boundary_jump_rebuilds_once_and_snaps(monkeypatch):
    s = CovStore(tagged=5, total=100)                        # 5% → bucket 1
    _patch(monkeypatch, s)
    embed.maybe_rebuild_content_vectors(s)
    s._tagged = 57                                           # 57% → bucket 11
    assert embed.maybe_rebuild_content_vectors(s) is True
    assert s.builds == 2
    assert s.get_setting("rec_content_cov_bucket") == "11"
