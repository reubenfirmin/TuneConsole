"""Function-scoped Radio browser API fixture with strict request assertions."""
from types import SimpleNamespace

import pytest
from fastapi.templating import Jinja2Templates
from playwright.sync_api import expect


@pytest.fixture
def radio_ui(live_app, store, page):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    tracks = [{"video_id": f"radio{i}", "title": f"Night frequency {i + 1}",
               "artist": f"Artist {i + 1}", "thumbnail": f"{live_app}/radio-art/{i}.svg"}
              for i in range(16)]
    api = SimpleNamespace(state={"active": False}, starts=[], playlists=[], stops=[],
                          hold_playlist=False, errors=[], tracks=tracks)
    api.ready = {"active": True, "starting": False, "playlist_id": "PLRADIO", "local_id": 1,
                 "tracks": tracks[:3], "history_count": 1, "title": "Radio Session",
                 "directions": [{"slot": i, "id": f"direction{i}", "active": i == 0,
                                 "label": ["Stay here", "Deeper", "Brighter", "Further"][i],
                                 "tracks": tracks[i + 1:i + 4]} for i in range(4)]}
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    options = templates.get_template('_partials/radio_options.html').render()
    api.playlist = ('<div class="radio-track-scroll"><table><thead><tr><th>Song</th><th>' + options +
                    '</th></tr></thead><tbody><tr data-vid="radio0"><td>Night frequency 1</td></tr></tbody></table></div>')

    def playlist(route):
        if api.hold_playlist:
            api.playlists.append(route)
        else:
            route.fulfill(content_type="text/html", body=api.playlist)

    def stop(route):
        api.stops.append(route.request.method)
        api.state = {"active": False}
        route.fulfill(json={"ok": True, **api.state})

    def art(route):
        index = int(route.request.url.rsplit("/", 1)[1].split(".")[0])
        hue = index * 29
        route.fulfill(content_type="image/svg+xml", body=f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">
          <defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="hsl({hue} 50% 25%)"/><stop offset="1" stop-color="hsl({hue + 70} 60% 55%)"/></linearGradient></defs>
          <path fill="url(#g)" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="95" fill="none" stroke="white" stroke-opacity=".25" stroke-width="28"/>
          <circle cx="150" cy="150" r="22" fill="white" fill-opacity=".65"/></svg>''')

    page.on("pageerror", lambda error: api.errors.append(str(error)))
    page.route("**/radio-art/*.svg", art)
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": tracks, "menu_id": "seeds"}))
    page.route("**/radio/status", lambda route: route.fulfill(json=api.state))
    page.route("**/radio/start", lambda route: api.starts.append(route))
    page.route("**/radio/playlist", playlist)
    page.route("**/radio/stop", stop)
    page.route("**/radio/seen", lambda route: route.fulfill(json={"ok": True}))
    page.route("**/bridge/status", lambda route: route.fulfill(json={"connected": True, "now_playing": None}))
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{live_app}/#radio")
    expect(page.locator(".radio-seed")).to_have_count(16)
    yield api
    assert not api.errors
