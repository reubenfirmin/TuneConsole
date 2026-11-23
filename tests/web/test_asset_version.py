"""Static content versions stay consistent within a render and fresh after edits."""
from pathlib import Path
import os
import re

from fastapi.testclient import TestClient

from yt_playlist.web import app as webapp
from yt_playlist.web.fao_static import HOME_SHELL


def test_home_scans_assets_once_and_detects_same_size_zero_mtime_edits(store, tmp_path, monkeypatch):
    web = Path(webapp.__file__).parent
    (tmp_path / 'templates').symlink_to(web / 'templates', target_is_directory=True)
    static = tmp_path / 'static'
    static.mkdir()
    replacements = {'app.js': 'window.version = 1;', 'app.css': 'body { color: red; }',
                    'favicon.svg': '<svg xmlns="http://www.w3.org/2000/svg"/>'}
    for names, extension, _, _ in HOME_SHELL.values():
        for name in names:
            filename = name + extension
            if filename not in replacements:
                path = static / filename
                path.parent.mkdir(exist_ok=True)
                path.symlink_to(web / 'static' / filename)
    for name, content in replacements.items():
        path = static / name
        path.write_text(content)
        os.utime(path, ns=(0, 0))
    monkeypatch.setattr(webapp, '__file__', str(tmp_path / 'app.py'))
    app = webapp.create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    scans = []
    glob = Path.glob

    def counted(path, pattern, *args, **kwargs):
        if path == static:
            scans.append(pattern)
        return glob(path, pattern, *args, **kwargs)

    with TestClient(app, base_url='http://127.0.0.1') as client:
        monkeypatch.setattr(Path, 'glob', counted)
        before = client.get('/')
        assert before.status_code == 200
        tokens = set(re.findall(r'/static/[^" ]+\?v=([0-9a-f]{12})', before.text))
        assert len(tokens) == 1
        assert scans == ['*.js', '*.css']
        bundle = client.get('/static/home-shell.js', params={'v': next(iter(tokens))})
        assert bundle.status_code == 200 and replacements['app.js'] in bundle.text

        (static / 'app.js').write_text('window.version = 2;')
        os.utime(static / 'app.js', ns=(0, 0))
        scans.clear()
        after = client.get('/')
        assert after.status_code == 200
        edited = set(re.findall(r'/static/[^" ]+\?v=([0-9a-f]{12})', after.text))
        assert len(edited) == 1 and edited != tokens
        assert scans == ['*.js', '*.css']
        refreshed = client.get('/static/home-shell.js', params={'v': next(iter(edited))})
        assert refreshed.status_code == 200 and 'window.version = 2;' in refreshed.text
        assert refreshed.headers['etag'] != bundle.headers['etag']
