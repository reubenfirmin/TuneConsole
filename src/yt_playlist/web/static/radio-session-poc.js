(() => {
  const root = document.getElementById("radio-session-poc");
  if (!root) return;
  const start = root.querySelector("#session-start");
  const stop = root.querySelector("#session-stop");
  const status = root.querySelector("#session-status");
  const error = root.querySelector("#session-error");
  const playlist = root.querySelector("#session-playlist");
  const tracks = root.querySelector("#session-tracks");
  let busy = false;
  let commandError = "";

  function render(data) {
    start.disabled = busy || data.active || !data.connected;
    stop.disabled = !data.active;
    playlist.hidden = !data.playlist_url;
    if (data.playlist_url) playlist.href = data.playlist_url;
    error.hidden = !(data.error || commandError);
    error.textContent = data.error || commandError;
    status.textContent = data.starting ? "Creating your three-song playlist…" :
      !data.connected ? "Connect the YouTube Music extension to start." :
      !data.active ? "Ready to start a new Radio Session." :
      data.position < 0 ? "Three songs saved. Waiting for the first song to play…" :
      `Playing song ${data.position + 1}. ${data.ahead} songs ahead. ${data.tracks.length} songs saved.`;
    if (data.exhausted) status.textContent += " No more songs are available in your synced library.";
    tracks.replaceChildren(...data.tracks.map((track, index) => {
      const item = document.createElement("li");
      item.textContent = `${track.title || track.video_id} — ${track.artist || "Unknown artist"}`;
      if (index === data.position) {
        item.setAttribute("aria-current", "true");
        item.textContent += " · Playing";
      } else if (index < data.position) item.textContent += " · Played";
      return item;
    }));
  }

  async function refresh() {
    const response = await fetch("/radio-session-poc/status");
    if (!response.ok) throw new Error("Could not read the Radio Session status.");
    render(await response.json());
  }

  async function command(action) {
    busy = true;
    commandError = "";
    start.disabled = true;
    error.hidden = true;
    if (action === "start") status.textContent = "Creating your three-song playlist…";
    try {
      const response = await fetch(`/radio-session-poc/${action}`, {method: "POST"});
      if (!response.ok) throw new Error("The Radio Session request failed.");
      const data = await response.json();
      if (!data.ok) throw new Error(data.reason || "The Radio Session request failed.");
    } catch (err) {
      commandError = err.message;
    } finally {
      busy = false;
      await refresh();
    }
  }

  start.addEventListener("click", () => command("start").catch(showError));
  stop.addEventListener("click", () => command("stop").catch(showError));
  function showError(err) {
    error.textContent = err.message;
    error.hidden = false;
  }
  async function poll() {
    try { if (!busy) await refresh(); } catch (err) { showError(err); }
    if (root.isConnected) setTimeout(poll, 2000);
  }
  poll();
})();
