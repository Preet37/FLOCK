/* ==========================================================================
   Flock console: live views over the Flock API (reads are open, POSTs need a token).
   Routes (hash): #/ overview · #/lumpsums · #/payout/<id> · #/theater · #/sources · #/scoreboard · #/benchmarks · #/finding/<id>
   API base: ?api=<url>  >  the page's own origin when it is served by a Flock API  >  saved setting  >  DEFAULT_API
   ========================================================================== */
const DEFAULT_API = 'https://web-production-812b4.up.railway.app';
const FAST_MS = 1500;   // audit log + agents
const SLOW_MS = 4000;   // findings, metrics, score, payouts, showtime, market, connectors, open views

function store(key, fallback = '') {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
    try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch { /* private mode */ }
}

const params = new URLSearchParams(location.search);
const settings = {
    api: '', apiSource: '',
    actor: 'Preet',                 // the signed-in user's name replaces this (GET /api/me)
    token: store('flock.token'),    // session token from POST /api/login, or the presenter token on an older backend
    user: null, auth: null,         // auth: 'login' when the API has /api/login, 'token' when it doesn't
};

const state = {
    log: [], findings: [], agents: null, score: null, metrics: null, payouts: null, showtime: null, market: null,
    connectors: null, payout: null, findingFilter: 'all', seenLog: 0, drawer: null, online: null, refusal: null,
    lastDecision: null, cache: {}, base: '', approvals: null, outbox: null, job: null, benchmarks: null, safety: null,
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (n) => (n == null || isNaN(n)) ? '—' : `${Number(n) < 0 ? '−' : ''}$${Math.abs(Number(n)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const num = (n) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString();
const pct = (n) => (n == null || isNaN(n)) ? '—' : `${Math.round(Number(n) * 100)}%`;
const icons = () => window.lucide && lucide.createIcons();
const label = (s) => String(s ?? '').replace(/_/g, ' ');
const fidOf = (f) => f.finding_id || f.id;

function ago(iso) {
    if (!iso) return 'idle';
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
}
const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';

/* ---------------- networking ---------------- */

class ApiError extends Error {
    constructor(status, data, path) {
        super((data && data.error) || `HTTP ${status}`);
        this.status = status; this.data = data || {}; this.path = path;
    }
}

async function get(path) {
    const res = await fetch(state.base + path, { cache: 'no-store', headers: settings.token ? { Authorization: `Bearer ${settings.token}` } : {} });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, data, path);
    return data;
}
const tryGet = (path) => get(path).catch(() => null);

async function post(path, body, retried = false) {
    const headers = { 'Content-Type': 'application/json' };
    if (settings.token) headers.Authorization = `Bearer ${settings.token}`;
    const res = await fetch(state.base + path, { method: 'POST', headers, body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    // First 401: ask for the presenter token once, then carry on with the same action
    if (res.status === 401 && !retried && await askToken()) return post(path, body, true);
    if (!res.ok) throw new ApiError(res.status, data, path);
    return data;
}

let tokenWaiter = null;
function askToken() {
    if (tokenWaiter) return tokenWaiter.promise;
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    tokenWaiter = { promise, resolve };
    if (settings.auth === 'login') { showLogin('Your session ended. Sign in again to continue.'); return promise; }
    $('tokenOnce').value = '';
    $('tokenModal').classList.replace('hidden', 'flex');
    setTimeout(() => $('tokenOnce').focus(), 0);
    return promise;
}
function finishToken(token) {
    $('tokenModal').classList.replace('flex', 'hidden');
    $('loginScreen').classList.replace('flex', 'hidden');
    if (token) { settings.token = token; save('flock.token', token); renderActor(); }
    const w = tokenWaiter; tokenWaiter = null;
    if (w) w.resolve(!!token);
}

// One place that turns an API error into words a presenter can act on
function explain(e) {
    if (!(e instanceof ApiError)) return `Can't reach the API (${e.message}).`;
    if (e.status === 401) return 'That token was not accepted. Fix it under the key icon.';
    if (e.status === 409 && e.data.running) return `A run is in progress (${e.data.running}); try again when it finishes.`;
    if (e.status === 409 || e.status === 403) return `Refused by the gatekeeper: ${e.message}`;
    if (e.status === 404) return `Not found: ${e.message}`;
    if (e.status === 400) return `Bad request: ${e.message}`;
    return `Server error ${e.status}: ${e.message}`;
}
function fail(e) {
    toast(explain(e), 'error');
}

// Re-render only when the payload actually changed, so polling never flickers
function changed(key, data) {
    const s = JSON.stringify(data);
    if (state.cache[key] === s) return false;
    state.cache[key] = s;
    return true;
}

async function detectBase() {
    const fromParam = params.get('api');
    if (fromParam) return [fromParam.replace(/\/$/, ''), '?api= in the URL'];
    if (/^https?:$/.test(location.protocol)) {
        try {
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), 2000);
            const res = await fetch(`${location.origin}/api/health`, { cache: 'no-store', signal: ctl.signal });
            clearTimeout(t);
            const h = res.ok ? await res.json() : null;
            if (h && h.status === 'ok') return [location.origin, 'this page is served by the Flock API'];
        } catch { /* not served by the API: fall through */ }
    }
    const saved = store('flock.api');
    if (saved) return [saved.replace(/\/$/, ''), 'saved in settings'];
    return [DEFAULT_API, 'the hosted default'];
}

function setOnline(ok, msg) {
    if (state.online === ok && !msg) return;
    state.online = ok;
    $('liveStatus').innerHTML = ok
        ? '<span class="w-2 h-2 rounded-full bg-purpleAccent pulse-dot"></span> Live'
        : `<span class="w-2 h-2 rounded-full bg-red-500"></span> ${esc(msg || 'Offline')}`;
}

async function pollFast() {
    try {
        const [log, agents] = await Promise.all([get('/api/log'), get('/api/agents')]);
        setOnline(true);
        let dirty = false;
        if (changed('log', log)) { state.log = log; dirty = true; }
        if (changed('agents', agents)) { state.agents = agents; dirty = true; }
        if (route().name === 'overview') { if (dirty) { renderLog(); } renderAgents(); }
    } catch (e) {
        setOnline(false, 'API unreachable');
    } finally {
        setTimeout(pollFast, FAST_MS);
    }
}

async function pollSlow() {
    try {
        const [findings, metrics, score, payouts, showtime, market, connectors, approvals, outbox] = await Promise.all([
            tryGet('/api/findings'), tryGet('/api/metrics'), tryGet('/api/score'), tryGet('/api/payouts'),
            tryGet('/api/showtime'), tryGet('/api/market'), tryGet('/api/connectors'), tryGet('/api/approvals'), tryGet('/api/outbox'),
        ]);
        const r = route();
        if (findings && changed('findings', findings)) { state.findings = findings; if (r.name === 'overview') renderFindings(); }
        if (changed('approvals', { approvals, n: state.findings.length })) { state.approvals = approvals; renderQueueCount(); if (r.name === 'overview') renderApprovals(); }
        if (changed('metrics', { metrics, score })) { state.metrics = metrics; state.score = score; if (r.name === 'overview') { renderMetrics(); renderHours(); } if (r.name === 'scoreboard') renderScoreboard(); }
        if (payouts && changed('payouts', payouts)) { state.payouts = payouts; if (r.name === 'lumpsums') renderLumpSums(); }
        if (changed('showtime', showtime)) { state.showtime = showtime && showtime.json ? showtime.json : null; renderBanner(); }
        if (market && changed('market', market)) { state.market = market; if (r.name === 'scoreboard') renderScoreboard(); }
        if (outbox) state.outbox = outbox.messages || [];
        if (r.name === 'benchmarks' && !state.benchmarks) loadBenchmarks();
        if (connectors && changed('connectors', connectors)) { state.connectors = connectors; if (r.name === 'sources') renderSources(); }
        if (r.name === 'payout') loadPayout(r.id);
        if (state.drawer) refreshDrawer();
    } catch { /* pollFast reports connectivity */ }
    finally {
        setTimeout(pollSlow, SLOW_MS);
    }
}

/* ---------------- shared bits (her visual language) ---------------- */

// Plain words for the people in the room: what each check means, what each verdict asks of you
const CHECK_HELP = {
    commission: 'Commission: app charged a higher % than the contract',
    deposit: "Deposit: payout lump sum doesn't match the orders inside it",
    fee: "Fee: a fee the contract waived or doesn't allow",
    promo: "Promo: the store was charged for a promotion the deal says the app pays for",
};
const checkHelp = (c) => CHECK_HELP[c] || CHECK_HELP[String(c || '').split('_')[0]] || CHECK_HELP[Object.keys(CHECK_HELP).find((k) => String(c || '').includes(k))] || '';
const VERDICT = { recoverable: ['Claim it', 'money we can claim back'], conflict: ['Needs you', 'documents disagree; a human decides'] };
const verdictText = (o) => (VERDICT[o] || [label(o)])[0];
const CONF_TIP = "judge panel's balance-weighted confidence";
function verdictPill(o, conf, sep = ' · ') {
    if (!o) return '';
    const tip = [(VERDICT[o] || [])[1], conf != null ? `${pct(conf)} sure: ${CONF_TIP}` : ''].filter(Boolean).join(' · ');
    return `<span title="${esc(tip)}">${pill(OUTCOME[o] || 'bg-gray-100 text-gray-600', `${verdictText(o)}${conf != null ? `${sep}${pct(conf)} sure` : ''}`)}</span>`;
}

const OUTCOME = {
    recoverable: 'bg-emerald-100 text-emerald-800', conflict: 'bg-amber-100 text-amber-800',
    correct: 'bg-gray-100 text-gray-600', disputed: 'bg-red-100 text-red-700',
};
const STATE_PILL = {
    NEW: 'bg-gray-100 text-gray-600', CHECKED: 'bg-gray-100 text-gray-700', JUDGED: 'bg-purpleAccent/10 text-purpleAccent',
    APPROVED: 'bg-emerald-100 text-emerald-800', REJECTED: 'bg-red-100 text-red-700', FILED: 'bg-sky-100 text-sky-800',
    RECOVERED: 'bg-limeAccent text-black',
};
const PAY_STATUS = {
    matched: ['bg-emerald-100 text-emerald-800', 'matched'], owed: ['bg-red-100 text-red-700', 'underpaid'],
    short_deposit: ['bg-amber-100 text-amber-800', 'short deposit'], missing_deposit: ['bg-red-100 text-red-700', 'missing deposit'],
    needs_review: ['bg-purpleAccent/10 text-purpleAccent', 'needs review'], recovered: ['bg-limeAccent text-black', 'recovered'],
};
const CHANNEL = {
    doordash: ['DoorDash', '#EB1700', 'DD'], ubereats: ['Uber Eats', '#06C167', 'UE'], grubhub: ['Grubhub', '#FF8000', 'GH'],
    direct_app: ['Direct app', '#0084FF', 'D'],
};
const AUTH_PILL = (a) => /^(contract|amendment|auth_email)$/.test(a) ? 'bg-emerald-100 text-emerald-800'
    : /unauth/.test(a) ? 'bg-red-100 text-red-700' : /call/.test(a) ? 'bg-amber-100 text-amber-800' : 'bg-gray-200 text-gray-700';

const pill = (cls, text) => `<span class="inline-block rounded-full px-2 py-0.5 text-[11px] font-bold whitespace-nowrap ${cls}">${esc(text)}</span>`;
const chanName = (c) => (CHANNEL[c] || [label(c)])[0];
function chanBadge(c, size = 'w-6 h-6 text-[9px]') {
    const [, color, short] = CHANNEL[c] || [c, '#9CA3AF', String(c || '?').slice(0, 2).toUpperCase()];
    return `<span class="${size} rounded-full shrink-0 inline-flex items-center justify-center font-extrabold text-white" style="background:${color}">${esc(short)}</span>`;
}
function riskPill(r) {
    if (r == null) return '<span class="text-gray-300">—</span>';
    const cls = r >= 67 ? 'bg-red-100 text-red-700' : r >= 34 ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-600';
    return pill(cls, Math.round(r));
}
const payPill = (s) => pill(...(PAY_STATUS[s] ? [PAY_STATUS[s][0], PAY_STATUS[s][1]] : ['bg-gray-100 text-gray-600', label(s)]));

const whiteCard = (inner, extra = '') => `<section class="bg-white p-5 sm:p-6 rounded-3xl card-shadow border border-gray-100 min-w-0 ${extra}">${inner}</section>`;
const cardHead = (title, sub = '', right = '') => `
    <div class="flex flex-col sm:flex-row sm:items-center justify-between shrink-0 mb-4 gap-3">
        <div class="min-w-0"><h3 class="font-extrabold text-gray-900 text-base sm:text-lg tracking-tight">${title}</h3>
        ${sub ? `<p class="text-xs text-gray-400 mt-0.5 font-medium">${sub}</p>` : ''}</div>${right}</div>`;
function statCard({ label: l, labelHtml, value, sub, icon, dark, how, cls = '' }) {
    return `<div class="${dark ? 'bg-cardDark text-white shadow-md' : 'bg-white text-gray-900 card-shadow border border-gray-100'} p-4 sm:p-5 rounded-3xl flex flex-col justify-between gap-2 min-w-0 ${cls}">
        <div class="flex items-center justify-between">
            <div class="w-9 h-9 sm:w-10 sm:h-10 rounded-full ${dark ? 'bg-limeAccent text-black' : 'bg-gray-100 text-gray-800'} flex items-center justify-center shrink-0"><i data-lucide="${icon || 'circle'}" class="w-4 h-4 sm:w-5 sm:h-5 stroke-[2.5]"></i></div>
            ${how ? `<button data-how="${esc(how)}" title="How we got this" aria-label="How we got this" class="w-8 h-8 rounded-full ${dark ? 'text-gray-400 hover:text-white hover:bg-white/10' : 'text-gray-400 hover:text-gray-800 hover:bg-gray-100'} flex items-center justify-center"><i data-lucide="more-vertical" class="w-4 h-4"></i></button>` : ''}
        </div>
        <div class="min-w-0">
            <p class="${dark ? 'text-gray-400' : 'text-gray-500'} text-xs font-medium mb-0.5">${labelHtml || esc(l)}</p>
            <h2 class="text-xl sm:text-2xl xl:text-3xl font-extrabold tracking-tight tabular-nums truncate">${value}</h2>
            ${sub ? `<p class="${dark ? 'text-limeAccent' : 'text-gray-500'} text-xs font-semibold mt-1">${sub}</p>` : ''}
        </div></div>`;
}
function filterPills(options, current, attr) {
    return `<div class="flex items-center gap-1.5 flex-wrap">${options.map(([key, text, n]) => `
        <button ${attr}="${esc(key)}" class="px-3 py-1.5 text-xs font-semibold rounded-full transition ${current === key ? 'bg-purpleAccent text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}">${esc(text)}${n != null ? ` <span class="text-[10px] ml-0.5 opacity-80">${n}</span>` : ''}</button>`).join('')}</div>`;
}
const empty = (msg, icon = 'hourglass') => `<div class="py-10 text-center text-gray-400 text-sm"><i data-lucide="${icon}" class="w-5 h-5 mx-auto mb-2"></i>${msg}</div>`;

/* ---------------- router ---------------- */

function route() {
    const h = location.hash.replace(/^#\/?/, '');
    const [path, query] = h.split('?');
    const [name, ...rest] = path.split('/');
    const id = rest.length ? decodeURIComponent(rest.join('/')) : null;
    return { name: name || 'overview', id, query: new URLSearchParams(query || '') };
}

const TITLES = {
    overview: ['Welcome back!', 'Dashboard'],
    lumpsums: ['Lump sums', 'Do the apps owe you money?'],
    payout: ['Lump sum', 'What it should have been vs what arrived'],
    theater: ['Judge theater', 'Judges vote, bet and debate; people decide; Clicky files'],
    sources: ['Sources / Inbox', 'Slack and email batches the connectors brought in, judged against the contracts'],
    scoreboard: ['Scoreboard', 'Answer key, learning curve and the judge market'],
    benchmarks: ['Benchmarks', 'Same findings, every contestant, scored on held-out seeds: rules, local models on the GX10, our tuned judge'],
};

let lastMain = '#/';
function onRoute() {
    const r = route();
    if (r.name === 'finding') {
        if (!$('view').innerHTML) renderView({ name: 'overview', query: new URLSearchParams() });
        openDrawer('finding', r.id);
        return;
    }
    if (state.drawer) closeDrawer(false);
    lastMain = location.hash || '#/';
    renderView(r);
}

function renderView(r) {
    const [t, s] = TITLES[r.name] || TITLES.overview;
    $('pageTitle').textContent = r.name === 'payout' && r.id ? `Lump sum ${r.id}` : t;
    $('pageSub').textContent = s;
    if (r.name === 'overview') setWelcome();
    document.querySelectorAll('.nav-btn').forEach((b) => {
        const on = b.dataset.nav === (r.name === 'payout' ? 'lumpsums' : r.name);
        b.classList.toggle('text-purpleAccent', on);
        b.classList.toggle('bg-cardDark', on);
        b.classList.toggle('text-gray-400', !on);
    });
    if (r.name !== 'theater') theaterStop();
    if (r.name === 'overview' && r.query.get('filter')) state.findingFilter = r.query.get('filter');
    const v = $('view');
    if (r.name === 'lumpsums') { v.innerHTML = '<div id="lumpsums"></div>'; renderLumpSums(); }
    else if (r.name === 'payout') { state.payout = null; state.cache.payout = null; v.innerHTML = `<div id="payout">${whiteCard(empty('Loading the lump sum…'))}</div>`; loadPayout(r.id); }
    else if (r.name === 'theater') { renderTheaterShell(); theaterStart(theater.mode); }
    else if (r.name === 'sources') { v.innerHTML = '<div id="sources"></div>'; renderSources(); }
    else if (r.name === 'scoreboard') { v.innerHTML = '<div id="scoreboard"></div>'; renderScoreboard(); }
    else if (r.name === 'benchmarks') { v.innerHTML = '<div id="benchmarks"></div>'; renderBenchmarks(); loadBenchmarks(); }
    else renderOverviewShell();
    renderBanner();
    icons();
}

/* ---------------- showtime banner ---------------- */

function setWelcome() {
    const first = String(settings.actor || '').trim().split(/\s+/)[0] || 'there';
    $('pageTitle').textContent = `Welcome back, ${first}!`;
    $('pageSub').textContent = 'Dashboard';
}

function renderBanner() {
    const el = $('showtimeBanner');
    const st = state.showtime;
    const r = route();
    if (!st || !st.lump_sum || r.name !== 'lumpsums') { el.classList.add('hidden'); el.innerHTML = ''; state.cache.bannerHtml = null; return; }
    const ls = st.lump_sum, h = st.headline || {};
    el.classList.remove('hidden');
    const html = `
        <div class="bg-cardDark text-white rounded-3xl px-4 sm:px-5 py-3 flex items-center justify-between gap-3 shadow-md">
            <p class="text-sm sm:text-base font-bold truncate">${esc(st.month_name || 'This month')}: ${esc(ls.platform || chanName(ls.channel))} paid <span class="text-limeAccent tabular-nums">${money(ls.short)}</span> less than it should have</p>
            <a href="#/payout/${encodeURIComponent(ls.payout_id)}" data-test="banner-payout" class="rounded-2xl bg-purpleAccent hover:bg-purpleAccentHover text-white font-bold text-sm px-4 py-2 flex items-center gap-1.5 shrink-0">Open <i data-lucide="arrow-right" class="w-4 h-4"></i></a>
        </div>`;
    if (changed('bannerHtml', html)) { el.innerHTML = html; icons(); }
}

/* ---------------- overview ---------------- */

function renderOverviewShell() {
    $('view').innerHTML = `
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 lg:gap-5 min-h-0 min-w-0">
            <div class="lg:col-span-8 flex flex-col gap-4 lg:gap-5 min-h-0 min-w-0">
                <div id="approvals" class="shrink-0"></div>
                <div id="metrics" class="grid grid-cols-1 sm:grid-cols-3 gap-3.5 lg:gap-4 shrink-0"></div>
                <div class="bg-white p-4 sm:p-5 rounded-3xl card-shadow border border-gray-100 flex flex-col shrink-0">
                    <div class="flex items-center justify-between shrink-0 mb-1 gap-3">
                        <h3 class="font-extrabold text-gray-900 text-base sm:text-lg tracking-tight">Hours</h3>
                        <div class="flex items-center gap-3">
                            <span class="hidden sm:flex items-center gap-1.5 text-[11px] font-semibold text-gray-500"><span class="w-2.5 h-2.5 rounded-full bg-limeAccent"></span>Agent</span>
                            <span class="hidden sm:flex items-center gap-1.5 text-[11px] font-semibold text-gray-500"><span class="w-2.5 h-2.5 rounded-full bg-purpleAccent"></span>Human</span>
                            <select id="hoursYear" class="text-xs font-semibold text-gray-600 bg-gray-50 px-2.5 py-1 rounded-full border border-gray-100 focus:outline-none"></select>
                        </div>
                    </div>
                    <div id="hours" class="h-40"></div>
                </div>
                <div class="bg-white p-5 sm:p-6 rounded-3xl card-shadow border border-gray-100 flex flex-col min-h-0 min-w-0">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between shrink-0 mb-4 gap-3">
                        <h3 class="font-extrabold text-gray-900 text-base sm:text-lg tracking-tight">Discrepancies</h3>
                        <div id="findingFilters"></div>
                    </div>
                    <div class="flex flex-col min-h-0 overflow-x-auto">
                        <div class="min-w-[640px]">
                            <div class="grid grid-cols-12 px-3 py-2 text-[11px] font-bold text-gray-400 uppercase tracking-wider border-b border-gray-100">
                                <div class="col-span-2">ID</div><div class="col-span-3">App</div><div class="col-span-2">Check</div>
                                <div class="col-span-1 text-right">Expected</div><div class="col-span-1 text-right">Actual</div><div class="col-span-1 text-right">Gap</div><div class="col-span-2 text-right">Status</div>
                            </div>
                            <div id="findingRows" class="overflow-y-auto custom-scroll pr-1 flex flex-col max-h-96 min-h-0 divide-y divide-gray-50"></div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="lg:col-span-4 bg-cardDark text-white p-4 sm:p-5 lg:p-6 rounded-3xl lg:rounded-4xl flex flex-col gap-5 shadow-xl min-h-0 min-w-0">
                <div class="flex flex-col min-h-0">
                    <div class="flex items-center justify-between mb-4 shrink-0">
                        <div><h3 class="text-lg font-bold tracking-tight">Audit Log</h3><p id="logCount" class="text-xs text-gray-400 font-medium"></p></div>
                    </div>
                    <div class="relative w-full shrink-0 bg-[#2B3048] border border-navy-2 rounded-3xl p-3 sm:p-4 shadow-xl">
                        <div id="logList" class="flex flex-col gap-1.5 max-h-[26rem] overflow-y-auto pr-1 text-xs dark-scroll"></div>
                    </div>
                </div>
                <div class="shrink-0">
                    <div class="flex items-center justify-between mb-3">
                        <div><h3 class="text-base font-bold">Agent Chain</h3><p id="agentCount" class="text-xs text-gray-400 font-medium"></p></div>
                    </div>
                    <div id="agentChain" class="flex flex-col gap-3"></div>
                </div>
            </div>
        </div>`;
    state.cache.agentHtml = null;
    renderApprovals(); renderMetrics(); renderHours(); renderFindings(); renderLog(); renderAgents();
}

/* ----- metric cards (GET /api/metrics, formula + inputs + labels behind "How we got this") ----- */

// Readable durations: 470 s -> "7.8 min", 280 h -> "280 h"
function duration(seconds) {
    const x = Number(seconds);
    if (seconds == null || isNaN(x)) return '—';
    if (x < 90) return `${x.toFixed(x < 10 ? 1 : 0)} s`;
    if (x < 5400) return `${(x / 60).toFixed(1)} min`;
    return `${(x / 3600).toLocaleString(undefined, { maximumFractionDigits: 1 })} h`;
}
const displayLabel = (l) => l === 'ASSUMPTION' ? 'industry estimate' : l;
const noAssume = (t) => String(t ?? '').replace(/ASSUMPTION/g, 'industry estimate');

// Claude Haiku 4.5 list price ($ per million tokens), the model inside CREAO's hosted agents.
// Used only when the API sends tokens but no dollar figure.
const HAIKU_45 = { in: 1.0, out: 5.0 };
function tokenEstimate(inputs) {
    const by = inputs?.creao_by_agent;
    if (!by) return null;
    let tin = 0, tout = 0;
    Object.values(by).forEach((a) => { tin += a.input_tokens || 0; tout += a.output_tokens || 0; });
    if (!tin && !tout) return null;
    return { usd: (tin * HAIKU_45.in + tout * HAIKU_45.out) / 1e6, tin, tout };
}

// Money for the Total Saved card: /api/metrics money.month / money.ytd, else the money_saved card and the run history
let savedPeriod = 'ytd';
function moneyPeriod(kind) {
    const m = state.metrics || {};
    const card = (m.cards || []).find((c) => c.id === 'money_saved')?.value || {};
    const src = m.money?.[kind] || (kind === 'month' ? card.month : card.ytd);
    if (src && typeof src === 'object') {
        return { usd: src.usd ?? src.total, months: src.months ?? src.by_month?.length, annual: src.annualized ?? src.annualized_projection,
            run_rate: src.run_rate_annual ?? src.run_rate ?? src.annualized, by_month: src.by_month };
    }
    if (kind === 'month') return { usd: card.monthly ?? card.usd, annual: card.annualized ?? card.annualized_projection };
    const runs = state.score?.runs || [];
    if (!runs.length) return null;
    const year = (String(runs[runs.length - 1].label || '').match(/\d{4}/) || [])[0];
    const these = runs.filter((r) => !year || String(r.label || r.period || '').includes(year));
    const usd = these.reduce((t, r) => t + (Number(r.dollars) || 0), 0);
    return { usd, months: these.length, run_rate: these.length ? usd / these.length * 12 : null };
}

function metricCards(m) {
    const cards = m && Array.isArray(m.cards) ? m.cards : [];
    const by = Object.fromEntries(cards.map((c) => [c.id, c]));
    const out = [];
    const a = by.accuracy, c = by.cost, s = by.money_saved;
    if (s) {
        const mo = moneyPeriod('month'), ytd = moneyPeriod('ytd');
        const cur = savedPeriod === 'ytd' && ytd ? ytd : mo;
        const toggle = `<span class="flex items-center justify-between gap-2"><span>Total Saved</span>
            <span class="inline-flex rounded-full bg-white/10 p-0.5 text-[10px] font-bold">${[['month', 'Month'], ['ytd', 'YTD']].map(([k, t]) => `
                <button data-saved="${k}" class="px-2 py-0.5 rounded-full ${(savedPeriod === k && (k === 'month' || ytd)) ? 'bg-purpleAccent text-white' : 'text-gray-400 hover:text-white'}">${t}</button>`).join('')}</span></span>`;
        const short = (n) => money(n).replace(/\.\d\d$/, '');
        out.push({ id: 'money_saved', label: 'Total Saved', labelHtml: toggle, icon: 'wallet', dark: true, value: money(cur?.usd),
            sub: cur === ytd ? `${num(ytd.months)} month${ytd.months === 1 ? '' : 's'}${ytd.run_rate != null ? ` · ≈ ${short(ytd.run_rate)}/yr run rate` : ''}`
                : cur?.annual != null ? `≈ ${short(cur.annual)} / year` : '' });
    }
    if (c) {
        const v = c.value || {};
        const est = v.usd != null ? null : (v.usd_estimate != null ? { usd: v.usd_estimate } : tokenEstimate(c.inputs));
        const usd = v.usd != null ? v.usd : est?.usd;
        const fmt = (n) => n === 0 ? '$0' : n != null && n < 1 ? `$${Number(n).toFixed(3)}` : money(n);
        out.push({ id: 'cost', label: 'Agent Cost', icon: 'cpu', value: fmt(usd), sub: est ? 'estimated from tokens' : '' });
    }
    if (a) {
        const v = a.value || {};
        out.push({ id: 'accuracy', label: 'Accuracy', icon: 'target', value: pct(v.accuracy), sub: `${num(v.caught)}/${num(v.total)} caught` });
    }
    return out;
}

function renderMetrics() {
    const el = $('metrics');
    if (!el) return;
    const cards = metricCards(state.metrics);
    if (!cards.length) {
        const s = state.score;
        el.innerHTML = s ? [
            statCard({ label: 'Traps caught', value: `${num(s.caught)}/${num(s.total)}`, sub: `${num(s.missed)} missed`, icon: 'target', dark: true }),
            statCard({ label: 'Recovered', value: money(s.recovered), sub: 'recoverable gaps found', icon: 'wallet' }),
            statCard({ label: 'Sent to a human', value: num(s.to_human), sub: 'judges disagreed', icon: 'user' }),
            statCard({ label: 'False alarms', value: num(s.false_alarms), sub: 'flagged but correct', icon: 'check' }),
        ].slice(0, 3).join('') : `<div class="sm:col-span-3">${whiteCard(empty('Loading…'))}</div>`;
        return icons();
    }
    el.innerHTML = cards.map((c) => statCard({ ...c, how: c.id })).join('');
    icons();
}

const LABEL_PILL = { MEASURED: 'bg-emerald-100 text-emerald-800', ASSUMPTION: 'bg-sky-100 text-sky-800', SIMULATED: 'bg-sky-100 text-sky-800', PROJECTION: 'bg-purpleAccent/10 text-purpleAccent' };

function showHow(id) {
    const card = (state.metrics?.cards || []).find((c) => c.id === id);
    if (!card) return;
    const kv = (o) => Object.entries(o || {}).filter(([, v]) => v == null || typeof v !== 'object').map(([k, v]) => `
        <div class="flex justify-between gap-3 py-1 border-b border-gray-50"><span class="text-gray-500">${esc(label(k))}</span>
        <span class="font-semibold text-gray-900 text-right">${v === 'ASSUMPTION' ? pill(LABEL_PILL.ASSUMPTION, displayLabel(v)) : esc(typeof v === 'number' ? num(v) : v)}</span></div>`).join('');
    const est = id === 'cost' && card.value?.usd == null ? tokenEstimate(card.inputs) : null;
    openModal(noAssume(card.title), `
        <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-1">Formula</p>
        <p class="text-sm text-gray-800 leading-relaxed mb-4">${esc(noAssume(card.formula))}</p>
        ${est ? `<p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-1">Estimate from tokens</p>
            <p class="text-sm text-gray-800 mb-4">${num(est.tin)} input + ${num(est.tout)} output tokens × Claude Haiku 4.5 list price ($${HAIKU_45.in}/$${HAIKU_45.out} per million) = <b>$${est.usd.toFixed(4)}</b></p>` : ''}
        <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-1">Value</p>
        <div class="text-xs mb-4">${kv(card.value)}</div>
        <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-1">Inputs</p>
        <div class="text-xs mb-4">${kv(card.inputs)}</div>
        <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-1">Sources</p>
        <div class="space-y-2 mb-4">${(card.sources || []).map((s) => `
            <div class="rounded-2xl bg-lightCardBg p-3 text-xs">
                <div class="flex items-center justify-between gap-2"><span class="font-bold text-gray-800">${esc(label(s.what))}</span>${pill(LABEL_PILL[s.label] || 'bg-gray-100 text-gray-600', displayLabel(s.label) || 'source')}</div>
                ${s.claim ? `<p class="mt-1 text-gray-700">${esc(noAssume(s.claim))}</p>` : ''}
                <p class="mt-1 text-gray-500 break-words">${esc(s.source || '')}</p>
            </div>`).join('')}</div>
        ${(card.notes || []).length ? `<p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-1">Notes</p><ul class="list-disc pl-5 space-y-1 text-xs">${card.notes.map((n) => `<li>${esc(noAssume(n))}</li>`).join('')}</ul>` : ''}`);
}

/* ----- learning curve (GET /api/score runs, month labels) ----- */

function renderCurve(id, tall = false) {
    const el = $(id);
    if (!el) return;
    const runs = (state.score?.runs || []).slice(-8);
    if (!runs.length) { el.innerHTML = empty('No runs yet: the curve fills after a run.', 'bar-chart-3'); return icons(); }
    const max = Math.max(1, ...runs.map((r) => Math.max(r.to_human || 0, r.by_rule || 0)));
    const h = tall ? 160 : 96;
    el.innerHTML = `<div class="h-full flex items-end justify-between px-1 gap-1.5 pt-6">${runs.map((r, i) => {
        const last = i === runs.length - 1;
        const hh = Math.max(6, Math.round(h * (r.to_human || 0) / max)), rh = Math.max(6, Math.round(h * (r.by_rule || 0) / max));
        return `<div class="flex flex-col items-center gap-1.5 flex-1 relative group min-w-0">
            <div class="absolute -top-6 left-1/2 -translate-x-1/2 bg-black text-white text-[10px] font-bold px-2 py-0.5 rounded-full shadow-md z-10 whitespace-nowrap ${last ? '' : 'opacity-0 group-hover:opacity-100'} transition-opacity pointer-events-none">Rules: ${num(r.by_rule)} | Human: ${num(r.to_human)}</div>
            <div class="flex items-end justify-center w-full gap-1.5">
                <div class="w-full max-w-[32px] rounded-full ${last ? 'bg-limeAccent' : 'striped-bar'}" style="height:${rh}px"></div>
                <div class="w-full max-w-[32px] rounded-full ${last ? 'bg-purpleAccent' : 'bg-purpleAccent/40'}" style="height:${hh}px"></div>
            </div>
            <span class="text-[10px] sm:text-xs ${last ? 'font-bold text-gray-900' : 'font-semibold text-gray-400'} truncate max-w-full">${esc(shortMonth(r.label || r.period || r.at))}</span>
        </div>`;
    }).join('')}</div>`;
}
// Hours per month: agent = measured run time; human = industry estimate per store-week × stores × weeks in the month
let hoursYear = null;
function hoursRows() {
    const bm = state.metrics?.money?.ytd?.by_month;
    if (Array.isArray(bm) && bm.length) {
        return bm.map((r, i) => {
            const lbl = r.label || r.month || r.period || '';
            return { label: lbl, year: (String(lbl).match(/\d{4}/) || [String(r.at || '').slice(0, 4)])[0] || String(new Date().getFullYear()),
                agent: r.agent_hours ?? null, approx: false, human: r.human_hours_est ?? r.human_hours ?? null, last: i === bm.length - 1 };
        });
    }
    const runs = state.score?.runs || [];
    const inputs = (state.metrics?.cards || []).find((c) => c.id === 'agent_time')?.inputs || {};
    const minPer = inputs.minutes_per_store_week || 60;
    return runs.map((r, i) => {
        const m = String(r.source || '').match(/(\d+) stores x (\d+) days/);
        const stores = r.stores ?? (m ? +m[1] : inputs.stores);
        const days = r.days ?? (m ? +m[2] : (inputs.weeks || 4) * 7);
        const last = i === runs.length - 1;
        let secs = r.seconds ?? r.agent_seconds ?? (last ? inputs.agent_seconds : null);
        let approx = false;
        if (secs == null && i > 0) {   // back-to-back runs: time since the previous run finished
            const d = (new Date(r.at) - new Date(runs[i - 1].at)) / 1000;
            if (d > 0 && d < 7200) { secs = d; approx = true; }
        }
        const year = (String(r.label || r.period || '').match(/\d{4}/) || [String(r.at || '').slice(0, 4)])[0];
        return { label: r.label || r.period || '', year, agent: secs != null ? secs / 3600 : null, approx,
            human: stores && days ? stores * (days / 7) * minPer / 60 : null, last };
    });
}
const hrs = (h) => h == null ? '—' : h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : `${Math.round(h).toLocaleString()} hrs`;
function renderHours() {
    const el = $('hours');
    if (!el) return;
    const all = hoursRows();
    const years = [...new Set(all.map((r) => r.year).filter(Boolean))];
    if (!hoursYear || !years.includes(hoursYear)) hoursYear = years[years.length - 1] || null;
    $('hoursYear').innerHTML = years.map((y) => `<option ${y === hoursYear ? 'selected' : ''}>${esc(y)}</option>`).join('') || '<option>—</option>';
    const rows = all.filter((r) => r.year === hoursYear).slice(-12);
    if (!rows.length) { el.innerHTML = empty('Hours fill in after the first run.', 'bar-chart-3'); return icons(); }
    const max = Math.max(1, ...rows.map((r) => Math.max(r.human || 0, r.agent || 0)));
    const H = 110;
    el.innerHTML = `<div class="h-full flex items-end justify-between px-1 gap-2 pt-7">${rows.map((r, i) => {
        const last = i === rows.length - 1;
        const ah = r.agent == null ? 0 : Math.max(6, Math.round(H * r.agent / max)), hh = Math.max(6, Math.round(H * (r.human || 0) / max));
        return `<div class="flex flex-col items-center gap-1.5 flex-1 relative group min-w-0">
            <div class="absolute -top-7 left-1/2 -translate-x-1/2 bg-cardDark text-white text-[10px] font-bold px-2 py-0.5 rounded-full shadow-md z-10 whitespace-nowrap ${last ? '' : 'opacity-0 group-hover:opacity-100'} transition-opacity pointer-events-none">Agent: ${r.approx ? '≈' : ''}${hrs(r.agent)} | Human: ${hrs(r.human)}</div>
            <div class="flex items-end justify-center w-full gap-1.5">
                <div class="w-full max-w-[30px] rounded-full ${r.agent == null ? 'striped-bar' : 'bg-limeAccent'}" style="height:${ah || 6}px"></div>
                <div class="w-full max-w-[30px] rounded-full ${last ? 'bg-purpleAccent' : 'hatched-blue'}" style="height:${hh}px"></div>
            </div>
            <span class="text-[10px] sm:text-xs ${last ? 'font-bold text-gray-900' : 'font-semibold text-gray-400'} truncate max-w-full">${esc(shortMonth(r.label).split(' ')[0])}</span>
        </div>`;
    }).join('')}</div>`;
}

function shortMonth(s) {
    const m = String(s || '').match(/^([A-Z][a-z]+) (\d{4})$/);
    return m ? `${m[1].slice(0, 3)} ${m[2].slice(2)}` : String(s || '').slice(0, 10);
}

/* ----- discrepancies (findings) ----- */

// One status pill per row: what it needs, or where it got to
function rowPill(f) {
    if (needsHuman(f)) return `<span title="${esc(VERDICT.conflict[1])}">${pill('bg-purpleAccent text-white', 'Needs you')}</span>`;
    if (['FILED', 'RECOVERED'].includes(f.state)) return pill(STATE_PILL[f.state], f.state === 'FILED' ? 'Filed' : 'Recovered');
    if (f.state === 'REJECTED') return pill(STATE_PILL.REJECTED, 'Closed');
    if (f.outcome === 'recoverable') return `<span title="${esc(VERDICT.recoverable[1])}">${pill(OUTCOME.recoverable, 'Claim it')}</span>`;
    return f.outcome ? pill(OUTCOME[f.outcome] || 'bg-gray-100 text-gray-600', label(f.outcome)) : pill('bg-gray-100 text-gray-600', label(f.state).toLowerCase());
}
const needsHuman = (f) => f.needs_human ?? (f.state === 'JUDGED');

// GET /api/approvals when the API has it; otherwise the findings waiting for a person
function approvalItems() {
    const a = state.approvals;
    const list = Array.isArray(a) ? a : a && (a.items || a.approvals || a.pending);
    if (Array.isArray(list)) return list;
    return state.findings.filter(needsHuman).map((f) => ({ kind: 'finding', finding_id: fidOf(f), gap: f.gap, channel: f.channel,
        check_type: f.check_type, outcome: f.outcome, orders_label: f.orders_label }));
}
function approvalRow(it, dark = false) {
    const id = it.finding_id || it.id;
    const what = it.title || (it.kind === 'follow_up' ? 'Vendor follow-up email' : `${label(it.check_type || it.kind || 'finding')} on ${chanName(it.channel)}`);
    return `<button data-finding="${esc(id)}" class="w-full text-left flex items-center justify-between gap-3 p-3 rounded-2xl ${dark ? 'bg-white/10 hover:bg-white/20' : 'bg-lightCardBg hover:bg-gray-100'} transition">
        <span class="flex items-center gap-2.5 min-w-0">${it.channel ? chanBadge(it.channel) : `<span class="w-6 h-6 rounded-full bg-purpleAccent text-white inline-flex items-center justify-center shrink-0"><i data-lucide="${it.kind === 'follow_up' ? 'mail' : 'scale'}" class="w-3.5 h-3.5"></i></span>`}
            <span class="min-w-0"><span class="block text-sm font-bold truncate">${esc(what)}</span>
            <span class="block text-[11px] ${dark ? 'text-gray-300' : 'text-gray-500'} font-mono truncate">${esc(id)}${it.orders_label ? ` · ${esc(it.orders_label)}` : ''}</span></span></span>
        <span class="font-extrabold tabular-nums shrink-0 ${dark ? 'text-limeAccent' : 'text-red-600'}">${it.gap != null ? money(it.gap) : it.amount != null ? money(it.amount) : ''}</span></button>`;
}
function renderQueueCount() {
    const n = approvalItems().length;
    const el = $('queueCount');
    el.textContent = n;
    el.classList.toggle('hidden', !n);
    if (!$('bellPanel').classList.contains('hidden')) renderBell();
}
function renderBell() {
    const items = approvalItems();
    $('bellPanel').innerHTML = `<div class="flex items-center justify-between px-1 pb-2"><p class="font-extrabold text-gray-900">Needs your approval <span class="text-purpleAccent">${items.length}</span></p>
        <span class="text-[11px] text-gray-400 font-semibold">acting as ${esc(settings.actor)}</span></div>
        <div class="flex flex-col gap-2 max-h-[60vh] overflow-y-auto custom-scroll">${items.map((it) => approvalRow(it)).join('') || '<p class="text-sm text-gray-400 text-center py-6">Nothing waiting. The agents settled everything else.</p>'}</div>`;
    icons();
}
function toggleBell(force) {
    const p = $('bellPanel');
    const open = force ?? p.classList.contains('hidden');
    p.classList.toggle('hidden', !open);
    if (open) renderBell();
}
function renderApprovals() {
    const el = $('approvals');
    if (!el) return;
    const items = approvalItems();
    const total = items.reduce((t, it) => t + (Number(it.gap ?? it.amount) || 0), 0);
    const html = items.length ? `<section data-test="approvals" class="bg-purpleAccent text-white rounded-3xl p-4 sm:p-5 shadow-md">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
            <h3 class="text-lg font-extrabold">Needs you (${items.length}) <span class="text-white/70 font-bold">· ${money(total)}</span></h3></div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-2">${items.slice(0, 4).map((it) => approvalRow(it, true)).join('')}</div>
        ${items.length > 4 ? `<button data-bell class="mt-2 text-xs font-bold text-limeAccent">+${items.length - 4} more</button>` : ''}</section>` : '';
    if (changed('approvalsHtml', html)) { el.innerHTML = html; icons(); }
}

function renderFindings() {
    const el = $('findingRows');
    if (!el) return;
    const all = state.findings;
    const count = (fn) => all.filter(fn).length;
    $('findingFilters').innerHTML = filterPills([
        ['all', 'All', all.length], ['review', 'Needs you', count(needsHuman)],
        ['recoverable', 'Claim it', count((f) => f.outcome === 'recoverable')],
        ['filed', 'Filed', count((f) => f.state === 'FILED')],
    ], state.findingFilter, 'data-ffilter');
    const rows = all.filter((f) => state.findingFilter === 'all' ? true : state.findingFilter === 'review' ? needsHuman(f)
        : state.findingFilter === 'filed' ? f.state === 'FILED' : f.outcome === state.findingFilter);
    const headline = state.showtime?.headline?.finding_id;
    el.innerHTML = rows.length ? rows.map((f) => `
        <div data-finding="${esc(fidOf(f))}" class="grid grid-cols-12 items-center px-3 py-3 hover:bg-gray-50/80 transition cursor-pointer text-xs sm:text-sm font-medium text-gray-800 ${fidOf(f) === headline ? 'bg-purpleAccent/5' : ''}">
            <div class="col-span-2 font-mono font-bold text-gray-900 text-xs truncate">${esc(fidOf(f))}</div>
            <div class="col-span-3 flex items-center gap-2 font-normal text-gray-700 min-w-0">${chanBadge(f.channel)}<span class="truncate">${esc(chanName(f.channel))}</span></div>
            <div class="col-span-2 font-normal text-gray-500 lowercase truncate" title="${esc(checkHelp(f.check_type))}">${esc(label(f.check_type))}</div>
            <div class="col-span-1 text-right font-mono font-normal text-gray-500 tabular-nums">${money(f.expected).replace(/\.\d\d$/, '')}</div>
            <div class="col-span-1 text-right font-mono font-normal text-gray-500 tabular-nums">${money(f.actual).replace(/\.\d\d$/, '')}</div>
            <div class="col-span-1 text-right font-mono font-bold tabular-nums ${f.gap > 0 ? 'text-red-600' : 'text-gray-900'}">${money(f.gap)}</div>
            <div class="col-span-2 text-right">${rowPill(f)}</div>
        </div>`).join('')
        : `<div class="py-8 text-center text-gray-400 text-sm">No findings ${state.findingFilter === 'all' ? 'yet' : 'in this view'}.</div>`;
}

/* ----- audit log (dark column) ----- */

function logIcon(e) {
    if (e.allowed === false) return ['bg-red-600', 'shield-alert'];
    if (e.role === 'human') return ['bg-limeAccent text-black', 'user'];
    if (e.role === 'gatekeeper' || e.actor === 'gatekeeper') return ['bg-sky-600', 'shield-check'];
    if (e.actor === 'clicky') return ['bg-orange-600', 'mouse-pointer-click'];
    return ['bg-purpleAccent', 'bot'];
}

function logRow(e, fresh) {
    const refused = e.allowed === false;
    const [bg, icon] = logIcon(e);
    const move = e.before || e.after ? `${esc(e.before || '∅')} → ${esc(e.after || '∅')}` : '';
    const tag = refused ? ['bg-red-500/20 text-red-300', 'Refused'] : e.role === 'human' ? ['bg-limeAccent/20 text-limeAccent', 'Human'] : ['bg-purpleAccent/30 text-purple-200', label(e.role || 'agent')];
    return `
        <button data-finding="${e.subject_type === 'finding' ? esc(e.subject_id) : ''}" class="w-full text-left flex items-start justify-between gap-3 p-3 rounded-2xl ${refused ? 'bg-red-500/10 border border-red-500/30' : 'bg-navy-1/70 hover:bg-navy-1'} transition ${fresh ? 'flash' : ''}">
            <div class="flex items-start gap-3 min-w-0">
                <div class="w-9 h-9 rounded-xl ${bg} shrink-0 flex items-center justify-center text-white shadow-sm"><i data-lucide="${icon}" class="w-4 h-4"></i></div>
                <div class="flex flex-col min-w-0 gap-0.5">
                    <div class="flex items-center gap-2 min-w-0">
                        <span class="font-bold text-gray-100 text-xs truncate">${esc(e.actor)}</span>
                        <span class="text-[9px] font-extrabold px-1.5 py-0.5 rounded uppercase tracking-wider ${tag[0]}">${esc(tag[1])}</span>
                    </div>
                    <p class="text-[11px] font-medium text-gray-300 truncate"><span class="font-bold text-white">${esc(e.action)}</span> ${esc(e.subject_id || '')} <span class="${refused ? 'text-red-400 line-through' : 'text-gray-400'}">${move}</span></p>
                    ${e.reason ? `<p class="text-[11px] text-gray-500 leading-snug line-clamp-2">${esc(e.reason)}</p>` : ''}
                </div>
            </div>
            <span class="text-[10px] font-semibold text-gray-400 whitespace-nowrap shrink-0 tabular-nums">${esc(clock(e.at))}</span>
        </button>`;
}

function shortAction(e) {
    const id = e.subject_id || '';
    const verbs = { file: 'Filed claim', approve: 'Approved', reject: 'Rejected', judge: 'Judged', check: 'Checked', rank: 'Ranked',
        draft: 'Drafted', send: 'Sent', follow_up: 'Followed up', learn: 'Learned rule', investigate: 'Investigated' };
    const v = verbs[e.action] || label(e.action).replace(/^./, (c) => c.toUpperCase());
    return e.allowed === false ? `Refused: ${label(e.action)} ${id}` : `${v} ${id}`;
}
function logLine(e, fresh) {
    const human = e.role === 'human', refused = e.allowed === false;
    return `<button data-finding="${e.subject_type === 'finding' ? esc(e.subject_id) : ''}" class="w-full text-left flex items-center gap-2.5 px-3 py-2.5 rounded-2xl ${refused ? 'bg-red-500/15' : 'bg-navy-1/70 hover:bg-navy-1'} transition ${fresh ? 'flash' : ''}">
        <span class="font-bold text-gray-100 text-xs truncate max-w-[34%]">${esc(e.actor)}</span>
        <span class="text-[9px] font-extrabold px-1.5 py-0.5 rounded uppercase tracking-wider shrink-0 ${refused ? 'bg-red-500/30 text-red-200' : human ? 'bg-limeAccent/25 text-limeAccent' : 'bg-purpleAccent/40 text-blue-100'}">${refused ? 'refused' : human ? 'human' : 'agent'}</span>
        <span class="text-[11px] text-gray-300 truncate flex-1 min-w-0">${esc(shortAction(e))}</span>
        <span class="text-[10px] font-semibold text-gray-500 whitespace-nowrap shrink-0 tabular-nums">${esc(clock(e.at).replace(/:\d\d(\s|$)/, '$1'))}</span></button>`;
}

function renderLog() {
    const el = $('logList');
    if (!el) return;
    const entries = state.log.slice(-200).reverse();
    const fresh = state.seenLog ? state.log.length - state.seenLog : 0;
    state.seenLog = state.log.length;
    const refused = state.log.filter((e) => e.allowed === false).length;
    $('logCount').textContent = `${num(state.log.length)} entries`;
    el.innerHTML = entries.length ? entries.map((e, i) => logLine(e, i < fresh)).join('')
        : '<p class="text-gray-500 text-sm text-center py-8">Nothing has happened yet.</p>';
    icons();
}

/* ----- agent chain (her looped avatar grid) ----- */

const AGENT_ICON = {
    collector: 'download', formatter: 'table', matcher: 'git-compare-arrows', deal_keeper: 'file-text', checkers: 'calculator',
    anomaly: 'activity', investigator: 'search', panel: 'scale', ranker: 'list-ordered', dispute_writer: 'pen-line',
    gatekeeper: 'shield-check', clicky: 'mouse-pointer-click', follow_up: 'mail', journal: 'book-open', learner: 'graduation-cap',
};
const AGENT_COLORS = ['bg-[#012F8A]', 'bg-[#0146A9]', 'bg-[#0159C4]', 'bg-[#017AF0]', 'bg-[#0084FF]', 'bg-[#2EA7FF]', 'bg-[#4A628A]',
    'bg-[#5890CB]', 'bg-[#60AAEF]', 'bg-[#0146A9]', 'bg-[#017AF0]', 'bg-[#2EA7FF]', 'bg-[#0159C4]', 'bg-[#5890CB]', 'bg-[#012F8A]'];

const STATUS_DOT = { working: 'bg-limeAccent pulse-dot', done: 'bg-emerald-400', error: 'bg-red-500', idle: 'bg-gray-400' };
function agentStatus(ag) {
    const st = String(ag.status || ag.state || '').toLowerCase();
    if (['working', 'running', 'active', 'busy'].includes(st)) return 'working';
    if (['done', 'finished', 'complete', 'completed', 'ok'].includes(st)) return 'done';
    if (['error', 'failed'].includes(st)) return 'error';
    if (['idle', 'waiting', 'pending', ''].includes(st) || !st) {
        const recent = ag.last_at && (Date.now() - new Date(ag.last_at).getTime()) < 8000;
        if (recent && state.job && !state.job.finished) return 'working';
        if (ag.actor === 'clicky' && filedFindings().length) return 'done';
        return ag.actions ? 'done' : 'idle';
    }
    return st;
}
function agentStatusText(ag, st) {
    if (st === 'working') return ag.doing || ag.message || 'working…';
    if (st === 'done') return ag.actions ? `done · ${num(ag.actions)}` : 'done';
    if (st === 'error') return 'error';
    return 'idle';
}

function renderAgents() {
    const el = $('agentChain');
    if (!el || !state.agents) return;
    const list = state.agents.agents || [];
    const working = list.filter((a) => agentStatus(a) === 'working').length;
    $('agentCount').textContent = working ? `${working} working now` : `${list.length} agents`;
    const html = `<div class="grid grid-cols-5 gap-x-2 gap-y-3">${list.map((ag, i) => {
        const st = agentStatus(ag);
        return `<button data-agent="${esc(ag.actor)}" class="flex flex-col items-center gap-1 group min-w-0" title="${esc(ag.name)}: ${esc(agentStatusText(ag, st))}">
            <div class="relative w-11 h-11 rounded-full ${AGENT_COLORS[i % AGENT_COLORS.length]} flex items-center justify-center text-white border-2 ${st === 'working' ? 'border-limeAccent' : 'border-transparent'} group-hover:border-limeAccent transition shadow-sm">
                <i data-lucide="bot" class="w-5 h-5"></i>
                <span class="absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-cardDark ${STATUS_DOT[st] || 'bg-gray-500'}"></span>
            </div>
            <span class="text-[10px] font-semibold text-gray-300 truncate max-w-full">${esc(String(ag.name).split(' ')[0])}</span>
        </button>`;
    }).join('')}</div>`;
    if (changed('agentHtml', html)) { el.innerHTML = html; icons(); }
}

/* ---------------- lump sums (GET /api/payouts) ---------------- */

function renderLumpSums() {
    const el = $('lumpsums');
    if (!el) return;
    const d = state.payouts;
    if (!d) { el.innerHTML = whiteCard(empty('Loading lump sums…')); return icons(); }
    const s = d.summary || {};
    const rows = d.payouts || [];
    const headline = state.showtime?.lump_sum?.payout_id;
    const statusCounts = Object.entries(s.by_status || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${PAY_STATUS[k]?.[1] || label(k)}`).join(' · ');
    el.innerHTML = `
        <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3.5 lg:gap-4 mb-4 lg:mb-5">
            ${statCard({ label: 'Apps owe you', value: money(s.owed), icon: 'wallet', dark: true })}
            ${statCard({ label: 'Should have been paid', value: money(s.expected_net), icon: 'file-text' })}
            ${statCard({ label: 'Actually paid', value: money(s.received), icon: 'landmark' })}
            ${statCard({ label: 'Deposits', value: num(s.payouts), icon: 'banknote' })}
            ${statCard({ label: 'Needs your call', value: money(s.in_question), icon: 'scale' })}
        </div>
        ${whiteCard(`${cardHead('Deposits')}
            ${rows.length ? `<div class="overflow-x-auto"><div class="min-w-[860px]">
                <div class="grid grid-cols-12 px-3 py-2 text-[11px] font-bold text-gray-400 uppercase tracking-wider border-b border-gray-100">
                    <div class="col-span-1">Paid</div><div class="col-span-2">Platform</div><div class="col-span-2">Payout</div><div class="col-span-1 text-right">Orders</div>
                    <div class="col-span-2 text-right">Should have been</div><div class="col-span-2 text-right">Paid</div><div class="col-span-1 text-right">Missing</div><div class="col-span-1 text-right">Status</div>
                </div>
                <div class="flex flex-col divide-y divide-gray-50">${rows.map((p) => `
                    <a href="#/payout/${encodeURIComponent(p.payout_id)}" data-payout="${esc(p.payout_id)}" class="grid grid-cols-12 items-center px-3 py-3 hover:bg-gray-50/80 transition text-xs sm:text-sm font-medium text-gray-800 ${p.payout_id === headline ? 'bg-limeAccent/15 ring-1 ring-limeAccent rounded-2xl' : ''}">
                        <div class="col-span-1 text-gray-500 text-xs">${esc((p.deposit_date || p.payout_date || '').slice(5))}</div>
                        <div class="col-span-2 flex items-center gap-2 min-w-0">${chanBadge(p.channel)}<span class="truncate">${esc(p.platform || chanName(p.channel))}</span></div>
                        <div class="col-span-2 font-mono font-bold text-gray-900 text-xs truncate">${esc(p.payout_id)}</div>
                        <div class="col-span-1 text-right tabular-nums text-gray-500">${num(p.orders)}</div>
                        <div class="col-span-2 text-right font-mono tabular-nums text-gray-700">${money(p.expected_net)}${p.expected_net_computable ? '' : '<span class="text-gray-400" title="a deduction has no deal term behind it">*</span>'}</div>
                        <div class="col-span-2 text-right font-mono tabular-nums text-gray-500">${money(p.received)}</div>
                        <div class="col-span-1 text-right font-mono font-bold tabular-nums ${p.shortfall > 0.05 ? 'text-red-600' : 'text-gray-400'}">${money(p.shortfall)}</div>
                        <div class="col-span-1 text-right">${payPill(p.status)}</div>
                    </a>`).join('')}</div>
            </div></div>
            ${d.unpaid_orders?.count ? `<p class="text-[11px] text-gray-400 mt-3">${num(d.unpaid_orders.count)} sold-never-paid order(s) (${money(d.unpaid_orders.gross)}) are in no lump sum.</p>` : ''}`
            : empty(esc(d.note || 'No lump sums yet. They come from a dataset or showtime run.'))}`)}`;
    icons();
}

/* ---------------- one lump sum (GET /api/payouts/<id>) ---------------- */

async function loadPayout(id) {
    try {
        const p = await get(`/api/payouts/${encodeURIComponent(id)}`);
        if (route().name !== 'payout' || route().id !== id) return;
        if (changed('payout', p)) { state.payout = p; renderPayout(); }
    } catch (e) {
        if (route().name === 'payout' && $('payout')) $('payout').innerHTML = whiteCard(empty(`${esc(explain(e))} <a class="underline" href="#/lumpsums">Back to lump sums</a>`, 'triangle-alert'));
        icons();
    }
}

function waterfallSvg(steps) {
    const W = 1000, H = 380, top = 36, bottom = 64, left = 8;
    const vals = steps.flatMap((s) => [s.running, s.running_expected, Math.abs(s.amount || 0)]);
    const max = Math.max(1, ...vals.map((v) => Math.abs(Number(v) || 0)));
    const y = (v) => top + (H - top - bottom) * (1 - Math.max(0, Number(v) || 0) / max);
    const n = steps.length, slot = (W - left * 2) / n, bw = Math.min(30, slot * 0.3);
    let prevA = 0, prevE = 0;
    const short = (s) => s.label.replace(/ \(.*\)$/, '').replace('Gross sales on the statement', 'Gross').replace('Net per statement', 'Statement net')
        .replace('Expected net per contract', 'Expected net').replace("Promotions (store's share)", 'Promos').replace('Shortfall (expected - received)', 'Shortfall')
        .replace('Other gaps the checkers found', 'Other gaps').replace('Other statement adjustments', 'Adjustments').replace('Deposit received', 'Deposit')
        .replace('Error charges', 'Errors').replace('Tax withheld', 'Tax');
    const parts = steps.map((s, i) => {
        const x = left + i * slot + slot / 2;
        let g = '';
        if (s.kind === 'deduction' || s.kind === 'adjustment' || s.kind === 'gap') {
            const ha = Math.max(2, Math.abs(y(prevA) - y(s.running))), he = Math.max(2, Math.abs(y(prevE) - y(s.running_expected)));
            g += `<rect x="${x - bw - 2}" y="${Math.min(y(prevA), y(s.running))}" width="${bw}" height="${ha}" rx="6" fill="#0084FF"><title>${esc(s.label)}: statement ${money(s.amount)}</title></rect>`;
            g += `<rect x="${x + 2}" y="${Math.min(y(prevE), y(s.running_expected))}" width="${bw}" height="${he}" rx="6" fill="${s.computable === false ? '#E5E7EB' : '#61CDFF'}" stroke="#23263A" stroke-opacity=".15"><title>${esc(s.label)}: contract ${money(s.expected)}</title></rect>`;
            if (s.gap) g += `<text class="gap" x="${x}" y="${Math.min(y(prevA), y(prevE)) - 10}" text-anchor="middle">+${money(s.gap)}</text>`;
        } else if (s.key === 'shortfall') {
            const t = y(s.running_expected), b = y(s.running);
            g += `<rect x="${x - bw / 2}" y="${t}" width="${bw}" height="${Math.max(4, b - t)}" rx="6" fill="#DC2626"><title>shortfall ${money(s.amount)}</title></rect>`;
            g += `<text class="gap" x="${x}" y="${t - 10}" text-anchor="middle">${money(s.amount)}</text>`;
        } else {
            const v = s.key === 'expected_net' ? s.running_expected : s.amount;
            const fill = s.key === 'expected_net' ? '#61CDFF' : s.key === 'deposit' ? '#0084FF' : '#23263A';
            g += `<rect x="${x - bw / 2}" y="${y(v)}" width="${bw}" height="${Math.max(2, y(0) - y(v))}" rx="8" fill="${fill}"><title>${esc(s.label)} ${money(v)}</title></rect>`;
            g += `<text class="val" x="${x}" y="${y(v) - 10}" text-anchor="middle">${money(v).replace(/\.\d\d$/, '')}</text>`;
        }
        prevA = s.running; prevE = s.running_expected;
        return `${g}<text class="lbl" x="${x}" y="${H - bottom + 24}" text-anchor="middle">${esc(short(s))}</text>`;
    });
    return `<svg class="wf w-full h-auto" viewBox="0 0 ${W} ${H}" role="img" aria-label="Waterfall from gross to deposit">
        <line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" stroke="#E5E7EB" stroke-width="2"/>${parts.join('')}</svg>
        <div class="flex flex-wrap gap-4 text-[11px] font-semibold text-gray-500 mt-2">
            <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-purpleAccent"></span> statement (what they charged)</span>
            <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-limeAccent border border-black/10"></span> per contract (what the deal says)</span>
            <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-red-600"></span> shortfall</span>
            <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-cardDark"></span> totals</span></div>`;
}

// The gaps are tiny next to a six-figure deposit: spell each one out, zoomed, so it reads on a projector
function gapCallouts(p) {
    const rows = (p.waterfall || []).filter((s) => s.kind === 'deduction' && s.gap).map((s) => ({
        title: s.label, a: Math.abs(s.amount), aL: 'charged', b: Math.abs(s.expected), bL: 'contract says', gap: s.gap, verb: 'owed back', ids: s.finding_ids || [] }));
    if (p.shortfall > 0.005) rows.push({ title: 'Deposit vs expected net', a: p.expected_net, aL: 'expected', b: p.received, bL: 'received', gap: p.shortfall, verb: 'short', ids: [] });
    if (!rows.length) return '';
    return `<div class="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4" data-test="gap-callouts">${rows.map((r) => {
        const hi = Math.max(r.a, r.b), lo = Math.min(r.a, r.b);
        const floor = Math.max(0, lo - (hi - lo) * 3);   // zoom: the bars start just below the smaller figure
        const w = (v) => Math.max(4, Math.round(100 * (v - floor) / (hi - floor || 1)));
        return `<div class="rounded-2xl bg-lightCardBg p-4">
            <div class="flex items-center justify-between gap-2"><p class="font-bold text-gray-900 text-sm">${esc(r.title)}</p><p class="text-red-600 font-extrabold tabular-nums">${money(r.gap)} ${esc(r.verb)}</p></div>
            <div class="mt-3 space-y-1.5 text-xs">
                <div class="flex items-center gap-2"><span class="w-24 text-gray-500 font-semibold shrink-0">${esc(r.aL)}</span><div class="flex-1 h-3 rounded-full bg-white"><div class="h-3 rounded-full bg-purpleAccent" style="width:${w(r.a)}%"></div></div><span class="w-28 text-right font-mono tabular-nums">${money(r.a)}</span></div>
                <div class="flex items-center gap-2"><span class="w-24 text-gray-500 font-semibold shrink-0">${esc(r.bL)}</span><div class="flex-1 h-3 rounded-full bg-white"><div class="h-3 rounded-full bg-limeAccent border border-black/10" style="width:${w(r.b)}%"></div></div><span class="w-28 text-right font-mono tabular-nums">${money(r.b)}</span></div>
            </div>
            ${r.ids.length ? `<p class="text-xs mt-2 text-gray-500">Finding ${r.ids.map((f) => `<a href="#/finding/${encodeURIComponent(f)}" class="font-mono font-bold text-purpleAccent hover:underline">${esc(f)}</a>`).join(', ')}</p>` : ''}
            <p class="text-[10px] text-gray-400 mt-1">zoomed: bars start at ${money(floor)}</p></div>`;
    }).join('')}</div>`;
}

function renderPayout() {
    const el = $('payout');
    const p = state.payout;
    if (!el || !p) return;
    const ob = p.owed_breakdown || {};
    $('pageTitle').textContent = `${p.platform || chanName(p.channel)} ${p.payout_id}`;
    $('pageSub').textContent = `${p.deposit_id || 'no deposit'}${p.deposit_date ? ` on ${p.deposit_date}` : ''} · ${num(p.stores)} stores · ${num(p.orders)} orders in one deposit`;
    const steps = (p.waterfall || []);
    el.innerHTML = `
        <div class="flex items-center gap-2 text-xs font-semibold text-gray-500 mb-3"><a href="#/lumpsums" class="flex items-center gap-1 hover:text-gray-900"><i data-lucide="arrow-left" class="w-3.5 h-3.5"></i> Lump sums</a>
            <span>/</span><span class="font-mono">${esc(p.payout_id)}</span> ${payPill(p.status)}</div>
        <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3.5 lg:gap-4 mb-4 lg:mb-5">
            ${statCard({ label: 'Sales', value: money(p.gross), icon: 'banknote' })}
            ${statCard({ label: 'Should have been paid', value: money(p.expected_net), icon: 'file-text' })}
            ${statCard({ label: 'Actually paid', value: money(p.received), icon: 'landmark' })}
            ${statCard({ label: 'Missing', value: `<span class="${p.shortfall > 0.05 ? 'text-red-600' : ''}">${money(p.shortfall)}</span>`, icon: 'triangle-alert' })}
            ${statCard({ label: 'Apps owe you', value: money(ob.total), icon: 'wallet', dark: true })}
        </div>
        ${whiteCard(`${cardHead('Waterfall', 'Gross → each deduction (statement vs contract) → statement net → expected net → deposit → shortfall')}
            <div id="waterfall">${waterfallSvg(steps)}</div>
            ${gapCallouts(p)}
            <div class="overflow-x-auto mt-4"><table class="w-full text-left text-xs sm:text-sm min-w-[640px]">
                <thead class="text-gray-400 text-[11px] uppercase tracking-wider"><tr class="border-b border-gray-100">
                    <th class="py-2 pr-3">Step</th><th class="py-2 pr-3 text-right">Statement</th><th class="py-2 pr-3 text-right">Contract</th><th class="py-2 pr-3 text-right">Gap</th><th class="py-2">Basis / findings</th></tr></thead>
                <tbody class="divide-y divide-gray-50">${steps.map((s) => `<tr data-step="${esc(s.key)}">
                    <td class="py-2 pr-3 font-bold text-gray-900">${esc(s.label)}</td>
                    <td class="py-2 pr-3 text-right font-mono tabular-nums">${s.key === 'expected_net' || s.key === 'shortfall' ? '' : money(s.amount)}</td>
                    <td class="py-2 pr-3 text-right font-mono tabular-nums">${s.kind === 'deduction' ? (s.computable === false ? '<span class="text-gray-400">no deal term</span>' : money(s.expected)) : s.key === 'expected_net' ? money(s.amount) : ''}</td>
                    <td class="py-2 pr-3 text-right font-mono font-bold tabular-nums ${s.gap > 0 || (s.key === 'shortfall' && s.amount > 0.05) ? 'text-red-600' : 'text-gray-300'}">${s.kind === 'deduction' ? (s.gap ? money(s.gap) : '') : s.key === 'shortfall' ? money(s.amount) : ''}</td>
                    <td class="py-2 text-xs text-gray-500">${esc(s.basis || s.note || '')} ${(s.finding_ids || []).map((f) => `<a href="#/finding/${encodeURIComponent(f)}" class="font-mono font-bold text-purpleAccent hover:underline">${esc(f)}</a>`).join(' ')}</td>
                </tr>`).join('')}</tbody></table></div>
            <div class="mt-4 rounded-2xl bg-lightCardBg p-3 text-xs text-gray-600"><b class="text-gray-900">Apps owe you ${money(ob.total)}</b> = ${money(ob.open_gaps)} open recoverable gaps + ${money(ob.deposit_shortfall)} deposit shortfall. ${esc(ob.formula || '')}</div>`, 'mb-4 lg:mb-5')}
        ${whiteCard(`${cardHead('Findings inside this lump sum', `${(p.findings_detail || []).length} · click one for the case file`)}
            ${(p.findings_detail || []).length ? `<div class="flex flex-col divide-y divide-gray-50">${p.findings_detail.map((f) => `
                <div data-finding="${esc(f.finding_id)}" class="grid grid-cols-12 items-center gap-2 px-3 py-3 hover:bg-gray-50/80 cursor-pointer text-xs sm:text-sm">
                    <div class="col-span-3 font-mono font-bold text-gray-900">${esc(f.finding_id)}</div>
                    <div class="col-span-4 min-w-0"><div class="font-semibold">${esc(label(f.line || f.check_type))}</div><div class="text-[11px] text-gray-400 truncate">${esc(f.orders_label || f.store_id || '')}</div></div>
                    <div class="col-span-2 text-right font-mono font-bold text-red-600 tabular-nums">${money(f.gap)}</div>
                    <div class="col-span-3 flex flex-wrap gap-1 justify-end">${f.outcome ? verdictPill(f.outcome) : pill('bg-gray-100 text-gray-600', '—')}${pill(STATE_PILL[f.state] || 'bg-gray-100 text-gray-600', f.state)}${f.needs_human ? pill('bg-purpleAccent text-white', 'needs you') : ''}</div>
                </div>`).join('')}</div>` : empty('No findings in this lump sum: every line matched the deal.', 'check')}`, 'mb-4 lg:mb-5')}
        ${whiteCard(`${cardHead('By store', "Each store's part of the lump sum; stores with open gaps first")}
            <div class="overflow-x-auto max-h-96 overflow-y-auto custom-scroll"><table class="w-full text-left text-xs sm:text-sm min-w-[620px]">
                <thead class="text-gray-400 text-[11px] uppercase tracking-wider sticky top-0 bg-white"><tr class="border-b border-gray-100">
                    <th class="py-2 pr-3">Store</th><th class="py-2 pr-3 text-right">Orders</th><th class="py-2 pr-3 text-right">Gross</th><th class="py-2 pr-3 text-right">Deductions</th><th class="py-2 pr-3 text-right">Net</th><th class="py-2 pr-3 text-right">Open gaps</th><th class="py-2 text-right">Share</th></tr></thead>
                <tbody class="divide-y divide-gray-50">${[...(p.stores_detail || [])].sort((a, b) => (b.open_gaps || 0) - (a.open_gaps || 0) || String(a.store_id).localeCompare(b.store_id)).map((s) => `
                    <tr class="${s.open_gaps ? 'bg-red-50/60' : ''}"><td class="py-2 pr-3 font-mono font-bold">${esc(s.store_id)}</td><td class="py-2 pr-3 text-right tabular-nums">${num(s.orders)}</td>
                    <td class="py-2 pr-3 text-right font-mono tabular-nums">${money(s.gross)}</td><td class="py-2 pr-3 text-right font-mono tabular-nums">${money(s.deductions)}</td>
                    <td class="py-2 pr-3 text-right font-mono tabular-nums">${money(s.net)}</td><td class="py-2 pr-3 text-right font-mono font-bold tabular-nums ${s.open_gaps ? 'text-red-600' : 'text-gray-300'}">${s.open_gaps ? money(s.open_gaps) : ''}</td>
                    <td class="py-2 text-right tabular-nums text-gray-500">${pct(s.share_of_gross)}</td></tr>`).join('')}</tbody></table></div>`)}`;
    icons();
}

/* ---------------- agent theater (GET /api/stream, /api/stream/replay) ---------------- */

const TH_JUDGES = ['strict', 'operator', 'skeptic'];
const theater = { mode: 'replay', speed: 2, cursor: 0, start: null, run: null, balances: {}, prevBal: {}, cols: {}, feed: [],
    human: new Map(), banner: null, verdict: null, timer: null, count: 0, done: false, busy: false };
function theaterClear() {
    Object.assign(theater, { cursor: 0, start: null, run: null, balances: {}, prevBal: {}, feed: [], human: new Map(),
        banner: null, verdict: null, count: 0, done: false });
    theater.cols = Object.fromEntries(TH_JUDGES.map((j) => [j, []]));
}
theaterClear();
const judgeKey = (j) => String(j || '').replace(/^judge\./, '');

function theaterApply(e) {
    theater.count++;
    if (e.balances) { theater.prevBal = { ...theater.balances }; theater.balances = { ...e.balances }; }
    if (e.run) theater.run = e.run;
    const col = theater.cols[judgeKey(e.judge)];
    if (col && ['vote', 'revision', 'bet_staked', 'bet_settled', 'second_opinion'].includes(e.type)) { col.unshift(e); col.length = Math.min(col.length, 10); }
    if (e.type === 'verdict' || e.type === 'rule_applied') theater.verdict = e;
    if (e.type === 'escalation') theater.human.set(e.finding_id, { ...e, decision: null });
    if (e.type === 'human_decision') {
        const c = theater.human.get(e.finding_id);
        if (c) c.decision = e; else theater.human.set(e.finding_id, { finding_id: e.finding_id, gap: e.gap, decision: e, text: e.text });
    }
    if (/^rule_/.test(e.type)) theater.banner = e;
    if (e.lane === 'agents' || e.lane === 'learn' || e.type === 'denied' || /_bulk$/.test(e.type) || e.type === 'filed' || e.type === 'human_decision') {
        theater.feed.unshift(e); theater.feed.length = Math.min(theater.feed.length, 14);
    }
}

async function theaterPoll() {
    if (theater.mode === 'live') {
        const d = await get(`/api/stream?since=${theater.cursor}&limit=300`);
        if (d.reset) { theaterClear(); theater.balances = d.balances || {}; }
        d.events.forEach(theaterApply);
        theater.cursor = d.cursor; theater.run = d.run || theater.run;
        if (!theater.count) theater.balances = d.balances || {};
        return d.events.length > 0 || d.reset;
    }
    if (theater.done) return false;
    const q = theater.start == null ? `speed=${theater.speed}` : `speed=${theater.speed}&since=${theater.cursor}&start=${theater.start}`;
    const d = await get(`/api/stream/replay?run=last&${q}`);
    theater.start = d.start; theater.run = d.run || theater.run;
    d.events.forEach(theaterApply);
    theater.cursor = d.cursor; theater.done = d.done;
    return d.events.length > 0 || d.done;
}

function theaterStop() { clearInterval(theater.timer); theater.timer = null; }
function theaterStart(mode) {
    theater.mode = mode || theater.mode;
    theaterClear();
    theaterStop();
    const tick = async () => {
        if (route().name !== 'theater' || theater.busy) return;
        theater.busy = true;
        try { if (await theaterPoll()) drawTheater(); } catch { /* keep trying */ } finally { theater.busy = false; }
    };
    theater.timer = setInterval(tick, theater.mode === 'live' ? 1000 : 250);
    drawTheater();
    tick();
}

function renderTheaterShell() {
    $('view').innerHTML = `<div id="theater" class="bg-cardDark text-white rounded-3xl lg:rounded-4xl p-4 sm:p-6 shadow-xl flex flex-col gap-4 min-w-0"></div>`;
}

const thCardCls = 'rounded-2xl bg-navy-1/70 p-3';
function thCard(e) {
    const f = e.finding_id ? `<a href="#/finding/${encodeURIComponent(e.finding_id)}" class="font-mono text-xs text-gray-400 hover:text-white">${esc(e.finding_id)}</a>` : '';
    if (e.type === 'vote' || e.type === 'revision' || e.type === 'second_opinion') {
        const out = e.type === 'revision' ? `${e.from} → ${e.to}` : (e.outcome || '');
        return `<div class="${thCardCls} ${e.type === 'revision' ? 'ring-1 ring-amber-400/60' : ''}">
            <div class="flex items-center justify-between gap-2">${pill(OUTCOME[e.to || e.outcome] || 'bg-gray-100 text-gray-700', out)}<span class="text-lg font-extrabold tabular-nums">${pct(e.confidence)}</span></div>
            <div class="h-1.5 rounded-full bg-navy-2 mt-2"><div class="h-1.5 rounded-full bg-limeAccent" style="width:${Math.round((e.confidence || 0) * 100)}%"></div></div>
            <div class="flex items-center justify-between mt-1.5 text-[11px] text-gray-400">${f}<span>${e.type === 'revision' ? 'changed after debate' : e.round ? `round ${esc(e.round)}` : esc(e.model || '')}</span></div></div>`;
    }
    if (e.type === 'bet_staked')
        return `<div class="${thCardCls}"><p class="text-sm"><i data-lucide="coins" class="w-3.5 h-3.5 inline -mt-0.5 text-limeAccent"></i> stakes <b>${esc(e.stake)}</b> on ${esc(e.outcome)}</p>
            <div class="flex items-center justify-between mt-1 text-[11px] text-gray-400">${f}<span>+${esc(e.to_win)} / −${esc(e.at_risk)}</span></div></div>`;
    const good = e.payout > 0, bad = e.payout < 0;
    return `<div class="${thCardCls}"><p class="text-sm"><b class="${good ? 'text-limeAccent' : bad ? 'text-red-400' : 'text-gray-400'} text-base tabular-nums">${e.payout > 0 ? '+' : ''}${esc(e.payout)}</b> ${esc(e.result)}</p>
        <div class="flex items-center justify-between mt-1 text-[11px] text-gray-400">${f}<span>truth ${esc(e.truth)} · bal ${esc(e.balance)}</span></div></div>`;
}

function drawTheater() {
    const el = $('theater');
    if (!el || route().name !== 'theater') return;
    const bal = (j) => {
        const b = theater.balances[j], p = theater.prevBal[j];
        const d = b != null && p != null ? b - p : 0;
        return `<div class="rounded-3xl bg-[#2B3048] border ${d > 0 ? 'border-limeAccent/60' : d < 0 ? 'border-red-500/60' : 'border-navy-2'} p-4">
            <p class="text-xs font-bold uppercase tracking-wider text-gray-400">${esc(j)} judge</p>
            <p class="text-3xl font-extrabold tabular-nums mt-1">${b == null ? '–' : Number(b).toFixed(1)}</p>
            <p class="text-xs font-bold tabular-nums ${d > 0 ? 'text-limeAccent' : d < 0 ? 'text-red-400' : 'text-gray-500'}">${d ? `${d > 0 ? '+' : ''}${d.toFixed(1)}` : 'balance'}</p></div>`;
    };
    const v = theater.verdict;
    const humans = [...theater.human.values()].slice(-8).reverse();
    const feedIcon = (e) => e.type === 'denied' ? 'shield-alert' : e.type === 'filed' ? 'mouse-pointer-click' : e.type === 'human_decision' ? 'user' : e.lane === 'learn' ? 'graduation-cap' : 'bot';
    el.innerHTML = `
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div><p class="text-sm text-gray-400 font-semibold">${theater.mode === 'live' ? 'Live: what the agents are doing now' : `Replay of the last run at ${esc(theater.speed)}×`}
                ${theater.run ? ` · <span class="font-mono">${esc(theater.run)}</span>` : ''} · <span data-test="th-count">${theater.count}</span> events${theater.mode === 'replay' && theater.done ? ' · <span class="text-limeAccent">replay finished</span>' : ''}</p></div>
            <div class="flex items-center gap-1.5 flex-wrap">
                <button data-th="live" class="px-3 py-1.5 text-xs font-bold rounded-full ${theater.mode === 'live' ? 'bg-limeAccent text-black' : 'bg-white/10 text-gray-200 hover:bg-white/20'}"><i data-lucide="radio" class="w-3.5 h-3.5 inline -mt-0.5"></i> Live</button>
                <button data-th="replay" class="px-3 py-1.5 text-xs font-bold rounded-full ${theater.mode === 'replay' ? 'bg-limeAccent text-black' : 'bg-white/10 text-gray-200 hover:bg-white/20'}"><i data-lucide="play" class="w-3.5 h-3.5 inline -mt-0.5"></i> Replay last run</button>
                <span class="w-px h-5 bg-white/10 mx-1"></span>
                ${[1, 2, 4, 8].map((s) => `<button data-speed="${s}" class="px-3 py-1.5 text-xs font-bold rounded-full ${theater.speed === s ? 'bg-purpleAccent text-white' : 'bg-white/10 text-gray-300 hover:bg-white/20'}">${s}×</button>`).join('')}
            </div>
        </div>
        ${theater.banner ? `<div class="rounded-2xl bg-limeAccent text-black px-4 py-3 text-sm sm:text-base font-bold flex items-center gap-2"><i data-lucide="graduation-cap" class="w-5 h-5 shrink-0"></i>
            <span>${theater.banner.type === 'rule_applied' ? 'Rule applied' : theater.banner.type === 'rule_approved' ? 'Rule approved' : 'Rule learned'}: <span class="font-semibold">${esc(theater.banner.text)}</span></span></div>` : ''}
        <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">${TH_JUDGES.map(bal).join('')}
            <div class="rounded-3xl bg-[#2B3048] border border-navy-2 p-4 min-w-0">${v ? `<p class="text-xs font-bold uppercase tracking-wider text-gray-400">Latest verdict</p>
                <div class="flex items-center gap-2 mt-2 flex-wrap">${verdictPill(v.outcome)} <a href="#/finding/${encodeURIComponent(v.finding_id)}" class="font-mono text-sm hover:underline">${esc(v.finding_id)}</a> <span class="font-bold tabular-nums">${v.gap != null ? money(v.gap) : ''}</span></div>
                <p class="text-xs text-gray-400 mt-2 line-clamp-3">${esc(v.text)}</p>` : '<p class="text-gray-500 text-sm">Waiting for the panel…</p>'}</div>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
            ${TH_JUDGES.map((j) => `<section class="rounded-3xl bg-[#2B3048] border border-navy-2 p-3 sm:p-4 min-w-0">
                <h4 class="font-bold text-base mb-3 flex items-center gap-2"><i data-lucide="scale" class="w-4 h-4 text-limeAccent"></i> ${esc(j)} <span class="text-xs text-gray-500 font-semibold">votes · bets</span></h4>
                <div class="flex flex-col gap-2 max-h-[30rem] overflow-y-auto dark-scroll pr-1">${theater.cols[j].map(thCard).join('') || '<p class="text-gray-500 text-sm py-6 text-center">No votes yet.</p>'}</div></section>`).join('')}
            <section class="rounded-3xl bg-[#2B3048] border border-purpleAccent/50 p-3 sm:p-4 min-w-0">
                <h4 class="font-bold text-base mb-3 flex items-center gap-2"><i data-lucide="user" class="w-4 h-4 text-limeAccent"></i> Human lane <span class="text-xs text-gray-500 font-semibold">escalations</span></h4>
                <div class="flex flex-col gap-2 max-h-[30rem] overflow-y-auto dark-scroll pr-1">${humans.map((c) => `<div class="${thCardCls} ${c.decision ? 'opacity-90' : 'ring-1 ring-purpleAccent'}">
                    <div class="flex items-center justify-between gap-2">${c.rank ? `<span class="text-xs font-extrabold text-limeAccent">#${esc(c.rank)}</span>` : '<span></span>'}<span class="font-bold tabular-nums">${c.gap != null ? money(c.gap) : ''}</span></div>
                    <a href="#/finding/${encodeURIComponent(c.finding_id)}" class="font-mono text-sm hover:underline">${esc(c.finding_id)}</a>
                    <p class="text-xs mt-1 ${c.decision ? (c.decision.decision === 'approve' ? 'text-limeAccent' : 'text-red-300') : 'text-gray-400'}">${c.decision ? `${esc(c.decision.human)} ${esc(c.decision.decision)}d${c.decision.reason ? `: “${esc(c.decision.reason)}”` : ''}` : c.status === 'deferred' ? 'deferred: over the attention budget' : 'waiting for a human'}</p></div>`).join('') || '<p class="text-gray-500 text-sm py-6 text-center">Nobody escalated yet.</p>'}</div>
            </section>
        </div>
        <section class="rounded-3xl bg-[#2B3048] border border-navy-2 p-3 sm:p-4">
            <h4 class="font-bold text-base mb-3">The flock <span class="text-xs text-gray-500 font-semibold">agents, case files, drafts, Clicky filings, decisions</span></h4>
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-2">${theater.feed.map((e) => `<div class="flex items-start gap-3 p-2.5 rounded-2xl ${e.type === 'denied' ? 'bg-red-500/10 border border-red-500/30' : 'bg-navy-1/70'}">
                <div class="w-8 h-8 rounded-xl ${e.type === 'denied' ? 'bg-red-600' : e.type === 'filed' ? 'bg-orange-600' : e.type === 'human_decision' ? 'bg-limeAccent text-black' : 'bg-purpleAccent'} flex items-center justify-center shrink-0"><i data-lucide="${feedIcon(e)}" class="w-4 h-4"></i></div>
                <p class="text-sm text-gray-200 leading-snug min-w-0">${esc(e.text)}${e.portal_ref ? ` <span class="font-mono text-limeAccent">${esc(e.portal_ref)}</span>` : ''}</p></div>`).join('') || '<p class="text-gray-500 text-sm">Quiet. Start a run, or replay the last one.</p>'}</div>
        </section>`;
    icons();
}

/* ---------------- sources / inbox (GET /api/connectors) ---------------- */

function renderSources() {
    const el = $('sources');
    if (!el) return;
    const batches = state.connectors?.received || [];   // newest first (flock/connectors.py)
    if (!batches.length) {
        el.innerHTML = whiteCard(`${cardHead('Sources / Inbox', 'Slack and email batches posted by the CREAO watchers (POST /api/connectors)')}
            ${empty('No connector batches yet. A watcher posts one with <code>POST /api/connectors</code> (example: <code>docs/connector_example.json</code>).', 'inbox')}`);
        return icons();
    }
    el.innerHTML = `<div class="flex flex-col gap-4 lg:gap-5">${batches.map((b) => whiteCard(`
        ${cardHead(`<span class="inline-flex items-center gap-2"><span class="w-9 h-9 rounded-full ${b.source === 'slack' ? 'bg-purpleAccent' : 'bg-sky-600'} text-white inline-flex items-center justify-center"><i data-lucide="${b.source === 'slack' ? 'slack' : 'mail'}" class="w-4 h-4"></i></span>${esc(label(b.source))} from ${esc(b.connector)}</span>`,
            `${esc(clock(b.at))} · ${num(b.items)} item(s) · ${num((b.claims || []).length)} deal claim(s)${b.superseded ? ` · ${num(b.superseded)} superseded` : ''}${b.ignored ? ` · ${num(b.ignored)} ignored (no deal term)` : ''}`,
            b.simulated ? pill('bg-sky-100 text-sky-800', 'simulated') : '')}
        <div class="flex flex-col gap-3">${(b.claims || []).map((c) => `
            <div class="rounded-2xl bg-lightCardBg p-4" data-claim>
                <div class="flex flex-wrap items-center gap-2 mb-2">
                    ${c.binding ? pill('bg-emerald-100 text-emerald-800', 'Binding') : pill('bg-red-100 text-red-700', 'Not binding')}
                    ${pill(AUTH_PILL(c.authority), label(c.authority))}
                    ${chanBadge(c.channel, 'w-5 h-5 text-[8px]')}<span class="text-xs font-semibold text-gray-700">${esc(chanName(c.channel))} ${esc(label(c.term_type))} · ${esc(c.store_id || 'all stores')} · value ${esc(c.value)}</span>
                    <span class="text-xs text-gray-400">${esc(c.effective_from || '')}${c.effective_to && c.effective_to !== c.effective_from ? ` → ${esc(c.effective_to)}` : ''}</span>
                </div>
                <p class="text-sm text-gray-900 leading-relaxed">“${esc(c.quote)}”</p>
                <p class="text-xs text-gray-500 mt-2"><b class="text-gray-700">${esc(c.sender || 'unknown sender')}</b> · ${esc(c.authority_basis || '')} · <span class="font-mono">${esc(c.locator || '')}</span></p>
            </div>`).join('') || '<p class="text-sm text-gray-400">No deal claims in this batch.</p>'}
            ${(b.unread || []).length ? `<p class="text-xs text-amber-700">Couldn't read: ${esc(b.unread.join(', '))}</p>` : ''}
        </div>`)).join('')}</div>`;
    icons();
}

/* ---------------- scoreboard + judge market (GET /api/score, /api/market) ---------------- */

function sparkline(values, w = 160, h = 36) {
    if (!values || values.length < 2) return '';
    const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
    const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - min) / span) * (h - 4) - 2}`).join(' ');
    return `<svg viewBox="0 0 ${w} ${h}" class="w-full h-9"><polyline points="${pts}" fill="none" stroke="#0084FF" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function renderScoreboard() {
    const el = $('scoreboard');
    if (!el) return;
    const s = state.score, m = state.market;
    const byType = {};
    (s?.traps || []).forEach((t) => { const b = (byType[t.trap_type] ||= { total: 0, caught: 0, rule: 0 }); b.total++; if (t.caught) b.caught++; if (t.by_rule) b.rule++; });
    el.innerHTML = `
        <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3.5 lg:gap-4 mb-4 lg:mb-5">
            ${s ? [
                statCard({ label: 'Caught (answer key)', value: `${num(s.caught)}/${num(s.total)}`, sub: `${num(s.missed)} missed`, icon: 'target', dark: true }),
                statCard({ label: 'False alarms', value: num(s.false_alarms), sub: 'flagged but the answer key says fine', icon: 'check' }),
                statCard({ label: 'Sent to a human', value: num(s.to_human), sub: `${num(s.by_rule)} settled by learned rules`, icon: 'user' }),
                statCard({ label: 'Recoverable found', value: money(s.recovered), sub: 'answer-key dollars on caught traps', icon: 'wallet' }),
            ].join('') : `<div class="sm:col-span-2 xl:col-span-4">${whiteCard(empty('Waiting for /api/score…'))}</div>`}
        </div>
        <div class="grid grid-cols-1 xl:grid-cols-2 gap-4 lg:gap-5 mb-4 lg:mb-5">
            ${whiteCard(`${cardHead('Hand-offs to a human', 'per month')}<div id="curveBig" class="h-56"></div>
                <div class="mt-3 flex flex-wrap gap-2">${(s?.runs || []).map((r) => `<span class="text-xs font-semibold rounded-full bg-lightCardBg px-3 py-1">${esc(r.label || r.period || '')}: <b>${num(r.to_human)}</b> to a human</span>`).join('')}</div>`)}
            ${whiteCard(`${cardHead('By trap type', 'Every planted mistake, caught or not (simulated answer key)')}
                <div class="flex flex-col divide-y divide-gray-50 max-h-72 overflow-y-auto custom-scroll">${Object.entries(byType).map(([k, b]) => `
                    <div class="flex items-center justify-between gap-3 py-2 text-sm"><span class="font-semibold text-gray-800">${esc(label(k))}</span>
                    <span class="flex items-center gap-2"><span class="tabular-nums font-bold ${b.caught === b.total ? 'text-emerald-700' : 'text-red-600'}">${num(b.caught)}/${num(b.total)}</span>${b.rule ? pill('bg-limeAccent text-black', `${num(b.rule)} by rule`) : ''}</span></div>`).join('') || empty('No answer key here.')}</div>`)}
        </div>
        ${whiteCard(`${cardHead('Judge market', m ? esc(m.rule) : 'GET /api/market', m ? `<span class="text-xs font-semibold text-gray-500">${num(m.settled)} settled · ${num(m.open_bets)} open bets · start ${esc(m.start)}</span>` : '')}
            ${m ? `<div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-4">${(m.leaderboard || []).map((j, i) => `
                <div data-judge="${esc(j.judge)}" class="${i === 0 ? 'bg-cardDark text-white' : 'bg-lightCardBg text-gray-900'} rounded-3xl p-4">
                    <div class="flex items-center justify-between"><p class="text-xs font-bold uppercase tracking-wider ${i === 0 ? 'text-limeAccent' : 'text-gray-500'}">${esc(judgeKey(j.judge))}</p><span class="text-xs font-bold ${i === 0 ? 'text-gray-400' : 'text-gray-500'}">weight ${pct(j.weight)}</span></div>
                    <p class="text-3xl font-extrabold tabular-nums mt-1">${Number(j.balance).toFixed(1)}</p>
                    <p class="text-xs font-semibold ${j.pnl >= 0 ? (i === 0 ? 'text-limeAccent' : 'text-emerald-700') : 'text-red-500'}">${j.pnl >= 0 ? '+' : ''}${esc(j.pnl)} since start · won ${num(j.won)} · lost ${num(j.lost)} · abstained ${num(j.abstained)}</p>
                    ${sparkline(j.history)}
                </div>`).join('')}</div>
                ${m.attention ? `<p class="text-xs text-gray-500 mb-3">Attention budget: ${num(m.attention.used)} of ${num(m.attention.budget)} human looks used${m.attention.remaining != null ? `, ${num(m.attention.remaining)} left` : ''}.</p>` : ''}
                <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-2">Recent bets and settlements</p>
                <div class="flex flex-col divide-y divide-gray-50 max-h-72 overflow-y-auto custom-scroll">${(m.recent || []).slice(0, 15).map((r) => `
                    <div class="flex items-center justify-between gap-3 py-2 text-xs sm:text-sm">
                        <span class="min-w-0 truncate"><b>${esc(judgeKey(r.judge))}</b> ${r.kind === 'settle' ? `${esc(r.result)} on` : r.kind === 'stake' ? 'staked on' : 'void'} <a href="#/finding/${encodeURIComponent(r.finding_id)}" class="font-mono text-purpleAccent hover:underline">${esc(r.finding_id)}</a> <span class="text-gray-400">called ${esc(r.called || r.outcome || '')}${r.truth ? `, truth ${esc(r.truth)}` : ''}</span></span>
                        <span class="tabular-nums font-bold ${r.payout > 0 ? 'text-emerald-700' : r.payout < 0 ? 'text-red-600' : 'text-gray-400'}">${r.kind === 'settle' ? `${r.payout > 0 ? '+' : ''}${esc(r.payout)}` : r.stake != null ? `stake ${esc(r.stake)}` : ''}</span>
                    </div>`).join('') || '<p class="text-sm text-gray-400 py-4">No bets yet.</p>'}</div>`
            : empty('The judge market shows up once the API serves /api/market.')}`)}
        ${safetyCard()}`;
    renderCurve('curveBig', true);
    icons();
}

/* ---------------- detail drawer: finding + agent ---------------- */

function openDrawer(kind, id) {
    if (!state.drawer || state.drawer.id !== id) { state.refusal = null; state.lastDecision = null; }
    state.drawer = { kind, id };
    state.cache.drawer = null;
    $('drawer').classList.remove('hidden');
    $('drawerTitle').textContent = id;
    $('drawerBody').innerHTML = '<p class="text-gray-400 text-sm">Loading…</p>';
    refreshDrawer();
}

function closeDrawer(nav = true) {
    const wasFinding = state.drawer?.kind === 'finding';
    state.drawer = null;
    $('drawer').classList.add('hidden');
    if (nav && wasFinding && route().name === 'finding') location.hash = lastMain;
}

async function refreshDrawer() {
    const d = state.drawer;
    if (!d) return;
    try {
        if (d.kind === 'finding') {
            const data = await get(`/api/findings/${encodeURIComponent(d.id)}`);
            if (state.drawer !== d) return;
            if (!changed('drawer', { data, refusal: state.refusal, last: state.lastDecision })) return;
            renderFindingDetail(data);
        } else {
            const data = await tryGet(`/api/agents/${encodeURIComponent(d.id)}`);
            if (state.drawer !== d) return;
            const payload = { data, log: data ? null : state.log.filter((e) => e.actor === d.id) };
            if (!changed('drawer', payload)) return;
            renderAgentDetail(d.id, payload);
        }
        icons();
    } catch (e) {
        if (state.drawer === d) $('drawerBody').innerHTML = `<p class="text-red-600 text-sm">Couldn't load: ${esc(explain(e))}</p>`;
    }
}

// Evidence references open agent E's cell-level viewer (frontend/evidence.js) when it is loaded
const evRef = (ev) => ev.source_file ? `${ev.source_file}${ev.row != null ? `:${ev.row}` : ''}` : '';
function evidenceRef(ev) {
    const ref = evRef(ev);
    const text = `${esc(ref)}${ev.locator ? ` (${esc(ev.locator)})` : ''}`;
    return ref ? `<button data-ev-ref="${esc(ref)}" class="truncate font-mono text-left text-purpleAccent hover:underline" title="Open the source cell">${text}</button>` : `<span class="truncate font-mono">${text}</span>`;
}
function openEvidence(ref) {
    if (window.FlockEvidence && typeof window.FlockEvidence.open === 'function') window.FlockEvidence.open(ref);
    else toast(`Source: ${ref}`, '');
}

const card = (title, body, extra = '') => `<section class="bg-white rounded-3xl p-4 sm:p-5 card-shadow border border-gray-100 ${extra}"><h4 class="font-bold text-gray-900 text-sm mb-3">${title}</h4>${body}</section>`;

function parseMemo(m) {
    if (!m) return null;
    if (typeof m === 'object') return m;
    try { return JSON.parse(m); } catch { return { memo: m }; }
}

function refusalCard() {
    const r = state.refusal;
    if (!r) return '';
    const e = r.entry || {};
    return `<section data-test="refusal" class="rounded-3xl p-4 sm:p-5 bg-red-50 border border-red-200">
        <div class="flex items-center gap-2 mb-2"><i data-lucide="shield-alert" class="w-5 h-5 text-red-600"></i><h4 class="font-extrabold text-red-700 text-sm">Gatekeeper refused the agent (HTTP ${esc(r.status)})</h4></div>
        <p class="text-sm text-red-800">${esc(r.error)}</p>
        ${r.entry ? `<div class="mt-3 rounded-2xl bg-white border border-red-100 p-3 text-xs font-mono text-gray-700 space-y-0.5">
            <div><span class="text-gray-400">actor</span> ${esc(e.actor)} <span class="text-gray-400">role</span> ${esc(e.role)} <span class="text-gray-400">action</span> ${esc(e.action)}</div>
            <div><span class="text-gray-400">allowed</span> <b class="text-red-600">${esc(e.allowed)}</b> <span class="text-gray-400">state</span> ${esc(e.before || '')} → ${esc(e.after || '')} <span class="text-gray-400">at</span> ${esc(clock(e.at))}</div>
            ${e.reason ? `<div class="font-sans text-gray-600">${esc(e.reason)}</div>` : ''}</div>
            <p class="text-[11px] text-red-700/80 mt-2">Logged in the audit trail below. Only a person can approve; only the gatekeeper files.</p>` : ''}
    </section>`;
}

const filedFindings = () => state.findings.filter((f) => f.portal_ref || f.state === 'FILED' || f.state === 'RECOVERED');
const screenUrl = (id) => `${state.base}/api/filings/${encodeURIComponent(id)}/screen`;

function clickyCard(f, data) {
    const filing = data.filing || f.filing || null;
    if (!f.portal_ref && !filing && !['FILED', 'RECOVERED'].includes(f.state)) return '';
    const ref = filing?.portal_ref || f.portal_ref;
    return card('<span class="flex items-center gap-2"><i data-lucide="mouse-pointer-click" class="w-4 h-4 text-orange-600"></i> Clicky filed it</span>', `
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <p class="text-sm text-gray-700">Claim ${ref ? `<span class="font-mono font-bold text-gray-900">${esc(ref)}</span>` : ''} in the ${esc(chanName(f.channel))} portal${filing?.filed_at ? ` at ${esc(clock(filing.filed_at))}` : ''}.</p>
            <button data-screen="${esc(fidOf(f))}" class="rounded-2xl bg-cardDark hover:bg-black text-white font-bold text-xs px-4 py-2.5 flex items-center gap-1.5 shrink-0"><i data-lucide="monitor" class="w-4 h-4"></i> See Clicky's screen</button>
        </div>`);
}

// Vendor email after filing: sent (to whom, when) or held in the outbox
function emailCard(f, data, fu) {
    const fid = fidOf(f);
    const fromOutbox = (state.outbox || []).filter((m) => m.finding_id === fid || String(m.subject || '').includes(fid) || String(m.body || '').includes(fid)).slice(-1)[0];
    const em = data.email || data.vendor_email || f.vendor_email || f.email || fu?.email || state.lastDecision?.email || fromOutbox || null;
    if (!em) return '';
    const sent = /sent|delivered/i.test(em.status || '') || !!em.sent_at;
    return card('<span class="flex items-center gap-2"><i data-lucide="mail" class="w-4 h-4 text-purpleAccent"></i> Email to the vendor</span>', `
        <div class="flex flex-wrap items-center gap-2 text-sm">
            ${sent ? pill('bg-emerald-100 text-emerald-800', 'sent') : pill('bg-amber-100 text-amber-800', label(em.status || 'in outbox'))}
            <span class="text-gray-700">to <b>${esc(em.to || em.recipient || '—')}</b>${em.intended_to && em.intended_to !== em.to ? ` <span class="text-gray-400 text-xs">(demo inbox; vendor contact ${esc(em.intended_to)})</span>` : ''}</span>
            ${em.sent_at ? `<span class="text-gray-400 text-xs">${esc(clock(em.sent_at))}</span>` : ''}
            ${em.transport || em.via ? `<span class="text-gray-400 text-xs">via ${esc(em.transport || em.via)}</span>` : ''}
        </div>
        ${em.subject ? `<p class="text-xs text-gray-500 mt-2">Subject: <b class="text-gray-800">${esc(em.subject)}</b></p>` : ''}
        ${em.note || em.error ? `<p class="text-xs text-gray-500 mt-1">${esc(em.note || em.error)}</p>` : ''}`);
}

function followUpCard(f, fu) {
    if (f.state !== 'FILED' && !fu) return '';
    const st = fu?.state;
    const stCls = { DRAFT: 'bg-amber-100 text-amber-800', APPROVED: 'bg-emerald-100 text-emerald-800', SENT: 'bg-sky-100 text-sky-800', REJECTED: 'bg-red-100 text-red-700' }[st] || 'bg-gray-100 text-gray-600';
    const typed = $('fuText')?.value;
    const btn = (step, text, cls, icon) => `<button data-fu="${step}" class="flex-1 rounded-2xl ${cls} font-bold text-sm py-2.5 flex items-center justify-center gap-1.5"><i data-lucide="${icon}" class="w-4 h-4"></i> ${text}</button>`;
    let actions = '';
    if (!fu || st === 'REJECTED') actions = btn('draft', st === 'REJECTED' ? 'Draft a new follow-up' : 'Draft follow-up', 'bg-purpleAccent hover:bg-purpleAccentHover text-white', 'pen-line');
    else if (st === 'DRAFT') actions = btn('approve', 'Approve', 'bg-purpleAccent hover:bg-purpleAccentHover text-white', 'check') + btn('reject', 'Reject', 'bg-cardDark hover:bg-black text-white', 'x');
    else if (st === 'APPROVED') actions = btn('send', 'Send via Clicky', 'bg-purpleAccent hover:bg-purpleAccentHover text-white', 'send');
    return card(`<span class="flex items-center gap-2"><i data-lucide="mail" class="w-4 h-4 text-purpleAccent"></i> Vendor follow-up ${st ? pill(stCls, st) : ''}</span>`, `
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs mb-3">
            <div class="rounded-2xl bg-lightCardBg p-2.5"><p class="text-gray-500 font-semibold">Claim</p><p class="font-mono font-bold">${esc(fu?.claim_ref || f.portal_ref || '—')}</p></div>
            <div class="rounded-2xl bg-lightCardBg p-2.5"><p class="text-gray-500 font-semibold">Filed</p><p class="font-bold">${esc(fu?.filed_on || '—')}</p></div>
            <div class="rounded-2xl bg-lightCardBg p-2.5"><p class="text-gray-500 font-semibold">Amount</p><p class="font-bold tabular-nums">${money(fu?.amount ?? f.gap)}</p></div>
            <div class="rounded-2xl bg-lightCardBg p-2.5"><p class="text-gray-500 font-semibold">Sent ref</p><p data-test="fu-sent-ref" class="font-mono font-bold">${esc(fu?.sent_ref || fu?.portal_ref || '—')}</p></div>
        </div>
        ${fu?.draft ? (st === 'DRAFT'
            ? `<textarea id="fuText" rows="7" class="w-full rounded-2xl border border-gray-200 px-3 py-2 text-xs mb-3 focus:outline-none focus:ring-2 focus:ring-purpleAccent/40">${esc(typed ?? fu.draft)}</textarea>`
            : `<pre class="text-xs whitespace-pre-wrap text-gray-700 font-sans rounded-2xl bg-lightCardBg p-3 mb-3">${esc(fu.text || fu.draft)}</pre>`)
            : '<p class="text-xs text-gray-500 mb-3">Filed and still unpaid? The follow-up agent drafts a reminder with the claim number and evidence; a person approves it; Clicky sends it.</p>'}
        ${fu && (fu.approved_by || fu.rejected_by || fu.sent_at) ? `<p class="text-[11px] text-gray-500 mb-3">${fu.approved_by ? `approved by <b>${esc(fu.approved_by)}</b> ` : ''}${fu.rejected_by ? `rejected by <b>${esc(fu.rejected_by)}</b> ` : ''}${fu.sent_at ? `sent ${esc(clock(fu.sent_at))}` : ''}</p>` : ''}
        ${actions ? `<div class="flex gap-2">${actions}</div>` : ''}`, st === 'DRAFT' || st === 'APPROVED' ? 'ring-2 ring-purpleAccent/30' : '');
}

function renderFindingDetail(data) {
    if (!data) { $('drawerBody').innerHTML = '<p class="text-gray-400 text-sm">This finding no longer exists.</p>'; return; }
    const f = data.finding || data;
    const log = data.log || [];
    const fu = data.follow_up || f.follow_up || null;
    $('drawerTitle').textContent = `${fidOf(f)} · ${label(f.check_type)} on ${chanName(f.channel)}`;

    const rounds = {};
    (f.votes || []).forEach((v) => (rounds[v.round || 1] ||= []).push(v));
    const memo = parseMemo(f.review_memo);
    const cf = f.case_file;
    const canDecide = f.state === 'JUDGED';
    const typed = $('decisionReason')?.value || '';
    const judges = Object.fromEntries((state.agents?.judges || []).map((j) => [j.id, j]));
    const payoutId = f.rollup?.payout_id;
    const ld = state.lastDecision;

    $('drawerBody').innerHTML = [
        `<div class="grid grid-cols-3 gap-3">
            ${[['Deal says', f.expected, 'text-gray-900'], ['Was charged', f.actual, 'text-gray-900'], ['Gap', f.gap, f.gap > 0 ? 'text-red-600' : 'text-gray-900']].map(([l, v, c]) => `
            <div class="bg-white rounded-2xl p-3 sm:p-4 card-shadow border border-gray-100">
                <p class="text-gray-500 text-[11px] font-semibold">${l}</p>
                <p class="text-lg sm:text-2xl font-extrabold tabular-nums ${c}">${money(v)}</p>
            </div>`).join('')}
        </div>`,
        `<div class="flex flex-wrap items-center gap-2 text-xs">
            ${verdictPill(f.outcome, f.confidence)}
            ${checkHelp(f.check_type) ? `<span class="text-gray-500 font-semibold">${esc(checkHelp(f.check_type))}</span>` : ''}
            ${pill(STATE_PILL[f.state] || 'bg-gray-100', f.state)}
            ${f.risk != null ? `<span class="text-gray-500 font-semibold">Risk</span> ${riskPill(f.risk)}` : ''}
            ${f.attention ? `<span class="text-gray-500 font-semibold">Queue rank #${esc(f.attention.rank)} · ${esc(f.attention.status)}</span>` : ''}
            ${f.portal_ref ? `<span class="text-gray-500 font-semibold">Portal ref <span data-test="portal-ref" class="font-mono text-gray-900">${esc(f.portal_ref)}</span></span>` : ''}
        </div>
        <p class="text-xs text-gray-600 font-semibold">Orders: <span data-test="orders-label">${esc(f.orders_label || (f.order_ids || []).slice(0, 10).join(', '))}</span>
            ${payoutId ? ` · in lump sum <a href="#/payout/${encodeURIComponent(payoutId)}" class="font-mono text-purpleAccent hover:underline">${esc(payoutId)}</a>` : ''}
            ${f.rollup?.by_store ? ` · ${Object.entries(f.rollup.by_store).map(([s, v]) => `${esc(s)} ${money(v)}`).join(', ')}` : ''}</p>`,
        ld ? `<section data-test="decision-result" class="rounded-3xl p-4 bg-emerald-50 border border-emerald-200 text-sm text-emerald-900">
            <b>${esc(ld.actor)} ${ld.action === 'approve' ? 'approved' : 'rejected'} it.</b> ${ld.filed?.length ? `Clicky filed it in the sandbox portal: <span class="font-mono font-bold">${esc(ld.filed.join(', '))}</span>` : ld.action === 'approve' ? 'Nothing new to file.' : 'Closed; the charge stands.'}</section>` : '',
        canDecide ? card('Your call', `
            <p class="text-xs text-gray-500 mb-3">Approve and Clicky files the claim in the platform's portal, then the vendor gets a follow-up email. Reject and the charge stands. Either way it goes in the audit log under your name.</p>
            <textarea id="decisionReason" rows="2" class="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-purpleAccent/40" placeholder="Why? (recorded in the log)"></textarea>
            <div class="flex gap-2">
                <button data-decide="approve" class="flex-1 rounded-2xl bg-purpleAccent hover:bg-purpleAccentHover text-white font-bold text-sm py-2.5 flex items-center justify-center gap-1.5"><i data-lucide="check" class="w-4 h-4"></i> Approve</button>
                <button data-decide="reject" class="flex-1 rounded-2xl bg-cardDark hover:bg-black text-white font-bold text-sm py-2.5 flex items-center justify-center gap-1.5"><i data-lucide="x" class="w-4 h-4"></i> Reject</button>
            </div>
            <p class="text-[11px] text-gray-400 mt-2">Deciding as <b>${esc(settings.actor)}</b>.</p>`, 'ring-2 ring-purpleAccent/30') : '',
        clickyCard(f, data),
        emailCard(f, data, fu),
        followUpCard(f, fu),
        f.reason ? card('Panel verdict', `<p class="text-sm text-gray-700 leading-relaxed">${esc(f.reason)}</p>`) : '',
        memo && (memo.memo || memo.summary) ? card('Reviewer memo', `<p class="text-sm text-gray-700 leading-relaxed">${esc(memo.memo || memo.summary)}</p>${memo.recommendation ? `<p class="text-xs text-gray-500 mt-2">Recommendation: <b>${esc(memo.recommendation)}</b></p>` : ''}`) : '',
        cf ? card('<span class="flex items-center gap-2"><i data-lucide="search" class="w-4 h-4 text-purpleAccent"></i> Case file (investigator)</span>', `
            <p class="text-xs text-gray-500 mb-2">Why it was opened: ${esc(cf.why || label(cf.trigger))}</p>
            <ul class="list-disc pl-5 space-y-1 text-sm text-gray-700">${(cf.facts || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
            ${cf.next_step ? `<p class="text-sm text-gray-900 font-semibold mt-3">Next: ${esc(cf.next_step)}</p>` : ''}
            ${(cf.claims || []).length ? `<div class="mt-3 space-y-2">${cf.claims.map((c) => `<div class="rounded-2xl bg-lightCardBg p-2.5 text-xs flex flex-wrap items-center gap-2">${pill(AUTH_PILL(c.authority), label(c.authority))}${c.binding != null ? (c.binding ? pill('bg-emerald-100 text-emerald-800', 'binding') : pill('bg-red-100 text-red-700', 'not binding')) : ''}<span class="text-gray-700">says ${esc(c.value)}</span>${c.sender ? `<span class="text-gray-500">${esc(c.sender)}</span>` : ''}</div>`).join('')}</div>` : ''}`) : '',
        card('Evidence', (f.evidence || []).map((ev) => `
            <div class="rounded-2xl bg-lightCardBg p-3 mb-2 last:mb-0" data-evidence>
                <div class="flex items-center justify-between gap-2 text-[11px] font-semibold text-gray-500">
                    ${evidenceRef(ev)}
                    <span class="flex gap-1 shrink-0">${ev.conflict ? pill('bg-amber-100 text-amber-800', 'conflicts') : ''}${ev.authority ? pill(AUTH_PILL(ev.authority), label(ev.authority)) : ''}</span>
                </div>
                <p class="text-sm text-gray-800 mt-1">${ev.quote ? `“${esc(ev.quote)}”` : esc(ev.what)}</p>
            </div>`).join('') || '<p class="text-sm text-gray-400">No evidence attached.</p>'),
        card('Judge votes', Object.keys(rounds).sort().map((r) => `
            <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-2 ${r > 1 ? 'mt-4' : ''}">Round ${esc(r)}${r > 1 ? ' · after debate' : ''}</p>
            <div class="grid gap-2">${rounds[r].map((v) => `
                <div class="rounded-2xl border border-gray-100 p-3">
                    <div class="flex items-center justify-between gap-2">
                        <span class="text-sm font-bold">${esc(judges[v.judge]?.name || judgeKey(v.judge))}${v.revised ? ' <span class="text-[11px] text-amber-600 font-semibold">changed vote</span>' : ''}${v.weight != null ? ` <span class="text-[11px] text-gray-400 font-semibold">weight ${pct(v.weight)}</span>` : ''}</span>
                        ${verdictPill(v.outcome, v.confidence)}
                    </div>
                    <p class="text-xs text-gray-600 mt-1 leading-snug">${esc(v.reason)}</p>
                </div>`).join('')}</div>`).join('') || '<p class="text-sm text-gray-400">Not judged yet.</p>'),
        f.dispute_draft ? card('Dispute draft', `<pre class="text-xs whitespace-pre-wrap text-gray-700 font-sans">${esc(typeof f.dispute_draft === 'string' ? f.dispute_draft : JSON.stringify(f.dispute_draft, null, 2))}</pre>`) : '',
        card('Audit trail', `<div class="bg-cardDark rounded-2xl p-2 space-y-1.5" data-test="audit-trail">${[...log].reverse().map((e) => logRow(e)).join('') || '<p class="text-gray-500 text-sm p-2">No moves yet.</p>'}</div>`),
    ].join('');

    if ($('decisionReason')) $('decisionReason').value = typed;
    icons();
}

const IMG_RE = /\.(png|jpe?g|gif|webp)(\?|$)/i;
const absUrl = (p) => /^https?:|^data:/.test(p) ? p : `${state.base}/${String(p).replace(/^\//, '')}`;
function collectImages(obj, out = []) {
    if (typeof obj === 'string' && (IMG_RE.test(obj) || obj.startsWith('data:image'))) out.push(obj);
    else if (Array.isArray(obj)) obj.forEach((x) => collectImages(x, out));
    else if (obj && typeof obj === 'object') Object.values(obj).forEach((x) => collectImages(x, out));
    return out;
}

function renderAgentDetail(id, { data, log }) {
    const ag = (state.agents?.agents || []).find((a) => a.actor === id) || {};
    $('drawerTitle').textContent = ag.name || id;
    const entries = data ? (data.log || data.activity || data.timeline || []) : log;
    const shots = data ? collectImages(data.screens || data.screenshots || []) : [];
    $('drawerBody').innerHTML = [
        card('What it does', `<p class="text-sm text-gray-700">${esc(ag.job || data?.job || '')}</p>
            <p class="text-xs text-gray-500 mt-2">Owner: ${esc(ag.owner || data?.owner || '—')} · ${num(ag.actions ?? entries.length)} actions · last ${esc(ago(ag.last_at))}</p>`),
        id === 'clicky' ? card("Clicky's screens", filedFindings().length ? `<div class="flex flex-col gap-2">${filedFindings().slice(0, 20).map((f) => `
            <div class="flex items-center justify-between gap-3 rounded-2xl bg-lightCardBg p-3 text-sm">
                <span class="min-w-0"><span class="font-mono font-bold">${esc(fidOf(f))}</span> <span class="text-gray-500">${esc(chanName(f.channel))} · ${money(f.gap)}</span>
                ${f.portal_ref ? `<span class="block text-[11px] font-mono text-gray-500">${esc(f.portal_ref)}</span>` : ''}</span>
                <button data-screen="${esc(fidOf(f))}" class="rounded-xl bg-cardDark text-white text-xs font-bold px-3 py-2 shrink-0">Open screen</button></div>`).join('')}</div>`
            : '<p class="text-sm text-gray-400">Nothing filed yet. Clicky files only after a person approves.</p>') : '',
        shots.length ? card('Screen', `<div class="grid gap-3">${shots.slice(-6).reverse().map((s) => `<img src="${esc(absUrl(s))}" alt="${esc(ag.name)} screenshot" class="w-full rounded-2xl border border-gray-100" loading="lazy">`).join('')}</div>`) : '',
        card('Activity', `<div class="bg-cardDark rounded-2xl p-2 space-y-1.5">${[...entries].slice(-100).reverse().map((e) => e.action ? logRow(e) : `<p class="text-xs text-gray-300 p-2">${esc(e.text || e.summary || JSON.stringify(e))}</p>`).join('') || '<p class="text-gray-500 text-sm p-2">No activity yet.</p>'}</div>`),
    ].join('');
}

/* ---------------- actions ---------------- */

async function decide(id, action) {
    const reason = $('decisionReason')?.value.trim() || '';
    try {
        const out = await post(`/api/findings/${encodeURIComponent(id)}/${action}`, { actor: settings.actor, reason });
        state.refusal = null;
        state.lastDecision = { action, actor: settings.actor, filed: out.filed || [], email: out.email || out.vendor_email || null };
        const em = state.lastDecision.email;
        toast(action === 'approve'
            ? `${id} approved.${out.filed?.length ? ` Clicky filed it: ${out.filed.join(', ')}.` : ''}${em ? ` Email ${/sent/i.test(em.status || '') ? 'sent' : 'queued'} to ${em.to || em.recipient}.` : ''}`
            : `${id} rejected.`, 'ok');
        state.cache.findings = state.cache.drawer = state.cache.approvals = null;
        refreshDrawer();
    } catch (e) { fail(e); }
}

// Safety check (scoreboard): send an approve as an agent on a waiting finding. The gatekeeper refuses and logs it.
async function tryAsAgent() {
    const target = approvalItems()[0];
    if (!target) return toast('Nothing is waiting for approval, so there is nothing for an agent to try.', '');
    const id = target.finding_id || target.id;
    try {
        await post(`/api/findings/${encodeURIComponent(id)}/approve`, { actor: 'dispute_writer', role: 'agent', reason: 'an agent trying to approve its own draft' });
        state.safety = { ok: false, id, error: 'The API accepted an agent approval. That should never happen.' };
    } catch (e) {
        if (e instanceof ApiError && (e.status === 403 || e.status === 409) && !e.data.running) {
            state.safety = { ok: true, id, status: e.status, error: e.message, entry: e.data.log_entry || null };
        } else return fail(e);
    }
    renderScoreboard();
}
function safetyCard() {
    const r = state.safety;
    return `<details class="bg-white rounded-3xl card-shadow border border-gray-100 p-5 mt-4 lg:mt-5" ${r ? 'open' : ''}>
        <summary class="cursor-pointer font-bold text-gray-900 text-sm flex items-center gap-2"><i data-lucide="shield-check" class="w-4 h-4 text-sky-600"></i> Safety check: can an agent approve its own claim?</summary>
        <p class="text-xs text-gray-500 mt-3 mb-3">Sends an approve on a waiting finding under an agent's name. Only a person can approve, so the gatekeeper should refuse and write the attempt to the audit log.</p>
        <button data-try-agent class="rounded-2xl border border-gray-200 hover:bg-gray-50 text-gray-700 font-bold text-xs px-4 py-2 flex items-center gap-1.5"><i data-lucide="bot" class="w-4 h-4"></i> Run the check</button>
        ${r ? `<div class="mt-3 rounded-2xl p-3 text-sm ${r.ok ? 'bg-emerald-50 border border-emerald-200 text-emerald-900' : 'bg-red-50 border border-red-200 text-red-800'}">
            <b>${r.ok ? `Refused (HTTP ${esc(r.status)})` : 'Not refused'}</b> on <span class="font-mono">${esc(r.id)}</span>: ${esc(r.error)}
            ${r.entry ? `<div class="mt-2 text-xs font-mono text-gray-700">actor ${esc(r.entry.actor)} · role ${esc(r.entry.role)} · allowed <b>${esc(r.entry.allowed)}</b> · ${esc(clock(r.entry.at))}</div>` : ''}</div>` : ''}
    </details>`;
}

async function followUp(id, step) {
    const body = {};
    if (step === 'approve' || step === 'reject') {
        body.actor = settings.actor;
        body.reason = step === 'approve' ? 'Follow-up approved from the console' : 'Follow-up rejected from the console';
        const text = $('fuText')?.value;
        if (step === 'approve' && text) body.text = text;
    }
    try {
        const out = await post(`/api/findings/${encodeURIComponent(id)}/follow_up${step === 'draft' ? '' : `/${step}`}`, body);
        const msg = { draft: 'Follow-up drafted; it waits for a person.', approve: 'Follow-up approved.', reject: 'Follow-up rejected.',
            send: `Follow-up ${out.email ? `${/sent/i.test(out.email.status || '') ? 'emailed' : 'queued in the outbox'} to ${out.email.to}` : 'sent'}${out.portal_ref ? ` · ${out.portal_ref}` : ''}.` }[step];
        if (out.email) state.lastDecision = { ...(state.lastDecision || {}), email: out.email };
        toast(msg, 'ok');
        state.cache.drawer = null;
        refreshDrawer();
    } catch (e) { fail(e); }
}

/* ---------------- live run: a fresh month, every agent, on screen (POST /api/run {"dataset":"live"}) ---------------- */

async function startLiveRun() {
    if (state.job && !state.job.finished) return toast('A live run is already going.', '');
    try {
        const job = await post('/api/run?wait=0', { dataset: 'live' });
        trackJob(job);
        toast('New month generated. The agents are on it.', 'ok');
    } catch (e) {
        if (e instanceof ApiError && e.status === 409 && e.data.running) return trackJob({ job_id: e.data.running, status: 'running' });
        if (e instanceof ApiError && e.status === 400) return toast(`This backend can't run a live month yet (${e.message}).`, 'error');
        fail(e);
    }
}

function trackJob(job) {
    const id = job.job_id || job.id;
    if (!id) return;
    state.job = { id, status: job.status || 'running', started: Date.now(), finished: false, data: job };
    renderJob();
    pollJob();
}

async function pollJob() {
    const j = state.job;
    if (!j || j.finished) return;
    try {
        const d = await get(`/api/jobs/${encodeURIComponent(j.id)}`);
        j.data = d; j.status = d.status;
        if (d.status !== 'running') {
            j.finished = true;
            Object.keys(state.cache).forEach((k) => { if (k !== 'bannerHtml') state.cache[k] = null; });
            toast(d.status === 'done' ? `Live run finished in ${duration(d.seconds)}. ${approvalItems().length ? 'Approvals are waiting.' : ''}` : `Live run ${d.status}: ${d.error || ''}`, d.status === 'done' ? 'ok' : 'error');
            setTimeout(() => { if (state.job === j) { state.job = null; renderJob(); } }, 20000);
        }
    } catch (e) {
        if (e instanceof ApiError && e.status === 404) { j.finished = true; j.status = 'lost (server restarted)'; }
    }
    renderJob();
    if (!j.finished) setTimeout(pollJob, 1500);
}

function renderJob() {
    const el = $('jobPanel');
    const j = state.job;
    $('liveRunBtn').classList.toggle('opacity-60', !!(j && !j.finished));
    if (!j) { el.classList.add('hidden'); el.innerHTML = ''; return; }
    const d = j.data || {}, pr = d.progress || {};
    const frac = pr.total ? Math.min(1, (pr.done || 0) / pr.total) : null;
    const stages = pr.stages || [];
    const tail = (d.output_tail || []).slice(-3);
    const secs = d.seconds ?? Math.round((Date.now() - j.started) / 1000);
    el.classList.remove('hidden');
    el.innerHTML = `<div class="bg-cardDark text-white rounded-3xl p-4 sm:p-5 shadow-md" data-test="job-panel">
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div class="flex items-center gap-3 min-w-0">
                <div class="w-10 h-10 rounded-full ${j.finished ? (j.status === 'done' ? 'bg-limeAccent text-black' : 'bg-red-600') : 'bg-purpleAccent'} flex items-center justify-center shrink-0">
                    <i data-lucide="${j.finished ? (j.status === 'done' ? 'check' : 'triangle-alert') : 'loader'}" class="w-5 h-5 ${j.finished ? '' : 'animate-spin'}"></i></div>
                <div class="min-w-0">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-limeAccent">Live run · <span class="font-mono">${esc(j.id)}</span> · ${esc(duration(secs))}</p>
                    <p class="text-sm sm:text-base font-bold truncate">${j.finished ? (j.status === 'done' ? 'Done: findings, approvals and metrics are refreshed' : `Stopped: ${esc(j.status)}`)
                        : esc(pr.stage ? `${label(pr.stage)}${pr.total ? ` · ${num(pr.done)}/${num(pr.total)} ${esc(pr.unit || '')}` : ''}` : 'Generating a new month of data…')}</p>
                </div>
            </div>
            <div class="flex items-center gap-2 shrink-0">
                <a href="#/theater" data-th-live class="rounded-2xl bg-white/10 hover:bg-white/20 text-white font-bold text-sm px-4 py-2.5 flex items-center gap-1.5"><i data-lucide="clapperboard" class="w-4 h-4"></i> Watch the agents</a>
                ${j.finished ? '<button data-job-close class="rounded-2xl bg-white/10 hover:bg-white/20 px-3 py-2.5" aria-label="Dismiss"><i data-lucide="x" class="w-4 h-4"></i></button>' : ''}
            </div>
        </div>
        ${frac != null && !j.finished ? `<div class="h-1.5 rounded-full bg-white/10 mt-3"><div class="h-1.5 rounded-full bg-limeAccent transition-all" style="width:${Math.round(frac * 100)}%"></div></div>` : ''}
        ${stages.length ? `<div class="flex flex-wrap gap-1.5 mt-3">${stages.map((st) => `<span class="text-[11px] font-semibold rounded-full bg-white/10 px-2.5 py-1">${esc(label(st.name))} ${st.total ? `${num(st.done)}/${num(st.total)}` : ''}</span>`).join('')}</div>` : ''}
        ${tail.length ? `<div class="mt-3 font-mono text-[11px] text-gray-400 space-y-0.5">${tail.map((l) => `<div class="truncate">${esc(l)}</div>`).join('')}</div>` : ''}
    </div>`;
    icons();
}

/* ---------------- benchmarks (GET /api/benchmarks) ---------------- */

async function loadBenchmarks() {
    try {
        const d = await get('/api/benchmarks');
        if (changed('benchmarks', d)) { state.benchmarks = d; if (route().name === 'benchmarks') renderBenchmarks(); }
    } catch (e) {
        state.benchmarks = { error: e instanceof ApiError && e.status === 404 ? 'missing' : explain(e) };
        if (route().name === 'benchmarks') renderBenchmarks();
    }
}

// Accepts a list (contestants / rows) or benchmark.py's results.json ({current: {name: aggregate}})
function benchRows(d) {
    const list = d.contestants || d.rows || d.results;
    const rows = Array.isArray(list) ? list : Object.entries(d.current || list || {}).map(([name, r]) => ({ name, ...r }));
    return rows.map((r) => {
        const total = r.total ?? r.traps;
        const caught = r.caught;
        return { ...r, name: r.label || r.name || r.contestant, total, caught,
            accuracy: r.accuracy ?? (total ? caught / total : null), p50: r.p50_s ?? r.p50, cost: r.cost_per_1000 ?? r.usd_per_1000 };
    }).filter((r) => r.name);
}

function renderBenchmarks() {
    const el = $('benchmarks');
    if (!el) return;
    const d = state.benchmarks;
    if (!d) { el.innerHTML = whiteCard(empty('Loading benchmarks…')); return icons(); }
    if (d.error) {
        el.innerHTML = whiteCard(`${cardHead('Benchmarks', 'GET /api/benchmarks')}${empty(d.error === 'missing' ? 'This backend does not serve /api/benchmarks yet. Results live in docs/gx10_results.md until it does.' : esc(d.error), 'flask-conical')}`);
        return icons();
    }
    const rows = benchRows(d).sort((a, b) => (b.accuracy ?? -1) - (a.accuracy ?? -1));
    const g = d.gx10 || {}, tr = d.training || {};
    const best = rows.find((r) => /tuned|flock-judge-v1/i.test(r.name)) || rows[0];
    const bar = (r) => {
        const w = Math.round(100 * (r.accuracy || 0));
        const tuned = /tuned|flock-judge-v1/i.test(r.name), rules = /^rules/i.test(r.name);
        return `<div class="flex items-center gap-3 text-xs sm:text-sm">
            <span class="w-40 sm:w-64 shrink-0 truncate font-semibold ${tuned ? 'text-gray-900' : 'text-gray-600'}" title="${esc(r.name)}">${esc(r.name)}</span>
            <div class="flex-1 h-4 rounded-full bg-lightCardBg overflow-hidden"><div class="h-4 rounded-full ${tuned ? 'bg-limeAccent border border-black/10' : rules ? 'bg-cardDark' : 'bg-purpleAccent/70'}" style="width:${w}%"></div></div>
            <span class="w-24 text-right font-mono font-bold tabular-nums">${r.caught != null && r.total != null ? `${num(r.caught)}/${num(r.total)}` : pct(r.accuracy)}</span></div>`;
    };
    const kv = (o) => Object.entries(o).filter(([, v]) => v != null && typeof v !== 'object').map(([k, v]) => `
        <div class="flex justify-between gap-3 py-1.5 border-b border-white/10 text-xs"><span class="text-gray-400">${esc(label(k))}</span><span class="font-semibold text-right">${esc(typeof v === 'number' ? num(v) : v)}</span></div>`).join('');
    el.innerHTML = `
        <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3.5 lg:gap-4 mb-4 lg:mb-5">
            ${best ? statCard({ label: 'Best model judge', value: esc(best.caught != null ? `${num(best.caught)}/${num(best.total)}` : pct(best.accuracy)), sub: esc(best.name), icon: 'trophy', dark: true }) : ''}
            ${statCard({ label: 'Contestants', value: num(rows.length), sub: esc(d.seeds ? `held-out seeds ${d.seeds}` : 'same findings for each'), icon: 'users' })}
            ${statCard({ label: 'False alarms (all contestants)', value: num(rows.reduce((t, r) => t + (r.false_alarms || 0), 0)), sub: 'flagged when the answer key says fine', icon: 'shield-check' })}
            ${g.speedup || g.speedup_vs_mac ? statCard({ label: 'GX10 vs a laptop', value: `${esc(g.speedup || g.speedup_vs_mac)}×`, sub: esc(g.speedup_note || 'lower p50 per finding'), icon: 'cpu' }) : ''}
        </div>
        ${whiteCard(`${cardHead('Traps caught by contestant', esc(d.note || d.source || 'Answer-key traps caught on held-out seeds'))}<div class="flex flex-col gap-2.5">${rows.map(bar).join('') || empty('No results yet.')}</div>`, 'mb-4 lg:mb-5')}
        ${whiteCard(`${cardHead('Results', 'Every contestant judged the same findings')}
            <div class="overflow-x-auto"><table class="w-full text-left text-xs sm:text-sm min-w-[720px]">
                <thead class="text-gray-400 text-[11px] uppercase tracking-wider"><tr class="border-b border-gray-100">
                    <th class="py-2 pr-3">Contestant</th><th class="py-2 pr-3 text-right">Accuracy</th><th class="py-2 pr-3 text-right">Caught</th><th class="py-2 pr-3 text-right">False alarms</th>
                    <th class="py-2 pr-3 text-right">To a human</th><th class="py-2 pr-3 text-right">p50 s/finding</th><th class="py-2 pr-3 text-right">$ / 1k findings</th><th class="py-2 text-right">Runs offline</th></tr></thead>
                <tbody class="divide-y divide-gray-50">${rows.map((r) => `<tr class="${/tuned|flock-judge-v1/i.test(r.name) ? 'bg-limeAccent/15' : ''}">
                    <td class="py-2 pr-3 font-bold text-gray-900">${esc(r.name)}${r.status && r.status !== 'ok' ? ` ${pill('bg-gray-100 text-gray-600', r.status)}` : ''}</td>
                    <td class="py-2 pr-3 text-right tabular-nums">${r.accuracy != null ? `${(r.accuracy * 100).toFixed(1)}%` : '—'}</td>
                    <td class="py-2 pr-3 text-right font-mono tabular-nums">${r.caught != null ? `${num(r.caught)}/${num(r.total)}` : '—'}</td>
                    <td class="py-2 pr-3 text-right tabular-nums">${num(r.false_alarms)}</td>
                    <td class="py-2 pr-3 text-right tabular-nums">${num(r.to_human)}</td>
                    <td class="py-2 pr-3 text-right tabular-nums">${r.p50 != null ? Number(r.p50).toFixed(r.p50 < 0.01 ? 3 : 1) : '—'}</td>
                    <td class="py-2 pr-3 text-right tabular-nums">${r.cost != null ? money(r.cost) : '—'}</td>
                    <td class="py-2 text-right">${r.offline == null ? '—' : r.offline ? pill('bg-emerald-100 text-emerald-800', 'yes') : pill('bg-gray-100 text-gray-600', 'no')}</td></tr>`).join('')}</tbody></table></div>`, 'mb-4 lg:mb-5')}
        <div class="grid grid-cols-1 xl:grid-cols-2 gap-4 lg:gap-5">
            <section class="bg-cardDark text-white rounded-3xl p-5 sm:p-6 shadow-md">
                <h3 class="font-extrabold text-lg flex items-center gap-2"><i data-lucide="cpu" class="w-5 h-5 text-limeAccent"></i> The GX10</h3>
                <p class="text-xs text-gray-400 mt-1 mb-3">${esc(g.summary || 'NVIDIA GB10 box on the team tailnet, Ollama over an SSH tunnel: judging runs with nothing leaving the network.')}</p>
                ${kv(g) || '<p class="text-sm text-gray-500">No GX10 details in this response.</p>'}
            </section>
            <section class="bg-white rounded-3xl p-5 sm:p-6 card-shadow border border-gray-100">
                <h3 class="font-extrabold text-lg text-gray-900 flex items-center gap-2"><i data-lucide="graduation-cap" class="w-5 h-5 text-purpleAccent"></i> Fine-tuning our judge</h3>
                <p class="text-xs text-gray-400 mt-1 mb-3">${esc(tr.summary || 'flock-judge-v1: a LoRA on Qwen2.5-7B trained on the judge’s own verdict format, served from the GX10.')}</p>
                <div class="text-gray-900">${Object.entries(tr).filter(([k, v]) => k !== 'summary' && v != null && typeof v !== 'object').map(([k, v]) => `
                    <div class="flex justify-between gap-3 py-1.5 border-b border-gray-100 text-xs"><span class="text-gray-500">${esc(label(k))}</span><span class="font-semibold text-right">${esc(typeof v === 'number' ? num(v) : v)}</span></div>`).join('') || '<p class="text-sm text-gray-400">No training details in this response.</p>'}</div>
            </section>
        </div>`;
    icons();
}

/* ---------------- Clicky's screen ---------------- */

function openScreen(id) {
    const url = screenUrl(id);
    $('screenTitle').textContent = id;
    $('screenOpen').href = url;
    $('screenFrame').src = url;
    $('screenModal').classList.replace('hidden', 'flex');
}
function closeScreen() {
    $('screenModal').classList.replace('flex', 'hidden');
    $('screenFrame').src = 'about:blank';
}

/* ---------------- sign in (POST /api/login, GET /api/me) ---------------- */

function setUser(user) {
    settings.user = user || null;
    settings.actor = user?.name || user?.username || 'Preet';
    renderActor();
    const org = user?.org || "Noah's Bagels";
    $('orgName').textContent = org;
    $('orgLogo').textContent = org.replace(/['’]s\b/g, '').split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
    if (route().name === 'overview') setWelcome();
}
function showLogin(msg = '') {
    $('loginError').textContent = msg;
    $('loginError').classList.toggle('hidden', !msg);
    $('loginScreen').classList.replace('hidden', 'flex');
    setTimeout(() => ($('loginUser').value ? $('loginPass') : $('loginUser')).focus(), 0);
}
// 'login': show the sign-in screen until /api/me says who this is. 'token': an older API, no sign-in.
async function checkAuth() {
    try {
        const me = await get('/api/me');
        settings.auth = 'login';
        setUser(me.user || me);
        return true;
    } catch (e) {
        if (e instanceof ApiError && e.status === 404) { settings.auth = 'token'; setUser(null); return true; }
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) { settings.auth = 'login'; showLogin(); return false; }
        settings.auth = 'token'; setUser(null); return true;   // unreachable: the console shows offline
    }
}
async function login(username, password) {
    $('loginBtn').disabled = true;
    try {
        const res = await fetch(state.base + '/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
        const data = await res.json().catch(() => ({}));
        if (res.status === 404) { settings.auth = 'token'; $('loginScreen').classList.replace('flex', 'hidden'); return finishToken(null); }
        if (!res.ok || !data.token) return showLogin(data.error || 'That username and password did not match.');
        settings.token = data.token;
        save('flock.token', data.token);
        $('loginPass').value = '';
        setUser(data.user || { name: username });
        finishToken(data.token);
        state.cache = {};
        toast(`Signed in as ${settings.actor}.`, 'ok');
        if (state.drawer) refreshDrawer();
    } catch (e) {
        showLogin(`Can't reach the API (${e.message}).`);
    } finally { $('loginBtn').disabled = false; }
}
function logout() {
    $('userMenu').classList.add('hidden');
    if (settings.token && settings.auth === 'login') fetch(state.base + '/api/logout', { method: 'POST', headers: { Authorization: `Bearer ${settings.token}` } }).catch(() => {});
    settings.token = '';
    save('flock.token', '');
    setUser(null);
    if (settings.auth === 'login') showLogin();
    else toast('Token cleared.', '');
}
function toggleUserMenu(force) {
    const m = $('userMenu');
    const open = force ?? m.classList.contains('hidden');
    m.classList.toggle('hidden', !open);
    if (open) {
        $('userName').textContent = settings.actor;
        $('userOrg').textContent = settings.user ? [settings.user.role, settings.user.org].filter(Boolean).join(' · ') : (settings.token ? 'presenter token set' : 'not signed in');
        icons();
    }
}

/* ---------------- settings, modal, toast ---------------- */

function openSettings() {
    toggleUserMenu(false);
    $('apiInput').value = store('flock.api');
    $('apiHint').textContent = `Now using ${settings.api} (${settings.apiSource}). ?api=<url> in the address overrides; when this page is served by a Flock API it uses that API.`;
    $('settingsModal').classList.replace('hidden', 'flex');
    setTimeout(() => $('apiInput').focus(), 0);
}
function closeSettings() { $('settingsModal').classList.replace('flex', 'hidden'); }

function openModal(title, html) {
    $('modalTitle').textContent = title;
    $('modalBody').innerHTML = html;
    $('actionModal').classList.remove('opacity-0', 'pointer-events-none');
    $('modalContainer').classList.replace('scale-95', 'scale-100');
    icons();
}
function closeModal() {
    $('actionModal').classList.add('opacity-0', 'pointer-events-none');
    $('modalContainer').classList.replace('scale-100', 'scale-95');
}

let toastTimer;
function toast(msg, kind = '') {
    const t = $('toast');
    t.textContent = msg;
    t.className = `fixed bottom-24 lg:bottom-6 left-1/2 -translate-x-1/2 z-[80] rounded-2xl text-sm font-semibold px-4 py-3 shadow-xl max-w-[90vw] ${kind === 'error' ? 'bg-red-600 text-white' : kind === 'ok' ? 'bg-limeAccent text-black' : 'bg-cardDark text-white'}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), kind === 'error' ? 7000 : 4500);
}

function renderActor() {
    const b = $('actorBadge');
    const initials = String(settings.actor || '').split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
    b.innerHTML = initials ? esc(initials) : '<i data-lucide="user" class="w-4 h-4"></i>';
    b.title = `Acting as ${settings.actor}`;
    icons();
}

/* ---------------- wiring ---------------- */

document.addEventListener('click', (e) => {
    const t = e.target;
    if (t.closest('[data-bell]')) return toggleBell();
    if (!t.closest('#bellPanel') || t.closest('[data-finding]')) toggleBell(false);
    if (t.closest('[data-user-menu]')) return toggleUserMenu();
    if (t.closest('[data-logout]')) return logout();
    if (!t.closest('#userMenu')) toggleUserMenu(false);
    if (t.closest('[data-live-run]')) return startLiveRun();
    if (t.closest('[data-job-close]')) { state.job = null; return renderJob(); }
    if (t.closest('[data-th-live]')) { theater.mode = 'live'; return; }
    if (t.closest('[data-token-cancel]')) return finishToken(null);
    if (t.closest('[data-close-screen]') || t.id === 'screenModal') return closeScreen();
    const evb = t.closest('[data-ev-ref]');
    if (evb) return openEvidence(evb.dataset.evRef);
    const scr = t.closest('[data-screen]');
    if (scr) return openScreen(scr.dataset.screen);
    if (t.closest('[data-close-drawer]')) return closeDrawer();
    if (t.closest('[data-open-settings]')) return openSettings();
    if (t.closest('[data-close-settings]')) return closeSettings();
    if (t.closest('[data-close-modal]') || t.id === 'actionModal') return closeModal();
    const how = t.closest('[data-how]');
    if (how) return showHow(how.dataset.how);
    const ff = t.closest('[data-ffilter]');
    if (ff) { state.findingFilter = ff.dataset.ffilter; return renderFindings(); }
    const dec = t.closest('[data-decide]');
    if (dec && state.drawer) return decide(state.drawer.id, dec.dataset.decide);
    if (t.closest('[data-try-agent]')) return tryAsAgent();
    const fu = t.closest('[data-fu]');
    if (fu && state.drawer) return followUp(state.drawer.id, fu.dataset.fu);
    const th = t.closest('[data-th]');
    if (th) return theaterStart(th.dataset.th);
    if (t.id === 'hoursYear') return;
    const sv = t.closest('[data-saved]');
    if (sv) { savedPeriod = sv.dataset.saved; return renderMetrics(); }
    const sp = t.closest('[data-speed]');
    if (sp) { theater.speed = Number(sp.dataset.speed); return theaterStart('replay'); }
    if (t.closest('a[href^="#"]')) return;   // plain links route themselves
    const f = t.closest('[data-finding]');
    if (f && f.dataset.finding && !(t.closest('#drawer') && f.closest('[data-test="audit-trail"]'))) {
        location.hash = `#/finding/${encodeURIComponent(f.dataset.finding)}`;
        return;
    }
    const a = t.closest('[data-agent]');
    if (a) return openDrawer('agent', a.dataset.agent);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeScreen(); toggleBell(false); closeDrawer(); closeSettings(); closeModal(); } });
window.addEventListener('hashchange', onRoute);
document.addEventListener('change', (e) => { if (e.target.id === 'hoursYear') { hoursYear = e.target.value; renderHours(); } });

$('loginForm').onsubmit = (e) => { e.preventDefault(); login($('loginUser').value.trim(), $('loginPass').value); };
$('tokenForm').onsubmit = (e) => { e.preventDefault(); finishToken($('tokenOnce').value.trim() || null); };

$('settingsForm').onsubmit = (e) => {
    e.preventDefault();
    const api = $('apiInput').value.trim().replace(/\/$/, '');
    const apiChanged = api !== store('flock.api');
    save('flock.api', api);
    closeSettings();
    renderActor();
    if (apiChanged) location.reload();
    else { toast('Saved.', 'ok'); state.cache.drawer = null; refreshDrawer(); }
};

(async function init() {
    // ?token=<t> once: keep it, then take it out of the address bar
    if (params.get('token')) {
        settings.token = params.get('token').trim();
        save('flock.token', settings.token);
        params.delete('token');
        history.replaceState(null, '', `${location.pathname}${params.toString() ? `?${params}` : ''}${location.hash}`);
    }
    renderActor();
    [settings.api, settings.apiSource] = await detectBase();
    state.base = settings.api;
    $('apiBase').textContent = `API: ${settings.api}`;
    await checkAuth();
    onRoute();
    pollFast();
    pollSlow();
    tryGet('/api/health').then((h) => { if (h?.running_job) trackJob({ job_id: h.running_job, status: 'running' }); });
})();
