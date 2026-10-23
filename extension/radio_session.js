// MAIN-world companion for saved-playlist Radio Sessions.
// Ask Music to fetch the saved playlist; never supply playback items ourselves.
(() => {
  const VERSION = 7;
  if (window.__tcRadioSession?.version === VERSION) {
    window.__tcPlaylistPoc = window.__tcRadioSession;
    return;
  }
  let expected = null;
  let lastRefresh = "";
  let lastRefreshAt = 0;
  let lastAutoplayClick = 0;
  let refreshAttempts = 0;
  let refreshError = null;
  let lastViewRefresh = "";
  let viewRefreshError = null;
  const events = [];
  const lifetime = new AbortController();
  window.__tcRadioSession?.dispose?.();

  function queueElement() {
    return document.querySelector("ytmusic-player-queue");
  }

  function renderer(entry) {
    return entry?.playlistPanelVideoRenderer ||
      entry?.playlistPanelVideoWrapperRenderer?.primaryRenderer?.playlistPanelVideoRenderer;
  }

  function nativePlayback() {
    // Music owns the playback order in its application store. The embedded
    // video's getPlaylist() can describe a different/stale list.
    const queue = queueElement()?.queue || queueElement()?.polymerController?.queue;
    let state;
    try { state = queue?.store?.store?.getState?.()?.queue; } catch (_) {}
    if (!Array.isArray(state?.items)) return {available: false};
    const items = state.items.map(renderer);
    return {
      available: true,
      videoIds: items.map(item => item?.videoId || ""),
      playlistIds: [...new Set(items.map(item => item?.navigationEndpoint?.watchEndpoint?.playlistId).filter(Boolean))],
      selectedIndex: state.selectedItemIndex ?? null,
      autoplay: typeof state.autoplay === "boolean" ? state.autoplay : null,
      autoPlaying: typeof queue.autoPlaying === "boolean" ? queue.autoPlaying : null,
      automixVideoIds: (state.automixItems || []).map(item => renderer(item)?.videoId).filter(Boolean),
      stateKeys: Object.keys(state),
    };
  }

  function player() {
    let api = document.querySelector("#movie_player");
    if (typeof api?.getVideoData !== "function") api = document.querySelector("ytmusic-player")?.playerApi;
    const video = document.querySelector("video");
    return {
      video_id: api?.getVideoData?.()?.video_id || "",
      playlist_ytm_id: api?.getPlaylistId?.() || "",
      paused: !video || video.paused,
      position: video?.currentTime || 0,
      duration: Number.isFinite(video?.duration) ? video.duration : null,
      loadedVideoIds: api?.getPlaylist?.() || [],
    };
  }

  function switches() {
    return [...document.querySelectorAll(
      'ytmusic-app tp-yt-paper-toggle-button#automix, ' +
      'ytmusic-player-queue tp-yt-paper-toggle-button, ytmusic-player-queue [role="switch"], ' +
      'ytmusic-player-queue input[type="checkbox"]'
    )];
  }

  function switchInfo(el) {
    const aria = el.getAttribute("aria-checked") ?? el.getAttribute("aria-pressed");
    return {
      tag: el.localName, id: el.id,
      label: el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent.trim().slice(0, 120),
      checked: aria === "true" ? true : aria === "false" ? false :
        typeof el.checked === "boolean" ? el.checked : null,
      parent: el.parentElement?.id || "",
    };
  }

  function autoplayOff() {
    const el = switches().find(el => {
      const info = switchInfo(el);
      return /autoplay|auto.?mix|reproducci.n autom.tica/i.test([info.id, info.label, info.parent].join(" "));
    });
    if (!el) return "switch not identified";
    const info = switchInfo(el);
    if (info.checked === false) return "off";
    if (info.checked !== true) return "switch state unknown";
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return "switch disabled";
    if (Date.now() - lastAutoplayClick < 3000) return "waiting for switch";
    lastAutoplayClick = Date.now();
    el.click();
    return switchInfo(el).checked === false ? "off" : "waiting for switch";
  }

  function matchesPlaylist(url, playlist) {
    try {
      const u = new URL(url, location.href);
      return u.origin === location.origin && ((u.pathname === "/playlist" && u.searchParams.get("list") === playlist) ||
        u.pathname === `/browse/VL${playlist}`);
    } catch (_) { return false; }
  }

  function sidebarLink(playlist) {
    const links = document.querySelectorAll("ytmusic-guide-renderer a[href], ytmusic-guide-entry-renderer a[href]");
    for (const link of links) if (matchesPlaylist(link.href, playlist)) return link;
    const entries = document.querySelectorAll("ytmusic-guide-entry-renderer");
    for (const entry of entries) {
      const endpoint = entry.data?.navigationEndpoint || entry.polymerController?.data?.navigationEndpoint;
      if (endpoint?.browseEndpoint?.browseId === `VL${playlist}`) return entry;
    }
    return null;
  }

  function refresh() {
    const current = player();
    if (!expected || current.playlist_ytm_id !== expected.playlist) return "different playlist playing";
    if (playbackMatches()) {
      // A later stale response can make this saved version diverge again.
      // A confirmed refresh ends its retry budget so that drift is repairable.
      lastRefresh = "";
      refreshError = null;
      return "confirmed";
    }
    if (!expected.videoIds.includes(current.video_id)) return "current song is not in saved playlist";
    const app = document.querySelector("ytmusic-app")?.polymerController;
    if (typeof app?.handleQueueUpdateCommand !== "function") return "native playlist refresh unavailable";
    let endpoint;
    try {
      endpoint = app.getState().playerPage?.playerPageWatchNextResponse?.currentVideoEndpoint;
    } catch (_) { return "playback endpoint unavailable"; }
    if (endpoint?.watchEndpoint?.playlistId !== expected.playlist ||
        endpoint.watchEndpoint.videoId !== current.video_id) return "waiting for current playback endpoint";

    const key = JSON.stringify([expected, current.video_id]);
    if (lastRefresh !== key) {
      lastRefresh = key;
      lastRefreshAt = 0;
      refreshAttempts = 0;
      refreshError = null;
    }
    if (refreshAttempts && Date.now() - lastRefreshAt < 8000) return "waiting for native playlist refresh";
    if (refreshAttempts >= 3) return refreshError || "native playlist refresh not confirmed after 3 requests";

    // A watch navigation to the current song only seeks/selects the old item.
    // Music's native fetch handler instead reads the saved playlist from /next
    // and reconciles its own playback items, retaining the selected item.
    // In this handler index selects a steering chip, so omit the track index.
    // No seek, play, pause, track array, or direct store mutation is involved.
    const {index, startTimeSeconds, ...watch} = endpoint.watchEndpoint;
    lastRefreshAt = Date.now();
    refreshAttempts++;
    try {
      app.handleQueueUpdateCommand({
        queueUpdateCommand: {fetchContentsCommand: {...endpoint, watchEndpoint: watch}},
      });
      refreshError = null;
      return "requested";
    } catch (error) {
      refreshError = `native playlist refresh failed: ${String(error?.message || error).slice(0, 300)}`;
      return refreshError;
    }
  }

  function playbackMatches() {
    if (!expected) return false;
    const current = player();
    if (current.playlist_ytm_id !== expected.playlist) return false;
    const savedIndex = expected.videoIds.indexOf(current.video_id);
    const native = nativePlayback();
    if (!native.available) return false;
    if (native.playlistIds.length && !native.playlistIds.includes(expected.playlist)) return false;
    const loadedIndex = native.videoIds.indexOf(current.video_id);
    if (savedIndex < 0 || loadedIndex < 0) return false;
    const saved = expected.videoIds.slice(savedIndex + 1);
    const loaded = native.videoIds.slice(loadedIndex + 1);
    return saved.length === loaded.length && saved.every((id, index) => id === loaded[index]);
  }

  function playlistRows() {
    return [...document.querySelectorAll("ytmusic-playlist-shelf-renderer ytmusic-responsive-list-item-renderer")];
  }

  function rowVideoId(el) {
    const data = el?.data || el?.polymerController?.data;
    return data?.playlistItemData?.videoId || rowEndpoint(el)?.watchEndpoint?.videoId || "";
  }

  function rowEndpoint(el) {
    const data = el?.data || el?.polymerController?.data;
    // The thumbnail's play action retains saved-playlist context and Music
    // configuration; a row's ordinary navigation link can describe something else.
    return data?.overlay?.musicItemThumbnailOverlayRenderer?.content?.musicPlayButtonRenderer
      ?.playNavigationEndpoint || data?.navigationEndpoint;
  }

  function pageMatches() {
    if (!expected) return false;
    const rows = playlistRows();
    const ids = rows.map(rowVideoId).filter(Boolean);
    return ids.length === expected.videoIds.length &&
      ids.every((id, index) => id === expected.videoIds[index]) &&
      rows.every(el => rowEndpoint(el)?.watchEndpoint?.playlistId === expected.playlist);
  }

  function refreshView() {
    const key = JSON.stringify(expected);
    if (pageMatches()) {
      lastViewRefresh = key;
      viewRefreshError = null;
      return "confirmed";
    }
    if (lastViewRefresh === key) return viewRefreshError || "requested";
    const link = sidebarLink(expected.playlist);
    if (!link) return "playlist link unavailable";
    // Browsing the playlist refreshes its visible rows without selecting a song.
    // Click once per adopted saved version, never on every status poll or retry.
    lastViewRefresh = key;
    viewRefreshError = null;
    try {
      link.click();
      return "clicked";
    } catch (error) {
      viewRefreshError = `playlist view refresh failed: ${String(error?.message || error).slice(0, 300)}`;
      return viewRefreshError;
    }
  }

  function snapshot() {
    return {
      observerVersion: VERSION,
      expected, player: player(), native: nativePlayback(), switches: switches().map(switchInfo),
      playback_synced: playbackMatches(),
      page_synced: pageMatches(),
      refreshRequest: {attempts: refreshAttempts, error: refreshError},
      viewRefreshError,
      pageVideoIds: playlistRows().map(rowVideoId).filter(Boolean),
      sidebarLinkFound: !!expected && !!sidebarLink(expected.playlist),
    };
  }

  function owners() {
    const result = {};
    for (const tag of ["ytmusic-app", "ytmusic-player-page", "ytmusic-player-queue", "ytmusic-player-bar", "ytmusic-playlist-shelf-renderer"]) {
      const el = document.querySelector(tag);
      if (!el) continue;
      result[tag] = el;
      if (el.polymerController) result[`${tag}.controller`] = el.polymerController;
      for (const key of ["queue", "store", "appStore", "playerApi", "networkManager", "navigator", "playlistPollingHandler"]) {
        const value = el[key] || el.polymerController?.[key];
        if (value) result[`${tag}.${key}`] = value;
      }
    }
    const store = queueElement()?.queue?.store?.store;
    if (store) result["playback.store"] = store;
    return result;
  }

  function methods(obj) {
    const result = {};
    for (let p = obj; p && p !== HTMLElement.prototype && p !== Object.prototype; p = Object.getPrototypeOf(p)) {
      for (const [key, desc] of Object.entries(Object.getOwnPropertyDescriptors(p))) {
        if (/playlist|queue|player|refresh|reload|autoplay|automix|continuation|action|watch|dispatch|state|next|update|fetch|load|navigate|poll|cache|execute|^play/i.test(key) &&
            typeof desc.value === "function" && !(key in result)) {
          result[key] = String(desc.value).slice(0, 2500);
        }
      }
    }
    return result;
  }

  // Read-only trace: endpoint IDs and argument shapes, without auth or headers.
  for (const type of ["yt-action", "yt-navigate"]) {
    document.addEventListener(type, ev => {
      const detail = ev.detail;
      if (!detail || (type === "yt-action" &&
          !/playlist|queue|autoplay|automix|reload|refresh|watch|next/i.test(detail.actionName || ""))) return;
      const watch = detail.endpoint?.watchEndpoint;
      events.push({type, action: detail.actionName,
        endpointKeys: Object.keys(detail.endpoint || {}),
        videoId: watch?.videoId, playlistId: watch?.playlistId,
        args: (Array.isArray(detail.args) ? detail.args : []).map(arg =>
          arg && typeof arg === "object" ? Object.keys(arg) : String(arg).slice(0, 160))});
      if (events.length > 30) events.shift();
    }, {signal: lifetime.signal});
  }

  window.__tcRadioSession = {
    version: VERSION,
    dispose: () => lifetime.abort(),
    inspect: (options = {}) => {
      const report = {...snapshot(), events: [...events]};
      const {automixVideoIds, stateKeys, ...native} = report.native;
      report.native = {...native, automixCount: automixVideoIds?.length || 0};
      if (options.methods) {
        report.methods = Object.fromEntries(
          Object.entries(owners()).map(([name, obj]) => [name, methods(obj)]));
        report.playlistLinks = playlistRows().map(el => {
          const endpoint = rowEndpoint(el);
          return {videoId: rowVideoId(el), endpointKeys: Object.keys(endpoint || {}), watch: endpoint?.watchEndpoint};
        });
      }
      return report;
    },
    source: (owner, method) => String(owners()[owner]?.[method] || "method not found"),
    refresh,
    update(frame) {
      expected = {playlist: frame.playlist, videoIds: frame.videoIds};
      const current = player();
      const matching = current.playlist_ytm_id === frame.playlist;
      const autoplay = matching ? autoplayOff() : "waiting for session playlist";
      const refreshed = frame.refresh ? refresh() : "not requested";
      // The native refresh and browse page have independent state. Wait for the
      // playback acknowledgement before refreshing the visible playlist, even
      // when the backend has already stopped requesting playback refreshes.
      const viewRefresh = matching && playbackMatches() ? refreshView() : "waiting for playback refresh";
      return {...snapshot(), ok: true, autoplay, refresh: refreshed, viewRefresh};
    },
  };
  // Existing service workers can finish their in-flight command during an update.
  window.__tcPlaylistPoc = window.__tcRadioSession;
})();
