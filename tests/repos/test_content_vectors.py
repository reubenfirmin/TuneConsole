import numpy as np
from yt_playlist.core.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


def test_content_vectors_round_trip(tmp_path):
    s = _store(tmp_path)
    assert s.rec_content_vectors_count() == 0
    v = np.ones(4, dtype=np.float32)
    s.replace_rec_content_vectors([("a|artist", v.tobytes()), ("b|artist", v.tobytes())])
    assert s.rec_content_vectors_count() == 2
    got = dict(s.get_rec_content_vectors())
    assert set(got) == {"a|artist", "b|artist"}
    assert np.frombuffer(got["a|artist"], dtype=np.float32).tolist() == [1, 1, 1, 1]


def test_content_vectors_replace_is_atomic(tmp_path):
    s = _store(tmp_path)
    s.replace_rec_content_vectors([("a|x", b"\x00\x00\x00\x00")])
    s.replace_rec_content_vectors([("b|x", b"\x00\x00\x00\x00")])
    assert [k for k, _ in s.get_rec_content_vectors()] == ["b|x"]
