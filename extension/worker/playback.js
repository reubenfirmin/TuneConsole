// Owns playback selection, pending observations, the ordered relay, and one rescue episode.
// All tab creation/navigation uses musicTabs.coordinate(); controls never create tabs.
function createPlayback({origin, appOrigin, musicTabs, sensors, send, warn}) {
  const playingTabs = new Set(), mediaMutedTabs = new Set(), pendingPlays = new Map();
  let playbackTabId = null, rescue = null, relay = Promise.resolve();
  const ratings = createNativeRatings({pickTab, sensors, send});
  const RESCUE_WAIT_MS = 2500, RESCUE_DWELL_MS = 600, RESCUE_RETRY_MS = 500, RESCUE_TIMEOUT_MS = 6000;

  async function pickTab() {
    const tabs = await musicTabs.list();
    if (!tabs.length) return null;
    const unmuted = tabs.filter(t => !t.mutedInfo?.muted && !mediaMutedTabs.has(t.id));
    const playing = unmuted.find(t => t.audible) ||
      unmuted.find(t => t.id === playbackTabId && playingTabs.has(t.id)) ||
      unmuted.find(t => playingTabs.has(t.id)) ||
      unmuted.find(t => t.id === playbackTabId) || tabs.find(t => t.id === playbackTabId);
    return playing || tabs.find(t => t.active) ||
      tabs.slice().sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
  }

  function cancelRescue() {
    if (!rescue) return;
    clearTimeout(rescue.timer);
    clearTimeout(rescue.retryTimer);
    rescue = null;
  }

  function matchesRescue(episode, state) {
    const playlist = value => (value || '').replace(/^RDAMPL/, '');
    return (!episode.videoId || state.videoId === episode.videoId) &&
      (!episode.playlist || playlist(state.playlist) === playlist(episode.playlist));
  }

  function armRescue(tabId, url) {
    cancelRescue();
    playingTabs.delete(tabId);
    mediaMutedTabs.delete(tabId);
    pendingPlays.delete(tabId);
    const target = new URL(url);
    rescue = {tabId, url: target.href, videoId: target.searchParams.get('v') || '',
      playlist: target.searchParams.get('list') || '',
      foregrounded: false, timer: setTimeout(() => runRescue(tabId), RESCUE_WAIT_MS)};
  }

  async function runRescue(tabId) {
    const episode = rescue;
    if (!episode || episode.tabId !== tabId || episode.foregrounded) return;
    let returnTab = null;
    try {
      const [current] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
      if (current && current.id !== tabId) returnTab = current;
    } catch (e) {}
    if (rescue !== episode) return;
    episode.foregrounded = true;
    try {
      const shown = await chrome.tabs.update(tabId, {active: true});
      if (rescue !== episode) return;
      if (returnTab && shown.windowId != null && shown.windowId !== returnTab.windowId) {
        await chrome.windows.update(shown.windowId, {focused: true});
      }
    } catch (e) {
      if (rescue === episode) cancelRescue();
      return;
    }
    if (rescue !== episode) return;
    episode.returnTab = returnTab;
    episode.startedAt = Date.now();
    episode.timer = setTimeout(() => {
      warn("Playback nudge timed out", tabId);
      return finishRescue(episode);
    }, RESCUE_TIMEOUT_MS);
    attemptRescue(episode);
  }

  async function attemptRescue(episode) {
    if (rescue !== episode || episode.finishing || episode.confirmed) return;
    episode.retryTimer = setTimeout(() => attemptRescue(episode), RESCUE_RETRY_MS);
    if (episode.inFlight) return;
    episode.inFlight = true;
    try {
      const reply = await chrome.tabs.sendMessage(episode.tabId, {type: "ensure-playing",
        url: episode.url, videoId: episode.videoId, playlist: episode.playlist});
      if (rescue !== episode || episode.finishing || !reply?.playing || !matchesRescue(episode, reply)) return;
      playingTabs.add(episode.tabId);
      episode.confirmed = true;
      clearTimeout(episode.retryTimer);
      clearTimeout(episode.timer);
      const remaining = Math.max(0, RESCUE_DWELL_MS - (Date.now() - episode.startedAt));
      episode.timer = setTimeout(() => finishRescue(episode), remaining);
    } catch (e) {
      if (rescue === episode && !episode.finishing) await sensors.inject(episode.tabId);
    } finally { episode.inFlight = false; }
  }

  async function finishRescue(episode) {
    if (rescue !== episode || episode.finishing) return;
    episode.finishing = true;
    clearTimeout(episode.timer);
    clearTimeout(episode.retryTimer);
    const {tabId, returnTab} = episode;
    try {
      const fromApp = returnTab && (returnTab.url || "").startsWith(appOrigin + "/");
      if (!returnTab || (!fromApp && !playingTabs.has(tabId))) return;
      const [current] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
      if (rescue !== episode || !current || current.id !== tabId) return;
      await chrome.tabs.update(returnTab.id, {active: true});
      if (rescue === episode && returnTab.windowId != null && returnTab.windowId !== current.windowId) {
        await chrome.windows.update(returnTab.windowId, {focused: true});
      }
    } catch (e) {}
    finally { if (rescue === episode) cancelRescue(); }
  }

  async function driveTab(tabId, type) {
    try { await chrome.tabs.sendMessage(tabId, {type}); }
    catch (e) {
      await sensors.inject(tabId);
      try { await chrome.tabs.sendMessage(tabId, {type}); } catch (e2) {}
    }
  }

  async function pauseOthers(keepId) {
    const tabs = await musicTabs.list();
    await Promise.all(tabs.filter(t => t.id !== keepId).map(async tab => {
      await driveTab(tab.id, "pause");
      playingTabs.delete(tab.id);
    }));
  }

  async function navigate(url) {
    try { if (new URL(url).origin !== origin) return; }
    catch (e) { return; }
    return musicTabs.coordinate(async () => {
      const tab = await pickTab();
      if (tab) {
        try {
          await musicTabs.suppressBeforeUnload(tab.id);
          await chrome.tabs.update(tab.id, {url});
          playbackTabId = tab.id;
          armRescue(tab.id, url);
          await pauseOthers(tab.id);
        } catch (e) { warn("Navigate update failed", e); }
      } else {
        try {
          const made = await chrome.tabs.create({url, active: false});
          if (made && made.id != null) {
            playbackTabId = made.id;
            armRescue(made.id, url);
            await pauseOthers(made.id);
          }
        } catch (e) {}
      }
    }).catch(e => warn("Navigate failed", e));
  }

  async function next(videoId) {
    const message = {type: "next", videoId};
    if (!videoId) return {ok: false, error: "Displayed song is missing"};
    const tab = await pickTab();
    if (!tab) return {ok: false, error: "No music player is available"};
    const deliver = () => chrome.tabs.sendMessage(tab.id, message);
    try { return await deliver() || {ok: false}; }
    catch (e) {
      if (!isMissingReceiver(e)) return {ok: false, error: String(e)};
      await sensors.inject(tab.id);
      try { return await deliver() || {ok: false}; }
      catch (retryError) { return {ok: false, error: String(retryError)}; }
    }
  }

  async function playPause(frame = {}) {
    // A retained paused player must never fall through to an unrelated browsing tab.
    const tab = frame.resumeOnly ? (await musicTabs.list()).find(t => t.id === playbackTabId) : await pickTab();
    if (!tab) {
      if (frame.resumeOnly) send({type: "player-closed"});
      return {ok: false, error: "No music player is available"};
    }
    // A manual pause must win over startup recovery, including an in-flight nudge.
    if (frame.action !== "play" && rescue?.tabId === tab.id &&
        (!frame.videoId || !rescue.videoId || frame.videoId === rescue.videoId)) cancelRescue();
    const confirmed = Number.isSafeInteger(frame.id) && frame.id > 0;
    const guarded = confirmed || frame.resumeOnly || frame.action;
    if (guarded) {
      const minimum = confirmed ? 14 : 9;
      let receiver = await sensors.ping(tab.id, false);
      if (!(receiver.reply?.receiverRevision >= minimum)) {
        await sensors.inject(tab.id);
        receiver = await sensors.ping(tab.id, false);
      }
      if (!(receiver.reply?.receiverRevision >= minimum))
        return {ok: false, error: "Reload YouTube Music to update its TuneConsole controls"};
    }
    const message = {type: guarded ? "player-control" : "playpause"};
    if (frame.action) message.action = frame.action;
    if (confirmed) Object.assign(message, {confirm: true, videoId: frame.videoId});
    if (frame.resumeOnly) Object.assign(message, {resumeOnly: true, videoId: frame.videoId});
    const deliver = async () => await chrome.tabs.sendMessage(tab.id, message) ||
      {ok: false, error: "Playback was not confirmed. Check YouTube Music before trying again."};
    try { return await deliver(); }
    catch (e) {
      if (!isMissingReceiver(e)) return {ok: false, error: "Playback was not confirmed. Check YouTube Music before trying again."};
      await sensors.inject(tab.id);
      try { return await deliver(); }
      catch (e2) { return {ok: false, error: "The YouTube Music player is unavailable"}; }
    }
  }

  async function refreshView(playlist) {
    for (const tab of await musicTabs.list()) {
      try { await chrome.tabs.sendMessage(tab.id, {type: "refresh-view", playlist}); }
      catch (e) {
        await sensors.inject(tab.id);
        try { await chrome.tabs.sendMessage(tab.id, {type: "refresh-view", playlist}); } catch (e2) {}
      }
    }
  }

  async function pauseForTv() {
    // Cancel a pending playback rescue before it can resume audio behind TV.
    cancelRescue();
    return musicTabs.coordinate(async () => {
      for (const tab of await musicTabs.list()) {
        let receiver = await sensors.ping(tab.id, false);
        if (!(receiver.reply?.receiverRevision >= 11)) {
          await sensors.inject(tab.id);
          receiver = await sensors.ping(tab.id, false);
        }
        if (!(receiver.reply?.receiverRevision >= 11))
          return {ok: false, error: 'Reload the TuneConsole browser extension'};
        const reply = await chrome.tabs.sendMessage(tab.id, {type: 'pause-for-tv'});
        if (!reply?.ok || reply.paused !== true) return {ok: false, error: 'Audio pause was not confirmed'};
        playingTabs.delete(tab.id);
      }
      return {ok: true, paused: true};
    });
  }

  async function relayMessage(msg, sender) {
    if (msg.type === 'pevent' && ['rate', 'playlist_edit', 'feedback', 'subscription', 'share_intent'].includes(msg.kind)
        && msg.brandKnown !== true) return;
    const srcTabId = sender && sender.tab && sender.tab.id;
    if ((msg.type === "play" || msg.type === "now-heartbeat") && srcTabId != null) {
      msg = Object.assign({}, msg, {muted: !!msg.muted || msg.volume === 0});
      if (msg.type === "play") pendingPlays.set(srcTabId, msg);
      if (msg.muted) mediaMutedTabs.add(srcTabId);
      else mediaMutedTabs.delete(srcTabId);
      if (msg.paused || msg.muted) playingTabs.delete(srcTabId);
      else playingTabs.add(srcTabId);
      const owner = await pickTab();
      playbackTabId = owner ? owner.id : null;
      if (srcTabId !== playbackTabId) return;
      msg = Object.assign({}, msg, {tabMuted: !!owner.mutedInfo?.muted,
        muted: !!msg.muted || msg.volume === 0});
      const pending = pendingPlays.get(srcTabId);
      if (msg.type === "now-heartbeat" && !msg.paused && !msg.muted && !owner.mutedInfo?.muted &&
          msg.brandKnown && (msg.metadataVerified !== false || msg.videoVerified === true) && pending &&
          msg.videoId === pending.videoId && msg.brandId === pending.brandId) {
        msg = Object.assign({}, pending, msg, {type: "play"});
      }
      // Navigation can report the old song, or an unpaused player whose clock is
      // stalled. Keep the foreground rescue until the requested queue is audible.
      if (!msg.paused && !msg.muted && !owner.mutedInfo?.muted &&
          rescue && rescue.tabId === srcTabId && !rescue.foregrounded &&
          owner.audible && msg.metadataVerified !== false && matchesRescue(rescue, msg)) cancelRescue();
      if (msg.type === "play" && (msg.muted || msg.tabMuted || msg.paused)) {
        msg = Object.assign({}, msg, {type: "now-heartbeat"});
      }
    }
    if (msg.type === "pevent" && ["state", "tick", "bye"].includes(msg.kind)) {
      const owner = await pickTab();
      if (owner) playbackTabId = owner.id;
      if (!owner || srcTabId !== playbackTabId) return;
      // Native events can arrive before the matching now-playing heartbeat.
      // Use both their sampled media state and the last known state for legacy
      // sensors. A muted sole tab can still be selected for controls.
      if ((msg.kind === "tick" || (msg.kind === "state" && msg.state === "playing")) &&
          (owner.mutedInfo?.muted || mediaMutedTabs.has(srcTabId) ||
           msg.muted || msg.volume === 0 || msg.paused)) return;
    }
    if (msg.type === "play") {
      const delivered = send({type: "play", title: msg.title, artist: msg.artist,
        metadataVerified: msg.metadataVerified !== false,
        videoVerified: msg.videoVerified === true,
        thumbnail: msg.thumbnail, likeStatus: msg.likeStatus, videoId: msg.videoId,
        playlist: msg.playlist || "", albumBrowseId: msg.albumBrowseId || "",
        position: msg.position, duration: msg.duration,
        brandId: msg.brandId || "", brandKnown: msg.brandKnown, paused: !!msg.paused, muted: !!msg.muted,
        tabMuted: !!msg.tabMuted, timeZone: msg.timeZone});
      if (delivered) pendingPlays.delete(srcTabId);
    } else if (msg.type === "now-heartbeat") {
      send({type: "now-heartbeat", title: msg.title, artist: msg.artist,
        metadataVerified: msg.metadataVerified !== false,
        videoVerified: msg.videoVerified === true,
        thumbnail: msg.thumbnail, likeStatus: msg.likeStatus, videoId: msg.videoId,
        playlist: msg.playlist || "", albumBrowseId: msg.albumBrowseId || "", paused: !!msg.paused,
        position: msg.position, duration: msg.duration,
        muted: !!msg.muted, tabMuted: !!msg.tabMuted});
    } else if (msg.type === "pevent") send(msg);
  }

  function receive(msg, sender) {
    if (!msg || !["play", "now-heartbeat", "pevent"].includes(msg.type)) return;
    ratings.observe(msg, sender?.tab?.id);
    relay = relay.then(() => relayMessage(msg, sender)).catch(error => warn("Playback relay failed", error));
  }

  function removed(tabId) {
    ratings.removed(tabId);
    playingTabs.delete(tabId);
    mediaMutedTabs.delete(tabId);
    pendingPlays.delete(tabId);
    if (playbackTabId === tabId) {
      playbackTabId = null;
      relay = relay.then(() => send({type: "player-closed"}));
    }
    if (rescue && rescue.tabId === tabId) cancelRescue();
  }

  return {pickTab, navigate, rate: ratings.rate, next, playPause, pauseForTv, refreshView, receive, removed, cancelRescue,
    get drained() { return relay; }};
}
