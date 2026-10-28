// Bounded 100 ms mono blocks travel only to the local offscreen document.
class ReferencePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.token = null;
    this.buffer = new Float32Array(Math.ceil(sampleRate / 10));
    this.length = 0;
    this.inflight = false;
    this.port.onmessage = ({data}) => {
      if (data.ack) { this.inflight = false; return; }
      if (data.token !== this.token) { this.length = 0; this.buffer.fill(0); }
      this.token = data.token;
    };
  }
  process(inputs) {
    const channels = inputs[0];
    if (!this.token || !channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      if (!this.length) this.started = currentTime + i / sampleRate;
      let mono = 0;
      for (const channel of channels) mono += channel[i] / channels.length;
      this.buffer[this.length++] = mono;
      if (this.length === this.buffer.length) {
        // One block may be in flight while the next is filled. A stalled
        // consumer drops blocks instead of growing an unbounded message queue.
        if (!this.inflight) {
          this.port.postMessage({samples: this.buffer, time: this.started, token: this.token}, [this.buffer.buffer]);
          this.inflight = true;
          this.buffer = new Float32Array(Math.ceil(sampleRate / 10));
        } else this.buffer.fill(0);
        this.length = 0;
      }
    }
    return true;
  }
}
registerProcessor('reference-pcm', ReferencePCM);
