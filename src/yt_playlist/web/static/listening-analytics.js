/* The main thread only lays out the already aggregated chart geometry. */
(() => {
    const workerURL = new URL('listening-analytics-worker.js', document.currentScript.src);
    workerURL.search = new URL(document.currentScript.src).search;
    const labels = { mixes: 'Mix tapes', albums: 'Long play', radio: 'On air', liked: 'Liked Music', other: 'Other' };
    const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const tip = description => `tabindex="0" aria-label="${escape(description)}" data-tip="${escape(JSON.stringify({text:description}))}"`;
    window.listeningAnalytics = (mode, initialWindow = '90d') => ({
        mode, window: initialWindow, period: 'auto', artist: [], genre: [], format: [], overlay: 'artist', historyMetric: '',
        start: '', end: '', track: [], measure: 'counts', compare: false, sort: 'plays', descending: true,
        brushStart: null, onPop: null, onVisibility: null,
        detailPage: 0, periodPage: 0,
        timezoneChoice: '', pendingTimezone: '', timezones: ['UTC',...(Intl.supportedValuesOf?.('timeZone') || [])],
        status: 'loading', busy: false, result: null, discovery: [], marks: [],
        histories: [], formatHistories: [], feedbackBars: [], feedbackHistories: [], active: null, axis: [], scale: 1,
        timeScale: 1, totalMarkup: '', feedbackMarkup: '',
        detail: '', error: '', request: 0, worker: null, timer: null, controller: null, alive: true,
        init() {
            this.readURL();
            this.onPop = () => { this.readURL(); if(this.visible()) this.update(false); };
            this.onVisibility = () => this.visible() ? this.load(true) : this.suspend();
            window.addEventListener('popstate',this.onPop);
            document.addEventListener('visibilitychange',this.onVisibility);
            this.$watch('view', value => value === 'analytics' ? this.load() : this.suspend());
            if (this.visible()) this.load();
        },
        destroy() {
            this.alive = false; this.controller?.abort(); this.worker?.terminate(); clearTimeout(this.timer);
            window.removeEventListener('popstate',this.onPop);
            document.removeEventListener('visibilitychange',this.onVisibility);
        },
        visible() { return this.alive && !document.hidden && this.view === 'analytics'; },
        suspend() { clearTimeout(this.timer); this.controller?.abort(); this.worker?.terminate(); this.worker=null; this.busy=false; ++this.request; },
        readURL() {
            const params=new URLSearchParams(location.search);
            for(const key of ['artist','genre','format','track']) this[key]=params.getAll(key).filter(Boolean);
            for(const key of ['start','end','historyMetric']) this[key]=params.get(key) || '';
            this.period=params.get('period') || 'auto'; this.overlay=params.get('overlay') || 'artist';
            this.measure=params.get('measure') || 'counts'; this.compare=params.get('compare') === 'true';
            this.window=params.get('window') || initialWindow;
        },
        filters() { return { window: this.window, period: this.period, artist: [...this.artist], genre: [...this.genre],
            format: [...this.format], overlay: this.overlay, historyMetric: this.historyMetric,
            start:this.start,end:this.end,track:[...this.track],measure:this.measure,compare:this.compare,mode }; },
        queryURL(filters) {
            const params=new URLSearchParams({view:'analytics'});
            for(const [key,value] of Object.entries(filters)) {
                if(key==='mode' || value === '' || value === false) continue;
                for(const item of Array.isArray(value) ? value : [value]) params.append(key,item);
            }
            return '/charts?'+params;
        },
        async load(refresh = false) {
            if (!this.visible() || this.busy || (this.worker && !refresh)) return;
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
                        if (event.data.error) { this.status = 'error'; this.error = event.data.error; return; }
                        this.result = event.data.result; this.status = 'ready'; this.draw();
                        if(this.pendingTimezone && this.result.timezone !== this.pendingTimezone) {
                            this.status='preparing'; clearTimeout(this.timer); this.timer=setTimeout(()=>this.load(true),5000);
                        } else { this.pendingTimezone=''; this.timezoneChoice=this.result.timezone; }
                    };
                    this.worker.onerror = () => { this.worker?.terminate(); this.worker = null; this.busy = false; this.status = 'error'; this.error = 'Could not prepare these charts.'; };
                }
                this.worker.postMessage({ id: ++this.request, encoded, filters: this.filters() });
                this.timer = setTimeout(() => this.load(true), 120000);
            } catch (error) {
                if (error.name !== 'AbortError') { this.status = 'error'; this.error = 'Listening data is unavailable. Try again.'; }
            } finally { this.busy = false; }
        },
        update(push = true) {
            this.detail = '';
            this.detailPage=0; this.periodPage=0;
            if (this.window === 'custom' && (!this.start || !this.end || this.end <= this.start)) {
                this.error='Choose an end date after the start date. The end date itself is not included.'; this.status='error'; return;
            }
            this.status=this.result ? 'ready' : 'loading'; this.error='';
            if (this.worker) { this.busy = true; this.worker.postMessage({ id: ++this.request, filters: this.filters() }); }
            else if(this.visible()) this.load();
            if (push) history.pushState(null,'',this.queryURL(this.filters()));
        },
        clear() { this.artist=[]; this.genre=[]; this.format=[]; this.track=[]; this.update(); },
        reset() { this.artist=[]; this.genre=[]; this.format=[]; this.track=[]; this.window='30d';
            this.period='auto'; this.measure='counts'; this.compare=false; this.overlay='artist'; this.historyMetric=''; this.update(); },
        async changeTimezone() {
            try {
                const response=await fetch('/charts/listening-timezone',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({timezone:this.timezoneChoice})});
                if(!response.ok) throw new Error();
                this.pendingTimezone=this.timezoneChoice; this.status='preparing'; this.load(true);
            } catch(_) { this.error='Could not change the reporting timezone.'; this.status='error'; }
        },
        custom() { this.start=this.result?.start || ''; this.end=this.result?.end || ''; this.window='custom'; },
        sortedDetails() { return [...(this.result?.details || [])].sort((a,b)=>{
            const d=typeof a[this.sort] === 'string' ? a[this.sort].localeCompare(b[this.sort]) : a[this.sort]-b[this.sort];
            return this.descending ? -d : d;
        }); },
        pageDetails() { return this.sortedDetails().slice(this.detailPage*50,(this.detailPage+1)*50); },
        pagePeriods() { return (this.result?.series || []).slice(this.periodPage*100,(this.periodPage+1)*100); },
        order(key) { this.descending=this.sort === key ? !this.descending : !['name','artist'].includes(key); this.sort=key; },
        exportCSV() {
            const r=this.result, q=value => '"'+String(value ?? '').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';
            const lines=[['Prepared',new Date(r.builtAt*1000).toISOString()],['Timezone',r.timezone],['Start inclusive',r.start],
                ['End date (not included)',r.end],['Grouping',r.period],['Filters',JSON.stringify(this.filters())],
                ['Units','Individual plays, daily song history entries, recorded listening seconds and saved ratings'],[],
                ['Recording ID','Song','Artist','Plays','Daily history entries','Listening seconds','Additions to recorded track history','Plays of tracks added to this history in the past 28 days','Hearts given','Skulls given','Ratings removed'],
                ...this.sortedDetails().map(v=>[v.id,v.name,v.artist,v.plays,v.sightings,v.seconds,v.new_tracks,v.recent_discoveries,v.hearts,v.skulls,v.clears])];
            const url=URL.createObjectURL(new Blob([lines.map(line=>line.map(q).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));
            const link=document.createElement('a'); link.href=url; link.download=`listening-${r.start}-${r.end}.csv`; link.click(); URL.revokeObjectURL(url);
        },
        brush(event, finish=false) {
            const bounds=event.currentTarget.getBoundingClientRect(), n=this.result.series.length;
            const index=Math.max(0,Math.min(n-1,Math.floor((event.clientX-bounds.left)/bounds.width*n)));
            if(!finish) { this.brushStart=index; event.currentTarget.setPointerCapture(event.pointerId); return; }
            if(this.brushStart===null || index===this.brushStart) { this.brushStart=null; return; }
            const lo=Math.min(index,this.brushStart), hi=Math.max(index,this.brushStart);
            this.start=this.result.series[lo].day; this.end=this.result.series[hi+1]?.day || this.result.end;
            this.brushStart=null; this.window='custom'; this.update();
        },
        count(value) { return Math.round(value || 0).toLocaleString(); },
        percent(a, b) { return b ? Math.round(a / b * 100) + '%' : '—'; },
        duration(seconds) { return seconds >= 3600 ? (seconds/3600).toFixed(1) + ' h' :
            seconds > 0 && seconds < 60 ? Math.round(seconds) + ' s' : Math.round(seconds/60) + ' min'; },
        label(key) { return labels[key]; },
        dateLabel(day) { return new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }); },
        lastDay(day) { return new Date(new Date(day+'T00:00:00Z').getTime()-86400000).toISOString().slice(0,10); },
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
            return this.queryURL({...this.filters(),window:'custom',start:this.result.start,end:this.result.end,
                [this.overlay === 'genre' ? 'genre' : 'artist']:[name]});
        },
        draw() {
            const original = this.result.series;
            if(this.periodPage*100>=original.length) this.periodPage=0;
            if(this.detailPage*50>=this.result.details.length) this.detailPage=0;
            if(!original.length) { this.histories=[]; this.marks=[]; this.axis=[]; this.discovery=[]; this.totalMarkup=''; return; }
            const metric = this.result.historyMetric;
            const ymax = Math.max(1, ...original.map(v => v.plays));
            this.marks = original.map(bin => ({ ...bin,
                description: `${this.dateLabel(bin.day)} · ${this.count(bin.plays)} plays · ${this.count(bin.sightings)} daily history entries · ${this.count(bin.new_tracks)} tracks and ${this.count(bin.new_artists)} artists added to this recorded history` }));
            // Aligned small multiples use the same time axis and the same scale
            // within each family. No interpolation invents activity between bins.
            const points = values => values.length === 1 ? [values[0],values[0]] : values;
            const spark = (values, maximum, height=38) => {
                const v = points(values), xy = v.map((value,i) => [i/(v.length-1)*600,height-value/maximum*(height-3)]);
                const path = xy.map((p,i)=>(i?' L':'M')+p[0]+','+p[1]).join('');
                return { path, area:path+' L600,'+height+' L0,'+height+' Z' };
            };
            this.scale = this.result.overlays.reduce((maximum,o)=>o.values.reduce((n,v)=>Math.max(n,v),maximum),1);
            this.histories = this.result.overlays.map(o => ({...o, ...spark(o.values,this.scale),
                color:'var(--rose-'+([...o.name].reduce((v,c)=>v+c.codePointAt(0),0)%10)+')',
                last:o.values.at(-1), url:this.artistURL(o.id) }));
            const totalMax = Math.max(1,...original.map(bin=>bin[metric]));
            this.totalMarkup = original.map((bin,i)=>bin[metric] ? `<rect class="lp-total-bar" x="${i*600/original.length}" y="${158-bin[metric]/totalMax*132}" width="${Math.max(.5,600/original.length-.7)}" height="${bin[metric]/totalMax*132}"/>` : '').join('');
            const timeMax = Math.max(1,...original.flatMap(bin=>Object.values(bin.secondsByFormat)));
            this.timeScale = timeMax;
            this.formatHistories = this.result.time.map(v => ({...v,
                ...spark(original.map(bin=>bin.secondsByFormat[v.key]),timeMax,48),
                playPath:spark(original.map(bin=>bin.formats[v.key]),ymax,18).path }));
            const width = 600/original.length;
            this.feedbackBars = this.result.total.rated ? original.flatMap((bin,i)=>{
                if(!bin.plays) return [];
                const factor=132/ymax, bottom=158;
                return [{day:bin.day,x:i*width,width:Math.max(.5,width-.7),
                    likedHeight:bin.liked*factor, knownHeight:(bin.rated-bin.liked)*factor,
                    unknownHeight:(bin.plays-bin.rated)*factor,
                    likedY:bottom-bin.liked*factor, knownY:bottom-bin.rated*factor,
                    unknownY:bottom-bin.plays*factor,
                    description:`${this.dateLabel(bin.day)} · ${this.count(bin.liked)} already liked · ${this.count(bin.rated-bin.liked)} not liked · ${this.count(bin.plays-bin.rated)} unknown`}];
            }) : [];
            this.feedbackMarkup = this.feedbackBars.map(bar=>`<g ${tip(bar.description)}><rect class="lp-feedback-liked" x="${bar.x}" y="${bar.likedY}" width="${bar.width}" height="${bar.likedHeight}"/><rect class="lp-feedback-known" x="${bar.x}" y="${bar.knownY}" width="${bar.width}" height="${bar.knownHeight}"/><rect class="lp-feedback-unknown" x="${bar.x}" y="${bar.unknownY}" width="${bar.width}" height="${bar.unknownHeight}"/></g>`).join('');
            const feedbackMax=Math.max(1,...original.flatMap(bin=>[bin.hearts,bin.skulls]));
            this.feedbackHistories=['hearts','skulls'].map(key=>({key,total:this.result.total[key],
                ...spark(original.map(bin=>bin[key]),feedbackMax,32)}));
            this.axis=[0, .25, .5, .75, 1].map(f=>{
                const i=Math.round(f*(original.length-1)); return {i,x:f*100,label:this.shortDate(original[i].day)};
            }).filter((item,i,all)=>all.findIndex(v=>v.i===item.i)===i);
            this.discovery = [{key:'tracks',name:'Tracks new to this history',field:'new_tracks',denominator:'plays'},
                {key:'artists',name:'Artists new to this history',field:'new_artists',denominator:'artist_plays'},
                {key:'recent',name:'Tracks added to this history in the past 28 days',field:'recent_discoveries',denominator:'plays'}]
                .filter(v=>this.result.modules.discovery && this.result.discovery.total[v.denominator]>0).map(v=>{
                    let path='',connected=false;
                    const dots=this.result.discovery.series.map((bin,i)=>{
                        const known=bin[v.denominator]>0, ratio=known ? bin[v.field]/bin[v.denominator] : 0;
                        const px=original.length===1 ? 300 : i/(original.length-1)*600, py=81-ratio*68;
                        if(known) { path+=(connected?' L':'M')+px+','+py; connected=true; } else connected=false;
                        return {x:px,y:py,known,r:1.5+Math.sqrt(bin[v.denominator]/ymax)*2.5,
                            description:`${this.dateLabel(bin.day)} · ${bin[v.field]} / ${bin[v.denominator]} · ${this.percent(bin[v.field],bin[v.denominator])}`};
                    });
                    const dotsMarkup = dots.filter(dot=>dot.known).map(dot=>`<circle class="lp-discovery-dot" cx="${dot.x}" cy="${dot.y}" r="${dot.r}" ${tip(dot.description)}/>`).join('');
                    return {...v,path,dotsMarkup,percent:this.percent(this.result.discovery.total[v.field],this.result.discovery.total[v.denominator])};
                });
            this.active=null;
        }
    });
})();
