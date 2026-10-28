/* Editorial geometry and qualified exposure; this surface never fetches history. */
(() => {
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const day = value => new Date(value + 'T00:00:00Z').getTime();
    const dateLabel = value => new Date(day(value)).toLocaleDateString(undefined, {month:'short',day:'numeric',timeZone:'UTC'});
    const color = i => `var(--rose-${[2,5,8,3,6,9,0][i%7]})`;
    const tip = text => `tabindex="0" aria-label="${esc(text)}" data-tip="${esc(JSON.stringify({text}))}"`;
    const playURL = row => row.video_id ? 'https://music.youtube.com/watch?v='+encodeURIComponent(row.video_id) : '';
    const miniPlayer = (row,x,y,context='') => {
        const url=playURL(row);
        if(!url) return '';
        const label=`Play ${row.name} by ${row.artist}${context ? ` (${context})` : ''} on YouTube Music`;
        const fallback='https://i.ytimg.com/vi/'+encodeURIComponent(row.video_id)+'/hqdefault.jpg';
        return `<a class="le-song-play" href="${esc(url)}" target="_blank" rel="noopener" aria-label="${esc(label)}"><title>${esc(label)}</title><rect class="le-song-sleeve" x="${x}" y="${y}" width="56" height="56" rx="8"/><foreignObject x="${x}" y="${y}" width="56" height="56"><img xmlns="http://www.w3.org/1999/xhtml" src="${esc(row.thumbnail || fallback)}" data-fallback="${esc(fallback)}" alt="" referrerpolicy="no-referrer" onerror="if(this.src !== this.dataset.fallback) this.src=this.dataset.fallback; else this.remove()"></foreignObject><rect class="le-play-shade" x="${x}" y="${y}" width="56" height="56" rx="8"/><path class="le-play-icon" d="M${x+22} ${y+15}v26l20-13Z"/></a>`;
    };
    // Midpoint controls preserve each daily value without overshooting a band.
    const curve = points => points.map(([x,y],i) => {
        if(!i) return `M${x},${y}`;
        const [px,py]=points[i-1], middle=(px+x)/2;
        return `C${middle},${py} ${middle},${y} ${x},${y}`;
    }).join('');
    const polar = (radius,angle) => [360+radius*Math.cos(angle*Math.PI/180),360+radius*Math.sin(angle*Math.PI/180)];
    const arc = (radius,start,end) => {
        const a=polar(radius,start),b=polar(radius,end);
        return `M${a}A${radius},${radius} 0 ${end-start>180 ? 1 : 0} 1 ${b}`;
    };
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
            this.$watch('view', () => this.qualify());
        },
        qualify() {
            for (const [id,timer] of this.timers) {
                if (document.hidden || this.view !== 'home' || !this.visible.has(id)) {
                    clearTimeout(timer); this.timers.delete(id);
                }
            }
            if (document.hidden || this.view !== 'home') return;
            for (const id of this.visible) {
                if (this.sent.has(id) || this.timers.has(id)) continue;
                this.timers.set(id, setTimeout(async () => {
                    this.timers.delete(id);
                    if (document.hidden || this.view !== 'home' || !this.visible.has(id)) return;
                    try {
                        const response = await fetch('/charts/listening-impressions', {method:'POST',
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
            const start=day(story.since), length=Math.max(1,Math.round((day(story.until)-start)/86400000));
            const days=Array.from({length},(_,i)=>new Date(start+i*86400000).toISOString().slice(0,10));
            if (story.visual === 'discovery') {
                const observed=new Map(story.discovery_days.map(row=>[row.day,row]));
                const series=[['new_tracks','plays','Tracks new to this history'],['new_artists','artist_plays','Artists new to this history']];
                const x=i=>60+i/Math.max(1,length-1)*600;
                return `<svg viewBox="0 0 720 400" role="group" aria-label="Daily rates of additions to recorded listening history. Tracks use all plays; artists use plays with a known artist. Gaps mean no qualifying plays.">${[0,25,50,75,100].map(n=>`<path class="le-orbit-guide" d="M60 ${310-n*2.5}H660"/><text class="le-svg-count" x="48" y="${315-n*2.5}" text-anchor="end">${n}%</text>`).join('')}${series.map(([field,denominator,label],s)=>{
                    let path='', connected=false;
                    const dots=days.map((d,i)=>{
                        const row=observed.get(d);
                        if(!row?.[denominator]) {connected=false; return '';}
                        const rate=100*row[field]/row[denominator], y=310-rate*2.5;
                        path+=`${connected ? 'L' : 'M'}${x(i)},${y}`; connected=true;
                        return `<circle class="le-score-note" cx="${x(i)}" cy="${y}" r="5" ${tip(`${dateLabel(d)}: ${label}, ${row[field]} of ${row[denominator]} plays (${Math.round(rate)}%)`)}/>`;
                    }).join('');
                    return `<g style="--le-ink:${color(s)}"><path class="le-thread-edge" d="${path}"/>${dots}</g>`;
                }).join('')}<text class="le-svg-date" x="60" y="355">${esc(dateLabel(days[0]))}</text><text class="le-svg-date" x="660" y="355" text-anchor="end">${esc(dateLabel(days.at(-1)))}</text></svg><div class="le-format-key">${series.map(([, ,label],i)=>`<span style="--le-ink:${color(i)}"><i></i>${label}</span>`).join('')}</div>`;
            }
            if (story.visual === 'rating_genres') {
                const all=story.rating_genres || [];
                const known=all.filter(g=>g.name && !g.combined).sort((a,b)=>b.hearts-a.hearts || a.name.localeCompare(b.name));
                const genres=known.slice(0,6);
                const contrast=known.filter(g=>g.hearts+g.skulls>=8 && g.skulls>g.hearts)
                    .sort((a,b)=>b.skulls/(b.hearts+b.skulls)-a.skulls/(a.hearts+a.skulls))[0];
                if(contrast && !genres.includes(contrast)) genres.push(contrast);
                const remainder=all.filter(g=>!genres.includes(g));
                if(remainder.length) genres.push({name:'Other genres / untagged',
                    hearts:remainder.reduce((n,g)=>n+g.hearts,0),skulls:remainder.reduce((n,g)=>n+g.skulls,0)});
                const counts=story.rating_counts;
                if(!genres.length) genres.push({name:'All retained ratings',...counts});
                return `<svg viewBox="0 0 720 ${110+genres.length*72}" role="group" aria-label="Current heart and skull proportions within each genre, with exact counts. These ratings have no known action dates."><text class="le-svg-count" x="260" y="30">Share of ratings</text><text class="le-svg-count" x="700" y="30" text-anchor="end">Counts</text>${genres.map((g,i)=>{
                    const y=70+i*72;
                    const heartWidth=300*g.hearts/Math.max(1,g.hearts+g.skulls);
                    return `<g ${tip(`${g.name}: ${g.hearts} retained hearts, ${g.skulls} retained skulls (${Math.round(heartWidth/3)}% hearts)`)}><text class="le-svg-name" x="36" y="${y+10}">${esc(g.name.length>14 ? g.name.slice(0,13)+'…' : g.name)}</text><rect class="le-rating-heart" x="260" y="${y-8}" width="${heartWidth}" height="24"/><rect class="le-rating-skull" x="${260+heartWidth}" y="${y-8}" width="${300-heartWidth}" height="24"/><text class="le-svg-count" x="700" y="${y+9}" text-anchor="end">${g.hearts} / ${g.skulls}</text></g>`;
                }).join('')}<text class="le-svg-date" x="36" y="${90+genres.length*72}">Current ratings · no action dates</text></svg><div class="le-format-key"><span style="--le-ink:${color(0)}"><i></i>Hearts <b>${counts.hearts}</b></span><span style="--le-ink:${color(1)}"><i></i>Skulls <b>${counts.skulls}</b></span></div>`;
            }
            if (story.visual === 'rating_share') {
                const counts=story.rating_counts, total=counts.liked+counts.not_liked+counts.unknown;
                const bands=[['liked','Already hearted'],['not_liked','Known, not hearted'],['unknown','Unknown at start']];
                let left=60;
                return `<svg viewBox="0 0 720 430" role="group" aria-label="${story.stat}% already hearted among plays with known starting ratings. ${counts.unknown} of ${total} plays have unknown starting ratings."><text class="le-orbit-number" x="360" y="150" text-anchor="middle">${story.stat}%</text><text class="le-orbit-label" x="360" y="190" text-anchor="middle">of known starting ratings</text>${bands.map(([key,label],i)=>{
                    const x=left,w=600*counts[key]/total;left+=w;
                    return `<rect x="${x}" y="240" width="${w}" height="62" fill="${i===2 ? 'var(--border-2)' : color(i)}" ${tip(`${label}: ${counts[key]} of ${total} recorded plays`)}/>`;
                }).join('')}</svg><div class="le-format-key">${bands.map(([key,label],i)=>`<span style="--le-ink:${i===2 ? 'var(--border-2)' : color(i)}"><i></i>${label} <b>${counts[key]}</b></span>`).join('')}</div>`;
            }
            if (story.visual === 'rating_actions') {
                const kinds=[['hearts','Hearts'],['skulls','Skulls'],['clears','Clears']];
                const observed=new Map(story.rating_days.map(d=>[d.day,d]));
                const maximum=Math.max(1,...story.rating_days.flatMap(d=>kinds.map(([k])=>d[k])));
                const slot=600/days.length;
                return `<svg viewBox="0 0 720 360" role="group" aria-label="Confirmed hearts, skulls and clears by date. Reversals count as separate actions.">${kinds.map(([kind,label],i)=>days.map((d,j)=>{
                    const n=observed.get(d)?.[kind] || 0;
                    return n ? `<rect x="${60+j*slot+i*slot/4}" y="${280-n/maximum*220}" width="${slot/4-1}" height="${n/maximum*220}" fill="${color(i)}" ${tip(`${dateLabel(d)}: ${n} ${label.toLowerCase()}`)}/>` : '';
                }).join('')).join('')}<path class="le-orbit-guide" d="M60 280H660"/><text class="le-svg-date" x="60" y="320">${esc(dateLabel(days[0]))}</text><text class="le-svg-date" x="660" y="320" text-anchor="end">${esc(dateLabel(days.at(-1)))}</text></svg><div class="le-format-key">${kinds.map(([key,label],i)=>`<span style="--le-ink:${color(i)}"><i></i>${label} <b>${story.rating_counts[key]}</b></span>`).join('')}</div>`;
            }
            if (story.visual === 'formats') {
                const total = story.bands.reduce((n,b) => n+b.seconds,0);
                const observed=new Map((story.band_days || []).map(d=>[d.day,d]));
                const daily=days.map(d=>story.bands.map(b=>observed.get(d)?.[b.key] || 0));
                const maximum=Math.max(1,...daily.map(values=>values.reduce((a,b)=>a+b,0)));
                const x=i=>60+i/Math.max(1,length-1)*600;
                const edges=story.bands.map((band,b)=>daily.map((values,i)=>{
                    const top=240-values.reduce((a,n)=>a+n,0)/maximum*190;
                    return {x:x(i),top:top+values.slice(0,b).reduce((a,n)=>a+n,0)/maximum*380,
                        height:values[b]/maximum*380};
                }));
                return `<svg class="le-engraving" viewBox="0 0 720 510" role="img" aria-label="Listening time woven into bands, one band per format">${[0,.25,.5,.75,1].map(f=>`<path class="le-weave-line" d="M${60+f*600} 30V450"/>`).join('')}${edges.map((values,i)=>{
                    const top=values.map(p=>[p.x,p.top]), bottom=values.map(p=>[p.x,p.top+p.height]);
                    const outline=curve(top)+curve([...bottom].reverse()).replace(/^M/,'L')+'Z';
                    const band=story.bands[i];
                    return `<g class="le-weave-layer" style="--le-ink:${color(i)}" ${tip(`${band.name}: ${Math.round(band.seconds/60)} minutes, ${Math.round(band.seconds/total*100)}% of recorded time`)}><path class="le-weave" d="${outline}"/>${Array.from({length:13},(_,j)=>`<path class="le-weave-thread" d="${curve(values.map(p=>[p.x,p.top+p.height*j/12]))}"/>`).join('')}<path class="le-weave-edge" d="${curve(top)}"/></g>`;
                }).join('')}<text class="le-svg-date" x="60" y="480">${esc(dateLabel(days[0]))}</text><text class="le-svg-date" x="660" y="480" text-anchor="end">${esc(dateLabel(days.at(-1)))}</text></svg><div class="le-format-key">${story.bands.map((b,i)=>`<span style="--le-ink:${color(i)}"><i></i>${esc(b.name)} <b>${Math.round(b.seconds/total*100)}%</b></span>`).join('')}</div>`;
            }
            if (story.visual === 'returns') {
                const subjects=rows.slice(0,5), single=subjects.length===1;
                const maximum=Math.max(1,...subjects.flatMap(r=>Object.values(r.counts)));
                const active=new Set(subjects.flatMap(r=>Object.keys(r.counts))).size;
                const value=story.stat ?? active, label=story.stat_label || 'days you listened';
                const slot=330/length;
                const players=single ? days.map((d,j)=>{
                    const song=subjects[0].songs?.[d];
                    if(!song || !subjects[0].counts[d]) return '';
                    const [x,y]=polar(312,-255+(j+.5)*slot);
                    return miniPlayer(song,x-28,y-28,dateLabel(d));
                }).join('') : '';
                return `<svg viewBox="0 0 720 720" role="group" aria-label="${esc(`${value} ${label}. Arcs show the days you listened during this period.`)}">${Array.from({length:12},(_,j)=>`<path class="le-orbit-guide" d="${arc(155+j*9,-255,75)}"/>`).join('')}${days.map((d,j)=>{
                    const angle=-255+(j+.5)*slot, a=polar(271,angle),b=polar(278,angle);
                    return `<path class="le-orbit-tick" d="M${a}L${b}"/>`;
                }).join('')}${subjects.map((r,i)=>{
                    const radius=single ? 210 : 170+i*20;
                    return Object.entries(r.counts).map(([d,n],j)=>{
                        const position=(day(d)-start)/86400000;
                        if(position<0 || position>=length) return '';
                        const a=-255+(position+.12)*slot,b=-255+(position+.88)*slot;
                        const thickness=single ? 12+60*n/maximum : 4+12*n/maximum;
                        return `<g style="--le-ink:${color(single ? j : i)}" ${tip(`${r.name}, ${dateLabel(d)}: ${n} ${n===1 ? 'play' : 'plays'}`)}><path class="le-orbit-arc" stroke-width="${thickness}" d="${arc(radius,a,b)}"/>${single ? Array.from({length:9},(_,k)=>`<path class="le-orbit-rib" d="${arc(radius-thickness/2+(k+1)*thickness/10,a,b)}"/>`).join('') : ''}${[1,2,3].map(k=>`<path class="le-orbit-echo" d="${arc(radius+thickness/2+k*5,a,b)}"/>`).join('')}</g>`;
                    }).join('');
                }).join('')}${players}<text class="le-orbit-number" x="360" y="370" text-anchor="middle">${esc(value)}</text><text class="le-orbit-label" x="360" y="410" text-anchor="middle">${esc(label)}</text><text class="le-svg-date" x="286" y="642" text-anchor="middle">${esc(dateLabel(days[0]))}</text><text class="le-svg-date" x="434" y="642" text-anchor="middle">${esc(dateLabel(days.at(-1)))}</text></svg>${single ? '' : `<div class="le-subject-key">${subjects.map((r,i)=>`<span style="--le-ink:${color(i)}"><i></i>${playURL(r) ? `<a class="le-mini-play" href="${esc(playURL(r))}" target="_blank" rel="noopener" aria-label="${esc(`Play ${r.name} on YouTube Music`)}">${r.thumbnail ? `<img src="${esc(r.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ''}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4v16l12-8Z"/></svg></a>` : ''}${esc(r.name)}</span>`).join('')}</div>`}`;
            }
            if (story.visual === 'decisions') {
                const heart=story.decision ? story.decision==='heart' : !story.title.includes('line drawn');
                const shape=heart ? 'M360 433C319 396 156 296 156 194C156 89 300 66 360 168C420 66 564 89 564 194C564 296 401 396 360 433Z' : 'M205 252C205 42 515 42 515 252V328L454 365V443H266V365L205 328ZM268 226V286H326V226ZM394 226V286H452V226ZM340 329H380L360 296Z';
                const marks=Object.entries(rows[0]?.counts || {});
                return `<svg viewBox="0 0 720 570" style="--le-ink:${heart ? 'var(--danger)' : 'var(--accent-2)'}" role="group" aria-label="${heart ? 'A heart' : 'A skull'}, with earlier plays marked along the bottom"><path class="le-choice-halo" d="${shape}" fill-rule="evenodd"/><g class="le-engraving">${Array.from({length:22},(_,i)=>`<path class="le-choice-shape" d="${shape}" fill-rule="evenodd" transform="translate(360 260) scale(${.24+i*.036}) translate(-360 -260)"/>`).join('')}</g>${rows[0] ? miniPlayer(rows[0],332,225) : ''}<path class="le-orbit-guide" d="M60 495H660"/>${marks.map(([d,n])=>`<circle class="le-score-note" cx="${60+((day(d)-start)/86400000+.5)/length*600}" cy="495" r="${4+Math.sqrt(n)*3}" ${tip(`${dateLabel(d)}: ${n} plays`)}/>`).join('')}<text class="le-svg-date" x="60" y="540">${esc(dateLabel(days[0]))}</text><text class="le-svg-date" x="660" y="540" text-anchor="end">${esc(dateLabel(days.at(-1)))}</text></svg>`;
            }
            const height=rows.length*110+30, maximum=Math.max(1,...rows.flatMap(r=>Object.values(r.counts)));
            return `<svg class="le-engraving" viewBox="0 0 720 ${height}" role="group" aria-label="Songs drawn as lines over time, with taller rises for more plays">${rows.map((r,i)=>{
                const baseline=90+i*110, x=j=>134+(j+.5)/length*526;
                const values=days.map(d=>r.counts[d] || 0);
                const points=values.map((n,j)=>[x(j),baseline-n/maximum*36]);
                const edge=curve(points), area=edge+`L${x(length-1)},${baseline}L${x(0)},${baseline}Z`;
                return `<g class="le-song-thread" style="--le-ink:${color(i)}">${miniPlayer(r,60,baseline-56)}<text class="le-svg-name" x="134" y="${baseline-50}">${esc(r.name.length>38 ? r.name.slice(0,37)+'…' : r.name)}</text><path class="le-thread-fill" d="${area}"/>${Array.from({length:9},(_,j)=>`<path class="le-thread" d="${curve(points.map(([px,py])=>[px,baseline+(py-baseline)*(j+1)/10]))}"/>`).join('')}<path class="le-thread-edge" d="${edge}"/>${values.map((n,j)=>n ? `<circle class="le-score-note" cx="${x(j)}" cy="${baseline-n/maximum*36}" r="4" ${tip(`${r.name}, ${dateLabel(days[j])}: ${n} ${n===1 ? 'play' : 'plays'}`)}/>` : '').join('')}</g>`;
            }).join('')}<text class="le-svg-date" x="134" y="${height-5}">${esc(dateLabel(days[0]))}</text><text class="le-svg-date" text-anchor="end" x="660" y="${height-5}">${esc(dateLabel(days.at(-1)))}</text></svg>`;
        }
    });
})();
