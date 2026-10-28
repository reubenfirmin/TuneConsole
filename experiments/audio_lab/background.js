export class CaptureController {
  constructor(api) {
    this.api = api;
    this.active = null;
    this.states = new Map();
    this.queue = Promise.resolve();
    // A restarted worker must never inherit consent or silently resume capture.
    this.ready = this.closeDocument().then(async hadDocument => {
      await this.api.action.setBadgeText({text: ''});
      if (hadDocument) {
        const tabs = await this.api.runtime.getContexts({contextTypes: ['TAB'], documentUrls: [this.referenceURL()]});
        await Promise.all(tabs.map(tab => this.notify(tab.tabId, 'stopped', {reason: 'Capture stopped after worker restart'})));
      }
    });
  }
  referenceURL() { return this.api.runtime.getURL('reference.html'); }
  async closeDocument() {
    const contexts = await this.api.runtime.getContexts({contextTypes: ['OFFSCREEN_DOCUMENT']});
    if (contexts.length) await this.api.offscreen.closeDocument();
    return contexts.length > 0;
  }
  notify(tabId, status, details = {}) {
    return this.api.runtime.sendMessage({target: 'reference', tabId, status, ...details}).catch(() => {});
  }
  serial(task) {
    const next = this.queue.then(() => this.ready).then(task);
    this.queue = next.catch(() => {});
    return next;
  }
  async stop(reason = 'Stopped') {
    const previous = this.active;
    this.active = null;
    await this.closeDocument(); // Closing releases tracks even if the page/worker failed.
    await this.api.action.setBadgeText({text: ''});
    if (previous) await this.notify(previous.tabId, 'stopped', {reason});
  }
  action(tab) {
    return this.serial(async () => {
      if (this.active) { await this.stop(); return; }
      if (tab.url !== this.referenceURL()) {
        await this.api.tabs.create({url: this.referenceURL()});
        return;
      }
      const state = this.states.get(tab.id);
      if (!state?.permitted || !/^sha256:[a-f0-9]{64}$/.test(state.source || '')
          || !Number.isFinite(state.at) || Date.now() - state.at > 1500 || Date.now() - state.at < -100
          || !state.permission_note?.trim()) {
        await this.notify(tab.id, 'error', {error: 'Choose a reference file and confirm its source permission first.'});
        return;
      }
      const session = crypto.randomUUID();
      this.active = {tabId: tab.id, session, source: state.source};
      try {
        await this.api.offscreen.createDocument({url: 'offscreen.html',
          reasons: ['USER_MEDIA', 'AUDIO_PLAYBACK', 'WORKERS'],
          justification: 'Analyse bounded windows of an explicitly permitted reference and preserve audible playback.'});
        // Called only from the toolbar action; ordinary runtime messages cannot start capture.
        const streamId = await this.api.tabCapture.getMediaStreamId({targetTabId: tab.id});
        let timeout;
        let result;
        try {
          result = await Promise.race([
            this.api.runtime.sendMessage({target: 'offscreen', type: 'start', streamId, session, model: state.model === true}),
            new Promise((resolve, reject) => { timeout = setTimeout(() => reject(Error('Capture startup timed out')), 10000); }),
          ]);
        } finally { clearTimeout(timeout); }
        if (!result?.ok) throw Error(result?.error || 'Capture did not start');
        await this.api.action.setBadgeText({text: 'ON'});
        await this.api.action.setBadgeBackgroundColor({color: '#315c40'});
        await this.notify(tab.id, 'capturing', {session, ...result});
      } catch (error) {
        await this.stop();
        await this.notify(tab.id, 'error', {error: error.message});
      }
    });
  }
  async message(message, sender) {
    if (message.target !== 'background') return;
    if (message.type === 'capture-event') {
      if (sender.url !== this.api.runtime.getURL('offscreen.html')
          || !this.active || message.session !== this.active.session) return;
      const {tabId} = this.active;
      if (['error', 'ended'].includes(message.status)) await this.serial(() => this.stop(message.error));
      else await this.notify(tabId, message.status, {result: message.result});
      return;
    }
    if (sender.url !== this.referenceURL()) return;
    // Extension tabs do not always have MessageSender.tab. Resolve the document
    // through Chrome, rather than trusting a tab ID supplied in a message.
    const contexts = await this.api.runtime.getContexts({contextTypes: ['TAB'], documentIds: [sender.documentId]});
    const tabId = contexts[0]?.tabId;
    if (!Number.isInteger(tabId) || tabId < 0) return;
    if (message.type === 'stop') return this.serial(() => this.stop());
    if (message.type === 'state') {
      const state = message.state;
      this.states.set(tabId, state);
      if (this.active?.tabId !== tabId) return;
      if (!state?.permitted || state.source !== this.active.source) {
        return this.serial(() => this.stop('Reference or permission changed'));
      }
      return this.api.runtime.sendMessage({target: 'offscreen', type: 'state',
        session: this.active.session, state}).catch(() => this.serial(() => this.stop('Capture document closed')));
    }
  }
  removed(tabId) {
    this.states.delete(tabId);
    if (this.active?.tabId === tabId) return this.serial(() => this.stop('Reference tab closed'));
  }
  updated(tabId, change) {
    if (this.active?.tabId === tabId && (change.status === 'loading' || change.url)) {
      this.states.delete(tabId);
      return this.serial(() => this.stop('Reference page navigated'));
    }
  }
}

if (typeof chrome !== 'undefined') {
  const controller = new CaptureController(chrome);
  chrome.action.onClicked.addListener(tab => controller.action(tab));
  chrome.tabs.onRemoved.addListener(id => controller.removed(id));
  chrome.tabs.onUpdated.addListener((id, change) => controller.updated(id, change));
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.target !== 'background') return;
    controller.message(message, sender).then(() => reply({ok: true}), error => reply({ok: false, error: error.message}));
    return true;
  });
}
