// Runs only on the public TuneConsole site. The service worker owns local-app detection.
(() => {
  if (!["https://tuneconsole.com", "https://www.tuneconsole.com"].includes(location.origin)) return;
  if (window.__tcSitePlayerInstalled) return;
  window.__tcSitePlayerInstalled = true;

  const BADGE_ID = "tuneconsole-local-player";
  const DIALOG_ID = "tuneconsole-local-player-dialog";
  const LOCAL_URL = "http://127.0.0.1:8765/";
  const DOWNLOADS = "https://github.com/reubenfirmin/TuneConsole/releases/latest/download/";
  let connected = false, revision = 0, setupNavigating = false;
  let connectionChecked = false, setupNeeded = false;

  function platformInstructions() {
    const platform = (navigator.userAgentData?.platform || navigator.platform || navigator.userAgent).toLowerCase();
    if (platform.includes("mac") && navigator.maxTouchPoints < 2) {
      return `<p>Open <strong>TuneConsole</strong> from Applications.</p>
        <p>Need to install it? Download the app for your Mac:</p>
        <div class="tc-player-downloads">
          <a href="${DOWNLOADS}TuneConsole-macos-arm64.dmg" target="_blank" rel="noopener">Apple Silicon</a>
          <a href="${DOWNLOADS}TuneConsole-macos-x86_64.dmg" target="_blank" rel="noopener">Intel</a>
        </div>
        <p>Open the DMG and drag TuneConsole to Applications.</p>
        <p>We’re in beta. The Mac app isn’t Apple notarized yet. Try opening it once and close the blocked-launch alert. In <strong>System Settings</strong>, choose <strong>Privacy &amp; Security</strong>, find TuneConsole under Security, and click <strong>Open Anyway</strong>. Authenticate and confirm opening the app. If the option is missing, try opening TuneConsole again.</p>`;
    }
    if (platform.includes("linux") && !/Android/i.test(navigator.userAgent)) {
      return `<p>Open <strong>TuneConsole</strong> from your applications menu, or run:</p>
        <pre><code>flatpak run com.tuneconsole.TuneConsole</code></pre>
        <p>Need to install it? <a href="${DOWNLOADS}TuneConsole.flatpak" target="_blank" rel="noopener">Download the Linux Flatpak</a>, install it, then open TuneConsole.</p>`;
    }
    if (platform.includes("win")) {
      return `<p>Start Docker Desktop, then start your existing TuneConsole container:</p>
        <pre><code>docker start tuneconsole</code></pre>
        <details><summary>First time? Create the container</summary>
          <pre><code>docker run -d --name tuneconsole -p 127.0.0.1:8765:8765 -v tuneconsole-data:/data ghcr.io/reubenfirmin/tuneconsole:latest</code></pre>
        </details>`;
    }
    return `<p>Install and open TuneConsole on this computer.</p>
      <p><a href="https://tuneconsole.com/extension-setup">View installation instructions</a> for Linux, macOS, or Docker / Podman.</p>`;
  }

  function updateDialog() {
    const dialog = document.getElementById(DIALOG_ID);
    if (!dialog) return;
    dialog.querySelector(".tc-player-status").textContent = connected
      ? "Your local player is ready."
      : "The extension is installed. Start TuneConsole on this computer to connect your local player.";
    dialog.querySelector(".tc-player-instructions").hidden = connected;
    const openLink = dialog.querySelector(".tc-player-open");
    openLink.hidden = !connected;
    openLink.onclick = openPlayer;
  }

  async function openPlayer(event) {
    event?.preventDefault();
    if (!connected) { showInstructions(); return; }
    try {
      const reply = await chrome.runtime.sendMessage({ type: "open-local-player" });
      if (reply?.opened) return;
      setConnected(reply?.connected);
      showInstructions();
      if (connected) {
        document.querySelector(`#${DIALOG_ID} .tc-player-status`).textContent = "The player tab could not be opened. Try again.";
      }
    } catch (e) {
      setConnected(false);
      showInstructions();
    }
  }

  function showInstructions() {
    let dialog = document.getElementById(DIALOG_ID);
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = DIALOG_ID;
      dialog.setAttribute("aria-labelledby", "tc-player-title");
      dialog.setAttribute("aria-describedby", "tc-player-status");
      dialog.innerHTML = `<form method="dialog" class="tc-player-heading">
          <h2 id="tc-player-title">Your local player</h2><button type="submit" autofocus>Close</button>
        </form>
        <p class="tc-player-status" id="tc-player-status"></p>
        <div class="tc-player-instructions">${platformInstructions()}
          <p class="tc-player-hint">Keep TuneConsole running. This badge turns green when it connects. Keep a signed-in YouTube Music tab open while using the player.</p>
        </div>
        <a class="tc-player-open" href="${LOCAL_URL}" target="_blank" rel="noopener">Open local player</a>`;
      document.body.append(dialog);
    }
    updateDialog();
    dialog.showModal();
  }

  function renderSetup() {
    const setup = document.querySelector("[data-tc-extension-setup]");
    if (!setup || !connectionChecked) return;
    // An already-running app needs no setup. A newly started app gets the step 3 payoff.
    if (connected && !setupNeeded && !setupNavigating) {
      setupNavigating = true;
      void openPlayer();
    }
    const install = setup.querySelector("[data-tc-setup-install]");
    const ready = setup.querySelector("[data-tc-setup-ready]");
    if (!install || !ready) return;
    const changed = ready.hidden === connected;
    install.hidden = connected;
    ready.hidden = !connected;
    const openLink = ready.querySelector("[data-tc-setup-open]");
    if (openLink) openLink.onclick = openPlayer;
    if (changed) {
      document.title = connected ? "Time to rock out! · TuneConsole" : "Extension installed! Next steps · TuneConsole";
      const heading = connected ? ready.querySelector("h1") : install.querySelector("h1");
      heading?.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    }
  }

  function renderBadge() {
    if (!chrome.runtime.id) return; // An old content context can outlive an extension reload.
    renderSetup();
    const links = document.querySelector(".site-nav .nav-links");
    if (!links) return; // Svelte may not have mounted the header yet.
    let badge = document.getElementById(BADGE_ID);
    if (!badge) {
      badge = document.createElement("a");
      badge.id = BADGE_ID;
      badge.href = LOCAL_URL;
      badge.target = "_blank";
      badge.rel = "noopener";
      badge.textContent = "Local Player";
    }
    // Replace a handler left behind by an invalidated content context after an extension update.
    badge.onclick = openPlayer;
    if (badge.parentElement !== links) links.prepend(badge);
    const state = String(connected);
    if (badge.dataset.connected === state) return;
    badge.dataset.connected = state;
    badge.title = connected ? "Open local player" : "Local player is offline. Show setup instructions";
    badge.setAttribute("aria-label", connected ? "Local Player, running" : "Local Player, offline");
    if (connected) {
      badge.removeAttribute("aria-haspopup");
      badge.removeAttribute("aria-controls");
    } else {
      badge.setAttribute("aria-haspopup", "dialog");
      badge.setAttribute("aria-controls", DIALOG_ID);
    }
  }

  function setConnected(value) {
    connected = value === true;
    if (!connectionChecked) {
      connectionChecked = true;
      setupNeeded = !connected;
    }
    renderBadge();
    updateDialog();
  }
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "local-player-status") {
      revision += 1;
      setConnected(msg.connected);
    }
  });
  async function refresh() {
    if (!chrome.runtime.id) {
      clearInterval(refreshTimer);
      observer.disconnect();
      return;
    }
    const current = ++revision;
    try {
      const reply = await chrome.runtime.sendMessage({ type: "local-player-status" });
      if (current === revision) setConnected(reply?.connected);
    } catch (e) {
      if (chrome.runtime.id && current === revision) setConnected(false);
    }
  }
  const observer = new MutationObserver(renderBadge);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  const refreshTimer = setInterval(refresh, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("pageshow", refresh);
  renderBadge();
  refresh();
})();
