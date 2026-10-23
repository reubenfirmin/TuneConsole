// Saved-playlist radio has no tab state, locks, timers, or transport listeners.
// The retired preview deck manager is deliberately not part of this module.
function createRadioSession({pickTab}) {
  async function update(frame) {
    const tab = await pickTab();
    if (!tab) return {ok: false, error: "No YouTube Music tab is open."};
    await chrome.scripting.executeScript({
      target: {tabId: tab.id}, world: "MAIN", files: ["radio_session.js"],
    });
    const results = await chrome.scripting.executeScript({
      target: {tabId: tab.id}, world: "MAIN",
      func: frame => window.__tcRadioSession.update(frame),
      args: [{playlist: frame.playlist, videoIds: frame.videoIds, refresh: !!frame.refresh}],
    });
    return results[0]?.result || {ok: false, error: "The playlist observer returned no result."};
  }
  return {update};
}
