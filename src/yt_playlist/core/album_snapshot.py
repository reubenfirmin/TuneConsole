"""Complete library reads, with bounded structural diagnostics and no raw payload logging."""
from itertools import islice

from ytmusicapi.continuations import get_continuation_params
from ytmusicapi.mixins._utils import prepare_order_params, validate_order_parameter
from ytmusicapi.navigation import GRID
from ytmusicapi.parsers.library import get_library_contents, parse_albums
from ytmusicapi.parsers.browsing import parse_playlist


class LibrarySnapshotError(ValueError):
    def __init__(self, resource, reason, response, page, *, item=None):
        self.reason = reason
        position = f"; item={item}" if item is not None else ""
        super().__init__(f"{resource} snapshot {reason}; page={page}{position}; shape={response_shape(response)}")


class PlaylistSnapshot(list):
    """Only the strict reader can certify an authoritative empty playlist library."""
    complete = True


def response_shape(response):
    """Report fixed schema names and list lengths, never titles, IDs, tokens, or credentials."""
    known = {"singleColumnBrowseResultsRenderer", "twoColumnBrowseResultsRenderer", "tabRenderer",
             "sectionListRenderer", "itemSectionRenderer", "gridRenderer", "gridContinuation",
             "messageRenderer", "musicShelfRenderer", "musicTwoRowItemRenderer",
             "musicResponsiveListItemRenderer", "continuationItemRenderer", "signInEndpoint",
             "musicLibraryEmptyStateRenderer", "createPlaylistRenderer", "createPlaylistEndpoint",
             "continuationContents", "error"}
    known.update({'musicPlaylistShelfRenderer', 'onResponseReceivedActions',
                  'appendContinuationItemsAction', 'musicPlaylistShelfContinuation',
                  'musicNotifierShelfRenderer', 'musicHistoryEmptyStateRenderer',
                  'playlistVideoListRenderer', 'playlistVideoRenderer', 'emptyState',
                  'emptyStateRenderer', 'musicPlaylistEmptyStateRenderer',
                  'browseEndpoint', 'navigateAction', 'alertRenderer',
                  'alertWithButtonRenderer', 'errorScreen', 'playlistHeaderRenderer',
                  'playlistSidebarRenderer', 'playlistSidebarPrimaryInfoRenderer',
                  'richGridRenderer', 'richItemRenderer', 'lockupViewModel'})
    found, pending, visited = set(), [(response, 0)], 0
    while pending and visited < 256:
        value, depth = pending.pop()
        visited += 1
        if depth > 12:
            continue
        if isinstance(value, dict):
            # Visit earlier response fields first: a large sidebar/topbar must
            # not consume the budget before the actual contents are inspected.
            for key, child in reversed(list(islice(value.items(), 32))):
                if key in known:
                    found.add(key)
                if key == 'musicTwoRowItemRenderer' and isinstance(child, dict):
                    for field in ('title', 'subtitle', 'navigationEndpoint', 'menu', 'thumbnailRenderer'):
                        item = child.get(field)
                        kind = ('missing' if field not in child else 'null' if item is None else
                                'object' if isinstance(item, dict) else 'other')
                        found.add(f'{key}.{field}={kind}')
                if key == 'musicPlaylistShelfRenderer' and isinstance(child, dict):
                    fields = {'contents', 'continuations', 'trackingParams', 'playlistId',
                              'collapsedItemCount', 'contentsMultiSelectable', 'messageRenderer'}
                    found.add(f'{key}.other_fields[{min(len(child.keys() - fields), 1000)}]')
                    for field in ('contents', 'continuations', 'messageRenderer', 'emptyState'):
                        item = child.get(field)
                        kind = ('missing' if field not in child else 'null' if item is None else
                                f'list[{min(len(item), 1000)}]' if isinstance(item, list) else
                                'object' if isinstance(item, dict) else 'other')
                        found.add(f'{key}.{field}={kind}')
                if key in ("gridRenderer", "gridContinuation") and isinstance(child, dict):
                    fields = {"items", "continuations", "trackingParams", "itemSize"}
                    found.add(f"{key}.other_fields[{min(len(child.keys() - fields), 1000)}]")
                    for field in ("items", "continuations", "trackingParams", "itemSize"):
                        item = child.get(field)
                        kind = ("missing" if field not in child else "null" if item is None else
                                "list" if isinstance(item, list) else "object" if isinstance(item, dict) else
                                "empty-string" if item == "" else
                                "string" if isinstance(item, str) else "other")
                        found.add(f"{key}.{field}={kind}")
                if key in ("items", "contents", "tabs", "continuations") and isinstance(child, list):
                    found.add(f"{key}[{min(len(child), 1000)}{'+' if len(child) > 1000 else ''}]")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in reversed(value[:8]))
    return ",".join(sorted(found)) or type(response).__name__


def _check_response(response, resource, page):
    if not isinstance(response, dict):
        raise LibrarySnapshotError(resource, "unavailable", response, page)
    context = response.get("responseContext")
    context = context.get("mainAppWebResponseContext") if isinstance(context, dict) else None
    logged_out = context.get("loggedOut") if isinstance(context, dict) else None
    error = response.get("error")
    auth_error = isinstance(error, dict) and error.get("code") in (401, 403)
    sign_in = (logged_out is not False and "signInEndpoint" in
               response_shape(response.get("contents", {})).split(","))
    if logged_out is True or auth_error or sign_in:
        raise LibrarySnapshotError(resource, "signed out", response, page)
    if "error" in response:
        raise LibrarySnapshotError(resource, "unavailable", response, page)


def complete_library_albums(client, order=None):
    """Fetch every page, rejecting the upstream parser's silent partial/empty fallbacks."""
    client._check_auth()
    validate_order_parameter(order)
    body = {"browseId": "FEmusic_liked_albums"}
    if order is not None:
        body["params"] = prepare_order_params(order)
    return [album for _, page in _complete_grid(client, body, "saved-album") for album in parse_albums(page)]


def complete_library_playlists(client):
    """A truncated listing cannot establish playlist membership or neutral-rating evidence."""
    client._check_auth()
    playlists = PlaylistSnapshot()
    for page_no, items in _complete_grid(client, {"browseId": "FEmusic_liked_playlists"}, "playlist-library"):
        # The create tile is optional; do not discard a real first playlist.
        for index, item in enumerate(items):
            if _create_tile(item):
                continue
            if not isinstance(item, dict) or "musicTwoRowItemRenderer" not in item:
                raise LibrarySnapshotError("playlist-library", "unsupported playlist grid item", item,
                                           page_no, item=index)
            try:
                playlists.append(_library_playlist(item["musicTwoRowItemRenderer"]))
            except (KeyError, TypeError, IndexError, ValueError):
                raise LibrarySnapshotError("playlist-library", "malformed playlist grid item", item,
                                           page_no, item=index) from None
    return playlists


def _tile_value(value, *path):
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value.get(key)
        elif isinstance(value, list) and isinstance(key, int) and key < len(value):
            value = value[key]
        else:
            return None
    return value


def _library_playlist(renderer):
    """Require membership evidence, but allow incomplete presentation metadata.

    ytmusicapi requires a linked title and subtitle object even though a tile can
    identify its playlist through its own browse endpoint. Detail reads supply
    the authoritative title and contents later; never omit an unidentified tile.
    """
    browse_ids = [_tile_value(renderer, *path, 'navigationEndpoint', 'browseEndpoint', 'browseId')
                  for path in (('title', 'runs', 0), ())]
    browse_ids = [bid for bid in browse_ids if bid is not None]
    if (not browse_ids or any(not isinstance(bid, str) or not bid.startswith('VL') or len(bid) <= 2
                              for bid in browse_ids) or len(set(browse_ids)) != 1):
        raise ValueError('Missing or conflicting playlist browse endpoints')
    playlist_id = browse_ids[0][2:]
    try:
        playlist = parse_playlist(renderer)
    except (KeyError, TypeError, IndexError, AttributeError):
        # Missing subtitles, non-text runs and unlinked titles are display
        # variants, not evidence that all preceding library pages are invalid.
        title = _tile_value(renderer, 'title', 'simpleText')
        if not isinstance(title, str):
            runs = _tile_value(renderer, 'title', 'runs')
            title = (''.join(run['text'] for run in runs
                             if isinstance(run, dict) and isinstance(run.get('text'), str))
                     if isinstance(runs, list) else None)
        menu = _tile_value(renderer, 'menu', 'menuRenderer', 'items')
        playlist = {'playlistId': playlist_id, 'title': title,
                    'owned': any(_tile_value(item, 'menuNavigationItemRenderer', 'navigationEndpoint',
                                             'playlistEditorEndpoint', 'playlistId') == playlist_id
                                 for item in (menu if isinstance(menu, list) else []))}
    thumbnails = _tile_value(renderer, 'thumbnailRenderer', 'musicThumbnailRenderer', 'thumbnail', 'thumbnails')
    playlist['thumbnails'] = ([thumb for thumb in thumbnails
                               if isinstance(thumb, dict) and isinstance(thumb.get('url'), str)]
                              if isinstance(thumbnails, list) else [])
    return playlist


def _create_tile(item):
    if not isinstance(item, dict):
        return False
    if "createPlaylistRenderer" in item:
        return True
    renderer = item.get("musicTwoRowItemRenderer")
    endpoint = renderer.get("navigationEndpoint") if isinstance(renderer, dict) else None
    return isinstance(endpoint, dict) and "createPlaylistEndpoint" in endpoint


def _complete_grid(client, body, resource):
    response = client._send_request("browse", body)
    page_no = 1
    _check_response(response, resource, page_no)
    try:
        page = get_library_contents(response, GRID)
    except (KeyError, TypeError, IndexError):
        raise LibrarySnapshotError(resource, "unsupported library grid layout", response, page_no) from None
    seen = set()
    while True:
        # YouTube can end pagination with tracking and optional display-size metadata.
        # Require this terminal shape after a validated first page; a
        # missing initial grid, null items or an unknown payload is not empty.
        if (page_no > 1 and isinstance(page, dict)
                and set(page) in ({"trackingParams"}, {"trackingParams", "itemSize"})
                and isinstance(page["trackingParams"], str) and page["trackingParams"]
                and ("itemSize" not in page or isinstance(page["itemSize"], str) and page["itemSize"])):
            return
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise LibrarySnapshotError(resource, "has no complete grid", response, page_no)
        yield page_no, page["items"]
        if "continuations" not in page or page["continuations"] == []:
            return
        try:
            continuations = page["continuations"]
            if not isinstance(continuations, list) or len(continuations) != 1:
                raise ValueError
            token = continuations[0]["nextContinuationData"]["continuation"]
            if not isinstance(token, str) or not token:
                raise ValueError
            params = get_continuation_params(page)
        except (KeyError, TypeError, IndexError, ValueError):
            raise LibrarySnapshotError(resource, "malformed grid continuation", response, page_no) from None
        if params in seen:
            raise LibrarySnapshotError(resource, "grid pagination repeated a continuation", response, page_no)
        seen.add(params)
        response = client._send_request("browse", body, params)
        page_no += 1
        _check_response(response, resource, page_no)
        contents = response.get("continuationContents")
        page = contents.get("gridContinuation") if isinstance(contents, dict) else None

