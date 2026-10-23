// Shared constants only. Modules own their mutable state inside factory closures.
const TC_CONFIG = Object.freeze({
  musicOrigin: "https://music.youtube.com",
  appOrigin: "http://127.0.0.1:8765",
  bridgeUrl: "ws://127.0.0.1:8765/bridge/ws",
  sensorVersion: 4,
  sensorRevision: 4,
  receiverRevision: 3,
});

function createBridgeWarnings() {
  const times = new Map();
  return (message, ...details) => {
    const now = Date.now();
    if (times.has(message) && now - times.get(message) < 60000) return;
    times.set(message, now);
    console.warn("[TuneConsole]", message, ...details);
  };
}

function isMissingReceiver(error) {
  return String(error && error.message ? error.message : error)
    === "Could not establish connection. Receiving end does not exist.";
}
