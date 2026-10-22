// Road Trip recipe form: chip inputs for their-artists / their-genres,
// a mine/theirs split slider, and an hours+minutes length pair. Genre suggestions reuse the same
// fetch-once-then-filter-client-side cache as genrePicker() (the taxonomy is small and static);
// artist suggestions can't be cached that way, so they're a debounced server search instead.
// The Road Trip recipe editor. Server-rendered pre-filled (the recipe currently on screen), and it
// steers that recipe's live draft: chip and slider changes submit themselves, so the form works as a
// control panel rather than a one-shot create dialog. Text fields wait for Apply, so the re-render a
// submit triggers can't pull the field out from under you mid-word.
function roadTripBalance(rows) {
  function apportion(weights, total) {
    const sum = weights.reduce((a, b) => a + b, 0);
    const raw = weights.map(v => total * (sum ? v / sum : 1 / weights.length));
    const values = raw.map(Math.floor);
    const order = raw.map((v, i) => i).sort((a, b) => (raw[b] - values[b]) - (raw[a] - values[a]));
    const left = total - values.reduce((a, b) => a + b, 0);
    order.slice(0, left).forEach(i => values[i]++);
    return values;
  }
  let shares = rows.map(row => row.balance_pct);
  // Templates and JS can update before an already-running Python server is restarted. Normalize
  // its older response too; the versioned slider endpoint prevents submitting to its old math.
  if (shares.some(value => value == null)) {
    const pinned = rows.map((row, i) => i).filter(i => rows[i].target != null);
    const floating = rows.map((row, i) => i).filter(i => rows[i].target == null);
    const requested = pinned.reduce((sum, i) => sum + rows[i].target, 0);
    const claimed = pinned.length ? (rows.length === 1 ? Math.round(requested * 100)
      : floating.length ? Math.min(100, Math.round(requested * 100)) : 100) : 0;
    shares = rows.map(() => 0);
    apportion(pinned.map(i => rows[i].target), claimed).forEach((v, i) => { shares[pinned[i]] = v; });
    apportion(floating.map(i => rows[i].share || 0), 100 - claimed).forEach((v, i) => { shares[floating[i]] = v; });
  }
  return {
    shares,
    error: '',
    savedShares: [...shares],
    dragBase: null,
    requestFailed(event) {
      this.shares = [...this.savedShares];
      this.dragBase = null;
      this.error = event.detail.xhr.status === 404
        ? 'Restart TuneConsole to load the updated mix controls.'
        : 'The balance could not be saved. Please try again.';
    },
    rebalance(index, value) {
      if (!this.dragBase) this.dragBase = [...this.shares];
      const pct = Math.max(0, Math.min(100, Math.round(Number(value))));
      const others = this.dragBase.map((share, i) => ({ i, share })).filter(row => row.i !== index);
      const total = others.reduce((sum, row) => sum + row.share, 0);
      others.forEach(row => {
        row.raw = (100 - pct) * (total ? row.share / total : 1 / others.length);
        row.pct = Math.floor(row.raw);
      });
      // Apportion rounding leftovers so the visible percentages add up to exactly 100.
      let left = 100 - pct - others.reduce((sum, row) => sum + row.pct, 0);
      others.sort((a, b) => (b.raw - b.pct) - (a.raw - a.pct));
      others.forEach(row => { if (left > 0) { row.pct++; left--; } });
      this.shares[index] = pct;
      others.forEach(row => { this.shares[row.i] = row.pct; });
    },
  };
}

function roadTripForm(initial, context) {
  initial = initial || {};
  context = context || {};
  const mins = initial.target_minutes != null ? initial.target_minutes : 60;
  return {
    id: initial.id || null,
    name: initial.name || '',
    renaming: false,
    ownPct: initial.own_pct != null ? initial.own_pct : 50,   // an even split to start
    // 0 = the lesser-played corners of the taste model, 100 = the most-played favorites.
    familiarity: initial.familiarity_pct != null ? initial.familiarity_pct : 50,
    hours: Math.floor(mins / 60),
    minutes: mins % 60,
    artists: initial.artists || [],
    genres: initial.genres || [],
    artistGenres: Object.fromEntries(Object.entries(context.artist_contexts || {})
      .map(([name, facts]) => [name, facts.genre || ''])),
    blendPct: context.blend_available ? 33 : 0,
    get yoursPct() { return Math.round(this.ownPct * (100 - this.blendPct) / 100); },
    get theirsPct() { return 100 - this.blendPct - this.yoursPct; },
    blacklist: initial.blacklist_genres || [],
    steerTimer: null,
    artistQuery: '', artistSuggestions: [], artistTimer: null,
    genreOpts: [], genreQuery: '', blacklistQuery: '', ownGenreQuery: '',
    formElement: null,
    disposed: false,
    destroy() {
      this.disposed = true;
      clearTimeout(this.artistTimer); clearTimeout(this.steerTimer);
      this.formElement = null;
    },
    init() {
      // Suggestion buttons disappear after selection. Keep the form from the component root,
      // since their delayed saves cannot resolve $refs through a detached suggestion element.
      this.formElement = this.$el.querySelector('form');
      this.artists.filter(name => !this.artistGenres[name]).forEach(name => this.loadArtistGenre(name));
    },
    loadArtistGenre(name) {
      return fetch('/road_trip/artist_genre?name=' + encodeURIComponent(name))
        .then(r => r.json()).then(d => {
          if (d.genre && this.artists.includes(name)) this.artistGenres[name] = d.genre;
          return d.genre || '';
        }).catch(() => '');
    },
    genreArtists(genre) {
      const key = value => (value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      return this.artists.filter(name => key(this.artistGenres[name]) === key(genre));
    },
    genreLabel(genre) {
      const artists = this.genreArtists(genre);
      return genre + (artists.length ? ' like ' + artists.join(', ') : '');
    },
    loadGenres() {
      if (window.__genreOpts) { this.genreOpts = window.__genreOpts; return; }
      fetch('/home/genres').then(r => r.json())
        .then(d => { window.__genreOpts = d.options || []; this.genreOpts = window.__genreOpts; })
        .catch(() => {});
    },
    genreSuggest(query) {
      const q = query.trim().toLowerCase();
      if (!q) return [];
      return this.genreOpts.filter(o => o.name.toLowerCase().includes(q)).slice(0, 8);
    },
    addGenre(name) {
      if (!this.genres.includes(name)) this.genres.push(name);
      this.genreQuery = ''; this.steer();
    },
    removeGenre(name) { this.genres = this.genres.filter(g => g !== name); this.steer(); },
    addBlacklist(name) {
      if (!this.blacklist.includes(name)) this.blacklist.push(name);
      this.blacklistQuery = ''; this.steer();
    },
    removeBlacklist(name) { this.blacklist = this.blacklist.filter(g => g !== name); this.steer(); },
    addOwnGenre(name, recipeId) {
      this.ownGenreQuery = '';
      if (!recipeId || !window.htmx) return;
      htmx.ajax('POST', '/road_trip/draft/' + recipeId + '/tilt', {
        target: '#road-trip-body', swap: 'innerHTML',
        values: { party: 'mine', axis: 'genre:' + name, share: '20' },
      });
    },
    searchArtists() {
      clearTimeout(this.artistTimer);
      const q = this.artistQuery.trim();
      if (!q) { this.artistSuggestions = []; return; }
      this.artistTimer = setTimeout(() => {
        fetch('/road_trip/autocomplete/artists?q=' + encodeURIComponent(q))
          .then(r => r.json()).then(d => { this.artistSuggestions = d.results || []; }).catch(() => {});
      }, 250);
    },
    // Keep the artist-to-genre link visible in the chip and slider while discovery is pending.
    // The steer waits for the genre lookup so both additions arrive in one request.
    addArtist(name) {
      if (!this.artists.includes(name)) this.artists.push(name);
      this.artistQuery = ''; this.artistSuggestions = [];
      this.loadArtistGenre(name)
        .then(genre => {
          if (this.artists.includes(name) && genre && !this.genres.includes(genre)) this.genres.push(genre);
        })
        .finally(() => this.steer());
    },
    removeArtist(name) { this.artists = this.artists.filter(a => a !== name); this.steer(); },
    // Apply an edit to the recipe already on screen. Debounced, so tapping through a few chips or
    // dragging a slider sends one request rather than one per step. A recipe that has never been
    // saved waits for the button: it has no draft to steer yet.
    steer() {
      if (this.disposed || !this.id) return;
      clearTimeout(this.steerTimer);
      this.steerTimer = setTimeout(() => this.submit(), 350);
    },
    reset() {
      this.id = null; this.name = ''; this.artists = []; this.genres = []; this.blacklist = [];
      this.artistGenres = {};
      this.renaming = false;
      this.artistQuery = ''; this.genreQuery = ''; this.blacklistQuery = '';
      this.artistSuggestions = [];
    },
    submit() {
      clearTimeout(this.steerTimer);
      const form = this.formElement;
      if (!form || !form.isConnected) return;
      form.querySelector('[name=artists]').value = JSON.stringify(this.artists);
      form.querySelector('[name=genres]').value = JSON.stringify(this.genres);
      form.querySelector('[name=blacklist_genres]').value = JSON.stringify(this.blacklist);
      form.querySelector('[name=target_minutes]').value = String(this.hours * 60 + this.minutes);
      htmx.trigger(form, 'roadtrip:submit');
    },
  };
}

