// A quiet, dismissible explanation for extension-only installs on YouTube Music.
(() => {
  if (location.origin !== "https://music.youtube.com") return;
  const previous = window.__tcMusicSetup;
  if (previous && !previous.stopped) return;
  const DISMISSED = "tuneconsole-setup-dismissed";
  const SHOWN = "tuneconsole-setup-shown";
  let connected = null, revision = 0, host = null, showTimer = null;
  let dismissed = false, automaticShown = false, mounting = false;
  try { dismissed = sessionStorage.getItem(DISMISSED) === "1"; } catch (e) {}

  function remove() { host?.remove(); host = null; }

  function mount(automatic) {
    host = document.createElement("aside");
    host.id = "tuneconsole-setup";
    // The Music SPA can replace its own app tree without losing this reminder. A shadow
    // root prevents Music styles and the reminder's styles from affecting each other.
    const root = host.attachShadow({mode: "open"});
    root.innerHTML = `<style>
      :host { position: fixed; top: 76px; right: 16px; width: min(360px, calc(100vw - 32px));
        z-index: 2500; color-scheme: dark; font: 14px/1.5 system-ui, sans-serif; color: #f6f8fa;
        pointer-events: none; }
      section, button { pointer-events: auto; }
      [hidden] { display: none !important; }
      section { position: relative; padding: 18px; border: 1px solid #50665c; border-radius: 8px;
        background: #16221c; box-shadow: 0 8px 28px #0006; }
      h2 { margin: 0 30px 8px 0; font: 600 17px/1.3 system-ui, sans-serif; }
      p { margin: 8px 0; } small { display: block; color: #c6d2cc; margin-top: 12px; }
      a { display: inline-block; color: #10231a; background: #8be0b3; border-radius: 4px;
        padding: 7px 12px; text-decoration: none; font-weight: 650; }
      button { position: absolute; right: 8px; top: 8px; width: 32px; height: 32px; border: 0;
        color: #f6f8fa; background: transparent; font: 22px system-ui; cursor: pointer; }
      .badge { position: relative; display: block; margin-left: auto; right: 0; top: 0;
        width: 44px; height: 44px; padding: 0; border-radius: 6px; background: #7fa2f5; }
      .badge img { display: block; width: 44px; height: 44px; border-radius: inherit; }
      .attention { position: absolute; right: -5px; top: -5px; width: 18px; height: 18px;
        border: 2px solid #16221c; border-radius: 50%; background: #8be0b3; color: #10231a;
        font: 800 14px/18px system-ui, sans-serif; }
      a:focus-visible, button:focus-visible { outline: 3px solid #fff; outline-offset: 3px; }
    </style>
    <button class="badge" type="button" aria-label="TuneConsole app not connected. Open setup reminder"
      aria-expanded="false" aria-controls="tc-setup-panel">
      <img alt=""><span class="attention" aria-hidden="true">!</span>
    </button>
    <section id="tc-setup-panel" aria-labelledby="tc-setup-title" hidden>
      <button type="button" aria-label="Dismiss setup reminder">×</button>
      <h2 id="tc-setup-title">Finish setting up TuneConsole</h2>
      <p>The extension connects YouTube Music to the TuneConsole app. Install and open the app
        on this computer for mixes, library tools, and listening history.</p>
      <a href="https://tuneconsole.com/extension-setup" target="_blank" rel="noopener">Get the app</a>
      <small>Already installed? Start TuneConsole from your applications menu.
        You can keep listening in YouTube Music.</small>
    </section>`;
    const badge = root.querySelector(".badge"), panel = root.querySelector("section");
    const close = panel.querySelector("button");
    badge.querySelector("img").src = chrome.runtime.getURL("icons/icon48.png");
    function open(focus) {
      panel.hidden = false;
      badge.hidden = true;
      badge.setAttribute("aria-expanded", "true");
      if (focus) close.focus();
    }
    function dismiss() {
      dismissed = true;
      try { sessionStorage.setItem(DISMISSED, "1"); } catch (e) {}
      panel.hidden = true;
      badge.hidden = false;
      badge.setAttribute("aria-expanded", "false");
      badge.focus();
    }
    badge.addEventListener("click", () => open(true));
    close.addEventListener("click", dismiss);
    root.addEventListener("keydown", event => {
      if (event.key === "Escape" && !panel.hidden) { event.stopPropagation(); dismiss(); }
    });
    document.documentElement.append(host);
    if (automatic) { automaticShown = true; open(false); }
  }

  function canMount() { return !controller.stopped && !!chrome.runtime.id && connected === false && !host; }

  async function show() {
    showTimer = null;
    if (!canMount() || mounting) return;
    mounting = true;
    try {
      // Music tabs share persistent origin storage. Serialize the read/increment so
      // simultaneous tabs cannot each spend the same one of the two automatic prompts.
      await navigator.locks.request(SHOWN, () => {
        if (!canMount()) return;
        let automatic = false;
        if (!dismissed && !automaticShown) {
          try {
            const saved = Number(localStorage.getItem(SHOWN));
            const count = Number.isFinite(saved) && saved > 0 ? saved : 0;
            if (count < 2) {
              localStorage.setItem(SHOWN, String(count + 1));
              automatic = true;
            }
          } catch (e) {} // Unavailable storage leaves the reminder accessible through the badge.
        }
        mount(automatic);
      });
    } catch (e) { if (canMount()) mount(false); }
    finally { mounting = false; }
  }

  function setConnected(value) {
    connected = value === true;
    if (connected) {
      clearTimeout(showTimer);
      showTimer = null;
      remove();
    }
    // Give an app that is starting a short grace period before prompting.
    else if (!host && !mounting && showTimer === null) showTimer = setTimeout(show, 5000);
  }

  function receive(msg) {
    if (controller.stopped || msg?.type !== "music-setup-status") return;
    revision += 1;
    setConnected(msg.connected);
  }

  async function refresh() {
    if (controller.stopped) return;
    if (!chrome.runtime.id) { controller.stop(); return; }
    const current = ++revision;
    try {
      const reply = await chrome.runtime.sendMessage({type: "music-setup-status"});
      if (!controller.stopped && current === revision && typeof reply?.connected === "boolean") {
        setConnected(reply.connected);
      }
    } catch (e) { if (!chrome.runtime.id) controller.stop(); }
  }

  function onVisible() { if (!document.hidden) refresh(); }
  const controller = window.__tcMusicSetup = {stopped: false,
    stop() {
      controller.stopped = true;
      clearTimeout(showTimer);
      clearInterval(poll);
      try { chrome.runtime.onMessage.removeListener(receive); } catch (e) {}
      document.removeEventListener("visibilitychange", onVisible);
      remove();
    }};
  const poll = setInterval(refresh, 30000);
  chrome.runtime.onMessage.addListener(receive);
  document.addEventListener("visibilitychange", onVisible);
  refresh();
})();
