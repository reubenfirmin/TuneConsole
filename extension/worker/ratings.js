// Reconcile one saved desired rating. Only an explicit rejection before a click is retryable.
function createNativeRatings({pickTab, sensors, send}) {
  let current = null, serial = 0;
  const desiredStates = new Set(["LIKE", "DISLIKE", "INDIFFERENT"]);

  function report(job, state, error = "") {
    try {
      send({type: "rating-reconciliation", ratingId: job.message.ratingId,
        videoId: job.message.videoId, desiredStatus: job.message.likeStatus,
        state, error: String(error).slice(0, 500),
        attempts: job.attempts, elapsedMs: Date.now() - job.started,
        first: job.first, last: job.last});
    } catch (e) {}
  }

  function finish(job, state, error = "") {
    if (current !== job) return;
    current = null;
    clearTimeout(job.timer);
    if (state !== "recovered" && job.delivering && job.tabId != null) {
      // Also invalidate a probe already awaiting its MAIN reply in this receiver.
      chrome.tabs.sendMessage(job.tabId, {type: "cancel-rating", ratingId: job.message.ratingId}).catch(() => {});
    }
    if (job.deferred) report(job, state, error);
  }

  function valid(job) { return current === job && Date.now() < job.message.expiresAt; }

  async function attempt(job) {
    if (!valid(job)) return {ok: false, error: "Native rating deadline expired"};
    // Probe both implementation revisions before delivery. A healthy old receiver must never
    // receive a command whose cancellation/deadline semantics it does not implement.
    let health = await sensors.ping(job.tabId);
    if (!valid(job)) return {ok: false, error: "Native rating cancelled or expired"};
    if (!health.ok && !job.reinjected) {
      job.reinjected = true;
      await sensors.inject(job.tabId);
      if (!valid(job)) return {ok: false, error: "Native rating cancelled or expired"};
      health = await sensors.ping(job.tabId);
    }
    if (!valid(job)) return {ok: false, error: "Native rating cancelled or expired"};
    if (!health.ok) return {ok: false, retryable: !health.outdatedReceiver,
      error: health.error, diagnostics: {receiverRevision: health.reply?.receiverRevision ?? null,
        mainVersion: health.reply?.mainVersion ?? null, mainRevision: health.reply?.mainRevision ?? null,
        mainAgeMs: health.reply?.mainAgeMs ?? null}};
    try {
      job.delivering = true;
      return await chrome.tabs.sendMessage(job.tabId, job.message) ||
        {ok: false, error: "Native rating reply missing; delivery is uncertain"};
    } catch (e) {
      // A missing receiver proves no delivery. A lost reply does not prove no click.
      return {ok: false, retryable: isMissingReceiver(e), error: String(e).slice(0, 500)};
    } finally { job.delivering = false; }
  }

  function remember(job, result) {
    job.last = {error: result.error || "", diagnostics: result.diagnostics || null};
    if (!job.first) job.first = job.last;
  }

  function schedule(job) {
    const delay = Math.min(2000, 250 * 2 ** (job.attempts - 1), job.message.expiresAt - Date.now());
    job.timer = setTimeout(() => recover(job), Math.max(0, delay));
  }

  async function recover(job) {
    if (current !== job) return;
    if (!valid(job)) { finish(job, "failed", "Native rating deadline expired; " + job.last.error); return; }
    // A suspended page can leave sendMessage unresolved. End recovery on time even while
    // awaiting it; the receiver independently rejects any late probe before clicking.
    job.timer = setTimeout(() => finish(job, "failed", "Native rating deadline expired; " + job.last.error),
      job.message.expiresAt - Date.now());
    let result;
    try { job.attempts += 1; result = await attempt(job); }
    catch (e) { result = {ok: false, error: String(e).slice(0, 500)}; }
    if (current !== job) return;
    clearTimeout(job.timer);
    remember(job, result);
    if (result.ok) finish(job, "recovered");
    else if (!result.retryable) finish(job, "failed", result.error);
    else schedule(job);
  }

  async function rate(action, videoId, likeStatus, skip = false, brandId) {
    if (current) finish(current, "cancelled", "A newer rating superseded this request");
    const started = Date.now();
    const job = current = {started, attempts: 1, reinjected: false,
      message: {type: "rate", action, videoId, likeStatus, skip, brandId,
        ratingId: `${started}-${++serial}`, expiresAt: started + 30000}};
    let result;
    try {
      const tab = await pickTab();
      if (current !== job) return {ok: true, superseded: true};
      job.tabId = tab?.id;
      result = !videoId ? {ok: false, error: "Displayed song is missing"}
        : !tab ? {ok: false, error: "No music player is available"} : await attempt(job);
    } catch (e) { result = {ok: false, error: String(e).slice(0, 500)}; }
    if (current !== job) return {ok: true, superseded: true};
    remember(job, result);
    if (result.retryable && !skip && typeof brandId === "string" && desiredStates.has(likeStatus) && valid(job)) {
      job.deferred = true;
      report(job, "pending");
      schedule(job);
      return {ok: true, pending: true};
    }
    finish(job, result.ok ? "recovered" : "failed", result.error);
    return result;
  }

  function observe(msg, tabId) {
    const job = current;
    // track_exit/ended events can describe the preceding song. Only a current snapshot
    // establishes that playback/account moved away from this job's target.
    if (!job || job.tabId !== tabId || !["play", "now-heartbeat"].includes(msg.type)) return;
    if ((msg.videoId && msg.videoId !== job.message.videoId) ||
        (msg.brandKnown && typeof job.message.brandId === "string" && msg.brandId !== job.message.brandId)) {
      finish(job, "cancelled", "The playing song or account changed");
    }
  }

  return {rate, observe, removed(tabId) {
    if (current?.tabId === tabId) finish(current, "cancelled", "The music tab closed");
  }};
}
