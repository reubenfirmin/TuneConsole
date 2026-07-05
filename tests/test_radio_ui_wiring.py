from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "yt_playlist" / "web"


def test_launch_card_present_and_status_button_removed():
    home = (WEB / "templates" / "home.html").read_text()
    assert "radio_launch.html" in home                 # launch card included
    assert 'class="hs-radio"' not in home              # v1 status-strip button removed
    assert 'id="radio-steer-slot"' in home             # teleport target in the now-playing card


def test_launch_card_is_parked_behind_the_radio_query_param():
    # Owner-parked (2026-07-05): the card renders only when the page is loaded with ?radio=true
    # (or ?radio=1); the /radio/* endpoints stay live underneath. The include must sit inside the
    # show_radio gate, and the route must derive show_radio from the query param.
    home = (WEB / "templates" / "home.html").read_text()
    gate = home.split("{% if show_radio %}", 1)
    assert len(gate) == 2, "radio_launch include is no longer gated on show_radio"
    assert "radio_launch.html" in gate[1].split("{% endif %}", 1)[0]
    route = (WEB / "routes" / "home.py").read_text()
    assert '"show_radio": request.query_params.get("radio") in ("true", "1")' in route


def test_customize_panel_posts_session_tilts():
    p = (WEB / "templates" / "_partials" / "radio_customize.html").read_text()
    assert 'hx-post="/radio/steer"' in p
    assert 'id="radio-customize-bars"' in p
    assert 'hx-post="/radio/steer/reset"' in p


def test_launch_card_has_start_stop_and_customize_toggle():
    p = (WEB / "templates" / "_partials" / "radio_launch.html").read_text()
    assert "startRadio()" in p
    assert "stopRadio()" in p
    assert "customizeOpen" in p
    assert "radio_customize.html" in p


def test_launch_card_migrates_via_teleport_when_active():
    p = (WEB / "templates" / "_partials" / "radio_launch.html").read_text()
    assert 'x-teleport="#radio-steer-slot"' in p
    assert 'x-if="radioActive"' in p


def test_app_js_keeps_radio_actions():
    js = (WEB / "static" / "app.js").read_text()
    assert "startRadio" in js and "stopRadio" in js and "this.radioActive = !!d.radio" in js
    assert "customizeOpen" in js


def test_app_js_polls_radio_waiting_off_the_same_bridge_status_poll():
    # Waiting-state net: no new poller -- radioWaiting rides the SAME /bridge/status poll that already
    # sets radioActive/nowPlaying/connected.
    js = (WEB / "static" / "app.js").read_text()
    assert "radioWaiting" in js
    poll = js.split("fetch('/bridge/status').then(r => r.json())", 1)[1][:400]
    assert "this.radioWaiting = !!d.radio_waiting" in poll


def test_launch_card_shows_waiting_prompt_with_no_em_dash():
    p = (WEB / "templates" / "_partials" / "radio_launch.html").read_text()
    assert 'x-show="radioActive && radioWaiting"' in p
    assert "Radio is ready. Click the radio tab once to start playback." in p
    assert "—" not in p   # no em-dashes in owner-facing copy
