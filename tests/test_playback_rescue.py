"""#101 Playback rescue: show the YouTube Music tab, briefly, when a background swap did not start
playing, then put the owner back.

navigateYtmTab deliberately swaps the tab in the background so you stay on TuneConsole, but Chrome
gates unmuted playback on a gesture or a high media-engagement score, and a tab that was never
foregrounded often loses that bet: the swap lands silently and nothing plays. These are source
assertions (the extension has no JS runtime under test, matching tests/test_radio_extension.py).
"""
from pathlib import Path

EXT = Path(__file__).resolve().parents[1] / "extension"


def _bg():
    return (EXT / "background.js").read_text()


def test_navigate_arms_the_rescue_on_both_paths():
    src = _bg()
    assert "armPlaybackRescue" in src
    # Both branches of navigateYtmTab need it: reusing an existing background tab AND opening a new
    # background one (a brand-new tab is even less likely to autoplay).
    nav = src[src.index("async function navigateYtmTab"):src.index("async function suppressBeforeUnload")]
    assert nav.count("armPlaybackRescue") == 2, "both the reuse and create branches must arm it"


def test_rescue_waits_before_showing_the_tab():
    """The common case must never flicker: a normal navigate + autoplay reports in well under this."""
    src = _bg()
    assert "RESCUE_WAIT_MS = 2500" in src
    assert "RESCUE_DWELL_MS = 600" in src


def test_a_playing_frame_cancels_the_rescue():
    """The whole point of "if needed": a real playing frame means the swap worked, so never switch."""
    src = _bg()
    assert "cancelPlaybackRescue" in src
    # Cancellation keys off a play frame that is actually PLAYING. content.js reports paused off the
    # real <video>, and a paused play frame must not count as success.
    assert "!msg.paused" in src


def test_rescue_returns_only_when_playback_actually_started():
    """If showing the tab did not help either, Chrome wants a click we cannot fake: stay on the
    player so one click starts it, rather than bouncing back and hiding the problem."""
    src = _bg()
    assert "playingTabs" in src


def test_closing_a_tab_clears_its_rescue_state():
    """playingTabs would otherwise grow for the life of the service worker: the deck branch of
    onRemoved returns early for non-deck tabs, which is most of them."""
    src = _bg()
    removed = src[src.index("chrome.tabs.onRemoved.addListener"):]
    removed = removed[:removed.index("\n});")]
    assert "playingTabs.delete(tabId)" in removed
    assert "cancelPlaybackRescue" in removed


def test_rescue_targets_the_window_the_owner_was_last_in():
    """A service worker has no window of its own, so currentWindow has nothing to resolve against
    and the return hop would silently never happen."""
    src = _bg()
    assert "lastFocusedWindow: true" in src
    assert "currentWindow: true" not in src


def test_rescue_asks_the_tab_to_start_playing_after_foregrounding():
    """Foregrounding often is not enough on its own: once the tab is visible we also nudge the page to
    actually play, rather than leave the owner staring at a paused player."""
    src = _bg()
    rescue = src[src.index("async function runPlaybackRescue"):]
    rescue = rescue[:rescue.index("\n}")]
    assert 'sendMessage(tabId, { type: "ensure-playing" })' in rescue
    # order matters: activate the tab (visibility helps autoplay), THEN ask it to play.
    assert rescue.index("active: true") < rescue.index("ensure-playing")


def _content():
    return (EXT / "content.js").read_text()


def test_content_handles_ensure_playing():
    src = _content()
    assert 'msg.type === "ensure-playing"' in src


def test_ensure_playing_only_acts_when_paused_and_prefers_direct_play():
    """Guard against pausing a tab that is already going, and try video.play() before clicking a
    button (the direct call is what a foregrounded high-engagement tab honours)."""
    src = _content()
    handler = src[src.index('msg.type === "ensure-playing"'):]
    handler = handler[:handler.index("clickMainPlay();") + len("clickMainPlay();")]
    assert "!v.paused) return" in handler           # never toggles an already-playing tab off
    assert "v.play().catch(() => clickMainPlay())" in handler   # direct play first, button fallback


def test_ensure_playing_clicks_the_page_play_button_as_a_fallback():
    """The button the owner pointed at: the page's main play control, used when a direct play() is
    rejected or there is no <video> yet."""
    src = _content()
    assert "ytmusic-play-button-renderer" in src
    """The radio decks have their own waiting-state net (deckWaitingFocused + deck-waiting pevents).
    The rescue is for the plain play path only, and must not double up on deck tabs."""
    src = _bg()
    rescue = src[src.index("async function runPlaybackRescue"):]
    rescue = rescue[:rescue.index("\n}")]
    assert "deckWindowId" not in rescue and "toggleDecks" not in rescue
