// Public-site integration sees only bridge availability, never music or library state.
function createSiteIntegration({appOrigin, connected}) {
  const globs = ["https://tuneconsole.com/*", "https://www.tuneconsole.com/*"];
  let openPromise = null;

  function isSite(url) {
    try { return ["https://tuneconsole.com", "https://www.tuneconsole.com"].includes(new URL(url).origin); }
    catch (e) { return false; }
  }

  function status() { return {type: "local-player-status", connected: connected()}; }

  function isPlayer(url) {
    try {
      const candidate = new URL(url), app = new URL(appOrigin);
      return candidate.protocol === app.protocol && candidate.port === app.port &&
        [app.hostname, "localhost"].includes(candidate.hostname);
    } catch (e) { return false; }
  }

  function openPlayer() {
    if (!connected()) return Promise.resolve({opened: false, connected: false});
    if (!openPromise) {
      openPromise = (async () => {
        const tabs = (await chrome.tabs.query({})).filter(t => isPlayer(t.pendingUrl || t.url));
        const tab = tabs.find(t => t.active) || tabs[0];
        if (tab) {
          await chrome.tabs.update(tab.id, {active: true});
          if (tab.windowId != null) {
            try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (e) {}
          }
        } else await chrome.tabs.create({url: appOrigin + "/", active: true});
        return {opened: true, connected: true};
      })().finally(() => { openPromise = null; });
    }
    return openPromise;
  }

  async function publish() {
    let tabs;
    try { tabs = await chrome.tabs.query({url: globs}); }
    catch (e) { return; }
    await Promise.all(tabs.map(async tab => {
      if (!isSite(tab.url)) return;
      try { await chrome.tabs.sendMessage(tab.id, status()); }
      catch (e) {
        try {
          await chrome.scripting.insertCSS({target: {tabId: tab.id}, files: ["site_player.css"]});
          await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["site_player.js"]});
          await chrome.tabs.sendMessage(tab.id, status());
        } catch (e) {}
      }
    }));
  }

  function receive(msg, sender, sendResponse) {
    if (!msg || !isSite(sender.url)) return;
    if (msg.type === "local-player-status") sendResponse(status());
    else if (msg.type === "open-local-player") {
      openPlayer().then(sendResponse).catch(() => sendResponse({opened: false, connected: connected()}));
      return true;
    }
  }

  return {publish, receive, openPlayer, status};
}
