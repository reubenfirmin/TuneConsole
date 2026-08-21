// Bridge from the CSS token layer to <canvas>, which cannot read CSS.
//
// Canvas takes concrete colour strings, so anything drawn in JS used to hard-code hexes — a second
// palette that silently drifted from the stylesheet's. Instead, resolve tokens at runtime here.
//
// Resolution goes through a probe element rather than
// getComputedStyle(root).getPropertyValue('--x'): a custom property's computed value is a token
// stream, so a value like `color-mix(in srgb, var(--cta) 40%, transparent)` would come back as that
// literal text, which canvas cannot parse. Assigning it to a real `color` property and reading the
// computed style forces the browser to actually evaluate it down to an rgb()/rgba() string.
// Normalise any computed colour to `rgba(r, g, b, a)` with 0-255 channels.
//
// Three syntaxes reach us and they are NOT interchangeable:
//   rgb()/rgba()      channels are 0-255
//   hex               channels are 0-255
//   color(srgb ...)   channels are 0-1 FLOATS   <- what a color-mix() token computes to
// A caller that cracks a colour open to restyle its alpha (the pulse animations do) would read the
// third form's fractional channels as 0-255 and paint near-black. Chrome's canvas round-trips CSS
// Color 4 unchanged, so it cannot do this conversion for us — parse it here instead.
function toRgba(v) {
  const s = String(v).trim();

  const col = s.match(/^color\(\s*srgb\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s*(?:\/\s*([\d.eE+-]+%?)\s*)?\)$/i);
  if (col) {
    const ch = [1, 2, 3].map(function (i) {
      return Math.max(0, Math.min(255, Math.round(parseFloat(col[i]) * 255)));
    });
    let a = col[4] == null ? 1
      : (String(col[4]).endsWith('%') ? parseFloat(col[4]) / 100 : parseFloat(col[4]));
    if (!isFinite(a)) a = 1;
    return 'rgba(' + ch[0] + ', ' + ch[1] + ', ' + ch[2] + ', ' + a + ')';
  }

  if (s.charAt(0) === '#') {
    const h = s.length === 4
      ? s.slice(1).split('').map(function (c) { return c + c; }).join('')
      : s.slice(1);
    return 'rgba(' + parseInt(h.slice(0, 2), 16) + ', ' + parseInt(h.slice(2, 4), 16) + ', '
                   + parseInt(h.slice(4, 6), 16) + ', 1)';
  }

  const rgb = s.match(/^rgba?\(([^)]+)\)$/i);
  if (rgb) {
    const parts = rgb[1].split(/[\s,\/]+/).filter(Boolean).map(parseFloat);
    return 'rgba(' + parts[0] + ', ' + parts[1] + ', ' + parts[2] + ', '
                   + (parts.length > 3 ? parts[3] : 1) + ')';
  }

  return s;   // a named colour or something exotic: hand it back untouched
}

window.themeColor = (function () {
  let probe = null;
  const cache = new Map();
  return function themeColor(token, fallback) {
    if (cache.has(token)) return cache.get(token);
    if (!probe) {
      probe = document.createElement('span');
      probe.setAttribute('aria-hidden', 'true');
      probe.style.cssText = 'position:absolute;width:0;height:0;visibility:hidden;pointer-events:none';
      (document.body || document.documentElement).appendChild(probe);
    }
    // Detect an undefined token without naming a colour: an unresolvable var() makes the
    // declaration invalid at computed-value time, and `color` inherits, so the computed value is
    // unchanged from the baseline. Comparing against that baseline needs no literal of our own.
    probe.style.removeProperty('color');
    const baseline = getComputedStyle(probe).color;
    probe.style.color = 'var(' + token + ')';
    let v = getComputedStyle(probe).color;
    if (!v || v === baseline) v = fallback || baseline;
    v = toRgba(v);
    cache.set(token, v);
    return v;
  };
})();

// Resolve a whole map of {name: '--token'} in one go.
window.themePalette = function (spec) {
  const out = {};
  for (const k in spec) out[k] = window.themeColor(spec[k]);
  return out;
};
