"""Isolated templates use the same song-link helper as the application."""
from html.parser import HTMLParser
import json

from fastapi.templating import Jinja2Templates

from yt_playlist.web.track_links import track_url
from yt_playlist.web.theme import mix_launch_tokens


def web_templates(*, directory='src/yt_playlist/web/templates'):
    templates = Jinja2Templates(directory=directory)
    templates.env.globals['track_url'] = track_url
    templates.env.globals['mix_launch_tokens'] = mix_launch_tokens
    return templates


def playlist_rows(markup):
    """Read the rows argument even when the component has other arguments."""
    class Arguments(HTMLParser):
        rows = None

        def handle_starttag(self, tag, attrs):
            expression = dict(attrs).get('x-data', '')
            if expression.startswith('playlistsTab('):
                self.rows, _ = json.JSONDecoder().raw_decode(expression[len('playlistsTab('):])

    parser = Arguments()
    parser.feed(markup)
    assert parser.rows is not None, 'Missing Playlists component rows'
    return parser.rows
