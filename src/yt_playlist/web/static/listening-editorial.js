/* Editorial geometry and qualified exposure; this surface never fetches history. */
(() => {
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const day = value => new Date(value + 'T00:00:00Z').getTime();
    window.listeningEditorial = edition => ({
        observer: null, timers: new Map(), visible: new Set(), sent: new Set(), onVisibility: null,
        init() {
            this.observer = new IntersectionObserver(entries => {
                for (const entry of entries) {
                    const id = entry.target.dataset.storyId;
                    if (entry.isIntersecting && entry.intersectionRatio >= .35) this.visible.add(id);
                    else this.visible.delete(id);
                }
                this.qualify();
            }, {threshold: [0,.35]});
            this.$el.querySelectorAll('[data-story-id]').forEach(el => this.observer.observe(el));
            this.onVisibility = () => this.qualify();
            document.addEventListener('visibilitychange', this.onVisibility);
            this.$watch('panel', () => this.qualify());
        },
        qualify() {
            for (const [id,timer] of this.timers) {
                if (document.hidden || this.panel !== 'portrait' || !this.visible.has(id)) {
                    clearTimeout(timer); this.timers.delete(id);
                }
            }
            if (document.hidden || this.panel !== 'portrait') return;
            for (const id of this.visible) {
                if (this.sent.has(id) || this.timers.has(id)) continue;
                this.timers.set(id, setTimeout(async () => {
                    this.timers.delete(id);
                    if (document.hidden || this.panel !== 'portrait' || !this.visible.has(id)) return;
                    try {
                        const response = await fetch('/home/listening-impressions', {method:'POST',
                            headers:{'Content-Type':'application/json'}, body:JSON.stringify({edition:edition.id,stories:[id]})});
                        if (response.ok) this.sent.add(id);
                    } catch (_) { /* A later visible reading can retry. */ }
                },1500));
            }
        },
        destroy() {
            this.observer?.disconnect(); this.timers.forEach(clearTimeout);
            document.removeEventListener('visibilitychange',this.onVisibility);
        },
        composition(index) {
            const story = edition.stories[index], rows = story.timeline;
            const color = i => `var(--rose-${(i*3+2)%10})`;
            if (story.visual === 'formats') {
                const total = story.bands.reduce((n,b) => n+b.seconds,0);
                return `<div class="le-bands">${story.bands.map((b,i) => `<div class="le-band" style="--le-ink:${color(i)}"><div><span>${esc(b.name)}</span><b>${Math.round(b.seconds/60)} min</b></div><i style="width:${b.seconds/total*100}%"></i><small>${Math.round(b.seconds/total*100)}% of measured time</small></div>`).join('')}</div>`;
            }
            if (story.visual === 'returns') {
                const subjects = rows.slice(0,3), width = 640;
                return `<svg viewBox="0 0 ${width} 340" role="img" aria-label="Distinct listening days by recording"><defs>${subjects.map((r,i) => `<clipPath id="le-clip-${story.id}-${i}"><circle cx="${110+i*210}" cy="${i%2 ? 184 : 144}" r="42"/></clipPath>`).join('')}</defs>${subjects.map((r,i) => {
                    const dates = Object.keys(r.counts), returns = Math.max(0,dates.length-1), x=110+i*210, y=i%2 ? 184 : 144;
                    return `<g style="--le-ink:${color(i)}"><title>${esc(r.name)}: ${returns} later listening days, ${Object.values(r.counts).reduce((a,b)=>a+b,0)} plays</title>${Array.from({length:Math.min(6,returns)},(_,j)=>`<circle class="le-return-ring" cx="${x}" cy="${y}" r="${51+j*8}" style="animation-delay:${(i+j)*70}ms"/>`).join('')}<circle class="le-record" cx="${x}" cy="${y}" r="42"/>${r.thumbnail ? `<image href="${esc(r.thumbnail)}" x="${x-42}" y="${y-42}" width="84" height="84" preserveAspectRatio="xMidYMid slice" clip-path="url(#le-clip-${story.id}-${i})"/>` : `<text class="le-monogram" x="${x}" y="${y+10}" text-anchor="middle">${esc(r.artist.slice(0,1))}</text>`}<text class="le-svg-name" text-anchor="middle" x="${x}" y="${y+113}">${esc(r.name.slice(0,23))}</text><text class="le-svg-count" text-anchor="middle" x="${x}" y="${y+134}">${returns} return days</text></g>`;
                }).join('')}</svg>`;
            }
            if (story.visual === 'decisions') {
                const heart = story.family !== 'decision' || !story.title.includes('line drawn');
                return `<div class="le-decision-mark ${heart ? 'is-heart' : 'is-skull'}"><svg viewBox="0 0 120 120" aria-hidden="true">${heart ? '<path d="M60 99 18 59C-8 27 30 2 60 31 90 2 128 27 102 59Z"/>' : '<path d="M22 55C22 4 98 4 98 55v23L82 87v20H38V87L22 78Zm14-3v18h17V52Zm31 0v18h17V52ZM55 77h10l-5-12Z" fill-rule="evenodd"/>'}</svg><span>${heart ? 'A heart, recorded.' : 'A skull, recorded.'}</span></div>`;
            }
            const start=day(story.since), span=Math.max(86400000,day(story.until)-start), height=Math.max(200,rows.length*49+64);
            const max=Math.max(1,...rows.flatMap(r=>Object.values(r.counts)));
            return `<svg viewBox="0 0 640 ${height}" role="img" aria-label="Listening score: recordings on fixed registers, marks sized by exact plays">${rows.map((r,i)=>{
                const y=42+i*49;
                return `<g style="--le-ink:${color(i)}"><text class="le-svg-name" x="0" y="${y-11}">${esc(r.name.slice(0,35))}</text><path class="le-register" d="M0 ${y}H640"/>${Object.entries(r.counts).map(([d,n])=>`<circle class="le-score-note" cx="${8+(day(d)-start)/span*620}" cy="${y}" r="${2+Math.sqrt(n/max)*10}"><title>${esc(r.name)} · ${d}: ${n} plays</title></circle>`).join('')}</g>`;
            }).join('')}<text class="le-svg-count" x="0" y="${height-5}">${esc(story.since)}</text><text class="le-svg-count" text-anchor="end" x="640" y="${height-5}">${esc(story.until)} · same count scale</text></svg>`;
        }
    });
})();
