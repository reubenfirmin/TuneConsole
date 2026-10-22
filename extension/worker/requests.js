// Auth and allowlisted request forwarding. Retry only a proven missing receiver:
// losing a response is not proof that a write failed.
function createRequestForwarder({origin, tabs, sensors}) {
  function isAllowed(url) {
    try {
      const parsed = new URL(url);
      return parsed.origin === origin && parsed.pathname.startsWith("/youtubei/v1/");
    } catch (e) { return false; }
  }

  async function authHeader() {
    let cookie = await chrome.cookies.get({url: origin, name: "SAPISID"});
    if (!cookie) cookie = await chrome.cookies.get({url: origin, name: "__Secure-3PAPISID"});
    if (!cookie) return null;
    const timestamp = Math.floor(Date.now() / 1000);
    const buffer = await crypto.subtle.digest("SHA-1",
      new TextEncoder().encode(`${timestamp} ${cookie.value} ${origin}`));
    const hash = [...new Uint8Array(buffer)].map(b => b.toString(16).padStart(2, "0")).join("");
    return `SAPISIDHASH ${timestamp}_${hash}`;
  }

  async function handle(frame) {
    const errorReply = error => ({id: frame.id, status: 0, body: JSON.stringify({error})});
    if (!isAllowed(frame.url)) return errorReply("blocked by allowlist");
    const tab = await tabs.ensure();
    if (!tab) return errorReply("open music.youtube.com");
    const auth = await authHeader();
    if (!auth) return errorReply("not signed in");
    const sendFetch = () => chrome.tabs.sendMessage(tab.id, {
      type: "fetch", url: frame.url, method: frame.method, body: frame.body, auth,
    });
    const failed = error => errorReply(isMissingReceiver(error)
      ? "content script not ready, reload the music.youtube.com tab"
      : "response lost; the request may have completed. Check YouTube Music before retrying.");
    let result;
    try {
      result = await sendFetch();
      if (!result) throw new Error("no response from content script");
    } catch (e) {
      if (!isMissingReceiver(e)) return failed(e);
      const installed = await sensors.inject(tab.id);
      if (!installed.ok) return failed(e);
      try {
        result = await sendFetch();
        if (!result) throw new Error("no response from content script");
      } catch (retryError) { return failed(retryError); }
    }
    return {id: frame.id, status: result.status, body: result.body, timeZone: result.timeZone};
  }

  return {handle};
}
