/* Bounded reads and quiet recovery. Mutations are never replayed, and each
 * fragment target owns at most one pending retry. */
(() => {
  const nativeFetch = window.fetch.bind(window);
  let lifetime = new AbortController();
  window.addEventListener('pagehide', () => lifetime.abort());
  // Restored documents keep this closure. This script loads before shared
  // pollers, so their restored requests use the new lifetime. Old calls keep
  // their original, canceled signal.
  window.addEventListener('pageshow', event => {
    if (event.persisted && lifetime.signal.aborted) lifetime = new AbortController();
  });
  const deferred = response => response.status === 503 &&
    (response.headers?.get('X-Read-Pending') || response.getResponseHeader?.('X-Read-Pending')) === 'true';
  const interval = (response, attempt) => {
    const seconds = Number(response.headers?.get('Retry-After') || response.getResponseHeader?.('Retry-After') || 2);
    return Math.min(30000, Math.max(1000, (Number.isFinite(seconds) ? seconds : 2) * 1000)
      * 2 ** Math.min(attempt, 5));
  };
  const wait = (milliseconds, signal) => new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const cleanup = () => signal.removeEventListener('abort', abort);
    const finish = () => { cleanup(); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const abort = event => { clearTimeout(timer); cleanup(); reject(event.target.reason); };
    signal.addEventListener('abort', abort, {once: true});
  });
  // JSON panels, polling and page navigation use the same explicit read
  // deferral. Preserve ordinary fetch behavior for all other responses.
  const failed = (url, method, unresponsive = false, reconcile = false) => window.dispatchEvent(Object.assign(new Event('app-request-failed'),
    {detail: {url: url.url || String(url), method, unresponsive, reconcile}}));
  window.fetch = async function recoverRead(url, options = {}) {
    const method = (options.method || url.method || 'GET').toUpperCase();
    const caller = options.signal || url.signal;
    // Native composition also preserves cancellation after response headers,
    // while the caller is still consuming a streamed body.
    const parent = lifetime.signal.constructor.any([caller, lifetime.signal].filter(Boolean));
    let attempt = 0;
    while (true) {
      parent.throwIfAborted();
      const deadline = lifetime.signal.constructor.timeout(20000);
      const signal = lifetime.signal.constructor.any([parent, deadline]);
      let reported = false;
      const report = error => {
        if (!parent.aborted && !reported) {
          reported = true;
          console.debug('[Read recovery] request unavailable', method, url.url || String(url), error);
          failed(url, method, deadline.aborted);
        }
        throw error;
      };
      const response = await nativeFetch(url, {...options, signal}).catch(report);
      // Keep the deadline through body consumption. Clearing it at headers
      // would still allow text()/json() and navigation to hang indefinitely.
      for (const name of ['text', 'json', 'blob', 'arrayBuffer', 'formData', 'bytes']) {
        if (!response[name]) continue;
        const consume = response[name].bind(response);
        response[name] = (...args) => consume(...args).catch(report);
      }
      if (method !== 'GET' || !deferred(response)) return response;
      // The recovery document is for initial browser navigation. A fetch
      // retry needs only its signal, not the unused response body.
      await response.body?.cancel();
      await wait(interval(response, attempt++), parent);
    }
  };

  const pending = new Map();
  // These controls reconcile their own durable receipt without replacing the
  // page. A global refresh would discard their unresolved action state.
  const reconcilePage = config => config?.elt?.dataset?.writeRecovery !== 'local';
  const generations = new WeakMap(), requests = new WeakMap();
  function clear(target) {
    if (!target) return;
    const state = pending.get(target);
    clearTimeout(state?.timer);
    (state?.target || target).removeAttribute('aria-busy');
    pending.delete(target);
    generations.set(target, (generations.get(target) || 0) + 1);
  }
  function retryFragment(request, requestConfig, xhr, target = request.target, swap, select) {
    const {generation, target: originalTarget, state} = request;
    const source = requestConfig.elt;
    request.retrying = true;
    state.swap = swap ?? state.swap;
    state.select = select ?? state.select;
    clearTimeout(state.timer);
    if (state.target !== target) state.target?.removeAttribute('aria-busy');
    state.target = target;
    pending.set(originalTarget, state);
    // Explicit preparation owns its busy indication. A failed transport must
    // release controls and leave useful content usable during quiet backoff.
    if (deferred(xhr)) target.setAttribute('aria-busy', 'true');
    else {
      target.removeAttribute('aria-busy');
      console.debug('[Read recovery] fragment unavailable', requestConfig.path, xhr.status);
    }
    state.timer = setTimeout(() => {
      if (!source?.isConnected || !target.isConnected) { clear(originalTarget); return; }
      if (generations.get(originalTarget) !== generation) return;
      htmx.ajax('GET', requestConfig.path, {source, target: originalTarget,
        swap: state.swap, select: state.select,
        values: requestConfig.parameters, headers: requestConfig.headers}).catch(() => {});
    }, interval(xhr, state.attempt++));
  }
  // beforeRequest is cancelable. A refresh vetoed by the board must not
  // invalidate the explicit selection that is already using this target.
  document.addEventListener('htmx:beforeSend', event => {
    const {target, xhr} = event.detail;
    const generation = (generations.get(target) || 0) + 1;
    generations.set(target, generation);
    requests.set(xhr, {target, generation, state: pending.get(target) || {attempt: 0}});
    clearTimeout(pending.get(target)?.timer);
  });
  document.addEventListener('htmx:configRequest', event => {
    event.detail.timeout = Math.min(event.detail.timeout || 20000, 20000);
  });
  document.addEventListener('htmx:beforeSwap', event => {
    const {xhr, requestConfig, target} = event.detail;
    const request = requests.get(xhr);
    // HX-Retarget can legitimately widen an import form's error slot to the
    // whole import block. Freshness belongs to the original request target.
    if (!request || !request.target.isConnected || request.generation !== generations.get(request.target)) {
      event.detail.shouldSwap = false;
      return;
    }
    if (requestConfig?.verb?.toLowerCase() !== 'get' ||
        !(deferred(xhr) || xhr.status >= 500 || xhr.status === 429)) return;
    event.detail.shouldSwap = false;
    event.detail.isError = false;
    // Programmatic fragment loads can override both swap and selection without
    // hx-* attributes on their source. Carry those overrides into the retry.
    retryFragment(request, requestConfig, xhr, target, event.detail.swapOverride,
      event.detail.select || event.detail.selectOverride);
  });
  document.addEventListener('htmx:afterRequest', event => {
    const request = requests.get(event.detail.xhr);
    if (!request || request.generation !== generations.get(request.target)) return;
    if (!request.retrying) clear(request.target);
    const config = event.detail.requestConfig;
    if (event.detail.xhr.status >= 500 && !['get', 'head'].includes(config?.verb?.toLowerCase()))
      failed(config.path, config.verb.toUpperCase(), false, reconcilePage(config));
  });
  for (const name of ['htmx:timeout', 'htmx:sendError']) document.addEventListener(name, event => {
    const {xhr, requestConfig} = event.detail;
    const request = requests.get(xhr);
    // afterRequest has just cleared this generation. An obsolete request must
    // not report a failure on a page or target that has since moved on.
    if (!request || request.generation !== generations.get(request.target) - 1 || !request.target.isConnected) return;
    if (requestConfig.verb.toLowerCase() === 'get') {
      request.generation = generations.get(request.target);
      retryFragment(request, requestConfig, xhr);
    } else failed(requestConfig.path, requestConfig.verb.toUpperCase(), name === 'htmx:timeout', reconcilePage(requestConfig));
  });
  window.addEventListener('app-page-leaving', () => [...pending.keys()].forEach(clear));
  window.addEventListener('pagehide', () => [...pending.keys()].forEach(clear));
})();
