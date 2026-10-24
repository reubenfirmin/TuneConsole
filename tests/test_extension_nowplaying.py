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
const video = { paused: true, currentTime: 12, duration: 200 };
let thumbnail = "https://i.ytimg.com/vi/v1/hqdefault.jpg";
let likeStatus = "";
const probes = [], timeouts = new Map();
let timerSerial = 0;
const window = { addEventListener: (type, fn) => { listeners[type] = fn; },
  postMessage: msg => probes.push(msg) };
const document = {
  addEventListener() {},
  querySelector(selector) {
    if (selector === "video") return video;
    if (selector === ".title.ytmusic-player-bar") return { textContent: "Exoskeleton (Official)" };
    if (selector === ".byline.ytmusic-player-bar") return { textContent: "GAUPA • Album" };
    if (selector === "img.image.ytmusic-player-bar") return { src: thumbnail };
    if (selector === "ytmusic-player-bar ytmusic-like-button-renderer" && likeStatus) {
      return {getAttribute: () => likeStatus, querySelector: () => null};
    }
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
  setTimeout(fn) { const id = ++timerSerial; timeouts.set(id, fn); return id; },
  clearTimeout: id => timeouts.delete(id),
  Date: { now: () => now },
  Intl: { DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: "America/Costa_Rica"})}) },
  fetch: async () => ({status: 200, text: async () => "{}"}),
  console: { log() {}, warn() {} },
};
vm.runInNewContext(fs.readFileSync("extension/content.js", "utf8"), context);
// A resync can precede the first MAIN tick. Unknown account is presence, never a main-account play.
video.paused = false;
listeners.runtime({type: "resync-now"}, {}, () => {});
assert.equal(messages.at(-1).type, "now-heartbeat");
messages.length = 0;
video.paused = true;
const main = (paused, playlist = "PL1") => listeners.message({
  source: window,
  data: { __tcSensorVersion: 4, __tcSensorRevision: 8,
    __tcNow: { title: "Exoskeleton", artist: "GAUPA", videoId: "v1", playlist, paused,
      position: video.currentTime, duration: video.duration,
      brandKnown: true, brandId: "brand-A", metadataVerified: true } },
});
main(true);
now = 6000;
intervals[0]();  // DOM fallback after the main sensor was throttled
main(true);
assert.deepEqual(messages.map(m => m.type), ["now-heartbeat", "now-heartbeat", "now-heartbeat"]);
assert(messages.every(m => m.paused));
assert.equal(messages[0].position, 12);
assert.equal(messages[2].duration, 200);
assert.equal(messages[1].position, null, 'DOM fallback cannot use the media stream clock');
assert.equal(messages[1].duration, null);
assert(messages.every(m => m.playlist === "PL1" && m.albumBrowseId === "MPRE_album"));
video.paused = false;
main(false);
assert.equal(messages.at(-1).timeZone, "America/Costa_Rica");
assert.equal(messages.at(-1).position, 12);
assert.equal(messages.at(-1).duration, 200);
now = 12000;
intervals[0]();  // different DOM title for the same playing video
assert.deepEqual(messages.map(m => m.type), [
  "now-heartbeat", "now-heartbeat", "now-heartbeat", "play", "now-heartbeat",
]);
assert(messages.every(m => m.playlist === "PL1" && m.albumBrowseId === "MPRE_album"));
// A queue change on the same song is context, not another play.
context.location.href = "https://music.youtube.com/watch?v=v1&list=PL2";
main(false, "PL2");
assert.equal(messages.at(-1).type, "now-heartbeat");
assert.equal(messages.at(-1).playlist, "PL2");
context.location.href = "https://music.youtube.com/watch?v=v1";
main(true, "");
assert.equal(messages.at(-1).playlist, "");
// An explicit context clear must not resurrect a playlist from the old page URL.
context.location.href = "https://music.youtube.com/watch?v=v1&list=PL_old";
listeners.message({source: window, data: {__tcSensorVersion: 4, __tcSensorRevision: 8, __tcNow: {
  title: "Exoskeleton", artist: "GAUPA", videoId: "v1", playlist: "", paused: false,
  metadataVerified: true,
  brandKnown: true, brandId: "brand-A",
}}});
assert.equal(messages.at(-1).playlist, "");
// Reports without context cannot borrow the queue of a different video.
listeners.message({source: window, data: {__tcSensorVersion: 4, __tcSensorRevision: 8, __tcNow: {
  title: "Autoplay", artist: "Artist", videoId: "v2", paused: false,
  metadataVerified: true,
  brandKnown: true, brandId: "brand-A",
}}});
assert.equal(messages.at(-1).playlist, "");
// Browsing a different watch page must not supply the fallback's identity or queue.
main(false, "PL1");
context.location.href = "https://music.youtube.com/watch?v=other&list=PL_other";
now = 18000;
intervals[0]();
assert.equal(messages.at(-1).type, "now-heartbeat");
assert.equal(messages.at(-1).videoId, "v1");
assert.equal(messages.at(-1).playlist, "PL1");
// Unknown artwork cannot turn the browsed URL into a persisted play.
thumbnail = "https://lh3.googleusercontent.com/artwork";
now = 24000;
intervals[0]();
assert.equal(messages.at(-1).type, "now-heartbeat");
assert.equal(messages.at(-1).videoId, "");
assert.equal(messages.at(-1).playlist, "");
// An interval left behind by a pre-update sensor cannot override the new sensor.
const count = messages.length;
listeners.message({source: window, data: {__tcNow: {
  title: "Wrong song", artist: "Artist", videoId: "other", paused: false,
}}});
assert.equal(messages.length, count);
// A new DOM song during MAIN throttling restores presence with the last verified account.
thumbnail = "https://i.ytimg.com/vi/v3/hqdefault.jpg";
now = 30000;
intervals[0]();
assert.equal(messages.at(-1).type, "now-heartbeat");
assert.equal(messages.at(-1).metadataVerified, false);
assert.equal(messages.at(-1).videoId, "v3");
assert.equal(messages.at(-1).brandId, "brand-A");
// Regular but incomplete MAIN snapshots must allow DOM fallback, even at a 2s cadence.
for (const id of ["v4", "v5"]) {
  thumbnail = `https://i.ytimg.com/vi/${id}/hqdefault.jpg`;
  now += 2000;
  listeners.message({source: window, data: {__tcSensorVersion: 4, __tcSensorRevision: 8, __tcNow: {
    title: "Stale MediaSession title", videoId: "", brandKnown: true, brandId: "brand-A",
  }}});
  intervals[0]();
  const presence = messages.at(-1);
  assert.equal(presence.type, "now-heartbeat");
  assert.equal(presence.metadataVerified, false);
  assert.equal(presence.videoId, id);
  assert.equal(presence.title, "Exoskeleton (Official)");
  assert.equal(presence.brandId, "brand-A");
}
const accountReport = (brandKnown, brandId) => listeners.message({source: window,
  data: {__tcSensorVersion: 4, __tcSensorRevision: 8, __tcNow: {title: "", videoId: "", brandKnown, brandId}}});
// Explicit account uncertainty invalidates the cache and cannot advance play deduplication.
accountReport(false, "");
assert.equal(messages.at(-1).type, "now-heartbeat");
thumbnail = "https://i.ytimg.com/vi/v6/hqdefault.jpg";
intervals[0]();
assert.equal(messages.at(-1).type, "now-heartbeat");
const beforeUnknownEvent = messages.length;
listeners.message({source: window, data: {__tcSensorVersion: 4,
  __tcPlayer: {kind: "tick", videoId: "v6", brandKnown: false, brandId: ""}}});
assert.equal(messages.length, beforeUnknownEvent);
accountReport(true, "brand-B");
assert.equal(messages.at(-1).type, "now-heartbeat");
const verifiedAccountPlay = brandId => listeners.message({source: window, data: {
  __tcSensorVersion: 4, __tcSensorRevision: 8, __tcNow: {title:"Song", artist:"Artist", videoId:"v6",
    brandKnown:true, brandId, metadataVerified:true, paused:false}}});
verifiedAccountPlay("brand-B");
assert.equal(messages.at(-1).type, "play");
assert.equal(messages.at(-1).brandId, "brand-B");
// The same video on a different account must be recorded for that account too.
accountReport(true, "");
assert.equal(messages.at(-1).type, "now-heartbeat");
verifiedAccountPlay("");
assert.equal(messages.at(-1).type, "play");
assert.equal(messages.at(-1).brandId, "");
video.muted = true;
now += 6000;
intervals[0]();
assert.equal(messages.at(-1).muted, true);
// A new playing ID must not inherit the previous bar's explicit rating or album.
video.muted = false;
likeStatus = "LIKE";
const snapshot = (videoId, playerBar, metadataVerified = true) => listeners.message({source: window,
  data: {__tcSensorVersion: 4, __tcSensorRevision: 8, __tcNow: {
    title: "Next song", artist: metadataVerified ? "Artist" : "Uploader channel",
    videoId, playerBar, metadataVerified,
    paused: false, brandKnown: true, brandId: "brand-A",
  }}});
snapshot("v7"); // The DOM still shows liked v6.
assert.equal(messages.at(-1).videoId, "v7");
assert.equal(messages.at(-1).likeStatus, "");
assert.equal(messages.at(-1).albumBrowseId, "");
snapshot("v10", undefined, false);
assert.equal(messages.at(-1).type, "now-heartbeat", 'unverified channel credits cannot be persisted');
snapshot("v10", {videoId: "v10", likeStatus: "INDIFFERENT", albumBrowseId: "MPRE_v10"});
const verifiedPlays = messages.filter(m => m.type === "play" && m.videoId === "v10");
assert.equal(verifiedPlays.length, 1, 'verified metadata must still record the first play');
assert.equal(verifiedPlays[0].artist, "Artist");
thumbnail = "https://i.ytimg.com/vi/v7/hqdefault.jpg";
likeStatus = "INDIFFERENT";
snapshot("v7");
assert.equal(messages.at(-1).likeStatus, "INDIFFERENT");
assert(!messages.some(m => m.type === "play" && m.videoId === "v7" && m.likeStatus === "LIKE"));
// MAIN can identify a bar backed by album art; use the rating captured with that ID rather than
// re-reading the independently changing DOM after the message was posted.
likeStatus = "DISLIKE";
snapshot("v8", {videoId: "v8", likeStatus: "LIKE", albumBrowseId: "MPRE_v8"});
assert.equal(messages.at(-1).likeStatus, "LIKE");
assert.equal(messages.at(-1).albumBrowseId, "MPRE_v8");
snapshot("v9", {videoId: "v8", likeStatus: "LIKE", albumBrowseId: "MPRE_v8"});
assert.equal(messages.at(-1).likeStatus, "");
assert.equal(messages.at(-1).albumBrowseId, "");
// Health pings ask MAIN for an immediate sample. An incomplete reply still proves MAIN liveness;
// a legacy sensor cannot satisfy the versioned handshake.
let health;
assert.equal(listeners.runtime({type: "sensor-ping", main: true}, {}, r => { health = r; }), true);
assert.equal(health, undefined);
const probeId = probes.at(-1).__tcSensorProbe.id;
listeners.message({source: window, data: {__tcSensorVersion: 3, __tcSensorProbeId: probeId,
  __tcNow: {title: "Legacy", videoId: "old"}}});
assert.equal(health, undefined);
listeners.message({source: window, data: {__tcSensorVersion: 4, __tcSensorProbeId: probeId,
  __tcSensorRevision: 7, __tcNow: {title: "Outdated implementation", videoId: "old"}}});
assert.equal(health, undefined, 'same protocol with an obsolete revision cannot satisfy a probe');
listeners.message({source: window, data: {__tcSensorVersion: 4, __tcSensorProbeId: probeId, __tcSensorRevision: 8,
  __tcNow: {title: "", videoId: "", brandKnown: false}}});
assert.equal(health.ok, true);
assert.equal(health.mainVersion, 4);
assert.equal(health.mainRevision, 8);
assert.equal(health.receiverRevision, 7);
assert.equal(health.mainAgeMs, 0);
assert.equal(health.capture.hasVideo, false);
assert.equal(health.capture.accountKnown, false);
assert.equal(timeouts.size, 0);
// A live but unverified player must expose the missing identity boundary on the next probe,
// without exposing credit/account values or converting its heartbeat into an exact play.
health = undefined;
listeners.runtime({type: "sensor-ping", main: true}, {}, r => { health = r; });
listeners.message({source: window, data: {__tcSensorVersion: 4, __tcSensorRevision: 8,
  __tcSensorProbeId: probes.at(-1).__tcSensorProbe.id, __tcNow: {
    title: "Private song", artist: "Private artist", videoId: "private-video", brandId: "private-account",
    brandKnown: true, metadataVerified: false, paused: false, muted: false,
    playerBar: {videoId: "", ratingVideoId: "private-video", title: "Private song", artist: "Private artist",
      binding: {barIds: [], targetIds: ["private-video"], sources: [{source: "bar.data", present: true,
        keys: ["videoDetails"], targetKeys: [], data: {secret: "private-value"}}]}},
  }}});
assert.equal(messages.at(-1).type, "now-heartbeat");
assert.equal(health.capture.metadataVerified, false);
assert.equal(health.capture.accountKnown, true);
assert.equal(health.capture.barHasVideo, false);
assert.equal(health.capture.ratingMatchesVideo, true);
assert.equal(health.capture.barTitleMatches, true);
assert.equal(health.capture.barArtistMatches, true);
assert.equal(health.capture.barCandidates, 0);
assert.equal(health.capture.ratingCandidates, 1);
assert.equal(health.capture.sources[0].keys[0], "videoDetails");
assert(!JSON.stringify(health.capture).includes("private"));
now += 20000;
health = undefined;
listeners.runtime({type: "sensor-ping", main: true}, {}, r => { health = r; });
[...timeouts.values()][0](); // MAIN did not answer the next probe.
assert.equal(health.mainAgeMs, 20000);
assert.equal(health.capture, null, 'an unanswered probe must not reuse older capture evidence');
assert.equal(timeouts.size, 0);
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


def test_main_sensor_uses_player_identity_and_preserves_exit_context():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension main-world sensor")

    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const messages = [], handlers = new Map(), timers = new Map();
let serial = 0, now = 30000, available = true, playlist = 'PL_playing';
let data = {video_id: 'playing', title: 'Song', author: 'Artist'};
let playerResponse = null;
let config = {LOGGED_IN: true, DELEGATED_SESSION_ID: 'brand-A'};
let bar = null;
const windowHandlers = new Map();
const video = {
  paused: false, currentTime: 12, duration: 200, muted: false, volume: 0.5,
  addEventListener(type, fn) { handlers.set(type, fn); },
  removeEventListener(type, fn) { if (handlers.get(type) === fn) handlers.delete(type); },
};
const player = {getVideoData: () => data, getPlaylistId: () => playlist,
  getCurrentTime: () => video.currentTime, getDuration: () => video.duration,
  getPlayerResponse: () => playerResponse};
const window = {__tcNowPoller: true, postMessage: msg => messages.push(msg),
  addEventListener: (type, fn) => windowHandlers.set(type, fn),
  removeEventListener: (type, fn) => {
    if (windowHandlers.get(type) === fn) windowHandlers.delete(type);
  },
  ytcfg: {get: key => config?.[key]}};
const location = {href: 'https://music.youtube.com/watch?v=browsed&list=PL_browsed'};
const navigator = {mediaSession: {metadata: {title: 'Song', artist: 'Artist', artwork: []}}};
const context = {
  window, location, navigator, URL,
  document: {querySelector(selector) {
    if (selector === 'video') return video;
    if (selector === '#movie_player' && available) return player;
    if (selector === 'ytmusic-player-bar') return bar;
    if (selector === '.title.ytmusic-player-bar' && bar) return {textContent: bar.title};
    if (selector === '.byline.ytmusic-player-bar' && bar) return {textContent: bar.artist + ' • Album'};
    if (selector === 'img.image.ytmusic-player-bar' && bar) return {src: bar.thumbnail};
    if (selector === 'ytmusic-player-bar ytmusic-like-button-renderer' && bar) {
      return {data: {target: {videoId: bar.ratingVideoId || bar.data.videoId}},
        getAttribute: () => 'LIKE', querySelector: () => null};
    }
    if (selector.startsWith('ytmusic-player-bar a[href*="/browse/MPRE"]') && bar) {
      return {href: 'https://music.youtube.com/browse/MPRE_album'};
    }
    return null;
  }},
  Date: {now: () => now},
  setInterval(fn) { timers.set(++serial, fn); return serial; },
  clearInterval(id) { timers.delete(id); },
};
const source = fs.readFileSync('extension/nowplaying_main.js', 'utf8');
vm.runInNewContext(source, context);
const sensorRevision = window.__tcNowPoller.revision;
const tick = () => [...timers.values()].forEach(fn => fn());
const latest = () => messages.filter(m => m.__tcNow).at(-1).__tcNow;
const exits = () => messages.filter(m => m.__tcPlayer?.kind === 'track_exit');
tick();
assert.equal(latest().videoId, 'playing');
assert.equal(latest().playlist, 'PL_playing');
assert.equal(latest().position, 12);
assert.equal(latest().duration, undefined, 'song length belongs to the app catalog');
location.href = 'https://music.youtube.com/browse/VLPL_other';
video.currentTime = 30;
now += 2000;
tick();
assert.equal(exits().length, 0, 'browsing must not synthesize a track exit');
assert.equal(latest().videoId, 'playing');
assert.equal(latest().playlist, 'PL_playing');
assert.equal(latest().position, 30, 'each heartbeat samples the player clock');
video.currentTime = 42;
handlers.get('pause')();
assert.equal(messages.at(-1).__tcPlayer.position, 42, 'events sample position between polls');
data = {video_id: 'next', title: 'Next', author: 'Artist'};
playlist = 'PL_next';
video.currentTime = 200;
handlers.get('ended')();
assert.equal(messages.at(-1).__tcPlayer.kind, 'ended');
assert.equal(messages.at(-1).__tcPlayer.videoId, 'playing', 'ended belongs to the observed track');
video.currentTime = 3;
tick();
assert.equal(exits().length, 1);
assert.equal(exits()[0].__tcPlayer.videoId, 'playing');
assert.equal(exits()[0].__tcPlayer.playlist, 'PL_playing');
assert.equal(exits()[0].__tcPlayer.position, 200);
assert.equal(latest().videoId, 'next');
assert.equal(latest().playlist, 'PL_next');
assert.equal(latest().position, 3, 'new tracks do not inherit the last exit position');
assert.equal(latest().title, 'Next', 'new video must not inherit the old MediaSession title');
assert.equal(latest().artist, 'Artist');
assert.equal(latest().thumbnail, '');
// A stale thumbnail explicitly tied to the old video must not authorize its metadata either.
navigator.mediaSession.metadata.artwork = [{src: 'https://i.ytimg.com/vi/playing/hqdefault.jpg'}];
tick();
assert.equal(latest().title, 'Next');
navigator.mediaSession.metadata = {title: 'Next (Audio)', artist: 'Recording artist',
  artwork: [{src: 'https://i.ytimg.com/vi/next/hqdefault.jpg'}]};
tick();
assert.equal(latest().title, 'Next (Audio)');
assert.equal(latest().artist, 'Recording artist', 'verified MediaSession preserves richer credits');
// Official album art has no video ID. A matching MAIN-world player-bar identity authorizes its
// artwork and artist credit even when the player reports an uploader and MediaSession is stale.
data.author = 'Uploader channel';
navigator.mediaSession.metadata = {title: 'Old song', artist: 'Old artist',
  artwork: [{src: 'https://lh3.googleusercontent.com/old-album=w544'}]};
bar = {data: {videoId: 'next'}, title: 'Next (Album)', artist: 'Recording artist',
  thumbnail: 'https://lh3.googleusercontent.com/next-album=w544'};
tick();
assert.equal(latest().title, 'Next (Album)');
assert.equal(latest().artist, 'Recording artist');
assert.equal(latest().thumbnail, bar.thumbnail);
assert.equal(latest().metadataVerified, true);
assert.equal(latest().playerBar.likeStatus, 'LIKE');
assert.equal(latest().playerBar.albumBrowseId, 'MPRE_album');
bar.data.videoId = 'playing'; // The bar itself has not caught up to the playing ID.
tick();
assert.equal(latest().title, 'Next');
assert.equal(latest().artist, 'Uploader channel');
assert.equal(latest().thumbnail, '');
assert.equal(latest().metadataVerified, false);
bar.data.videoId = 'next';
bar.thumbnail = 'https://i.ytimg.com/vi/playing/hqdefault.jpg';
tick();
assert.equal(latest().playerBar.videoId, '', 'conflicting bar bindings must not authorize metadata');
assert.equal(latest().playerBar.likeStatus, '');
bar.thumbnail = 'https://lh3.googleusercontent.com/next-album=w544';
bar.ratingVideoId = 'playing';
tick();
assert.equal(latest().playerBar.likeStatus, '', 'a stale rating target must not rate the next song');
bar = null;
data.author = 'Artist';
// Official album artwork has no embedded video ID, but a matching player response
// can provide it without authorizing stale MediaSession titles or uploader credits.
playerResponse = {videoDetails: {videoId: 'next', thumbnail: {thumbnails: [
  {url: 'https://yt3.googleusercontent.com/album=w120'},
  {url: 'https://yt3.googleusercontent.com/album=w544'},
]}}};
tick();
assert.equal(latest().thumbnail, 'https://yt3.googleusercontent.com/album=w544');
assert.equal(latest().title, 'Next');
assert.equal(latest().artist, 'Artist');
assert.equal(latest().metadataVerified, false, 'artwork alone must not validate recording credits');
playerResponse.videoDetails.videoId = 'playing';
tick();
assert.equal(latest().thumbnail, '', 'a stale player response must not supply the previous artwork');
playerResponse = {videoDetails: {videoId: 'next', thumbnail: {thumbnails: []}}};
tick();
assert.equal(latest().thumbnail, '');
playerResponse = null;
playlist = '';
tick();
assert.equal(latest().playlist, '', 'an actual queue clear must stay clear');
available = false;
tick();
assert.equal(latest().videoId, '', 'unknown player state must not borrow a browsed ID');
assert.equal(latest().playlist, '');
assert.equal(exits().length, 1);
available = true;
navigator.mediaSession.metadata = null;
tick();
assert.equal(latest().title, 'Next', 'player metadata can cover an absent MediaSession');
assert.equal(latest().brandKnown, true);
assert.equal(latest().brandId, 'brand-A');
config = null;
tick();
assert.equal(latest().brandKnown, false, 'uninitialized config is not the main account');
config = {LOGGED_IN: true};
tick();
assert.equal(latest().brandKnown, true);
assert.equal(latest().brandId, '', 'signed-in non-delegated config verifies the main account');
video.volume = 0;
tick();
assert.equal(latest().muted, true);
assert(messages.every(m => m.__tcSensorVersion === 4));
// A continuous media stream cannot stand in for unavailable or changing per-song timing.
const originalPosition = player.getCurrentTime, originalDuration = player.getDuration;
video.currentTime = 650; video.duration = 720;
player.getCurrentTime = () => 17;
player.getDuration = () => 240;
tick();
assert.equal(latest().position, 17);
assert.equal(latest().duration, undefined);
player.getDuration = () => { throw new Error('Buffered duration is unavailable'); };
tick();
assert.equal(latest().position, 17, 'elapsed time never depends on a buffered duration getter');
player.getCurrentTime = () => {
  data = {...data, video_id: 'clock-changed'};
  return 18;
};
tick();
assert.equal(latest().position, null, 'a clock sampled across a song change is unknown');
assert.equal(latest().duration, undefined);
player.getCurrentTime = undefined;
tick();
assert.equal(latest().videoId, 'clock-changed');
assert.equal(latest().position, null, 'a missing player API must not borrow the media clock');
assert.equal(latest().duration, undefined);
player.getCurrentTime = () => { throw new Error('Player is changing'); };
tick();
assert.equal(latest().videoId, 'clock-changed', 'a timing error must preserve song identity');
assert.equal(latest().position, null);
assert.equal(latest().duration, undefined);
player.getCurrentTime = originalPosition;
player.getDuration = originalDuration;
now += 60000; // A background timer may have been throttled; an explicit probe samples immediately.
windowHandlers.get('message')({source: window,
  data: {__tcSensorProbe: {version: 4, id: 7}}});
assert.equal(messages.at(-1).__tcSensorProbeId, 7);
assert.equal(window.__tcNowPoller.lastSampleAt, now);
vm.runInNewContext(source, context);
assert.equal(timers.size, 1, 'reinjection must not add another interval');
assert.equal(handlers.size, 4);
delete window.__tcNowPoller.revision; // An implementation upgrade keeps the wire protocol.
vm.runInNewContext(source, context);
assert.equal(timers.size, 1, 'the revised sensor replaces a healthy predecessor without duplicate timers');
assert.equal(handlers.size, 4);
assert.equal(window.__tcNowPoller.revision, sensorRevision);
window.__tcNowPoller.version = 1; // a future version replaces the existing sensor in place
vm.runInNewContext(source, context);
tick();
assert.equal(timers.size, 1);
assert.equal(handlers.size, 4);
window.__tcNowPoller.stop();
assert.equal(timers.size, 0);
assert.equal(handlers.size, 0);
assert.equal(windowHandlers.size, 0);
vm.runInNewContext(source, context);
assert.equal(timers.size, 1, 'a stopped sensor must restart even at the same version');
assert.equal(windowHandlers.size, 1);
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
