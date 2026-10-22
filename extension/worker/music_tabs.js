// Sole owner of the music-tab find/create queue and shared fetch-readiness promise.
// Playback uses coordinate() too; readiness waits happen outside that queue.
function createMusicTabs({origin, inject, pingContent}) {
  let coordination = Promise.resolve();
  let ensurePromise = null;

  async function list() {
    const tabs = await chrome.tabs.query({});
    return tabs.filter(tab => {
      try { return new URL(tab.pendingUrl || tab.url).origin === origin; }
      catch (e) { return false; }
    });
  }

  function coordinate(operation) {
    const result = coordination.then(operation);
    coordination = result.catch(() => {});
    return result;
  }

  async function suppressBeforeUnload(tabId) {
    try {
      await chrome.scripting.executeScript({
        target: {tabId}, world: "MAIN",
        func: () => {
          try { window.onbeforeunload = null; } catch (e) {}
          window.addEventListener("beforeunload", e => e.stopImmediatePropagation(), true);
        },
      });
    } catch (e) {}
  }

  // Bounded document wait; remove every temporary listener on completion or removal.
  function waitForComplete(tabId, budgetMs = 30000) {
    return new Promise(resolve => {
      let done = false, timer = null;
      const finish = ok => {
        if (done) return;
        done = true;
        if (timer != null) clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        chrome.tabs.onRemoved.removeListener(onRemoved);
        resolve(ok);
      };
      const onUpdated = (id, info) => {
        if (id === tabId && info.status === "complete") finish(true);
      };
      const onRemoved = id => { if (id === tabId) finish(false); };
      chrome.tabs.onUpdated.addListener(onUpdated);
      chrome.tabs.onRemoved.addListener(onRemoved);
      timer = setTimeout(() => finish(false), budgetMs);
      chrome.tabs.get(tabId, tab => {
        if (chrome.runtime.lastError) { finish(false); return; }
        if (tab && tab.status === "complete" && !tab.pendingUrl) finish(true);
      });
    });
  }

  async function waitForReady(tab) {
    const deadline = Date.now() + 15000;
    if (!await waitForComplete(tab.id, 15000)) return null;
    await inject(tab.id);
    while (Date.now() < deadline) {
      if (await pingContent(tab.id)) return tab;
      await inject(tab.id);
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    return null;
  }

  async function createAndWait() {
    const selected = await coordinate(async () => {
      const [existing] = await list();
      if (existing) return {tab: existing, created: false};
      try {
        return {tab: await chrome.tabs.create({url: `${origin}/`, active: false}), created: true};
      } catch (e) { return null; }
    });
    if (!selected || !selected.tab) return null;
    const {tab, created} = selected;
    return created || tab.status === "loading" || tab.pendingUrl ? waitForReady(tab) : tab;
  }

  function ensure() {
    if (!ensurePromise) {
      ensurePromise = createAndWait().finally(() => { ensurePromise = null; });
    }
    return ensurePromise;
  }

  return {list, coordinate, ensure, suppressBeforeUnload};
}
