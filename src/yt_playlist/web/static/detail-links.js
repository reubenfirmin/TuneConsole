// Routes and preview fields come from the server's canonical navigation contract.
(() => {
  const config = JSON.parse(document.getElementById('detail-link-config').textContent);
  const encode = value => encodeURIComponent(String(value)).replace(/[!'()*]/g,
    char => '%' + char.charCodeAt(0).toString(16).toUpperCase());
  const query = values => Object.entries(values).filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => encode(key) + '=' + encode(value).replace(/%20/g, '+')).join('&');
  window.artistUrl = (artistId = null, browse = null) => {
    const params = artistId !== null ? {artist_id: artistId} : browse ? {browse} : null;
    return params ? config.routes.artist + '?' + query(params) : null;
  };
  window.albumUrl = browse => browse ? config.routes.album + '?' + query({browse}) : null;
  window.playlistUrl = (playlistId = null, accountId = null, providerId = null) => {
    if (playlistId !== null) return config.routes.playlist + encode(playlistId);
    return accountId !== null && providerId ? config.routes.saved_playlist + encode(accountId) + '/' + encode(providerId) : null;
  };
  window.trackUrl = (song, page = null) => {
    if (typeof song === 'string') song = {video_id: song};
    const video = song?.video_id || song?.videoId || song?.vid;
    if (!video) return '';
    const hints = {};
    for (const [field, keys] of config.track_hints) {
      hints[field] = keys.map(key => song[key]).find(value => value !== undefined && value !== null && value !== '');
    }
    if (page !== null) hints.page = page;
    const params = query(hints);
    return config.routes.track + encode(video) + (params ? '?' + params : '');
  };
})();
