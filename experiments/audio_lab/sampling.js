// No Chrome or DOM dependency: the same policy runs in the lab and its checks.
export const POLICY = 'reference-windows-1';
export const MAX_RATE = 192000;
export const MAX_SECONDS = 10;
export const MAX_AGE = .75;

export function windows(duration) {
  if (!Number.isFinite(duration) || duration < 3) return [];
  const result = [];
  for (const start of [Math.min(2, duration * .1), duration / 2, duration - 12]) {
    const end = Math.min(start + MAX_SECONDS, duration - 1);
    if (start < 0 || end - start < 1 || result.some(w => start < w.end + 2)) continue;
    result.push({start, end});
  }
  return result;
}

export function coverage(intervals, duration) {
  if (!Number.isFinite(duration) || duration <= 0) return {seconds: 0, fraction: null};
  const valid = intervals.filter(w => Number.isFinite(w.start) && Number.isFinite(w.end)
    && w.start >= 0 && w.end > w.start && w.end <= duration).sort((a, b) => a.start - b.start);
  let seconds = 0, end = 0;
  for (const window of valid) {
    seconds += Math.max(0, window.end - Math.max(end, window.start));
    end = Math.max(end, window.end);
  }
  return {seconds, fraction: seconds / duration};
}

export class Sampler {
  constructor(rate, accept) {
    if (!Number.isFinite(rate) || rate < 8000 || rate > MAX_RATE) throw Error('Unsupported sample rate');
    this.rate = rate;
    this.accept = accept;
    this.state = null;
    this.buffer = null;
    this.used = new Set();
    this.discarded = 0;
  }
  discard() {
    if (this.buffer) this.discarded++;
    this.buffer = null;
  }
  update(state, contextTime, now = Date.now()) {
    const valid = state && typeof state.source === 'string' && /^sha256:[a-f0-9]{64}$/.test(state.source)
      && Number.isInteger(state.epoch) && Number.isFinite(state.position) && state.position >= 0
      && Number.isFinite(state.duration) && state.duration >= 3 && state.position <= state.duration
      && state.rate === 1 && !state.paused && state.permitted === true
      && Number.isFinite(state.at) && now - state.at >= -100 && now - state.at <= MAX_AGE * 1000;
    if (!valid) {
      this.discard();
      this.state = null;
      return;
    }
    const anchor = contextTime - Math.max(0, now - state.at) / 1000;
    if (this.state && this.state.source !== state.source) this.used.clear();
    if (this.state && (this.state.source !== state.source || this.state.epoch !== state.epoch
        || Math.abs(this.state.position + anchor - this.anchor - state.position) > .3)) this.discard();
    this.state = {...state};
    this.anchor = anchor;
    this.plan = windows(state.duration);
  }
  push(samples, audioTime, token, now = Date.now()) {
    const s = this.state;
    if (!s || token !== `${s.source}/${s.epoch}` || now - s.at > MAX_AGE * 1000) {
      this.discard();
      return;
    }
    const start = s.position + audioTime - this.anchor;
    const end = start + samples.length / this.rate;
    if (!Number.isFinite(start) || !samples.length || samples.length > this.rate / 2) return;
    for (let index = 0; index < this.plan.length; index++) {
      const w = this.plan[index];
      if (this.used.has(index) || end <= w.start || start >= w.end) continue;
      if (!this.buffer) {
        // Do not fill a missed beginning with unrelated later samples.
        if (start > w.start + .025) { this.used.add(index); continue; }
        this.buffer = {index, samples: new Float32Array(Math.ceil((w.end - w.start) * this.rate)),
          length: 0, next: null, start: w.start, end: w.end, token};
      }
      const b = this.buffer;
      if (b.index !== index || (b.next !== null && Math.abs(start - b.next) > .025)) {
        this.discard();
        this.used.add(index);
        return;
      }
      const first = Math.max(0, Math.round((w.start - start) * this.rate));
      const last = Math.min(samples.length, Math.round((w.end - start) * this.rate));
      const count = Math.min(last - first, b.samples.length - b.length);
      if (count > 0) { b.samples.set(samples.subarray(first, first + count), b.length); b.length += count; }
      b.next = end;
      if (end >= w.end) {
        this.used.add(index);
        this.buffer = null;
        // Clock alignment can differ by one render quantum. Never publish a hole.
        if (b.length >= b.samples.length - this.rate * .025) {
          this.accept({samples: b.samples.subarray(0, b.length), start: w.start,
            end: w.start + b.length / this.rate, token, source: s.source, epoch: s.epoch,
            duration: s.duration, index, partial: w.end - w.start < MAX_SECONDS});
        } else this.discarded++;
      }
      return;
    }
  }
}
