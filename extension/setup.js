// The toolbar panel asks the worker for connection availability only.
async function refreshSetup() {
  let connected = false;
  try {
    connected = (await chrome.runtime.sendMessage({type: "music-setup-status"}))?.connected === true;
  } catch (e) {}
  document.getElementById("status").textContent = connected ? "App connected" : "App not connected";
  document.getElementById("setup").hidden = connected;
  document.getElementById("open").hidden = !connected;
}
refreshSetup();
setInterval(refreshSetup, 3000);
