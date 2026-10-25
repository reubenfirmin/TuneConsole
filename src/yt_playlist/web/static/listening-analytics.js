/* The main thread only lays out the already aggregated chart geometry. */
(() => {
    const workerURL = new URL('listening-analytics-worker.js', document.currentScript.src);
    workerURL.search = new URL(document.currentScript.src).search;
    const labels = { mixes: 'Mix tapes', albums: 'Long play', radio: 'On air', liked: 'Liked Music', other: 'Other / unknown' };
    const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const tip = description => `tabindex="0" aria-label="${escape(description)}" data-tip="${escape(JSON.stringify({text:description}))}"`;
    window.listeningAnalytics = (mode, initialWindow = '90d') => ({
        mode, window: initialWindow, period: 'day', artist: '', genre: '', format: '', overlay: 'artist', historyMetric: '',
        status: 'loading', busy: false, result: null, discovery: [], marks: [],
        histories: [], formatHistories: [], feedbackBars: [], feedbackHistories: [], active: null, axis: [], scale: 1,
        timeScale: 1, totalMarkup: '', feedbackMarkup: '',
        detail: '', error: '', request: 0, worker: null, timer: null, controller: null, alive: true,
        init() {
            const params = new URLSearchParams(location.search);
            if (mode === 'charts') for (const key of ['artist', 'genre', 'format', 'period', 'overlay', 'historyMetric'])
                if (params.has(key)) this[key] = params.get(key);
            this.$watch(mode === 'home' ? 'panel' : 'view', value => {
                if (value === (mode === 'home' ? 'portrait' : 'analytics')) this.load();
            });
            if ((mode === 'home' ? this.panel === 'portrait' : this.view === 'analytics')) this.load();
        },
        destroy() {
            this.alive = false; this.controller?.abort(); this.worker?.terminate(); clearTimeout(this.timer);
        },
        filters() { return { window: this.window, period: this.period, artist: this.artist, genre: this.genre,
            format: this.format, overlay: this.overlay, historyMetric: this.historyMetric, mode }; },
        async load(refresh = false) {
            if (!this.alive || this.busy || (this.worker && !refresh)) return;
            clearTimeout(this.timer); this.busy = true; this.error = '';
            this.controller = new AbortController();
            try {
                const response = await fetch('/charts/listening-data', { signal: this.controller.signal });
                if (response.status === 202) {
                    this.status = 'preparing'; this.timer = setTimeout(() => this.load(true), 5000); return;
                }
                if (!response.ok) throw new Error();
                const encoded = await response.text();
                if (!this.alive) return;
                if (!this.worker) {
                    this.worker = new Worker(workerURL);
                    this.worker.onmessage = event => {
                        if (!this.alive || event.data.id !== this.request) return;
                        this.busy = false;
                        if (event.data.error) { this.status = 'error'; this.error = 'Could not read listening data.'; return; }
                        this.result = event.data.result; this.status = 'ready'; this.draw();
                    };
                    this.worker.onerror = () => { this.worker?.terminate(); this.worker = null; this.busy = false; this.status = 'error'; this.error = 'Could not prepare these charts.'; };
                }
                this.worker.postMessage({ id: ++this.request, encoded, filters: this.filters() });
                this.timer = setTimeout(() => this.load(true), 120000);
            } catch (error) {
                if (error.name !== 'AbortError') { this.status = 'error'; this.error = 'Listening data is unavailable. Try again.'; }
            } finally { this.busy = false; }
        },
        update() {
            this.detail = '';
            if (this.worker) { this.busy = true; this.worker.postMessage({ id: ++this.request, filters: this.filters() }); }
            if (mode === 'charts') {
                const params = new URLSearchParams({ view: 'analytics', ...this.filters() });
                params.delete('mode');
                for (const [key, value] of [...params]) if (!value) params.delete(key);
                history.replaceState(null, '', '/charts?' + params);
            }
        },
        clear() { this.artist = ''; this.genre = ''; this.format = ''; this.update(); },
        count(value) { return Math.round(value || 0).toLocaleString(); },
        percent(a, b) { return b ? Math.round(a / b * 100) + '%' : '—'; },
        duration(seconds) { return seconds >= 3600 ? (seconds/3600).toFixed(1) + ' h' :
            seconds > 0 && seconds < 60 ? Math.round(seconds) + ' s' : Math.round(seconds/60) + ' min'; },
        label(key) { return labels[key]; },
        dateLabel(day) { return new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }); },
        shortDate(day) { const long = this.result.series.at(-1).day.slice(0,4) !== this.result.series[0].day.slice(0,4);
            return new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', ...(long ? {year:'2-digit'} : {day:'numeric'}), timeZone: 'UTC' }); },
        selectPeriod(index) { this.active = index; this.detail = this.marks[index]?.description || ''; },
        inspect(event) {
            const bounds = event.currentTarget.getBoundingClientRect();
            this.selectPeriod(Math.max(0, Math.min(this.marks.length-1,
                Math.round((event.clientX-bounds.left)/bounds.width*(this.marks.length-1)))));
        },
        inspectKey(event) {
            const amount = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0;
            if (amount) { event.preventDefault(); this.selectPeriod(Math.max(0,Math.min(this.marks.length-1,(this.active ?? 0)+amount))); }
        },
        artistURL(name) {
            return '/charts?' + new URLSearchParams({ view:'analytics', window:this.window, period:this.period,
                [this.overlay === 'genre' ? 'genre' : 'artist']:name });
        },
        draw() {
            const original = this.result.series;
            const metric = this.result.historyMetric;
            const ymax = Math.max(1, ...original.map(v => v.plays));
            this.marks = original.map(bin => ({ ...bin,
                description: `${this.dateLabel(bin.day)} · ${this.count(bin.plays)} plays · ${this.count(bin.sightings)} dated sightings · ${this.percent(bin.new_tracks, bin.plays)} new songs · ${this.percent(bin.new_artists, bin.artist_plays)} new artists` }));
            // Aligned small multiples use the same time axis and the same scale
            // within each family. No interpolation invents activity between bins.
            const points = values => values.length === 1 ? [values[0],values[0]] : values;
            const spark = (values, maximum, height=38) => {
                const v = points(values), xy = v.map((value,i) => [i/(v.length-1)*600,height-value/maximum*(height-3)]);
                const path = xy.map((p,i)=>(i?' L':'M')+p[0]+','+p[1]).join('');
                return { path, area:path+' L600,'+height+' L0,'+height+' Z' };
            };
            this.scale = Math.max(1,...this.result.overlays.flatMap(o=>o.values));
            this.histories = this.result.overlays.map(o => ({...o, ...spark(o.values,this.scale),
                color:'var(--rose-'+([...o.name].reduce((v,c)=>v+c.codePointAt(0),0)%10)+')',
                last:o.values.at(-1), url:this.artistURL(o.name) }));
            const totalMax = Math.max(1,...original.map(bin=>bin[metric]));
            this.totalMarkup = original.map((bin,i)=>`<rect class="lp-total-bar" x="${i*600/original.length}" y="${158-bin[metric]/totalMax*132}" width="${Math.max(.5,600/original.length-.7)}" height="${bin[metric]/totalMax*132}"/>`).join('');
            const timeMax = Math.max(1,...original.flatMap(bin=>Object.values(bin.secondsByFormat)));
            this.timeScale = timeMax;
            this.formatHistories = this.result.time.map(v => ({...v,
                ...spark(original.map(bin=>bin.secondsByFormat[v.key]),timeMax,48),
                playPath:spark(original.map(bin=>bin.formats[v.key]),ymax,18).path }));
            const width = 600/original.length;
            this.feedbackBars = original.map((bin,i)=>{
                const factor=132/ymax, bottom=158;
                return {day:bin.day,x:i*width,width:Math.max(.5,width-.7),
                    likedHeight:bin.liked*factor, knownHeight:(bin.rated-bin.liked)*factor,
                    unknownHeight:(bin.plays-bin.rated)*factor,
                    likedY:bottom-bin.liked*factor, knownY:bottom-bin.rated*factor,
                    unknownY:bottom-bin.plays*factor,
                    description:`${this.dateLabel(bin.day)} · ${this.count(bin.liked)} already liked · ${this.count(bin.rated-bin.liked)} not liked · ${this.count(bin.plays-bin.rated)} unknown`};
            });
            this.feedbackMarkup = this.feedbackBars.map(bar=>`<g ${tip(bar.description)}><rect class="lp-feedback-liked" x="${bar.x}" y="${bar.likedY}" width="${bar.width}" height="${bar.likedHeight}"/><rect class="lp-feedback-known" x="${bar.x}" y="${bar.knownY}" width="${bar.width}" height="${bar.knownHeight}"/><rect class="lp-feedback-unknown" x="${bar.x}" y="${bar.unknownY}" width="${bar.width}" height="${bar.unknownHeight}"/></g>`).join('');
            const feedbackMax=Math.max(1,...original.flatMap(bin=>[bin.hearts,bin.skulls]));
            this.feedbackHistories=['hearts','skulls'].map(key=>({key,total:this.result.retained[key],
                ...spark(original.map(bin=>bin[key]),feedbackMax,32)}));
            this.axis=[0, .25, .5, .75, 1].map(f=>{
                const i=Math.round(f*(original.length-1)); return {i,x:f*100,label:this.shortDate(original[i].day)};
            }).filter((item,i,all)=>all.findIndex(v=>v.i===item.i)===i);
            this.discovery = [{key:'tracks',name:'New songs',field:'new_tracks',denominator:'plays'},
                {key:'artists',name:'New artists',field:'new_artists',denominator:'artist_plays'}].map(v=>{
                    let path='',connected=false;
                    const dots=original.map((bin,i)=>{
                        const known=bin[v.denominator]>0, ratio=known ? bin[v.field]/bin[v.denominator] : 0;
                        const px=original.length===1 ? 300 : i/(original.length-1)*600, py=81-ratio*68;
                        if(known) { path+=(connected?' L':'M')+px+','+py; connected=true; } else connected=false;
                        return {x:px,y:py,known,r:1.5+Math.sqrt(bin[v.denominator]/ymax)*2.5,
                            description:`${this.dateLabel(bin.day)} · ${bin[v.field]} / ${bin[v.denominator]} · ${this.percent(bin[v.field],bin[v.denominator])}`};
                    });
                    const dotsMarkup = dots.filter(dot=>dot.known).map(dot=>`<circle class="lp-discovery-dot" cx="${dot.x}" cy="${dot.y}" r="${dot.r}" ${tip(dot.description)}/>`).join('');
                    return {...v,path,dotsMarkup,percent:this.percent(this.result.total[v.field],this.result.total[v.denominator])};
                });
            this.active=null;
        }
    });
})();
