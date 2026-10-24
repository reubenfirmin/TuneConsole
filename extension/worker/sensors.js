// Sensor installation, independent isolated/MAIN health, and idle-only repair.
// Playback and request readiness consume this interface; probes never reset play deduplication.
function createSensors({version, revision, receiverRevision, warn, send, suppressBeforeUnload}) {
  const reloadedAt = new Map();

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

  async function ping(tabId, requireMain = true) {
    try {
      const reply = await chrome.tabs.sendMessage(tabId, {type: "sensor-ping", main: requireMain});
      if (!reply || !reply.ok) return {ok: false, error: "invalid sensor reply"};
      // Fetch readiness remains independent of MAIN. Playback needs a receiver which supports
      // exact desired-state reconciliation, including cancellation before a delayed click.
      if (requireMain && reply.receiverRevision !== receiverRevision) {
        return {ok: false, reply, outdatedReceiver: true,
          error: "Outdated rating receiver; reload YouTube Music when playback can be interrupted"};
      }
      if (requireMain && (reply.mainVersion !== version ||
          reply.mainRevision !== revision ||
          typeof reply.mainAgeMs !== "number" || !Number.isFinite(reply.mainAgeMs) ||
          reply.mainAgeMs < 0 || reply.mainAgeMs >= 10000)) {
        return {ok: false, reply, error: "MAIN sensor missing, outdated, or stale"};
      }
      return {ok: true, reply};
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
      let result = await ping(tab.id);
      if (!result.ok) {
        const installed = await inject(tab.id);
        if (installed.ok) reinjected += 1;
        else errors.push(installed.error);
        result = installed.ok ? await ping(tab.id) : result;
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
    const report = {type: "sensor-health", ytmTabs: tabs.length, respondingTabs: responding,
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
