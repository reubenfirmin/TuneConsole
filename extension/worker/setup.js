// Setup surfaces receive connection availability only. They never receive listening data.
function createSetupIntegration({musicOrigin, connected, openPlayer}) {
  let connectionChecked = false;
  let destinationPromise = null;
  const setupUrl = "https://tuneconsole.com/extension-setup";
  const connectionWaiters = new Set();
  function isMusic(url) {
    try { return new URL(url).origin === musicOrigin; }
    catch (e) { return false; }
  }

  function status() { return {type: "music-setup-status", connected: connected()}; }

  function receive(msg, sender, sendResponse) {
    if (msg?.type !== "music-setup-status") return;
    const popup = typeof chrome.runtime.getURL === "function" &&
      sender.url === chrome.runtime.getURL("setup.html");
    if (isMusic(sender.url) || popup) sendResponse(status());
  }

  async function publish() {
    connectionChecked = true;
    for (const done of connectionWaiters) done();
    try {
      const tabs = await chrome.tabs.query({url: musicOrigin + "/*"});
      await Promise.all(tabs.filter(tab => isMusic(tab.url)).map(async tab => {
        try { await chrome.tabs.sendMessage(tab.id, status()); } catch (e) {}
      }));
    } catch (e) {}
  }

  async function install() {
    // Re-enabling an extension does not fire onInstalled or re-run content scripts
    // in an existing tab. Install this small, idempotent surface on worker startup.
    try {
      const tabs = await chrome.tabs.query({url: musicOrigin + "/*"});
      await Promise.all(tabs.filter(tab => isMusic(tab.url)).map(async tab => {
        try { await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["music_setup.js"]}); }
        catch (e) {}
      }));
    } catch (e) {}
  }

  function isSite(url) {
    try { return ["https://tuneconsole.com", "https://www.tuneconsole.com"].includes(new URL(url).origin); }
    catch (e) { return false; }
  }

  function isSetup(url) {
    if (!isSite(url)) return false;
    return ["/extension-setup", "/extension-setup/", "/extension-setup.html"].includes(new URL(url).pathname);
  }

  async function chooseDestination() {
    // Give the initial local connection attempt a chance to finish after a reload.
    if (!connected() && !connectionChecked) {
      await new Promise(resolve => {
        const done = () => { clearTimeout(timer); connectionWaiters.delete(done); resolve(); };
        const timer = setTimeout(done, 1500);
        connectionWaiters.add(done);
      });
    }
    try {
      if (connected() && (await openPlayer()).opened) return;
      const tabs = (await chrome.tabs.query({})).filter(tab => isSite(tab.pendingUrl || tab.url));
      const tab = tabs.find(tab => isSetup(tab.pendingUrl || tab.url)) || tabs.find(tab => tab.active) || tabs[0];
      if (tab) {
        const update = {active: true};
        if (!isSetup(tab.pendingUrl || tab.url)) update.url = setupUrl;
        await chrome.tabs.update(tab.id, update);
        if (tab.windowId != null) {
          try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (e) {}
        }
      } else await chrome.tabs.create({url: setupUrl});
    }
    catch (e) {}
  }

  function openDestination() {
    if (!destinationPromise) {
      destinationPromise = chooseDestination().finally(() => { destinationPromise = null; });
    }
    return destinationPromise;
  }

  async function onInstalled(details) {
    if (details?.reason === "install") return openDestination();
    if (details?.reason !== "update") return;
    // Chrome reports unpacked reloads as updates, including runtime.reload().
    // getSelf needs no management permission; store updates remain quiet.
    try {
      if ((await chrome.management.getSelf()).installType === "development") return openDestination();
    } catch (e) {}
  }

  return {receive, publish, install, onInstalled, openDestination};
}
