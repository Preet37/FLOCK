/* ==========================================================================
   Flock agent theater: the run as a show, visual first. Feeds on the API's theater stream (flock/theater.py):
     GET /api/stream/replay?all=1&speed=N   the last run, paced (Replay)
     GET /api/stream/sse?since=<cursor>     live events (fallback: poll GET /api/stream?since=)
     GET /api/market                        judge wallets
     POST /api/run {"dataset":"live"}       a fresh month, generated and processed live (needs the token)
     GET /api/filings/<id>/screen           Clicky's captured filing screen, if the server has it
   Every number on screen comes from an event; nothing is invented.
   ========================================================================== */
(() => {
    const DEFAULT_API = 'https://web-production-812b4.up.railway.app';
    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const money0 = (n) => `$${Math.round(Number(n) || 0).toLocaleString()}`;
    const store = (k) => { try { return localStorage.getItem(k) || ''; } catch { return ''; } };
    const save = (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* private */ } };
    const icons = () => window.lucide && lucide.createIcons();
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const params = new URLSearchParams(location.search);
    if (params.get('token')) {   // one-time presenter link: keep the token, strip it from the address bar
        save('flock.token', params.get('token'));
        params.delete('token');
        history.replaceState(null, '', location.pathname + (params.toString() ? `?${params}` : '') + location.hash);
    }
    const CHANNEL = { doordash: 'DoorDash', ubereats: 'Uber Eats', grubhub: 'Grubhub', direct: 'Direct ordering', skipthedishes: 'SkipTheDishes' };

    // ---- the cast -----------------------------------------------------------------------------------------------
    const STAGES = [
        { id: 'collector', name: 'Collector', icon: 'inbox', c: '#012F8A' },
        { id: 'formatter', name: 'Formatter', icon: 'table-2', c: '#0146A9' },
        { id: 'matcher', name: 'Matcher', icon: 'git-merge', c: '#0159C4' },
        { id: 'deal_keeper', name: 'Deal Keeper', icon: 'scroll-text', c: '#017AF0' },
        { id: 'checkers', name: 'Checkers', icon: 'calculator', c: '#0084FF' },
        { id: 'judges', name: 'Judges', icon: 'scale', c: '#2EA7FF' },
        { id: 'ranker', name: 'Ranker', icon: 'list-ordered', c: '#5890CB' },
        { id: 'writer', name: 'Writer', icon: 'pen-line', c: '#4A628A' },
        { id: 'human', name: 'You', icon: 'user-round', c: '#f59e0b', glow: '#fbbf24' },
        { id: 'clicky', name: 'Clicky', icon: 'mouse-pointer-click', c: '#61CDFF', dark: true },
        { id: 'journal', name: 'Journal', icon: 'book-open', c: '#6DB4F1', dark: true },
        { id: 'learner', name: 'Learner', icon: 'graduation-cap', c: '#97E2FF', dark: true },
    ];
    const ORDER = Object.fromEntries(STAGES.map((s, i) => [s.id, i]));
    const JUDGE_COLOR = { strict: '#0146A9', operator: '#017AF0', skeptic: '#2EA7FF' };
    const ROLE_STAGE = {
        collector: 'collector', formatter: 'formatter', matcher: 'matcher', worker: 'matcher', workers: 'matcher',
        deal_keeper: 'deal_keeper', reader: 'deal_keeper', connector: 'deal_keeper',
        checker: 'checkers', checkers: 'checkers', anomaly: 'checkers',
        judge: 'judges', panel: 'judges', reviewer: 'judges', investigator: 'judges', market: 'judges',
        ranker: 'ranker', writer: 'writer', dispute_writer: 'writer',
        human: 'human', gatekeeper: 'human', clicky: 'clicky', follow_up: 'clicky', journal: 'journal', learner: 'learner',
    };
    const DEFAULT_PACE = { vote: 500, verdict: 800, bet_staked: 220, bet_settled: 300, human_decision: 1400, filed: 800,
        escalation: 1400, checked: 1000, workers: 700, agent_spawned: 250, agent_working: 300, agent_done: 350 };

    const S = {
        api: '', mode: 'idle', queue: [], playing: false, pace: { ...DEFAULT_PACE }, speed: 6, cursor: 0, es: null, pollTimer: null,
        stage: {}, money: 0, shownMoney: 0, findings: new Set(), humans: 0, filed: 0,
        judges: {}, cases: {}, show: Promise.resolve(), pendingShows: 0, clicky: Promise.resolve(), pendingClicky: 0, screens: null,
    };

    // ---- API ----------------------------------------------------------------------------------------------------
    async function detectBase() {
        if (params.get('api')) return params.get('api').replace(/\/$/, '');
        if (/^https?:$/.test(location.protocol)) {
            try {
                const ctl = new AbortController(); setTimeout(() => ctl.abort(), 2000);
                const r = await fetch(`${location.origin}/api/health`, { cache: 'no-store', signal: ctl.signal });
                const h = r.ok ? await r.json() : null;
                if (h && h.status === 'ok') return location.origin;
            } catch { /* fall through */ }
        }
        return (store('flock.api') || DEFAULT_API).replace(/\/$/, '');
    }
    const get = async (path) => {
        const r = await fetch(S.api + path, { cache: 'no-store' });
        if (!r.ok) throw Object.assign(new Error(`${r.status}`), { status: r.status });
        return r.json();
    };
    const token = () => store('flock.session') || store('flock.token');

    // ---- geometry + motion --------------------------------------------------------------------------------------
    const stage = () => $('stage');
    function center(el) {
        const s = stage().getBoundingClientRect(), r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2 - s.left, y: r.top + r.height / 2 - s.top };
    }
    const discOf = (id) => document.querySelector(`#av-${id} .disc`);
    function place(el, p) { el.style.left = `${p.x}px`; el.style.top = `${p.y}px`; el.style.transform = 'translate(-50%,-50%)'; }
    function fly(el, from, to, ms, extra = {}) {
        place(el, from);
        const dx = to.x - from.x, dy = to.y - from.y;
        return el.animate([
            { transform: `translate(-50%,-50%) scale(${extra.s0 ?? 1})`, opacity: extra.o0 ?? 1 },
            { transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(${extra.s1 ?? 1})`, opacity: extra.o1 ?? 1 },
        ], { duration: ms, easing: extra.ease || 'cubic-bezier(.45,0,.2,1)', fill: 'forwards' }).finished;
    }
    function spawnEl(cls, html) {
        const d = document.createElement('div'); d.className = cls; d.innerHTML = html; stage().appendChild(d); return d;
    }
    const pace = (ms) => Math.max(40, ms * (6 / Math.max(2, S.speed)));

    let capTimer = null;
    function caption(text) {
        const c = $('caption');
        c.textContent = text; c.style.opacity = 1;
        clearTimeout(capTimer); capTimer = setTimeout(() => (c.style.opacity = 0), 2600);
    }
    function tickMoney() {
        const target = S.money;
        const start = S.shownMoney, t0 = performance.now(), dur = 1200;
        const step = (t) => {
            const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
            S.shownMoney = start + (target - start) * e;
            $('money').textContent = money0(S.shownMoney);
            if (k < 1 && S.money === target) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    }
    function counters() {
        $('nFind').textContent = S.findings.size; $('nHuman').textContent = S.humans; $('nFiled').textContent = S.filed;
        const bc = $('hdrBellCount');
        if (bc) { bc.textContent = S.humans; bc.classList.toggle('hidden', !S.humans); }
    }

    // ---- the row of agents --------------------------------------------------------------------------------------
    function buildRow() {
        $('row').innerHTML = STAGES.map((s) => `
            <div class="av" id="av-${s.id}" style="--glow:${s.glow || '#2EA7FF'}">
                <div class="disc" style="background:${s.c};color:${s.dark ? '#012F8A' : '#fff'}">
                    <i data-lucide="${s.icon}" class="w-6 h-6"></i><span class="ok"><i data-lucide="check" class="w-3 h-3"></i></span>
                    ${s.id === 'human' ? '<span class="bell" id="bell">0</span>' : ''}
                </div>
                <div class="nm">${esc(s.name)}</div>
            </div>`).join('');
        icons();
        requestAnimationFrame(wires);
    }
    function wires() {
        document.querySelectorAll('#row .wire').forEach((w) => w.remove());
        const row = $('row').getBoundingClientRect();
        for (let i = 0; i < STAGES.length - 1; i++) {
            const a = $(`av-${STAGES[i].id}`).getBoundingClientRect(), b = $(`av-${STAGES[i + 1].id}`).getBoundingClientRect();
            if (Math.abs(a.top - b.top) > 4) continue;
            const w = document.createElement('div'); w.className = 'wire'; w.id = `w-${STAGES[i].id}`;
            w.style.left = `${a.left + a.width / 2 - row.left + 34}px`; w.style.width = `${b.left - a.left - 68}px`;
            w.innerHTML = '<i></i>';
            if (S.stage[STAGES[i].id] === 'done') w.firstChild.style.transform = 'scaleX(1)';
            $('row').appendChild(w);
        }
    }
    function setStage(id, status) {
        if (!id || !(id in ORDER)) return;
        if (S.stage[id] === 'done' && status === 'working' && !['judges', 'human', 'clicky', 'writer'].includes(id)) status = 'done';
        S.stage[id] = status;
        const el = $(`av-${id}`);
        el.classList.toggle('working', status === 'working');
        el.classList.toggle('done', status === 'done');
        const w = $(`w-${id}`); if (w && status === 'done') w.firstChild.style.transform = 'scaleX(1)';
        if (status !== 'idle') {   // a later agent moving means every earlier one has handed off
            for (const s of STAGES) {
                if (ORDER[s.id] >= ORDER[id]) break;
                if (s.id === 'human') continue;
                if (S.stage[s.id] !== 'done') {
                    S.stage[s.id] = 'done';
                    const e = $(`av-${s.id}`); e.classList.remove('working'); e.classList.add('done');
                    const ww = $(`w-${s.id}`); if (ww) ww.firstChild.style.transform = 'scaleX(1)';
                }
            }
        }
        el.classList.add('bump'); setTimeout(() => el.classList.remove('bump'), 220);
    }
    function finishAll() { STAGES.forEach((s) => S.stage[s.id] === 'working' && s.id !== 'human' && setStage(s.id, 'done')); }
    function bell(n) { const b = $('bell'); if (!b) return; b.textContent = n; b.classList.remove('on'); void b.offsetWidth; b.classList.add('on'); }

    function swarm(fromId, toId, n) {   // the Matcher's per-store workers fanning out and coming back
        const a = center(discOf(fromId)), k = Math.min(n, 60);
        for (let i = 0; i < k; i++) {
            const d = document.createElement('div'); d.className = 'dot'; stage().appendChild(d);
            const ang = Math.PI + (i / (k - 1 || 1)) * Math.PI, r = 90 + Math.random() * 120;
            const mid = { x: a.x + Math.cos(ang) * r * 1.6, y: a.y + Math.sin(ang) * r };
            fly(d, a, mid, 700 + Math.random() * 300, { ease: 'cubic-bezier(.15,.8,.3,1)' })
                .then(() => fly(d, mid, center(discOf(toId)), 700 + Math.random() * 300, { o1: 0.2 }))
                .then(() => d.remove());
        }
    }

    // ---- a finding's trip through the judges --------------------------------------------------------------------
    function caseOf(fid) {
        if (!S.cases[fid]) S.cases[fid] = { fid, votes: {}, models: {}, verdict: null, shown: false };
        return S.cases[fid];
    }
    const short = (j) => String(j || '').replace(/^judge\./, '');
    function modelTag(m) {
        const s = String(m || '');
        if (!s || /offline|rules/i.test(s)) return 'rules';
        if (/flock-judge/i.test(s)) return 'GX10 · tuned';
        if (/^creao/i.test(s)) return 'CREAO';
        if (/^local\//.test(s)) return 'GX10';
        if (/anthropic|claude/i.test(s)) return 'Claude';
        return s.split('/')[0];
    }
    function queueShow(c) {
        S.pendingShows += 1;
        S.show = S.show.then(() => judgeShow(c, S.pendingShows > 2)).catch((e) => console.warn('[theater]', e))
            .then(() => { S.pendingShows -= 1; });
    }
    async function judgeShow(c, quick) {
        if (c.shown) return; c.shown = true;
        const q = (ms) => sleep(pace(ms) / (quick ? 3 : 1));
        const vd = c.verdict || {};
        const jc = center(discOf('judges'));
        const W = stage().clientWidth;
        // 1. the case card flies in
        const ch = CHANNEL[vd.channel] || vd.channel || 'Finding';
        const card = spawnEl('fly fcard', `<i data-lucide="file-search" class="w-5 h-5" style="color:#0159C4"></i>${esc(ch)}${Number(vd.gap) > 0 ? ` · <span class="amt">${money0(vd.gap)}</span>` : ''}`);
        icons();
        const cardAt = { x: jc.x, y: Math.max(70, jc.y - 260) };
        await fly(card, { x: -120, y: cardAt.y }, cardAt, pace(quick ? 260 : 650), { ease: 'cubic-bezier(.2,.9,.3,1.05)' });
        // 2. three judges pop out of the Judges agent
        const names = Object.keys(c.votes).filter((n) => n !== 'reviewer');
        if (!names.length) names.push('strict', 'operator', 'skeptic');
        const spread = Math.min(150, W / 8);
        const js = names.map((n, i) => {
            const v = c.votes[n] || {};
            const el = spawnEl('judge', `
                <div class="jd" style="background:${JUDGE_COLOR[n] || '#0084FF'}"><i data-lucide="bot" class="w-7 h-7"></i>
                    <svg class="jring" viewBox="0 0 78 78"><circle cx="39" cy="39" r="35" fill="none" stroke="rgba(255,255,255,.12)" stroke-width="5"/>
                    <circle data-r cx="39" cy="39" r="35" fill="none" stroke="#97E2FF" stroke-width="5" stroke-linecap="round" stroke-dasharray="220" stroke-dashoffset="220" style="transition:stroke-dashoffset .7s ease-out"/></svg>
                    <span class="thumb" data-t></span></div>
                <div class="jn">${esc(n)}</div><div class="jm">${esc(modelTag(c.models[n]))}</div>`);
            const to = { x: jc.x + (i - (names.length - 1) / 2) * spread, y: jc.y - 140 };
            return { n, v, el, to };
        });
        icons();
        await Promise.all(js.map((o, i) => sleep(i * 90).then(() => fly(o.el, jc, o.to, pace(quick ? 250 : 520), { s0: 0.2, o0: 0, ease: 'cubic-bezier(.2,1.5,.4,1)' }))));
        caption(quick ? `${ch}: judges voting` : `3 judges spawned for ${ch}${Number(vd.gap) > 0 ? ' ' + money0(vd.gap) : ''}`);
        // 3. votes: confidence ring + thumb
        for (const o of js) {
            const conf = Number(o.v.confidence) || 0, out = o.v.outcome;
            o.el.querySelector('[data-r]').style.strokeDashoffset = String(220 * (1 - conf));
            const t = o.el.querySelector('[data-t]');
            const look = out === 'recoverable' ? ['thumbs-up', '#34d399', '#0b2a1f'] : out === 'correct' ? ['thumbs-down', '#61CDFF', '#012F8A']
                : out ? ['circle-help', '#fbbf24', '#3a2a00'] : ['minus', '#3E445C', '#cbd5ff'];
            t.style.background = look[1]; t.style.color = look[2];
            t.innerHTML = `<i data-lucide="${look[0]}" class="w-4 h-4"></i>`; icons();
            t.classList.add('on');
            await q(380);
        }
        await q(700);
        // 4. they merge into one verdict
        const merge = { x: jc.x, y: jc.y - 140 };
        await Promise.all(js.map((o) => fly(o.el, o.to, merge, pace(quick ? 200 : 420), { s1: 0.3, o1: 0 })));
        js.forEach((o) => o.el.remove());
        const human = vd.outcome === 'conflict' || vd.outcome === 'needs_info';
        const fair = vd.outcome === 'correct';
        const look = human ? ['Needs you', '#fbbf24', '#2b1d00', 'bell-ring'] : fair ? ['Fair charge', '#61CDFF', '#012F8A', 'check']
            : ['Claim it', '#34d399', '#06281b', 'badge-dollar-sign'];
        const chip = spawnEl('fly chip', `<i data-lucide="${look[3]}" class="w-5 h-5"></i>${look[0]}`);
        chip.style.background = look[1]; chip.style.color = look[2]; icons();
        card.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, fill: 'forwards' }).finished.then(() => card.remove());
        await fly(chip, merge, merge, pace(quick ? 150 : 350), { s0: 0.4, ease: 'cubic-bezier(.2,1.6,.4,1)' });
        caption(human ? 'Judges disagree → asking a human' : fair ? 'The charge matches the deal → no claim' : 'Judges agree → claim it');
        await q(500);
        // 5. the chip goes where the work goes next
        const dest = human ? 'human' : fair ? 'learner' : 'writer';
        await fly(chip, merge, center(discOf(dest)), pace(quick ? 300 : 700), { s1: 0.35, o1: 0.2 });
        chip.remove();
        if (human) { S.humans += 1; bell(S.humans); setStage('human', 'working'); counters(); }
        else if (!fair) setStage('writer', 'working');
        await q(200);
    }

    // ---- Clicky files: a tiny browser fills the claim form ------------------------------------------------------
    function queueClicky(ev) {
        S.pendingClicky += 1;
        S.clicky = S.clicky.then(() => clickyShow(ev, S.pendingClicky > 2)).catch(() => {}).then(() => { S.pendingClicky -= 1; });
    }
    async function clickyShow(ev, quick) {
        setStage('clicky', 'working');
        if (quick) { await sleep(pace(150)); return; }
        const at = center(discOf('clicky'));
        const b = spawnEl('fly browser', `<div class="bar"><i></i><i></i><i></i><span></span></div>
            <div class="pg"><div class="fld"><b></b></div><div class="fld"><b></b></div><div class="fld" style="width:70%"><b></b></div>
            <div class="sub"></div><svg class="cursor" viewBox="0 0 24 24" style="left:20px;top:20px"><path d="M4 2l16 9-7 2-3 7z" fill="#012F8A" stroke="#fff" stroke-width="1.5"/></svg>
            <div class="done"><div><i data-lucide="check" class="w-8 h-8"></i></div></div></div>`);
        icons();
        const to = { x: Math.min(stage().clientWidth - 130, at.x), y: at.y - 175 };
        await fly(b, at, to, pace(450), { s0: 0.2, o0: 0, ease: 'cubic-bezier(.2,1.3,.4,1)' });
        caption('Clicky is filing the claim in the portal');
        const cur = b.querySelector('.cursor'), flds = b.querySelectorAll('.fld b');
        for (let i = 0; i < flds.length; i++) {
            cur.style.top = `${16 + i * 20}px`; cur.style.left = '150px';
            flds[i].style.width = '100%';
            await sleep(pace(380));
        }
        cur.style.top = '78px'; cur.style.left = '190px';
        await sleep(pace(400));
        b.querySelector('.sub').style.transform = 'scale(.9)';
        await sleep(120);
        b.querySelector('.done').classList.add('on');
        S.filed += ev.count || 1; counters();
        caption(`Filed ✓ ${ev.portal_ref || ''}`.trim());
        if (ev.finding_id && await screenAvailable(ev.finding_id)) {   // Clicky's live screen, or the captured one
            S.lastFiled = ev.finding_id;
            b.style.pointerEvents = 'auto'; b.style.cursor = 'pointer'; b.title = "Watch Clicky's screen";
            b.onclick = () => openScreen(ev.finding_id);
            const av = $('av-clicky'); av.style.cursor = 'pointer'; av.title = "Watch Clicky's screen";
            av.onclick = () => S.lastFiled && openScreen(S.lastFiled);
        }
        await sleep(pace(900));
        await fly(b, to, at, pace(450), { s1: 0.2, o1: 0 });
        b.remove();
        setStage('clicky', 'done');
    }
    async function screenAvailable(fid) {
        if (S.agentScreen == null) {   // agent-screen.html: the live filing screen, served next to this page
            S.agentScreen = await fetch('agent-screen.html', { method: 'HEAD', cache: 'no-store' }).then((r) => r.ok).catch(() => false);
        }
        if (S.agentScreen) return true;
        if (S.screens === false) return false;
        const ok = await fetch(`${S.api}/api/filings/${encodeURIComponent(fid)}/screen`, { cache: 'no-store' }).then((r) => r.ok).catch(() => false);
        if (ok) S.screens = true; else if (S.screens == null) S.screens = false;
        return ok;
    }
    function openScreen(fid) {
        if (S.agentScreen) {
            const api = S.api && S.api !== location.origin ? `&api=${encodeURIComponent(S.api)}` : '';
            window.open(`agent-screen.html?finding=${encodeURIComponent(fid)}${api}`, '_blank', 'noopener');
            return;
        }
        const back = document.createElement('div');
        back.style.cssText = 'position:fixed;inset:0;background:rgba(15,18,32,.7);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
        back.innerHTML = `<div style="width:min(1000px,100%);height:min(720px,90vh);background:#fff;border-radius:22px;overflow:hidden;display:flex;flex-direction:column">
            <div style="padding:12px 18px;background:#012F8A;color:#fff;font-weight:800;display:flex;align-items:center">Clicky's screen · ${esc(fid)}
            <button style="margin-left:auto;background:rgba(255,255,255,.15);border:0;color:#fff;border-radius:50%;width:30px;height:30px;cursor:pointer">×</button></div>
            <iframe src="${S.api}/api/filings/${encodeURIComponent(fid)}/screen" style="flex:1;border:0" sandbox=""></iframe></div>`;
        back.onclick = (e) => { if (e.target === back || e.target.tagName === 'BUTTON') back.remove(); };
        document.body.appendChild(back);
    }

    // ---- wallets ------------------------------------------------------------------------------------------------
    function ensureJudge(name) {
        name = short(name);
        if (!name || name === 'reviewer') return null;
        if (!S.judges[name]) S.judges[name] = { balance: null };
        return S.judges[name];
    }
    function drawWallets() {
        const names = Object.keys(S.judges);
        const max = Math.max(150, ...names.map((n) => S.judges[n].balance || 0));
        names.forEach((n) => {
            let col = $(`wal-${n}`);
            if (!col) {
                col = document.createElement('div'); col.id = `wal-${n}`; col.className = 'flex flex-col items-center gap-2';
                col.innerHTML = `<div class="text-sm font-extrabold" style="color:#d97706" data-b></div>
                    <div class="stack"><div class="coins" data-c style="height:0"></div></div>
                    <div class="w-12 h-12 rounded-full grid place-items-center text-white" style="background:${JUDGE_COLOR[n] || '#0084FF'}"><i data-lucide="bot" class="w-6 h-6"></i></div>
                    <div class="text-xs font-bold capitalize text-gray-500">${esc(n)}</div>`;
                $('wallets').appendChild(col); icons();
            }
            const bal = S.judges[n].balance;
            col.querySelector('[data-b]').textContent = bal == null ? '' : Math.round(bal);
            col.querySelector('[data-c]').style.height = `${Math.max(6, ((bal || 0) / max) * 190)}px`;
        });
    }
    function plusMinus(name, d) {
        const col = $(`wal-${short(name)}`); if (!col || !d || Math.abs(d) < 0.05) return;
        const el = document.createElement('div'); el.className = 'pm';
        el.textContent = `${d > 0 ? '+' : '−'}${Math.abs(d).toFixed(0) === '0' ? Math.abs(d).toFixed(1) : Math.abs(d).toFixed(0)}`;
        el.style.color = d > 0 ? '#34d399' : '#f87171';
        col.querySelector('.stack').appendChild(el);
        el.animate([{ transform: 'translate(-50%,0)', opacity: 1 }, { transform: 'translate(-50%,-34px)', opacity: 0 }], { duration: 1100, easing: 'ease-out' }).finished.then(() => el.remove());
    }
    function applyBalances(b) {
        if (!b) return;
        for (const [k, v] of Object.entries(b)) {
            const j = ensureJudge(k); if (!j) continue;
            const before = j.balance; j.balance = Number(v);
            if (before != null) plusMinus(k, j.balance - before);
        }
        drawWallets();
    }

    // ---- events -------------------------------------------------------------------------------------------------
    function stageOfAgent(ev) {
        const id = String(ev.agent_id || ev.actor || '');
        const role = String(ev.role || id.split(':')[0] || '').replace(/\./g, '_');
        return ROLE_STAGE[role] || ROLE_STAGE[id.split(':')[0]] || ROLE_STAGE[id.split('.')[0]] || null;
    }
    function reset() {
        Object.assign(S, { stage: {}, money: 0, shownMoney: 0, findings: new Set(), humans: 0, filed: 0, cases: {} });
        document.querySelectorAll('#stage .fly, #stage .judge, #stage .dot').forEach((e) => e.remove());
        buildRow(); counters(); $('money').textContent = '$0';
    }
    function handle(ev) {
        const t = ev.type;
        if (t === 'run_started') { reset(); setStage('collector', 'working'); caption('The flock is waking up'); return; }
        if (ev.finding_id) S.findings.add(ev.finding_id);
        switch (t) {
            case 'agent_spawned': case 'agent_working': setStage(stageOfAgent(ev), 'working'); break;
            case 'agent_done': case 'agent_failed': setStage(stageOfAgent(ev), 'done'); break;
            case 'spawn': {
                const c = caseOf(ev.finding_id || ''), j = short(ev.judge);
                if (j) { c.votes[j] = c.votes[j] || {}; c.models[j] = ev.model || c.models[j]; }
                setStage('judges', 'working'); break;
            }
            case 'workers':
                setStage('matcher', 'working'); swarm('matcher', 'checkers', ev.count || 0);
                caption(`${ev.count} workers spawned, one per store`); break;
            case 'checked':
                setStage('checkers', 'done');
                if (ev.gap_total != null) { S.money += Number(ev.gap_total); tickMoney(); }
                caption(`Checkers found ${money0(ev.gap_total)} in ${ev.count} gaps`); break;
            case 'vote': case 'revision': {
                setStage('judges', 'working');
                const c = caseOf(ev.finding_id), j = short(ev.judge);
                if (j && j !== 'reviewer') { c.votes[j] = { outcome: ev.outcome, confidence: ev.confidence }; c.models[j] = ev.model || c.models[j]; ensureJudge(j); }
                break;
            }
            case 'verdict': { const c = caseOf(ev.finding_id); c.verdict = ev; setStage('judges', 'working'); queueShow(c); break; }
            case 'judged_bulk': setStage('judges', 'working'); break;
            case 'escalation': setStage('judges', 'done'); break;   // the bell rings when the chip lands
            case 'bet_staked': case 'bet_settled': case 'bets_bulk': case 'settled_bulk':
                applyBalances(ev.balances || (ev.balance != null && ev.judge ? { [short(ev.judge)]: ev.balance } : null)); break;
            case 'ranked': setStage('ranker', 'done'); break;
            case 'dispute_drafted': case 'drafted_bulk': setStage('writer', 'working'); break;
            case 'human_decision': case 'decided_bulk':
                setStage('writer', 'done'); setStage('human', 'done');
                caption(ev.decision === 'reject' ? 'A human rejected it' : 'A human approved it');
                break;
            case 'filed': case 'filed_bulk': queueClicky(ev); break;
            case 'denied': caption('Gatekeeper refused an agent'); break;
            case 'journal': setStage('journal', 'done'); break;
            case 'rule_applied': case 'rule_proposed': case 'rule_approved': setStage('learner', 'done');
                if (t !== 'rule_applied') caption('Learner proposed a new rule'); break;
            case 'run_finished': case 'run_done': finishAll(); break;
            default: setStage(stageOfAgent(ev), 'working');
        }
        counters();
    }

    // ---- player: a paced queue ----------------------------------------------------------------------------------
    function enqueue(events, live) { for (const e of events) S.queue.push({ e, live }); if (!S.playing) play(); }
    async function play() {
        S.playing = true;
        let prevT = null;
        while (S.queue.length) {
            const { e, live } = S.queue.shift();
            let wait = (!live && e.t_ms != null && prevT != null) ? e.t_ms - prevT : (S.pace[e.type] || 250) / (live ? 4 : S.speed);
            prevT = e.t_ms;
            if (S.pendingShows > 1 && e.type === 'verdict') await S.show;   // let the audience see each case
            if (S.queue.length > 400) wait = Math.min(wait, 15);
            await sleep(Math.max(0, Math.min(wait, 1500)));
            try { handle(e); } catch (err) { console.warn('[theater]', err, e); }
        }
        S.playing = false;
        if (S.mode === 'replay') { await S.show; await S.clicky; finishAll(); setMode('live'); }
    }
    function setMode(m) {
        S.mode = m;
        const map = { idle: ['Idle', '#d1d5db'], live: ['Live', '#10b981'], replay: ['Replay', '#0084FF'], running: ['Running', '#f59e0b'] };
        const [t, dot] = map[m] || map.idle;
        $('modePill').innerHTML = `<span class="w-2 h-2 rounded-full" style="background:${dot}"></span> ${t}`;
    }
    async function replay() {
        stopLive(); S.queue = [];
        try {
            const r = await get(`/api/stream/replay?run=last&all=1&speed=${S.speed}&limit=5000`);
            if (r.pace_ms) S.pace = { ...DEFAULT_PACE, ...r.pace_ms };
            S.cursor = Math.max(S.cursor, r.cursor || 0);
            setMode('replay'); reset();
            enqueue(r.events || [], false);
        } catch (e) { caption(`Could not load the last run (${e.message})`); }
        startLive();
    }

    // ---- live ---------------------------------------------------------------------------------------------------
    function stopLive() { if (S.es) { S.es.close(); S.es = null; } clearInterval(S.pollTimer); S.pollTimer = null; }
    function startLive() {
        stopLive();
        const onEvent = (msg) => {
            try {
                const ev = JSON.parse(msg.data);
                if (ev.cursor != null) { if (ev.cursor <= S.cursor) return; S.cursor = ev.cursor; }
                if (S.mode === 'replay') return;
                if (S.mode !== 'running') setMode('live');
                enqueue([ev], true);
            } catch { /* keep-alive */ }
        };
        if (window.EventSource) {
            const es = new EventSource(`${S.api}/api/stream/sse?since=${S.cursor}`);
            ['run_started', 'agent_spawned', 'agent_working', 'agent_done', 'agent_failed', 'spawn', 'workers', 'checked', 'case_file',
                'cases_bulk', 'vote', 'revision', 'second_opinion', 'verdict', 'rule_applied', 'memo', 'escalation', 'bet_staked', 'bets_bulk',
                'judged_bulk', 'ranked', 'dispute_drafted', 'drafted_bulk', 'human_decision', 'decided_bulk', 'bet_settled', 'settled_bulk',
                'filed', 'filed_bulk', 'denied', 'journal', 'rule_proposed', 'rule_approved', 'follow_up', 'run_finished', 'run_done', 'message']
                .forEach((t) => es.addEventListener(t, onEvent));
            let fails = 0;
            es.onerror = () => { if (++fails > 3) { es.close(); S.es = null; poll(); } };
            es.onopen = () => { fails = 0; };
            S.es = es;
        } else poll();
    }
    function poll() {
        clearInterval(S.pollTimer);
        S.pollTimer = setInterval(async () => {
            try {
                const page = await get(`/api/stream?since=${S.cursor}&limit=500`);
                const evs = (page.events || []).filter((e) => e.cursor > S.cursor);
                S.cursor = Math.max(S.cursor, page.cursor || 0);
                if (evs.length && S.mode !== 'replay') { if (S.mode !== 'running') setMode('live'); enqueue(evs, true); }
            } catch { /* offline for a moment */ }
        }, 1500);
    }

    // ---- run a new month live -----------------------------------------------------------------------------------
    async function runLive() {
        const tk = token();
        if (!tk) { $('tokenBox').classList.remove('hidden'); $('tokenInput').focus(); return; }
        $('liveBtn').disabled = true;
        try {
            const r = await fetch(`${S.api}/api/run?wait=0`, {
                method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tk}` },
                body: JSON.stringify({ dataset: 'live', actor: store('flock.actor') || 'Preet' }),
            });
            const body = await r.json().catch(() => ({}));
            if (r.status === 401) { save('flock.token', ''); $('tokenBox').classList.remove('hidden'); $('tokenInput').focus(); return; }
            if (!r.ok) { caption(body.error ? `Run refused: ${body.error}`.slice(0, 90) : `Run refused (${r.status})`); return; }
            const id = body.job_id || (body.job && body.job.job_id) || body.id;
            S.queue = []; setMode('running'); startLive();
            caption('Generating a fresh month of deposits…');
            if (id) watchJob(id);
        } catch (e) { caption(`Could not start a run: ${e.message}`); } finally { $('liveBtn').disabled = false; }
    }
    async function watchJob(id) {
        for (;;) {
            await sleep(2500);
            let j; try { j = await get(`/api/jobs/${id}`); } catch { continue; }
            if (j.status !== 'running' && j.status !== 'queued') {
                await S.show; await S.clicky; setMode('live');
                if (j.status === 'done') { finishAll(); caption('Month reconciled'); } else caption(`Run ${j.status}`);
                return;
            }
        }
    }

    // ---- boot ---------------------------------------------------------------------------------------------------
    async function whoAmI() {   // the signed-in user and their company, like the console's header
        const tk = store('flock.token');
        if (!tk) return;
        try {
            const r = await fetch(`${S.api}/api/me`, { headers: { Authorization: `Bearer ${tk}` }, cache: 'no-store' });
            if (!r.ok) return;
            const me = await r.json(), u = me.user || me;
            const org = u.org || 'Ringtide Bagels', name = u.name || u.username || 'Preet';
            $('orgName').textContent = org;
            $('orgLogo').textContent = org.replace(/['’]s\b/g, '').split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
            $('actorBadge').textContent = name.slice(0, 1).toUpperCase(); $('actorBadge').title = name;
        } catch { /* older backend: keep the defaults */ }
    }
    async function loadMarket() {
        try {
            const m = await get('/api/market');
            for (const row of m.leaderboard || []) { const j = ensureJudge(row.judge); if (j && j.balance == null) j.balance = Number(row.balance); }
        } catch { /* no market yet */ }
        drawWallets();
    }
    async function boot() {
        buildRow(); icons();
        window.addEventListener('resize', () => requestAnimationFrame(wires));
        $('replayBtn').onclick = replay;
        $('liveBtn').onclick = runLive;
        $('tokenSave').onclick = () => { const v = $('tokenInput').value.trim(); if (!v) return; save('flock.token', v); $('tokenBox').classList.add('hidden'); runLive(); };
        $('tokenInput').onkeydown = (e) => e.key === 'Enter' && $('tokenSave').click();
        if (params.get('speed')) S.speed = Number(params.get('speed')) || S.speed;
        S.api = await detectBase();
        whoAmI();
        await loadMarket();
        let running = null;
        try { running = (await get('/api/health')).running_job; } catch { /* offline */ }
        if (running) {
            const page = await get('/api/stream?since=0&limit=5000').catch(() => null);
            setMode('running');
            if (page) { enqueue(page.events || [], true); S.cursor = page.cursor || 0; }
            startLive(); watchJob(running);
        } else if (params.get('autoplay') !== '0') replay();
        else startLive();
    }
    boot();
})();
