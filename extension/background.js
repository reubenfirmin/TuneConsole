// MV3 bootstrap: construct modules, wire explicit interfaces, and own Chrome lifecycle listeners.
// Factories register no listeners and perform no work until called here.
importScripts("worker/config.js", "worker/music_tabs.js", "worker/sensors.js", "worker/ratings.js",
  "worker/playback.js", "worker/requests.js", "worker/radio.js", "worker/site.js", "worker/setup.js", "worker/transport.js",
  "worker/audio_preview.js");

const tuneConsole = (() => {
  const warn = createBridgeWarnings();
  const tabs = createMusicTabs({origin: TC_CONFIG.musicOrigin,
    inject: id => sensors.inject(id), pingContent: id => sensors.pingContent(id)});
  const sensors = createSensors({version: TC_CONFIG.sensorVersion,
    revision: TC_CONFIG.sensorRevision, receiverRevision: TC_CONFIG.receiverRevision, warn,
    send: frame => transport.send(frame), suppressBeforeUnload: tabs.suppressBeforeUnload});
  const playback = createPlayback({origin: TC_CONFIG.musicOrigin, appOrigin: TC_CONFIG.appOrigin,
    musicTabs: tabs, sensors, send: frame => transport.send(frame), warn});
  const requests = createRequestForwarder({origin: TC_CONFIG.musicOrigin, tabs, sensors});
  const radio = createRadioSession({pickTab: playback.pickTab});
  const site = createSiteIntegration({appOrigin: TC_CONFIG.appOrigin, connected: () => transport.connected});
  const setup = createSetupIntegration({musicOrigin: TC_CONFIG.musicOrigin,
    connected: () => transport.connected, openPlayer: site.openPlayer});
  const audioPreview = createAudioPreview({appOrigin: TC_CONFIG.appOrigin, pickTab: playback.pickTab});

  async function dispatch(frame) {
    if (frame && frame.type === "navigate") { playback.navigate(frame.url); return; }
    if (frame && frame.type === "playpause") {
      const result = await playback.playPause(frame);
      if (Number.isSafeInteger(frame.id) && frame.id > 0)
        return {id: frame.id, status: 200, body: JSON.stringify(result)};
      return;
    }
    if (frame && frame.type === "refresh-view") { playback.refreshView(frame.playlist); return; }
    if (frame && ["rate", "next", "pause-for-tv", "radio-session", "playlist-session-poc"].includes(frame.type)) {
      let result;
      try {
        result = frame.type === "rate" ? await playback.rate(frame.action, frame.videoId, frame.likeStatus, frame.skip, frame.brandId)
          : frame.type === "next" ? await playback.next(frame.videoId)
          : frame.type === "pause-for-tv" ? await playback.pauseForTv() : await radio.update(frame);
      } catch (e) { result = {ok: false, error: String(e)}; }
      return {id: frame.id, status: 200, body: JSON.stringify(result)};
    }
    return requests.handle(frame);
  }

  const transport = createBridgeTransport({url: TC_CONFIG.bridgeUrl, dispatch, warn,
    onStatus: () => { site.publish(); setup.publish(); },
    onOpen: async () => { await sensors.probe(); sensors.resync(); }});

  chrome.runtime.onMessage.addListener(site.receive);
  chrome.runtime.onMessage.addListener(setup.receive);
  chrome.runtime.onMessage.addListener(playback.receive);
  chrome.runtime.onConnectExternal?.addListener(audioPreview.connect);
  chrome.tabs.onRemoved.addListener(id => { playback.removed(id); sensors.removed(id); });
  chrome.tabs.onUpdated.addListener((id, info, tab) => {
    if (info.status === "complete" && tab.url?.startsWith(TC_CONFIG.musicOrigin + "/")) sensors.probe();
  });
  chrome.alarms.create("bridge-keepalive", {periodInMinutes: 0.4});
  chrome.alarms.onAlarm.addListener(() => { transport.connect(); sensors.probe(); });
  chrome.runtime.onStartup.addListener(transport.connect);
  chrome.runtime.onInstalled.addListener(transport.connect);
  chrome.runtime.onInstalled.addListener(sensors.injectAll);
  chrome.runtime.onInstalled.addListener(site.publish);
  chrome.runtime.onInstalled.addListener(setup.onInstalled);
  chrome.runtime.onEnabled?.addListener(setup.openDestination);
  chrome.runtime.onStartup.addListener(site.publish);
  setup.install();
  transport.connect();
  return {tabs, sensors, playback, requests, radio, site, setup, transport, audioPreview};
})();
