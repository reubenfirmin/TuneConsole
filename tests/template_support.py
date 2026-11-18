"""Isolated templates use the same song-link helper as the application."""
from html.parser import HTMLParser
import json

from fastapi.templating import Jinja2Templates

from yt_playlist.util.navigation import LINK_CONFIG, album_url, artist_url, playlist_url, track_url
from yt_playlist.web.theme import mix_launch_tokens
from yt_playlist.util.music_catalog import SEARCH_SECTIONS


def web_templates(*, directory='src/yt_playlist/web/templates'):
    templates = Jinja2Templates(directory=directory)
    templates.env.globals['track_url'] = track_url
    templates.env.globals['music_search_sections'] = SEARCH_SECTIONS
    templates.env.globals.update(album_url=album_url, artist_url=artist_url, playlist_url=playlist_url,
                                 detail_link_config=LINK_CONFIG)
    templates.env.globals['mix_launch_tokens'] = mix_launch_tokens
    templates.env.globals['player_tour_dismissed'] = lambda: True
    # Isolated templates have no catalog to resolve missing credits. Tests that
    # exercise navigation supply already projected identity links explicitly.
    templates.env.globals['artist_links'] = lambda subject, album=False: subject.get('artist_links', [])
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
