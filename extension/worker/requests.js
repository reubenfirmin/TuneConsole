// Auth and allowlisted request forwarding. Retry only a proven missing receiver:
// losing a response is not proof that a write failed.
function createRequestForwarder({origin, tabs, sensors}) {
  function isAllowed(url) {
    try {
      const parsed = new URL(url);
      return parsed.origin === origin && !parsed.username && !parsed.password && !parsed.hash &&
        parsed.pathname.startsWith("/youtubei/v1/") && !/%|\\/.test(parsed.pathname);
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
    let outcome = "not-dispatched";
    const uncertain = "response lost; the request may have completed. Check YouTube Music before retrying.";
    const errorReply = error => ({id: frame.id, status: 0, body: JSON.stringify({error, outcome})});
    try {
      if (!isAllowed(frame.url)) return errorReply("blocked by allowlist");
      const tab = await tabs.ensure();
      if (!tab) return errorReply("open music.youtube.com");
      const auth = await authHeader();
      if (!auth) return errorReply("not signed in");
      const sendFetch = () => {
        outcome = "uncertain";
        return chrome.tabs.sendMessage(tab.id, {
          type: "fetch", url: frame.url, method: frame.method, body: frame.body, auth,
        });
      };
      const failed = error => {
        if (isMissingReceiver(error)) outcome = "not-dispatched";
        return errorReply(outcome === "not-dispatched"
          ? "content script not ready, reload the music.youtube.com tab" : uncertain);
      };
      let result;
      try {
        result = await sendFetch();
        if (!result) throw new Error("no response from content script");
      } catch (e) {
        if (!isMissingReceiver(e)) return failed(e);
        outcome = "not-dispatched"; // Chrome explicitly confirms no receiver accepted the request.
        const installed = await sensors.inject(tab.id);
        if (!installed.ok) return failed(e);
        try {
          result = await sendFetch();
          if (!result) throw new Error("no response from content script");
        } catch (retryError) { return failed(retryError); }
      }
      // Never forward cookies, authorization, or arbitrary upstream headers.
      const retry = result.headers?.["retry-after"];
      const headers = typeof retry === "string" && retry.length <= 256 && !/[\r\n]/.test(retry)
        ? {"retry-after": retry} : {};
      return {id: frame.id, status: result.status, body: result.body, timeZone: result.timeZone, headers};
    } catch (e) {
      // Never put cookie, digest, URL, or arbitrary Chrome exception text on the wire.
      return errorReply(outcome === "not-dispatched" ? "request setup failed before dispatch" : uncertain);
    }
  }

  return {handle};
}
