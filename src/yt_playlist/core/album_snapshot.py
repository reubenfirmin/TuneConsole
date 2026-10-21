"""Complete saved-album reads for destructive replacement of the shared library mirror."""
from ytmusicapi.continuations import get_continuation_params
from ytmusicapi.mixins._utils import prepare_order_params, validate_order_parameter
from ytmusicapi.navigation import GRID
from ytmusicapi.parsers.library import get_library_contents, parse_albums
from ytmusicapi.parsers.browsing import parse_content_list, parse_playlist


def _check_response(response):
    if not isinstance(response, dict) or "error" in response:
        raise ValueError("saved-album snapshot is unavailable")
    if response.get("responseContext", {}).get("mainAppWebResponseContext", {}).get("loggedOut"):
        raise ValueError("saved-album snapshot is signed out")


def complete_library_albums(client, order=None):
    """Fetch every page, rejecting the upstream parser's silent partial/empty fallbacks."""
    client._check_auth()
    validate_order_parameter(order)
    body = {"browseId": "FEmusic_liked_albums"}
    if order is not None:
        body["params"] = prepare_order_params(order)
    return [album for page in _complete_grid(client, body) for album in parse_albums(page)]


def complete_library_playlists(client):
    """A truncated listing cannot establish playlist membership or neutral-rating evidence."""
    client._check_auth()
    playlists = []
    for i, items in enumerate(_complete_grid(client, {"browseId": "FEmusic_liked_playlists"})):
        # YouTube puts the create-playlist tile first, only on the initial page.
        playlists.extend(parse_content_list(items[1:] if i == 0 else items, parse_playlist))
    return playlists


def _complete_grid(client, body):
    response = client._send_request("browse", body)
    _check_response(response)
    page = get_library_contents(response, GRID)
    seen = set()
    while True:
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ValueError("saved-album snapshot has no complete album grid")
        yield page["items"]
        if "continuations" not in page:
            return
        params = get_continuation_params(page)
        if params in seen:
            raise ValueError("saved-album pagination repeated a continuation")
        seen.add(params)
        response = client._send_request("browse", body, params)
        _check_response(response)
        page = response.get("continuationContents", {}).get("gridContinuation")

