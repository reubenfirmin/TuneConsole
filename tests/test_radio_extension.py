import json
import re
from pathlib import Path

EXT = Path(__file__).resolve().parents[1] / "extension"


def test_manifest_bumped_and_permissions_unchanged():
    m = json.loads((EXT / "manifest.json").read_text())
    # Bumped past the 0.1.4 store baseline; an exact pin here broke on every legitimate bump, so
    # assert the floor instead. The permission surface is the real invariant. Floor raised to
    # 0.1.13 with the waiting-state net (probe-order cold start + deck-waiting reporting): extension
    # behavior changed again, so the owner must see a version he can verify after his manual reload.
    assert tuple(int(x) for x in m["version"].split(".")) >= (0, 1, 13)
    assert m["permissions"] == ["cookies", "tabs", "alarms", "scripting"]   # unchanged surface


def test_background_dispatches_radio_prime():
    src = (EXT / "background.js").read_text()
    assert 'frame.type === "radio-prime"' in src
    assert "primeRadioInYtmTab" in src


def test_content_stores_prime_and_hands_off_on_ended():
    src = (EXT / "content.js").read_text()
    assert 'msg.type === "radio-prime"' in src
    assert "primedUrl = msg.url" in src
    # #93 v3: the ended handoff now branches on the dual-deck boundary first (see
    # test_content_handles_boundary_and_keeps_list_guard); the v2 fallback re-sync survives
    # underneath, still gated to a playlist watch url so it never hands off to YTM's own radio.
    assert 'if (p.kind === "ended")' in src
    assert 'primedUrl.includes("list=")' in src


def test_content_radio_prime_null_url_clears_stored_prime():
    # #93 defect 2: /radio/stop sends a clearing frame with url: null; content.js must store that
    # as a clear (never a URL to navigate to), not stringify it into something truthy like "null".
    src = (EXT / "content.js").read_text()
    assert "primedUrl = msg.url || null" in src


def test_background_opens_a_tab_when_no_ytm_tab_exists_for_fetch():
    # Owner-reported gap: with zero music.youtube.com tabs open, every bridge fetch used to fail
    # immediately with "open music.youtube.com". The fetch dispatch path must now open a tab itself
    # (mirroring what navigateYtmTab already does for the navigate control) instead of giving up.
    src = (EXT / "background.js").read_text()
    assert "function ensureYtmTab" in src
    assert "async function handleFrame(frame)" in src
    handle_frame = src.split("async function handleFrame(frame)", 1)[1]
    assert "ensureYtmTab()" in handle_frame
    assert "chrome.tabs.create" in src


def test_background_ensure_ytm_tab_dedupes_concurrent_creates():
    # A second fetch arriving while the first is still waiting on the new tab must not open a
    # second tab: the in-flight "create and wait" promise is shared, not re-triggered.
    src = (EXT / "background.js").read_text()
    assert "let ensureTabPromise" in src
    assert "if (!ensureTabPromise)" in src or "if (ensureTabPromise)" in src


def test_background_manages_deck_window_and_tags_frames():
    # #93 v3 dual-deck (T7j): the deck manager owns a dedicated compact radio window (the
    # probe-verified mechanism, docs/superpowers/plans/2026-07-03-radio-v2.md "Probe results")
    # whose two tabs are the live/standby decks.
    src = (EXT / "background.js").read_text()
    assert 'frame.type === "deck-start"' in src
    assert 'frame.type === "deck-navigate"' in src
    assert 'frame.type === "deck-boundary-config"' in src
    assert 'frame.type === "deck-toggle"' in src
    assert 'frame.type === "deck-stop"' in src
    assert "toggleDecks" in src
    assert "chrome.windows.create" in src            # dedicated radio window (probe-verified mechanism)
    assert "chrome.windows.remove" in src            # deck-stop tears the window down
    assert "chrome.tabs.create" in src               # standby tab creation / self-heal
    assert "sender.tab" in src and '"standby"' in src and '"live"' in src   # frame attribution


def test_background_permission_surface_unchanged_by_deck_manager():
    # chrome.windows.create/remove (the dedicated radio window) need no manifest permission grant in
    # MV3, so the deck manager must not have moved the declared permission surface at all.
    manifest = json.loads((EXT / "manifest.json").read_text())
    assert manifest["permissions"] == ["cookies", "tabs", "alarms", "scripting"]
    assert manifest["host_permissions"] == [
        "https://music.youtube.com/*",
        "http://127.0.0.1:8765/*",
    ]


def test_background_deck_start_and_ready_carry_gen():
    # deck-start {..., gen} identifies one dual-deck "generation"; deck-ready must echo the same gen
    # back verbatim (never recomputed) alongside fallback, so the backend can tell a late/duplicate
    # deck-ready from a stale generation apart from the current one.
    src = (EXT / "background.js").read_text()
    assert "deckGen = frame.gen" in src
    assert 'type: "deck-ready"' in src
    assert "gen: deckGen" in src
    assert "fallback: !!fallback" in src or "fallback: fallback" in src


def test_send_deck_ready_carries_reason():
    # Fallback diagnostics (visibility wave, Part 1): sendDeckReady gains an optional `reason`, carried
    # in the frame as `reason` (null on a non-fallback deck-ready -- nothing to explain there).
    src = (EXT / "background.js").read_text()
    fn = src.split("function sendDeckReady(fallback, reason)", 1)[1]
    fn = fn.split("\n// Close the radio window", 1)[0]
    assert "type: \"deck-ready\"" in fn
    assert "reason: fallback ? (reason || null) : null" in fn


def test_every_fallback_call_site_attaches_a_reason():
    # Every fallback:true call site must pass SOMETHING for the backend to log/surface: the caught
    # error's message for handleDeckStart/handleDeckNavigate/toggleDecks, and a fixed string for the
    # onRemoved path (there is no caught error there, just an observed window close).
    src = (EXT / "background.js").read_text()
    assert src.count("sendDeckReady(true, e && e.message)") >= 4   # deck-start, both navigate self-heal
                                                                    # legs, and toggleDecks
    assert 'sendDeckReady(true, "radio window closed")' in src
    # The one success path is unchanged: no reason to attach when dual actually worked.
    assert "sendDeckReady(false);" in src


def test_background_toggle_echoes_epoch():
    # T7g contract amendment: deck-boundary-config/deck-toggle frames from the backend carry an
    # "epoch"; toggleDecks() must echo it back verbatim in deck-toggled, tracked as module-scope
    # state set whenever one of those frames arrives (never recomputed at toggle time).
    src = (EXT / "background.js").read_text()
    assert "lastDeckEpoch" in src
    assert 'frame.type === "deck-boundary-config"' in src
    assert "lastDeckEpoch = frame.epoch" in src
    assert 'type: "deck-toggled", epoch: lastDeckEpoch' in src


def test_content_handles_boundary_and_keeps_list_guard():
    src = (EXT / "content.js").read_text()
    assert 'msg.type === "radio-boundary"' in src
    assert "boundaryVid" in src
    assert 'type: "deck-boundary"' in src
    assert 'primedUrl.includes("list=")' in src        # fallback never hands off to YTM radio
    assert 'msg.type === "deck-pause' in src            # standby pause (no phantom playback)


def test_content_deck_play_and_skip_at_boundary_intercept():
    # The probe-verified toggle order: the newly-live tab calls play() while still muted (background
    # unmutes right after); S1 intercepts the boundary track's own "next" control so it never hands
    # off to YouTube Music's own radio.
    src = (EXT / "content.js").read_text()
    assert 'msg.type === "deck-play"' in src
    assert "next-button" in src
    assert "preventDefault" in src


def test_toggle_decks_verifies_before_committing_and_degrades_on_failure():
    # Review defect (HIGH): toggleDecks() used to swap ids and ack "deck-toggled" unconditionally,
    # even when the critical steps (activate/deck-play/unmute) silently failed -- a false-positive
    # ack. It must now verify both tabs exist up front, and on ANY failure must NOT swap ids or send
    # deck-toggled: instead degrade via the existing gen-guarded deck-ready fallback path.
    src = (EXT / "background.js").read_text()
    toggle = src.split("async function toggleDecks()", 1)[1]
    body = toggle.split("\nasync function handleDeckStop", 1)[0]
    assert "await chrome.tabs.get(standby)" in body
    assert "await chrome.tabs.get(live)" in body
    assert "throw new Error" in body   # a failed critical step (e.g. deck-play) aborts the commit
    assert "sendDeckReady(true, e && e.message)" in body   # fallback diagnostics: reason attached
    assert "await teardownDeckWindow()" in body
    # The happy-path commit (id swap + deck-toggled ack) must still exist and stay inside the try,
    # after the verified critical steps -- not deleted, not reordered before them.
    assert "liveTabId = standby; standbyTabId = live;" in body
    assert 'type: "deck-toggled", epoch: lastDeckEpoch' in body


def test_toggle_decks_overlap_is_documented_as_deliberate():
    # By-design double-audio overlap (crossfade-like, probe-order-constrained): must not be "fixed",
    # just documented so a future reviewer does not flag it again.
    src = (EXT / "background.js").read_text()
    assert "BY DESIGN" in src
    assert "crossfade" in src.lower()


def test_background_has_window_and_tab_removed_listeners():
    # Review defect (HIGH-adjacent): the radio window/tab can die outside our control (user closes
    # it). onRemoved listeners must exist to clear state and send the same fallback deck-ready as a
    # failed toggle, rather than leaving stale ids around for the next control frame to trip over.
    src = (EXT / "background.js").read_text()
    assert "chrome.windows.onRemoved.addListener" in src
    assert "chrome.tabs.onRemoved.addListener" in src
    onremoved = src.split("chrome.windows.onRemoved.addListener", 1)[1][:600]
    assert 'sendDeckReady(true, "radio window closed")' in onremoved   # fallback diagnostics: fixed reason
    assert "deckWindowId = null" in onremoved


def test_deck_navigate_degrades_on_dead_window():
    # Review defect (MEDIUM): a dead radio WINDOW (not just a dead tab) cannot self-heal by recreating
    # a tab into it. handleDeckNavigate must detect that (windows.get failing, or tabs.create still
    # throwing) and degrade via the fallback deck-ready instead of silently swallowing the frame.
    src = (EXT / "background.js").read_text()
    navigate = src.split("async function handleDeckNavigate(frame)", 1)[1]
    body = navigate.split("\nasync function handleDeckBoundaryConfig", 1)[0]
    assert "chrome.windows.get(deckWindowId)" in body
    # both the windows.get failure and the tabs.create failure, each carrying its own caught reason
    assert body.count("sendDeckReady(true, e && e.message)") >= 2
    assert body.count("await teardownDeckWindow()") >= 2


def test_standby_play_is_self_corrected_in_frame_relay():
    # Review defect (LOW): a recycled standby tab must never be allowed to progress on its own. If a
    # "play" report arrives from the tracked standby tab, background must immediately pause it again,
    # while still tagging+forwarding the frame as "standby" (the backend already drops those).
    src = (EXT / "background.js").read_text()
    relay = src.split('chrome.runtime.onMessage.addListener((msg, sender) => {', 1)[1]
    assert 'msg.type === "play" && deck === "standby"' in relay
    assert 'driveTab(standbyTabId, "deck-pause")' in relay


def test_background_seeds_epoch_from_deck_start_and_persists_it():
    # C1 (final review): lastDeckEpoch started null and was only ever set by deck-boundary-config /
    # deck-toggle frames, so a skip-free first session (no rebuild ever sends either) acked its
    # first natural toggle with deck-toggled {epoch:null}, which the backend's strict echo guard
    # drops -- desyncing physical tabs from logical decks at the very first boundary. deck-start now
    # carries the session epoch; handleDeckStart must seed lastDeckEpoch from it, and the value must
    # ride saveDeckState/restoreDeckState so an SW restart mid-deck does not reproduce the same
    # null-echo drop at the next local boundary.
    src = (EXT / "background.js").read_text()
    start = src.split("async function handleDeckStart(frame)", 1)[1]
    start = start.split("\nasync function handleDeckNavigate", 1)[0]
    assert "lastDeckEpoch = frame.epoch" in start
    save = src.split("function saveDeckState()", 1)[1][:500]
    assert "lastDeckEpoch" in save
    restore = src.split("async function restoreDeckState()", 1)[1][:900]
    assert "lastDeckEpoch" in restore


def test_background_arms_standby_boundary_at_deck_start_and_rearms_after_navigation():
    # C2 (final review): the initial standby deck's boundary was never armed (only the live tab
    # got frame.boundaryVideoId), and every rebuild's radio-boundary raced the deck-navigate hard
    # reload -- so a promoted deck's last-track end could fall through to audible YTM autoplay (S2
    # only) or a stall. Background must remember each deck's target boundary vid, arm the standby
    # at deck-start, and re-arm deterministically from the tabs.onUpdated status=="complete"
    # listener once a navigated deck tab finishes loading.
    src = (EXT / "background.js").read_text()
    assert "standbyBoundaryVid" in src and "liveBoundaryVid" in src
    # deck-start arms BOTH decks' boundaries, not just the live one's
    start = src.split("async function handleDeckStart(frame)", 1)[1]
    start = start.split("\nasync function handleDeckNavigate", 1)[0]
    assert "frame.standbyBoundaryVideoId" in start
    assert 'driveTab(standbyTabId, "radio-boundary"' in start
    # deck-boundary-config records the per-role vid for later re-arming (and persists it)
    cfg = src.split("async function handleDeckBoundaryConfig(frame)", 1)[1]
    cfg = cfg.split("\n// The swap itself", 1)[0]
    assert "standbyBoundaryVid = frame.videoId" in cfg
    assert "liveBoundaryVid = frame.videoId" in cfg
    assert "saveDeckState()" in cfg
    # the onUpdated deck listener re-arms after the navigated document reports complete
    upd = src.split("chrome.tabs.onUpdated.addListener", 1)[1]
    upd = upd.split("chrome.windows.onRemoved", 1)[0]
    assert '"complete"' in upd
    assert 'driveTab(standbyTabId, "radio-boundary", { videoId: standbyBoundaryVid })' in upd
    assert 'driveTab(liveTabId, "radio-boundary", { videoId: liveBoundaryVid })' in upd
    # persisted across SW restarts, and swapped tab-correctly on toggle
    save = src.split("function saveDeckState()", 1)[1][:500]
    assert "standbyBoundaryVid" in save and "liveBoundaryVid" in save
    toggle = src.split("async function toggleDecks()", 1)[1]
    toggle = toggle.split("\nasync function handleDeckStop", 1)[0]
    assert "liveBoundaryVid = standbyBoundaryVid" in toggle


def test_backend_deck_start_frame_carries_epoch_and_standby_boundary():
    # Backend half of the C1/C2 contract, pinned as source text alongside the extension halves so
    # the pair cannot drift apart silently (the WS-level pin lives in test_radio_routes.py's
    # test_start_dual_deck_start_carries_epoch_and_standby_boundary).
    bridge_src = (Path(__file__).resolve().parents[1]
                  / "src/yt_playlist/web/routes/bridge.py").read_text()
    deck_start = bridge_src.split('"type": "deck-start"', 1)[1][:600]
    assert '"epoch": deck_epoch' in deck_start
    assert '"standbyBoundaryVideoId"' in deck_start


def test_background_deck_boundary_is_sender_gated_to_live_tab():
    # L6 (final review): a stray `ended` from the old, still-armed standby tab (user manually plays
    # it before its rebuild navigate lands) must not fire a spurious toggle: the deck-boundary
    # runtime message only toggles when it came from the tracked LIVE tab.
    src = (EXT / "background.js").read_text()
    relay = src.split('chrome.runtime.onMessage.addListener((msg, sender) => {', 1)[1]
    gate = relay.split('if (!msg) return;', 1)[0]
    assert 'msg.type === "deck-boundary"' in gate
    assert "sender.tab.id === liveTabId" in gate
    assert "await ensureDeckStateRestored()" in gate   # SW woken by this message restores ids first


def test_background_onremoved_listeners_await_restored_state():
    # L7 (final review): an SW woken by the window/tab close must restore the tracked ids before
    # reading them, or it can read null and skip the fallback signal entirely.
    src = (EXT / "background.js").read_text()
    win = src.split("chrome.windows.onRemoved.addListener", 1)[1][:700]
    assert "await ensureDeckStateRestored()" in win
    tab = src.split("chrome.tabs.onRemoved.addListener", 1)[1][:500]
    assert "await ensureDeckStateRestored()" in tab


def test_content_clears_boundary_before_firing_deck_boundary():
    # L9 (final review): one boundary, one toggle. content.js must clear boundaryVid before firing
    # deck-boundary (both the `ended` handoff and the S1 next-click intercept), so a re-delivered
    # ended / double click cannot send a second toggle trigger; background re-arms explicitly.
    src = (EXT / "content.js").read_text()
    ended = src.split('if (p.kind === "ended")', 1)[1][:900]
    assert "boundaryVid = null" in ended.split('type: "deck-boundary"', 1)[0]
    # Anchor on the S1 next-click intercept's specific signature (not just any "click" listener): the
    # new deck-play waiting-retry listener (added for the waiting-state net) also registers a
    # document click listener, with a different signature, earlier in the file.
    click = src.split('document.addEventListener("click", (ev) => {', 1)[1][:900]
    assert "boundaryVid = null" in click.split('type: "deck-boundary"', 1)[0]


def test_deck_play_reports_waiting_for_missing_video_and_rejected_play():
    # F1: content.js used to silently no-op when deck-play arrived with no <video> element yet (SPA
    # player not constructed despite the tab reporting complete) -- no play(), no rejection, no
    # report. It must now route through armWaitingRetry with a distinguishing "no-video" err, same as
    # a genuine play() rejection routes through it with e.name. Neither path may throw.
    src = (EXT / "content.js").read_text()
    deck_play = src.split('if (msg.type === "deck-play")', 1)[1]
    deck_play = deck_play.split('if (msg.type === "resync-now")', 1)[0]
    # The old silent catch/no-op shapes are gone from THIS handler.
    assert "v.play().catch(() => {})" not in deck_play
    assert "if (v && v.paused)" not in deck_play
    # Missing-video path: distinguishing err, no play() call attempted.
    assert 'if (!v) {' in deck_play
    assert 'armWaitingRetry("no-video");' in deck_play
    # Rejected-play path: routes through the same helper with the real error name.
    assert "} else if (v.paused) {" in deck_play
    assert "v.play().catch((e) => armWaitingRetry(e && e.name));" in deck_play


def test_waiting_retry_rearms_on_failure_without_spending_episode():
    # F2: the one-time activation retry's own play() can also fail (autoplay still blocked). The old
    # behavior silently swallowed that and left the episode "spent": no further report, no further
    # listeners, so a second click did nothing extension-side. It must now re-report deck-waiting and
    # re-install a fresh listener pair so the next click/keydown retries again -- guarded so pairs
    # cannot stack (only re-arms once the previous pair has actually fired).
    src = (EXT / "content.js").read_text()
    arm = src.split("const armWaitingRetry = (errName) => {", 1)[1]
    arm = arm.split("\n  chrome.runtime.onMessage.addListener", 1)[0]
    # Reports deck-waiting with the distinguishing err before arming.
    assert 'kind: "deck-waiting", detail: { err: errName }' in arm
    # Any previously-armed pair is cleared before a new one is installed (nit: no stacking), both on
    # entry to armWaitingRetry and again right before the retry fires.
    assert arm.count("clearWaitingRetry();") >= 2
    # The retry itself is guarded against double-firing (click+keydown racing each other)...
    assert "let fired = false;" in arm and "if (fired) return;" in arm
    # ...and on a rejected retry, it recurses back into armWaitingRetry (re-report + re-arm) rather
    # than a bare swallow.
    retry_play = arm.split("const retryPlay = () => {", 1)[1]
    assert "v2.play().catch((e3) => {" in retry_play
    assert "armWaitingRetry(e3 && e3.name);" in retry_play
    assert "v2.play().catch(() => {})" not in retry_play
    # Handler reference is tracked so a later arm (new episode, or this same re-arm) can remove the
    # previous un-fired pair instead of accumulating (nit 5).
    assert "waitingRetryHandler = retryPlay;" in arm


def test_handle_deck_start_mutes_before_play_and_unmutes_only_after():
    # Part A: cold start must mirror toggleDecks' probe-verified order -- mute the live tab, wait for
    # load + injection, THEN deck-play, verify delivery, and only then unmute. Pin the ORDER (not just
    # presence) via string offsets, the same technique test_background_toggle_echoes_epoch-style tests
    # use elsewhere in this file.
    src = (EXT / "background.js").read_text()
    start = src.split("async function handleDeckStart(frame)", 1)[1]
    start = start.split("\nasync function handleDeckNavigate", 1)[0]
    mute_idx = start.index("chrome.tabs.update(liveTabId, { muted: true }")
    play_idx = start.index('driveTab(liveTabId, "deck-play")')
    unmute_idx = start.index("chrome.tabs.update(liveTabId, { muted: false }")
    assert mute_idx < play_idx < unmute_idx
    # The deck-play delivery is verified (critical-step discipline, same as toggleDecks): a failed
    # delivery throws instead of a false-positive unmute.
    assert 'if (!(await driveTab(liveTabId, "deck-play"))) throw new Error(' in start
    # F6: a waitForTabComplete timeout is a failed critical step too (same discipline), for BOTH the
    # live and standby tab loads -- neither may be silently ignored the way the old bare `await
    # waitForTabComplete(...)` (no return-value check) did.
    live_wait_idx = start.index('if (!(await waitForTabComplete(liveTabId))) throw new Error(')
    standby_wait_idx = start.index('if (!(await waitForTabComplete(standbyTabId))) throw new Error(')
    assert live_wait_idx < play_idx
    assert live_wait_idx < standby_wait_idx < play_idx


def test_wait_for_tab_complete_has_deadline_and_times_out_to_false():
    # F6 (pre-existing gap, now fixed): waitForTabComplete used to have no deadline at all, so a live
    # tab that never reaches "complete" hung handleDeckStart forever with no deck-ready in either
    # direction. It must now resolve false after a bounded budget (30s default) instead of hanging,
    # so callers (handleDeckStart) can treat a timeout as a failed critical step degrading via
    # sendDeckReady(true), exactly like any other probe failure in this file.
    src = (EXT / "background.js").read_text()
    fn = src.split("function waitForTabComplete(tabId, budgetMs) {", 1)[1]
    fn = fn.split("\n// Non-destructive probe", 1)[0]
    assert "budgetMs == null ? 30000 : budgetMs" in fn
    assert "setTimeout(() => finish(false), budgetMs)" in fn
    # Completion still resolves true (the happy path callers rely on).
    assert "if (id === tabId && info.status === \"complete\") finish(true);" in fn
    # The timer/listener are cleaned up once settled, so a late completion after a timeout (or vice
    # versa) cannot double-resolve or leak the onUpdated listener.
    assert "if (done) return;" in fn
    assert "chrome.tabs.onUpdated.removeListener(onUpdated);" in fn
    # Other waitForTabComplete callers still degrade sensibly with the new deadline: createAndWaitFor-
    # YtmTab already races it against its OWN 15s timeout and ignores the resolved value (it falls
    # through to the inject/ping loop's own bounded retry regardless), so it is unaffected by the
    # longer 30s default.
    caller = src.split("async function createAndWaitForYtmTab() {", 1)[1][:800]
    assert "Promise.race([waitForTabComplete(tab.id), new Promise((r) => setTimeout(r, 15000))])" in caller
    # toggleDecks and handleDeckNavigate do not call waitForTabComplete at all (they verify tab/window
    # liveness directly via chrome.tabs.get/chrome.windows.get instead), so a hung load there was never
    # this function's problem to begin with; nothing to change in those paths.
    assert "waitForTabComplete" not in src.split("async function toggleDecks() {", 1)[1].split("\nasync function handleDeckStop", 1)[0]
    assert "waitForTabComplete" not in src.split("async function handleDeckNavigate(frame) {", 1)[1].split("\nasync function handleDeckBoundaryConfig", 1)[0]


def test_background_focuses_radio_window_once_per_waiting_episode():
    # Waiting-state net: a "deck-waiting" pevent from a tracked deck tab focuses the radio window,
    # guarded so it fires at most once per episode, and the guard resets on the next real deck play.
    src = (EXT / "background.js").read_text()
    assert "let deckWaitingFocused = false;" in src
    relay = src.split('chrome.runtime.onMessage.addListener((msg, sender) => {', 1)[1]
    assert 'msg.type === "pevent" && msg.kind === "deck-waiting"' in relay
    assert "chrome.windows.update(deckWindowId, { focused: true })" in relay
    assert "deckWaitingFocused = true" in relay
    assert 'msg.type === "play" && (deck === "live" || deck === "standby")' in relay
    assert "deckWaitingFocused = false" in relay


def test_tabs_create_never_passes_muted_createproperties():
    # Live bug (owner's SW console, 2026-07-05): chrome.tabs.create's createProperties does NOT accept
    # a `muted` key -- it is chrome.tabs.update-only -- so handleDeckNavigate's self-heal passing
    # `muted` (shorthand for `muted: muted`) threw `TypeError: Unexpected property: 'muted'` on EVERY
    # invocation, which permanently degraded the session to single-tab fallback on any deck tab loss:
    # exactly the silent failure this visibility wave exists to instrument. node --check cannot catch
    # an invalid (but syntactically valid) Chrome API property, so pin it here as a source-text check
    # instead: no chrome.tabs.create({...}) call site may carry a `muted` key (explicit `muted: ...` or
    # object-shorthand `muted`) in its argument object. Mute via a SEPARATE chrome.tabs.update call
    # after creation instead -- the pattern handleDeckStart's standby tab creation, and the fixed
    # self-heal in handleDeckNavigate, both use.
    src = (EXT / "background.js").read_text()
    # A `muted` KEY, not merely the substring (e.g. `active: !muted` is a value reference, not a key):
    # not preceded by "!" or a word char, not followed by a word char, then optionally whitespace and
    # one of `:` (explicit) / `,` or `}` (shorthand, end of the property).
    muted_key = re.compile(r"(?<![!\w])muted(?!\w)\s*(?::|[,}])")
    sites = list(re.finditer(r"chrome\.tabs\.create\(\{(.*?)\}\)", src, re.DOTALL))
    assert len(sites) >= 3, "expected to find every chrome.tabs.create call site in the file"
    for m in sites:
        # Search the FULL match (group 0), not the brace interior (group 1): shorthand `muted` as the
        # LAST property needs the closing `}` to satisfy the key pattern's `[,}]` terminator, and
        # group(1) stops before it, which let exactly that shape slip the pin.
        assert not muted_key.search(m.group(0)), \
            f"chrome.tabs.create call site passes a muted createProperty: {m.group(0)!r}"


def test_deck_navigate_self_heal_mutes_via_separate_update_call():
    # The actual fix: the self-heal tab creation drops `muted` from tabs.create and mutes via a
    # follow-up tabs.update, same shape as handleDeckStart's standby tab (mute AFTER create, never in
    # createProperties).
    src = (EXT / "background.js").read_text()
    navigate = src.split("async function handleDeckNavigate(frame)", 1)[1]
    body = navigate.split("\nasync function handleDeckBoundaryConfig", 1)[0]
    create_idx = body.index("chrome.tabs.create({ windowId: deckWindowId, url: frame.url, active: !muted })")
    update_idx = body.index('chrome.tabs.update(t.id, { muted: true })')
    assert create_idx < update_idx


def test_deck_control_handlers_await_restored_state():
    # Review defect (LOW): a service-worker wake on an incoming deck-* frame can race
    # restoreDeckState()'s async storage read. Every deck-* control handler must await a memoized
    # restore promise before touching tracked ids.
    src = (EXT / "background.js").read_text()
    assert "let restoreDeckStatePromise" in src
    assert "function ensureDeckStateRestored" in src
    for fn in [
        "async function handleDeckStart(frame) {",
        "async function handleDeckNavigate(frame) {",
        "async function handleDeckBoundaryConfig(frame) {",
        "async function toggleDecks() {",
        "async function handleDeckStop() {",
    ]:
        chunk = src.split(fn, 1)[1][:200]
        assert "await ensureDeckStateRestored()" in chunk, f"{fn} missing restore await"
