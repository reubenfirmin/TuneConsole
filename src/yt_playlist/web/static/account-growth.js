/* Readiness is server-observed. This browser remembers only notice visibility. */
function accountGrowth(initial) {
  return {
    growth: initial, seen: [], later: false, selected: null, choosing: false,
    homeChoice: '', choiceError: '', savingChoice: false, refreshing: false,
    growthPanel: location.hash.slice(1), twinkling: false, hovered: null, mapPanned: false,
    destinationPopup: null,
    get newNodes() {
      return this.growth.nodes.filter(node => node.ready && !node.bonus
        && node.id !== 'first' && !this.seen.includes(node.id));
    },
    get notice() { return this.newNodes[0] || null; },
    get choiceInvite() { return this.growth.home_choice_available && !this.later; },
    get selectedNode() { return this.growth.nodes.find(node => node.id === this.selected); },
    node(id) { return this.growth.nodes.find(node => node.id === id); },
    homePanel(anchor) { return anchor.startsWith('gen-') ? 'directions' : anchor; },
    areaAccessible(panel) {
      return ['unlocks', 'notices'].includes(panel) || this.growth.ready_areas[panel] === true ||
        this.growth.nodes.some(node => node.unlocked && node.href === '/#' + panel);
    },
    get pathStates() { return this.growth.nodes.some(node => node.state === 'explored') ? ['explored'] : []; },
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
      this.savingChoice = true; this.choiceError = '';
      try {
        const response = await fetch('/home/growth/format', {method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams({format: this.homeChoice})});
        if (!response.ok) throw new Error(await response.text());
        this.growth.home_choice_available = false; this.choosing = false; this.later = true;
        this.$refs.homeDialog.close(); this.saveVisibility();
      } catch (error) { this.choiceError = error.message || 'Could not save your choice. Try again.'; }
      finally { this.savingChoice = false; }
    },
    async refresh() {
      if (this.refreshing || document.hidden) return;
      this.refreshing = true;
      try {
        const response = await fetch('/home/growth');
        if (response.ok) this.growth = await response.json();
      } catch (_) { /* Keep the last real snapshot during a temporary disconnect. */ }
      finally { this.refreshing = false; }
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
      this.timer = setInterval(() => this.refresh(), 15000);
    },
    destroy() { clearInterval(this.timer); clearTimeout(this.twinkleTimer); },
  };
}
