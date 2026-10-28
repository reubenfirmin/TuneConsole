"""Isolated templates use the same song-link helper as the application."""
from fastapi.templating import Jinja2Templates

from yt_playlist.web.track_links import track_url


def web_templates(*, directory='src/yt_playlist/web/templates'):
    templates = Jinja2Templates(directory=directory)
    templates.env.globals['track_url'] = track_url
    return templates
