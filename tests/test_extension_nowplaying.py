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
video.paused = false;
main(false);
now = 12000;
intervals[0]();  // different DOM title for the same playing video
assert.deepEqual(messages.map(m => m.type), [
  "now-heartbeat", "now-heartbeat", "now-heartbeat", "play", "now-heartbeat",
]);
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "-e", script], cwd=root, text=True, capture_output=True,
                            check=False)
    assert result.returncode == 0, result.stderr
