/* Keep TV's iframe connected while server-rendered pages change around it.
 * The Navigation API covers links, forms, scripted redirects, and Back/Forward:
 * https://developer.chrome.com/docs/web-platform/navigation-api
 */
(() => {
  if (!window.navigation) return;
  let scripts = null, stylesheets = null, renderedUrl = location.href, pageBodyClasses = [];
  const assetKey = url => new URL(url, location.href).pathname;

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
    pageBodyClasses.forEach(name => document.body.classList.remove(name));
    pageBodyClasses = [...page.body.classList];
    pageBodyClasses.forEach(name => document.body.classList.add(name));
    document.body.classList.toggle('app-standalone', !page.querySelector('.topbar'));
    document.documentElement.className = page.documentElement.className;
    document.title = page.title;
    window.syncTopbarH?.();
  }

  async function showPage(event) {
    const previous = renderedUrl;
    let swapped = false;
    const main = document.getElementById('app-page');
    main.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(event.destination.url, {
        signal: event.signal, headers: {Accept: 'text/html'},
        ...(event.formData ? {method: 'POST', body: event.formData} : {}),
      });
      if (!response.ok) throw new Error('Page unavailable');
      if (!response.headers.get('Content-Type')?.includes('text/html'))
        throw new Error('Unexpected page response');
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
      window.dispatchEvent(new Event('app-page-leaving'));
      document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
      updateNavigation(page);
      renderedUrl = response.redirected ? response.url : event.destination.url;
      if (response.redirected) history.replaceState(history.state, '', renderedUrl);
      await new Promise(resolve => htmx.swap(main, next.outerHTML,
        {swapStyle: 'outerHTML', settleDelay: 0},
        {afterSettleCallback: () => Alpine.nextTick(resolve)}));
      swapped = true;
      window.dispatchEvent(new Event('app-page-changed'));
      // The persistent player card also needs to update its Back to source link.
      window.dispatchEvent(new Event('hashchange'));
      // Modules can bind directly to their page DOM, like a deferred module on
      // a normal document load. Classic Alpine factories load before the swap.
      await loadScripts(page, event.signal, true);
      const toasts = page.querySelector('#toasts');
      if (toasts?.innerHTML.trim()) htmx.swap('#toasts', toasts.innerHTML, {swapStyle: 'beforeend', settleDelay: 0});
      document.getElementById('navigation-error')?.remove();
    } catch (error) {
      if (event.signal.aborted) return;
      if (!swapped) {
        renderedUrl = previous;
        history.replaceState(history.state, '', previous);
      }
      window.dispatchEvent(new Event('app-page-changed'));
      document.getElementById('navigation-error')?.remove();
      const toast = document.createElement('div');
      toast.id = 'navigation-error'; toast.className = 'toast toast-err'; toast.setAttribute('role', 'alert');
      toast.textContent = 'Couldn’t open that page. Please try again.';
      document.getElementById('toasts')?.append(toast);
      console.debug('[Navigation] page unavailable', error);
    } finally {
      if (!event.signal.aborted) document.getElementById('app-page')?.removeAttribute('aria-busy');
    }
  }

  navigation.addEventListener('navigate', event => {
    if (!window.tvPlaybackControls?.keepAlive() || !event.canIntercept || event.hashChange
        || event.downloadRequest !== null || event.navigationType === 'reload') return;
    // Existing panels use pushState/replaceState for local filters. Let those
    // update their own DOM; actual document navigation and traversal use fetch.
    if (event.destination.sameDocument && ['push', 'replace'].includes(event.navigationType)) {
      renderedUrl = event.destination.url;
      return;
    }
    event.intercept({handler: () => showPage(event)});
  });

  document.addEventListener('htmx:beforeOnLoad', event => {
    if (!window.tvPlaybackControls?.keepAlive()) return;
    const xhr = event.detail.xhr;
    const refresh = xhr.getResponseHeader('HX-Refresh') === 'true';
    const redirect = xhr.getResponseHeader('HX-Redirect');
    if (!refresh && !redirect) return;
    const url = new URL(redirect || location.href, location.href);
    if (url.origin !== location.origin) return;
    event.preventDefault();
    navigation.navigate(url.href, {history: refresh ? 'replace' : 'auto'});
  });
})();
