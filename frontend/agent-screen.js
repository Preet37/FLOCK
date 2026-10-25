/* Clicky's live agent screen: drives the sandbox portal (portal.html, same-origin iframe) with a visible cursor.
   GET  /api/clicky/plan/<id>   what to type (built on the server from the human-approved finding)
   GET  /api/clicky/queue       approved findings waiting for Clicky (queue mode, no ?finding)
   POST /api/clicky/file/<id>   the portal's Submit (window.clickyFile below); the server checks the typed form and
                                the gatekeeper moves APPROVED -> FILED. Already filed: the screen replays it, no POST. */
(() => {
    const q = new URLSearchParams(location.search);
    const API = (q.get('api') || '').replace(/\/$/, '');
    let speed = Math.max(1, Math.min(4, Number(q.get('speed')) || 1));
    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const money = (n) => `$${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms / speed));
    const token = () => { try { return localStorage.getItem('flock.token') || ''; } catch { return ''; } };
    const setToken = (t) => { try { localStorage.setItem('flock.token', t); } catch { /* private mode */ } };
    if (q.get('embed') === '1') document.body.classList.add('embed');
    if (q.get('token')) { setToken(q.get('token')); q.delete('token'); history.replaceState(null, '', `${location.pathname}?${q}`); }

    const frame = $('frame'), stage = $('stage'), cursor = $('cursor');
    const pos = { x: -60, y: 40 };
    let running = false;

    // ---- speed buttons ----------------------------------------------------------------------------------------
    const paintSpeed = () => $('speed').querySelectorAll('button').forEach((b) => b.classList.toggle('on', Number(b.dataset.s) === speed));
    $('speed').onclick = (e) => { const s = Number(e.target.dataset.s); if (s) { speed = s; paintSpeed(); } };
    paintSpeed();

    // ---- step log ---------------------------------------------------------------------------------------------
    let current = null;
    function step(text, kind = 'now') {
        if (current && current.classList.contains('now')) { current.classList.replace('now', 'ok'); current.querySelector('i').textContent = '✓'; }
        const el = document.createElement('div');
        el.className = `step ${kind}`;
        el.innerHTML = `<i>${kind === 'bad' ? '!' : kind === 'ok' ? '✓' : '•'}</i><span>${esc(text)}</span>`;
        $('steps').appendChild(el);
        $('steps').scrollTop = $('steps').scrollHeight;
        current = el;
        return el;
    }
    const doneStep = () => { if (current && current.classList.contains('now')) { current.classList.replace('now', 'ok'); current.querySelector('i').textContent = '✓'; } };

    // ---- networking -------------------------------------------------------------------------------------------
    async function api(path, opts = {}) {
        const headers = { ...(opts.body ? { 'Content-Type': 'application/json' } : {}), ...(token() ? { Authorization: `Bearer ${token()}` } : {}) };
        const res = await fetch(API + path, { cache: 'no-store', ...opts, headers });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { const e = new Error(data.error || `HTTP ${res.status}`); e.status = res.status; e.data = data; throw e; }
        return data;
    }
    function askToken() {
        return new Promise((resolve) => {
            $('tok').classList.add('on'); $('tokIn').value = ''; $('tokIn').focus();
            $('tokGo').onclick = () => { const t = $('tokIn').value.trim(); if (!t) return; setToken(t); $('tok').classList.remove('on'); resolve(true); };
        });
    }
    // the portal's Submit calls this (portal.html: window.parent.clickyFile)
    window.clickyFile = async (fid, fields) => {
        try { return await api(`/api/clicky/file/${encodeURIComponent(fid)}`, { method: 'POST', body: JSON.stringify({ fields }) }); }
        catch (e) {
            if (e.status === 401) { step('Needs a reviewer sign-in to submit', 'bad'); await askToken(); step('Signed in, submitting again'); return window.clickyFile(fid, fields); }
            throw e;
        }
    };

    // ---- the cursor ------------------------------------------------------------------------------------------
    const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
    function place(x, y) { pos.x = x; pos.y = y; cursor.style.transform = `translate(${x - 2}px, ${y - 2}px)`; }
    function glide(x, y, ms) {
        const sx = pos.x, sy = pos.y, dur = Math.max(120, ms / speed), t0 = performance.now();
        const bend = (Math.random() - 0.5) * 60;   // a slight curve, like a hand on a trackpad
        return new Promise((resolve) => {
            const tick = (now) => {
                const t = Math.min(1, (now - t0) / dur), e = ease(t);
                const arc = Math.sin(Math.PI * t) * bend;
                place(sx + (x - sx) * e + arc * 0.3, sy + (y - sy) * e - arc * 0.2);
                t < 1 ? requestAnimationFrame(tick) : resolve();
            };
            requestAnimationFrame(tick);
        });
    }
    const doc = () => frame.contentDocument;
    const win = () => frame.contentWindow;
    const fire = (name) => { const W = win(); return new W.Event(name, { bubbles: true }); };
    function center(el) {
        const r = el.getBoundingClientRect();
        return { x: r.left + Math.min(r.width / 2, 60 + r.width * 0.15), y: r.top + r.height / 2 };
    }
    async function moveTo(sel) {
        const el = typeof sel === 'string' ? doc().querySelector(sel) : sel;
        if (!el) throw new Error(`the portal has no ${sel} (it changed?) — Clicky stops instead of guessing`);
        const r = el.getBoundingClientRect(), h = stage.clientHeight;
        if (r.top < 40 || r.bottom > h - 40) { el.scrollIntoView({ block: 'center', behavior: 'smooth' }); await sleep(450); }
        const c = center(el);
        const d = Math.hypot(c.x - pos.x, c.y - pos.y);
        await glide(c.x, c.y, 380 + Math.min(700, d * 0.9));
        return el;
    }
    function ripple() {
        const r = document.createElement('div');
        r.className = 'ripple'; r.style.left = `${pos.x}px`; r.style.top = `${pos.y}px`;
        stage.appendChild(r); setTimeout(() => r.remove(), 600);
    }
    async function click(sel) {
        const el = await moveTo(sel);
        await sleep(120); ripple(); el.focus?.(); el.click(); await sleep(260);
        return el;
    }
    function setValue(el, v) {
        const proto = el.tagName === 'TEXTAREA' ? win().HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? win().HTMLSelectElement.prototype : win().HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
        el.dispatchEvent(fire('input'));
    }
    async function type(sel, text, { fast = false } = {}) {
        const el = await click(sel);
        setValue(el, '');
        text = String(text ?? '');
        const slow = fast ? 40 : 220;                 // type the start key by key, then in bursts (long descriptions)
        for (let i = 0; i < text.length;) {
            const n = i < slow ? 1 : 6 + Math.floor(Math.random() * 10);
            setValue(el, text.slice(0, i + n)); i += n;
            if (el.tagName === 'TEXTAREA') el.scrollTop = el.scrollHeight;
            await sleep(i < slow ? 28 + Math.random() * 40 : 16);
        }
        el.dispatchEvent(fire('change'));
        await sleep(140);
    }
    async function choose(sel, value) {
        const el = await click(sel);
        await sleep(260);
        setValue(el, value); el.dispatchEvent(fire('change'));
        await sleep(240);
    }
    function load(url) {
        return new Promise((resolve) => { frame.onload = () => { frame.onload = null; resolve(); }; frame.src = url; });
    }
    function setUrl(text, loading) {
        $('url').innerHTML = `<b>🔒</b> ${esc(text)}`;
        $('tabspin').style.display = loading ? '' : 'none';
    }

    // ---- one filing ------------------------------------------------------------------------------------------
    function showCase(p, state) {
        $('case').innerHTML = `<div class="meta">${esc(p.platform)} · ${esc(p.finding_id)}</div><div class="amt">${money(p.gap)}</div>
            <div class="meta">${esc(p.fields?.category_label || p.check_type || '')}${p.fields?.payout_id ? ` · ${esc(p.fields.payout_id)}` : ''}</div>
            <span class="state ${state === 'FILED' ? 'filed' : ''}">${esc(state)}</span>`;
    }
    async function file(fid) {
        const p = await api(`/api/clicky/plan/${encodeURIComponent(fid)}`);
        const replay = !p.fileable && p.portal_ref ? p.portal_ref : '';
        if (!p.fileable && !replay) {
            showCase(p, p.state);
            step(`${fid} is ${p.state}: Clicky only files claims a person approved`, 'bad');
            return false;
        }
        showCase(p, replay ? 'FILED · replay' : 'APPROVED');
        const host = `sandbox-portal.flock.example/${p.channel}`;
        $('bot').classList.add('work'); $('glow').classList.add('on');
        $('tabtitle').textContent = `${p.platform} Merchant Support (sandbox)`;
        step(`Opening the ${p.platform} sandbox portal`);
        setUrl(`${host}/login`, true);
        const qs = new URLSearchParams({ platform: p.channel, finding: fid, ...(replay ? { replay } : {}), ...(API ? { api: API } : {}) });
        await load(`portal.html?${qs}`);
        win().portalFiles = p.evidence || [];
        setUrl(`${host}/login`, false);
        await sleep(400);

        step('Signing in to the merchant account');
        await type('#email', 'finance@ringtide-bagels.example', { fast: true });
        await type('#password', '••••••••••••', { fast: true });
        await click('#signin');
        setUrl(`${host}/disputes`, false);
        await sleep(500);

        step('Opening a new dispute');
        await click('#new-dispute');
        setUrl(`${host}/disputes/new`, false);
        await sleep(350);

        const f = p.fields;
        step(`Category: ${f.category_label}`);
        await choose('#category', f.category);
        if (f.payout_id) { step(`Payout ID ${f.payout_id}`); await type('#payout_id', f.payout_id); }
        step(`Amount ${money(f.amount)}`);
        await type('#amount', f.amount);
        step(`Our reference ${f.merchant_ref}`);
        await type('#merchant_ref', f.merchant_ref);
        await type('#order_ids', f.order_ids.length > 80 ? `${f.order_ids.slice(0, 77)}…` : f.order_ids, { fast: true });
        step('Writing the description from the approved draft');
        await type('#message', f.message);

        const files = p.evidence || [];
        if (files.length) {
            step(`Attaching ${files.length} evidence file${files.length > 1 ? 's' : ''}`);
            await click('#attach');
            await sleep(300);
            for (let i = 0; i < files.length; i++) await click(`#file-${i}`);
            await click('#attach-ok');
        }

        step(replay ? 'Submit (already filed: showing the saved result)' : 'Submitting the dispute');
        win().portalResult = null;
        await click('#submit');
        const t0 = Date.now();
        while (!win().portalResult && Date.now() - t0 < 30000) await sleep(150);
        const out = win().portalResult || { error: 'the portal did not answer' };
        $('bot').classList.remove('work'); $('glow').classList.remove('on');
        if (out.error) { step(`Stopped: ${out.error}`, 'bad'); return false; }
        setUrl(`${host}/disputes/${out.portal_ref}`, false);
        await moveTo('#portal-ref');
        step(`Filed → ${out.portal_ref}`, 'ok');
        showCase(p, 'FILED');
        $('screenLink').style.display = ''; $('screenLink').href = `${API}/api/filings/${encodeURIComponent(fid)}/screen`;
        if (!replay) step('Gatekeeper logged it: APPROVED → FILED (actor: Clicky)', 'ok');
        try { window.parent?.postMessage({ type: 'clicky-filed', finding_id: fid, portal_ref: out.portal_ref }, '*'); } catch { /* not embedded */ }
        return true;
    }

    async function idle(msg) {
        $('empty').innerHTML = msg; $('empty').classList.add('on');
    }
    async function run() {
        if (running) return; running = true;
        $('steps').innerHTML = ''; current = null; $('empty').classList.remove('on');
        try {
            const one = q.get('finding');
            if (one) { await file(one); return; }
            const { queue } = await api('/api/clicky/queue');
            if (!queue.length) {
                // never an empty screen: replay the latest filing, or preview the biggest claim waiting for approval
                const all = await api('/api/findings').catch(() => []);
                const filed = (Array.isArray(all) ? all : all.findings || []).filter((f) => f.portal_ref);
                if (filed.length) {
                    step('Clicky is waiting for your approvals. Replay mode: the most recent filing', 'ok');
                    await file(filed[0].finding_id || filed[0].id);
                    return;
                }
                const ap = await api('/api/approvals').catch(() => ({ items: [] }));
                const next = (ap.items || [])[0];
                if (next) {
                    const p = await api(`/api/clicky/plan/${encodeURIComponent(next.finding_id)}`).catch(() => null);
                    if (p) showCase(p, 'PREVIEW · waiting for your OK');
                }
                return idle('<div><b>Clicky is waiting for your approvals.</b><br>Approve a claim on the dashboard and Clicky files it here.</div>');
            }
            step(`${queue.length} approved claim${queue.length > 1 ? 's' : ''} in Clicky's queue`, 'ok');
            for (const it of queue) { await file(it.finding_id); await sleep(900); }
        } catch (e) {
            step(String(e.message || e), 'bad');
        } finally { running = false; doneStep(); $('bot').classList.remove('work'); $('glow').classList.remove('on'); }
    }
    $('again').onclick = run;
    place(-60, 40);
    run();
})();
