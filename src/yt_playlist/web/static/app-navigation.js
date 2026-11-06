/* Keep a recoverable document while server-rendered pages change around it,
 * including TV's connected iframe.
 * The Navigation API covers links, forms, scripted redirects, and Back/Forward:
 * https://developer.chrome.com/docs/web-platform/navigation-api
 */
(() => {
  let scripts = null, stylesheets = null, renderedUrl = location.href, pageBodyClasses = null;
  let activePage = null, recovery = null;
  const writes = new Set();
  document.addEventListener('htmx:beforeSend', event => {
    if (!['get', 'head'].includes(event.detail.requestConfig?.verb?.toLowerCase())) writes.add(event.detail.xhr);
  });
  document.addEventListener('htmx:afterRequest', event => writes.delete(event.detail.xhr));
  const assetKey = url => new URL(url, location.href).pathname;

  function cancelRecovery() {
    if (!recovery) return;
    clearTimeout(recovery.timer);
    recovery.controller.abort();
    recovery = null;
  }

  function recoverPage(url) {
    // One owner survives repeated failures. A later navigation cancels both
    // its backoff and its active read, so old destinations cannot reappear.
    if (recovery && recovery.url !== url) cancelRecovery();
    const state = recovery ||= {url, attempt: 0, controller: new AbortController()};
    clearTimeout(state.timer);
    state.timer = setTimeout(() => showPage({destination: {url},
      info: {refresh: true, recovery: state}, signal: state.controller.signal}),
    Math.min(30000, 2000 * 2 ** Math.min(state.attempt++, 4)));
  }
  window.addEventListener('pagehide', cancelRecovery);

  async function loadAssets(page, signal) {
    scripts ||= new Map([...document.scripts].filter(script => script.src)
      .map(script => [assetKey(script.src), Promise.resolve()]));
    stylesheets ||= new Map([...document.querySelectorAll('head link[rel="stylesheet"]')]
      .map(style => [assetKey(style.href), Promise.resolve()]));
    const styles = [...page.querySelectorAll('head link[rel="stylesheet"]')];
    await Promise.all(styles.map(style => {
      const key = assetKey(style.href);
      if (stylesheets.has(key)) return stylesheets.get(key);
      const loaded = new Promise((resolve, reject) => {
        const link = document.importNode(style, true);
        const fail = () => { clearTimeout(timeout); link.remove(); reject(new Error('Stylesheet unavailable')); };
        const timeout = setTimeout(fail, 10000);
        link.onload = () => { clearTimeout(timeout); resolve(); }; link.onerror = fail;
        document.head.append(link);
      });
      stylesheets.set(key, loaded);
      loaded.catch(() => stylesheets.delete(key));
      return loaded;
    }));
    await loadScripts(page, signal, false);
    signal.throwIfAborted();
    // Keep page-specific rules from leaking into the next page. TV's own style
    // is pinned because its component remains mounted outside <main>.
    const active = new Set(styles.map(style => assetKey(style.href)));
    for (const style of document.querySelectorAll('head link[rel="stylesheet"]'))
      style.disabled = !style.hasAttribute('data-persistent') && !active.has(assetKey(style.href));
  }

  async function loadScripts(page, signal, modules) {
    for (const source of page.querySelectorAll('head script[src]')) {
      if ((source.type === 'module') !== modules) continue;
      signal.throwIfAborted();
      const key = assetKey(source.src);
      if (!scripts.has(key)) {
        const loaded = new Promise((resolve, reject) => {
          const script = document.createElement('script');
          for (const attribute of source.attributes) script.setAttribute(attribute.name, attribute.value);
          script.async = false;
          const fail = () => { clearTimeout(timeout); script.remove(); reject(new Error('Page script unavailable')); };
          const timeout = setTimeout(fail, 10000);
          script.onload = () => { clearTimeout(timeout); resolve(); }; script.onerror = fail;
          document.head.append(script);
        });
        scripts.set(key, loaded);
        loaded.catch(() => scripts.delete(key));
      }
      await scripts.get(key);
    }
  }

  function updateNavigation(page) {
    for (const link of document.querySelectorAll('.topbar nav a[href]')) {
      const next = [...page.querySelectorAll('.topbar nav a[href]')]
        .find(candidate => candidate.getAttribute('href') === link.getAttribute('href'));
      link.classList.toggle('active', !!next?.classList.contains('active'));
    }
    document.querySelector('.tools-btn')?.classList.toggle('active',
      !!page.querySelector('.tools-btn.active'));
    const search = document.querySelector('.topbar .omni');
    if (search) search.hidden = !page.querySelector('.topbar .omni');
    (pageBodyClasses || [...document.body.classList]).forEach(name => document.body.classList.remove(name));
    pageBodyClasses = [...page.body.classList];
    pageBodyClasses.forEach(name => document.body.classList.add(name));
    document.body.classList.toggle('app-standalone', !page.querySelector('.topbar'));
    document.documentElement.className = page.documentElement.className;
    document.title = page.title;
    window.syncTopbarH?.();
  }

  async function showPage(event) {
    if (event.info?.recovery && writes.size) { recoverPage(event.destination.url); return; }
    const request = {};
    activePage = request;
    const previous = renderedUrl;
    let swapped = false;
    const main = document.getElementById('app-page');
    if (!event.info?.recovery) main.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(event.destination.url, {
        signal: event.signal, headers: {Accept: 'text/html'},
        ...(event.formData ? {method: 'POST', body: event.formData} : {}),
      });
      if (!response.ok) throw Object.assign(new Error('Page unavailable'), {httpStatus: response.status});
      if (!response.headers.get('Content-Type')?.includes('text/html'))
        throw Object.assign(new Error('Unexpected page response'), {httpStatus: response.status});
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      await loadAssets(page, event.signal);
      let next = page.querySelector('#app-page');
      if (!next) {
        // Standalone recap and generation screens still share the same player.
        page.querySelectorAll('body script[data-persistent]').forEach(script => script.remove());
        next = page.createElement('main'); next.id = 'app-page';
        next.append(...page.body.childNodes);
      }
      event.signal.throwIfAborted();
      // A quiet refresh can have started before a new save. Let that action
      // settle on its original controls before reconciling the page again.
      if (event.info?.recovery && writes.size) { recoverPage(event.destination.url); return; }
      const destination = new URL(response.redirected ? response.url : event.destination.url, location.href);
      const current = new URL(previous);
      const state = event.info?.refresh && !event.formData
        && destination.pathname === current.pathname && destination.search === current.search ? {} : null;
      window.dispatchEvent(new CustomEvent('app-page-leaving', {detail: {state}}));
      document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
      updateNavigation(page);
      renderedUrl = response.redirected ? response.url : event.destination.url;
      if (response.redirected || event.info?.recovery) history.replaceState(history.state, '', renderedUrl);
      // Dispose page scopes while their full subtree is still intact. HTMX
      // clears descendants before Alpine's removal observer can walk them;
      // teleported panels and page timers would otherwise survive the swap.
      Alpine.destroyTree(main);
      await new Promise(resolve => htmx.swap(main, next.outerHTML,
        {swapStyle: 'outerHTML', settleDelay: 0},
        {afterSettleCallback: () => Alpine.nextTick(resolve)}));
      swapped = true;
      // A document opened in the library has not mounted Home's persistent
      // TV player yet. Install it on first entry; later visits retain its
      // iframe and browsing context outside the replaceable page subtree.
      const tv = page.querySelector('body > .tv-session');
      if (tv && !document.querySelector('body > .tv-session')) {
        document.body.append(document.importNode(tv, true));
        await Alpine.nextTick();
      }
      window.dispatchEvent(new CustomEvent('app-page-changed', {detail: {state}}));
      // The persistent player card also needs to update its Back to source link.
      window.dispatchEvent(new Event('hashchange'));
      // Modules can bind directly to their page DOM, like a deferred module on
      // a normal document load. Classic Alpine factories load before the swap.
      await loadScripts(page, event.signal, true);
      const toasts = page.querySelector('#toasts');
      if (toasts?.innerHTML.trim()) htmx.swap('#toasts', toasts.innerHTML, {swapStyle: 'beforeend', settleDelay: 0});
      cancelRecovery();
    } catch (error) {
      if (event.signal.aborted) return;
      if (!swapped) {
        renderedUrl = previous;
        history.replaceState(history.state, '', previous);
      }
      window.dispatchEvent(new Event('app-page-changed'));
      // A failed write is never replayed: reconcile the last usable page.
      // Ordinary HTTP rejections stay rejected; only transient reads retry.
      if (event.formData || !error.httpStatus || error.httpStatus >= 500 || error.httpStatus === 429)
        recoverPage(event.formData ? previous : event.destination.url);
      else cancelRecovery();
      console.debug('[Navigation] page unavailable', error);
    } finally {
      if (activePage === request) {
        activePage = null;
        document.getElementById('app-page')?.removeAttribute('aria-busy');
      }
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!document.querySelector('[data-read-recovery]')) return;
    const controller = new AbortController();
    const cancel = () => controller.abort();
    window.addEventListener('pagehide', cancel, {once: true});
    window.navigation?.addEventListener('navigate', cancel, {once: true});
    showPage({destination: {url: location.href}, signal: controller.signal})
      .finally(() => {
        window.removeEventListener('pagehide', cancel);
        window.navigation?.removeEventListener('navigate', cancel);
      });
  });

  window.addEventListener('app-request-failed', event => {
    // Navigation, JSON controls and background polls own their reads. Only
    // uncertain fragment writes request page reconciliation here; a failed
    // status poll must never replace an unrelated form or interaction.
    if (!event.detail.reconcile || activePage || recovery) return;
    recoverPage(renderedUrl);
  });
  if (!window.navigation) return;
  navigation.addEventListener('navigate', event => {
    if (!document.getElementById('app-page') || !event.canIntercept || event.hashChange
        || event.downloadRequest !== null || event.navigationType === 'reload') return;
    // Existing panels use pushState/replaceState for local filters. Let those
    // update their own DOM; actual document navigation and traversal use fetch.
    if (event.destination.sameDocument && !event.formData && !event.info?.refresh
        && ['push', 'replace'].includes(event.navigationType)) {
      renderedUrl = event.destination.url;
      return;
    }
    cancelRecovery();
    event.intercept({handler: () => showPage(event)});
  });

  document.addEventListener('htmx:beforeOnLoad', event => {
    const xhr = event.detail.xhr;
    const refresh = xhr.getResponseHeader('HX-Refresh') === 'true';
    const redirect = xhr.getResponseHeader('HX-Redirect');
    if (!refresh && !redirect) return;
    const url = new URL(redirect || location.href, location.href);
    if (url.origin !== location.origin) return;
    event.preventDefault();
    // Cancelling beforeOnLoad also skips HTMX's response-header events.
    // Deliver them before navigating so confirmed saves can retire their
    // launch window and duplicate-save guard when the new page is ready.
    const trigger = xhr.getResponseHeader('HX-Trigger');
    if (trigger) {
      if (trigger.trim().startsWith('{')) {
        for (const [name, value] of Object.entries(JSON.parse(trigger))) {
          const detail = value !== null && typeof value === 'object' ? value : {value};
          htmx.trigger(detail.target || event.detail.elt, name, detail);
        }
      } else {
        for (const name of trigger.split(',')) htmx.trigger(event.detail.elt, name.trim(), []);
      }
    }
    navigation.navigate(url.href, {history: refresh ? 'replace' : 'auto', info: {refresh: true}});
  });
})();
