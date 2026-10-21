"""Exercise the content sensor with alternating MediaSession and DOM reports."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_paused_metadata_sources_do_not_emit_plays_until_resume():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension content script")

    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const messages = [];
const listeners = {};
const intervals = [];
let now = 0;
const video = { paused: true };
const window = { addEventListener: (type, fn) => { listeners[type] = fn; } };
const document = {
  addEventListener() {},
  querySelector(selector) {
    if (selector === "video") return video;
    if (selector === ".title.ytmusic-player-bar") return { textContent: "Exoskeleton (Official)" };
    if (selector === ".byline.ytmusic-player-bar") return { textContent: "GAUPA • Album" };
    if (selector.startsWith('ytmusic-player-bar a[href*="/browse/MPRE"]')) {
      return { href: "https://music.youtube.com/browse/MPRE_album" };
    }
    return null;
  },
};
const context = {
  window, document, URL,
  location: { href: "https://music.youtube.com/watch?v=v1&list=PL1" },
  chrome: { runtime: {
    onMessage: { addListener: fn => { listeners.runtime = fn; } },
    sendMessage: msg => messages.push(msg),
  } },
  setInterval: fn => intervals.push(fn),
  Date: { now: () => now },
  Intl: { DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: "America/Costa_Rica"})}) },
  fetch: async () => ({status: 200, text: async () => "{}"}),
  console: { log() {}, warn() {} },
};
vm.runInNewContext(fs.readFileSync("extension/content.js", "utf8"), context);
const main = paused => listeners.message({
  source: window,
  data: { __tcNow: { title: "Exoskeleton", artist: "GAUPA", videoId: "v1", paused } },
});
main(true);
now = 6000;
intervals[0]();  // DOM fallback after the main sensor was throttled
main(true);
assert.deepEqual(messages.map(m => m.type), ["now-heartbeat", "now-heartbeat", "now-heartbeat"]);
assert(messages.every(m => m.paused));
assert(messages.every(m => m.playlist === "PL1" && m.albumBrowseId === "MPRE_album"));
video.paused = false;
main(false);
assert.equal(messages.at(-1).timeZone, "America/Costa_Rica");
now = 12000;
intervals[0]();  // different DOM title for the same playing video
assert.deepEqual(messages.map(m => m.type), [
  "now-heartbeat", "now-heartbeat", "now-heartbeat", "play", "now-heartbeat",
]);
assert(messages.every(m => m.playlist === "PL1" && m.albumBrowseId === "MPRE_album"));
// A queue change on the same song is context, not another play.
context.location.href = "https://music.youtube.com/watch?v=v1&list=PL2";
main(false);
assert.equal(messages.at(-1).type, "now-heartbeat");
assert.equal(messages.at(-1).playlist, "PL2");
context.location.href = "https://music.youtube.com/watch?v=v1";
main(true);
assert.equal(messages.at(-1).playlist, "");
// An explicit context clear must not resurrect a playlist from the old page URL.
context.location.href = "https://music.youtube.com/watch?v=v1&list=PL_old";
listeners.message({source: window, data: {__tcNow: {
  title: "Exoskeleton", artist: "GAUPA", videoId: "v1", playlist: "", paused: false,
}}});
assert.equal(messages.at(-1).playlist, "");
// Legacy reports without context cannot borrow the queue of a different video.
listeners.message({source: window, data: {__tcNow: {
  title: "Autoplay", artist: "Artist", videoId: "v2", paused: false,
}}});
assert.equal(messages.at(-1).playlist, "");
(async () => {
  const reply = await new Promise(resolve => listeners.runtime({
    type: "fetch", method: "POST", url: "https://music.youtube.com/youtubei/v1/browse",
    body: {browseId: "FEmusic_history"},
  }, {}, resolve));
  assert.equal(reply.status, 200);
  assert.equal(reply.timeZone, "America/Costa_Rica");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "-e", script], cwd=root, text=True, capture_output=True,
                            check=False)
    assert result.returncode == 0, result.stderr
