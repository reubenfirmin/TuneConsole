/* Readiness is server-observed. This browser remembers only notice visibility. */
function accountGrowth(initial, initialPanel = '') {
  return {
    growth: initial, seen: [], later: false, selected: null, choosing: false,
    homeChoice: '', choiceError: '', savingChoice: false, refreshing: false,
    growthPanel: location.hash.slice(1) || initialPanel, twinkling: false, hovered: null, mapPanned: false,
    destinationPopup: null, destroyed: false, choiceVersion: 0,
    hasActionNotices: false,
    get newNodes() {
      const unseen = this.growth.nodes.filter(node => !this.seen.includes(node.id));
      return [
        ...unseen.filter(node => node.recommendation_at != null)
          .sort((a, b) => a.recommendation_at - b.recommendation_at),
        ...unseen.filter(node => node.ready && !node.bonus && node.id !== 'first'),
      ];
    },
    get notice() { return this.newNodes[0] || null; },
    get hasIntegrationNotice() { return this.growth.nodes.some(node => node.recommendation_at != null); },
    get choiceInvite() { return this.growth.home_choice_available && !this.later; },
    get selectedNode() { return this.growth.nodes.find(node => node.id === this.selected); },
    node(id) { return this.growth.nodes.find(node => node.id === id); },
    trail(id) { return this.growth.trails.find(trail => trail.id === id); },
    homePanel(anchor) { return anchor.startsWith('gen-') ? 'directions' : anchor; },
    areaAccessible(panel) {
      return ['unlocks', 'notices'].includes(panel) || this.growth.ready_areas[panel] === true ||
        this.growth.open_areas?.includes(panel) ||
        this.growth.nodes.some(node => node.unlocked && node.href === '/#' + panel);
    },
    get pathStates() { return this.growth.trails.some(trail => ['connected', 'explored', 'ready'].includes(trail.state)) ? ['open'] : []; },
    isNew(node) { return this.newNodes.some(item => item.id === node.id); },
    saveVisibility() {
      try { localStorage.setItem('tc-growth-notices-v1', JSON.stringify({seen: this.seen, later: this.later})); } catch (_) {}
    },
    markSeen(node) {
      if (node && !this.seen.includes(node.id)) this.seen.push(node.id);
      this.saveVisibility();
    },
    decideLater() { this.later = true; this.choosing = false; this.$refs.homeDialog.close(); this.saveVisibility(); },
    openChoice() {
      this.choosing = true; this.choiceError = '';
      this.$refs.homeDialog.showModal();
    },
    openNode(id) {
      if (id === 'first') return;
      this.selected = id;
      // Open synchronously: a queued close event from the previous destination
      // must see the new open dialog rather than clearing its selection.
      this.destinationPopup.showModal();
    },
    closeNode() { this.destinationPopup.close(); },
    visit(node) {
      if (!node.bonus && node.unlocked && node.href) {
        this.closeNode(); this.go(node.href.split('#')[1] || 'directions');
      }
    },
    go(panel) {
      if (!this.areaAccessible(panel)) panel = 'unlocks';
      if (this.destinationPopup?.open) this.closeNode();
      location.hash = panel;
      fetch('/home/format', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({panel})}).catch(() => {});
    },
    async saveHome() {
      if (!this.homeChoice || this.savingChoice) return;
      const choice = this.homeChoice;
      this.choiceVersion++;
      this.savingChoice = true; this.choiceError = '';
      try {
        const response = await fetch('/home/growth/format', {method: 'POST',
          signal: AbortSignal.timeout(20000), headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: new URLSearchParams({format: choice})});
        if (!response.ok) {
          console.debug('[Home choice] response unavailable', response.status, await response.text());
          throw new Error('Home choice failed');
        }
        if (!this.destroyed) this.homeSaved(choice);
      } catch (error) {
        console.debug('[Home choice] save not confirmed', error);
        if (this.destroyed) return;
        this.choiceError = 'Choice not confirmed. Your selection is kept here.';
        // The write may have completed before its reply was lost. Read the
        // saved choice once; never replay the POST to discover its outcome.
        try {
          const response = await fetch('/home/growth', {signal: AbortSignal.timeout(20000)});
          if (response.ok) {
            const state = await response.json();
            if (!this.destroyed && state.home_choice === choice) this.homeSaved(choice);
          }
        } catch (error) { console.debug('[Home choice] saved choice unavailable', error); }
      }
      finally { this.savingChoice = false; }
    },
    homeSaved(choice) {
      this.choiceVersion++;
      this.growth.home_choice = choice; this.growth.home_choice_available = false;
      this.choosing = false; this.later = true; this.choiceError = '';
      this.$refs.homeDialog.close(); this.saveVisibility();
    },
    applyGrowth(state) {
      if (state.has_action_notices !== undefined) this.hasActionNotices = state.has_action_notices;
      if (state.vibes_ready === null && this.growth.vibes_ready != null) {
        state.vibes_ready = this.growth.vibes_ready;
      }
      this.growth = state;
    },
    async refresh(includeVibes = true) {
      if (document.hidden || this.destroyed) return;
      if (this.refreshing) {
        if (includeVibes && this.navigationCheck) this.vibesRequested = true;
        return;
      }
      const choiceVersion = this.choiceVersion;
      this.refreshing = true;
      this.navigationCheck = !includeVibes;
      const requests = this.growthRequests ||= new AbortController();
      try {
        const response = await fetch(includeVibes ? '/home/growth' : '/home/growth?navigation=true',
          {signal: AbortSignal.any([requests.signal, AbortSignal.timeout(20000)])});
        if (response.ok) {
          const state = await response.json();
          if (!this.destroyed && choiceVersion === this.choiceVersion && Array.isArray(state.nodes))
            this.applyGrowth(state);
        }
      } catch (_) { /* Keep the last real snapshot during a temporary disconnect. */ }
      finally {
        this.refreshing = false;
        if (this.vibesRequested) { this.vibesRequested = false; this.refresh(true); }
      }
    },
    init() {
      // The map lives inside Home's separate tab component. Keep its dialog
      // available to the corner notice as well as the map's own controls.
      this.destinationPopup = this.$el.querySelector('.growth-detail');
      try {
        const saved = JSON.parse(localStorage.getItem('tc-growth-notices-v1') || '{}');
        this.seen = Array.isArray(saved.seen) ? saved.seen.filter(value => typeof value === 'string') : [];
        this.later = saved.later === true;
      } catch (_) {}
      this.twinkling = this.newNodes.length > 0;
      this.twinkleTimer = setTimeout(() => { this.twinkling = false; }, 1800);
      // The second frame starts after the browser can paint the selected tab.
      this.paint = requestAnimationFrame(() => {
        this.paint = requestAnimationFrame(() => {
          if (this.$el.querySelector('.home-dashboard')) this.refresh(this.growthPanel === 'unlocks');
        });
      });
      this.timer = setInterval(() => {
        if (this.$el.querySelector('.home-dashboard')) this.refresh(this.growthPanel === 'unlocks');
      }, 15000);
    },
    destroy() {
      this.destroyed = true;
      this.growthRequests?.abort();
      cancelAnimationFrame(this.paint);
      clearInterval(this.timer);
      clearTimeout(this.twinkleTimer);
    },
  };
}
