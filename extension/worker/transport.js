// Owns the WebSocket and its reconnect timer. Request dispatch is supplied by the bootstrap.
function createBridgeTransport({url, dispatch, onOpen, onStatus, warn = () => {}}) {
  let socket = null;

  function send(frame) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(frame));
    return true;
  }

  function connect() {
    if (socket && socket.readyState !== WebSocket.CLOSED) return;
    const sock = new WebSocket(url);
    socket = sock;
    sock.onopen = async () => { onStatus(); await onOpen(); };
    sock.onmessage = async event => {
      let frame;
      try { frame = JSON.parse(event.data); }
      catch (e) { warn("Malformed bridge request"); return; }
      if (!frame || typeof frame !== "object" || Array.isArray(frame)) {
        warn("Malformed bridge request"); return;
      }
      if (frame && frame.ping) return;
      const acknowledged = Number.isSafeInteger(frame.id) && frame.id > 0;
      if (!acknowledged && ("id" in frame || !["navigate", "playpause", "refresh-view"].includes(frame.type))) {
        warn("Bridge request has no usable ID"); return;
      }
      let reply;
      try { reply = await dispatch(frame); }
      catch (e) {
        warn("Bridge request dispatch failed");
        // Unknown dispatch failures may follow a completed mutation. Do not replay them.
        if (acknowledged) reply = {id: frame.id, status: 0, body: JSON.stringify({
          error: "bridge request failed; the request may have completed. Check YouTube Music before retrying.",
          outcome: "uncertain",
        })};
      }
      // Reply on the socket which received the request, even if it was replaced meanwhile.
      if (reply) { try { sock.send(JSON.stringify(reply)); } catch (e) {} }
    };
    sock.onclose = () => {
      if (socket === sock) {
        socket = null;
        onStatus();
        setTimeout(connect, 3000);
      }
    };
    sock.onerror = () => { try { sock.close(); } catch (e) {} };
  }

  return {connect, send, get connected() { return !!socket && socket.readyState === WebSocket.OPEN; }};
}
