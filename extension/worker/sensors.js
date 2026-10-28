// Sensor installation, independent isolated/MAIN health, and idle-only repair.
// Playback and request readiness consume this interface; probes never reset play deduplication.
function createSensors({version = TC_CONFIG.sensorVersion, revision = TC_CONFIG.sensorRevision,
    receiverRevision = TC_CONFIG.receiverRevision,
    minimumRevision = TC_CONFIG.minimumSensorRevision ?? revision,
    minimumReceiverRevision = TC_CONFIG.minimumReceiverRevision ?? receiverRevision,
    warn, send, suppressBeforeUnload}) {
  const reloadedAt = new Map();
  const atLeast = (actual, minimum) => Number.isSafeInteger(actual) && actual >= minimum;

  async function inject(tabId) {
    try {
      await chrome.scripting.executeScript({target: {tabId}, files: ["content.js", "music_setup.js"]});
      await chrome.scripting.executeScript({target: {tabId}, world: "MAIN", files: ["nowplaying_main.js"]});
      await chrome.scripting.executeScript({target: {tabId}, world: "MAIN", files: ["observer_main.js"]});
      return {ok: true, error: ""};
    } catch (e) {
      const error = String(e && e.message ? e.message : e);
      warn("YTM sensor injection failed for tab", tabId, error);
      return {ok: false, error};
    }
  }

  async function injectAll() {
    for (const tab of await chrome.tabs.query({url: "https://music.youtube.com/*"})) await inject(tab.id);
  }

  async function resync() {
    for (const tab of await chrome.tabs.query({url: "https://music.youtube.com/*"})) {
      try { await chrome.tabs.sendMessage(tab.id, {type: "resync-now"}); } catch (e) {}
    }
  }

  // Keep the stricter default for older rating modules which call ping(tabId) directly.
  async function ping(tabId, requireMain = true, {nativeRating = true} = {}) {
    try {
      const reply = await chrome.tabs.sendMessage(tabId, {type: "sensor-ping", main: requireMain});
      if (!reply || !reply.ok) return {ok: false, error: "invalid sensor reply"};
      // Revisions are additive within one protocol version. Legacy bootstrap calls can omit
      // revision options; missing tab revisions request an upgrade without disconnecting it.
      // Native ratings additionally need cancellation/deadline and player-binding support.
      if (requireMain && nativeRating && !atLeast(reply.receiverRevision, minimumReceiverRevision)) {
        return {ok: false, reply, outdatedReceiver: true,
          error: "Outdated rating receiver; reload YouTube Music when playback can be interrupted"};
      }
      if (requireMain && (reply.mainVersion !== version ||
          typeof reply.mainAgeMs !== "number" || !Number.isFinite(reply.mainAgeMs) ||
          reply.mainAgeMs < 0 || reply.mainAgeMs >= 10000)) {
        return {ok: false, reply, error: "MAIN sensor missing, outdated, or stale"};
      }
      if (requireMain && nativeRating && !atLeast(reply.mainRevision, minimumRevision)) {
        return {ok: false, reply, error: "MAIN sensor needs an update for native ratings"};
      }
      return {ok: true, reply, needsUpgrade: requireMain &&
        (!atLeast(reply.receiverRevision, receiverRevision) || !atLeast(reply.mainRevision, revision))};
    } catch (e) {
      return {ok: false, error: String(e && e.message ? e.message : e)};
    }
  }

  async function pingContent(tabId) { return (await ping(tabId, false)).ok; }

  async function reloadUnresponsive(tabId) {
    if (Date.now() - (reloadedAt.get(tabId) || 0) < 60000) return false;
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.audible) return false;
      const results = await chrome.scripting.executeScript({
        target: {tabId},
        func: () => Array.from(document.querySelectorAll("video, audio"))
          .some(media => !media.ended && (!media.paused || media.currentTime > 0)),
      });
      if (results[0]?.result !== false) return false;
      reloadedAt.set(tabId, Date.now());
      await suppressBeforeUnload(tabId);
      await chrome.tabs.reload(tabId);
      return true;
    } catch (e) {
      warn("Could not reload YTM tab", tabId, e);
      return false;
    }
  }

  async function probe() {
    let tabs = [], responding = 0, reinjected = 0, reloading = 0, errors = [], versions = [];
    try { tabs = await chrome.tabs.query({url: "https://music.youtube.com/*"}); }
    catch (e) { errors.push(String(e && e.message ? e.message : e)); }
    for (const tab of tabs) {
      let result = await ping(tab.id, true, {nativeRating: false});
      if (!result.ok || result.needsUpgrade) {
        const installed = await inject(tab.id);
        if (installed.ok) reinjected += 1;
        else if (!result.ok) errors.push(installed.error);
        result = installed.ok ? await ping(tab.id, true, {nativeRating: false}) : result;
      }
      if (result.ok) responding += 1;
      else if (await reloadUnresponsive(tab.id)) reloading += 1;
      else errors.push(`tab ${tab.id}: ${result.error || "sensor did not answer"}`);
      if (versions.length < 8) versions.push({tabId: tab.id, ok: result.ok,
        receiverRevision: result.reply?.receiverRevision ?? null,
        mainVersion: result.reply?.mainVersion ?? null, mainRevision: result.reply?.mainRevision ?? null,
        mainAgeMs: result.reply?.mainAgeMs ?? null,
        capture: result.reply?.capture ?? null});
    }
    const report = {type: "sensor-health", playbackConfirmation: true,
      ytmTabs: tabs.length, respondingTabs: responding,
      reinjectedTabs: reinjected, healthy: tabs.length > 0 && responding === tabs.length,
      recovering: reloading > 0, reloadedTabs: reloading,
      versions,
      error: errors.filter(Boolean).join("; ").slice(0, 500)};
    try { send(report); } catch (e) {}
    return report;
  }

  return {inject, injectAll, resync, ping, pingContent, reloadUnresponsive, probe,
    removed(tabId) { reloadedAt.delete(tabId); }};
}
