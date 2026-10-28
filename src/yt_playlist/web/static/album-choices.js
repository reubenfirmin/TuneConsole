/* Album-only temporary steering. Server responses own the rendered menu and its evidence ID. */
function albumMenu(tuning, defaults, opportunity) {
  const toPercent = values => Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Math.round(value * 100)]));
  return {
    draft: toPercent(tuning.values), genres: [...tuning.genres], busy: false, refreshing: false, error: '', reset: false,
    openTuning() {
      this.draft = toPercent(tuning.values);
      this.genres = [...tuning.genres];
      this.reset = false;
      this.error = '';
      this.$refs.albumTuner.showModal();
    },
    resetDraft() { this.draft = toPercent(defaults); this.genres = []; this.reset = true; },
    async apply(refresh = false) {
      if (this.busy) return;
      this.busy = true;
      this.refreshing = refresh;
      this.error = '';
      const body = new URLSearchParams(this.draft);
      if (opportunity) body.set('opportunity_id', opportunity);
      for (const genre of this.genres) body.append('genre', genre);
      if (this.reset) body.set('reset', '1');
      try {
        const response = await fetch('/home/album-choices/tune', {method: 'POST', body});
        if (!response.ok) throw new Error('Could not update this selection. Try again.');
        const html = await response.text();
        this.$refs.albumTuner.close();
        htmx.swap('#home-album-choices', html, {swapStyle: 'outerHTML'}, {
          afterSettleCallback: () => document.querySelector('.album-tune')?.focus()});
      } catch (error) {
        this.error = error.message;
        this.busy = false;
        this.refreshing = false;
      }
    }
  };
}
