"""Static content versions stay consistent within a render and fresh after edits."""
from pathlib import Path
import os
import re

from fastapi.testclient import TestClient

from yt_playlist.core import fao
from yt_playlist.web import app as webapp


def test_home_scans_assets_once_and_detects_same_size_zero_mtime_edits(store, tmp_path, monkeypatch):
    web = Path(webapp.__file__).parent
    (tmp_path / 'templates').symlink_to(web / 'templates', target_is_directory=True)
    static = tmp_path / 'static'
    static.mkdir()
    for name, content in [('app.js', 'window.version = 1;'), ('app.css', 'body { color: red; }'),
                          ('favicon.svg', '<svg xmlns="http://www.w3.org/2000/svg"/>')]:
        path = static / name
        path.write_text(content)
        os.utime(path, ns=(0, 0))
    monkeypatch.setattr(webapp, '__file__', str(tmp_path / 'app.py'))
    app = webapp.create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    scans = []
    stat = fao.stat
    def counted(path, *args, **kwargs):
        if Path(path).parent == static:
            scans.append(Path(path).name)
        return stat(path, *args, **kwargs)
    monkeypatch.setattr(fao, 'stat', counted)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        before = client.get('/')
        assert before.status_code == 200
        tokens = set(re.findall(r'/static/[^" ]+\?v=([0-9a-f]{12})', before.text))
        assert len(tokens) == 1
        assert scans.count('app.js') == 1
        assert scans.count('app.css') == 1
        assert scans.count('favicon.svg') == 1

        (static / 'app.js').write_text('window.version = 2;')
        os.utime(static / 'app.js', ns=(0, 0))
        scans.clear()
        after = client.get('/')
        assert after.status_code == 200
        edited = set(re.findall(r'/static/[^" ]+\?v=([0-9a-f]{12})', after.text))
        assert len(edited) == 1 and edited != tokens
        assert scans.count('app.js') == 1
