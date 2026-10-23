// Home and Road Trip open the same generation experience before their async playlist request.
function genOpenYT(id) {
  try {
    const thumbs = Array.from(document.getElementById(id).querySelectorAll('.gen-row, .rt-row'))
      .map((row) => row.dataset.thumb).filter(Boolean).slice(0, 16);
    localStorage.setItem('tc_gen_thumbs', JSON.stringify(thumbs));
  } catch (_) {}
  try { window.__ytTab = window.open('/home/generating', '_blank'); } catch (_) {}
}

