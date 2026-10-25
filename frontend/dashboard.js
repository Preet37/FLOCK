/* ==========================================================================
   Flock console: live views over the Flock API (reads are open, POSTs need a token)
   ========================================================================== */
const DEFAULT_API = 'https://web-production-812b4.up.railway.app';
const FAST_MS = 1500;   // audit log + agents
const SLOW_MS = 4000;   // findings, score, metrics, transactions

function store(key, fallback = '') {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode: keep in memory only */ }
}

// ?api=http://localhost:8787 overrides the backend for local runs
const params = new URLSearchParams(location.search);
const settings = {
    api: (params.get('api') || store('flock.api', DEFAULT_API)).replace(/\/$/, ''),
    actor: store('flock.actor'),
    token: store('flock.token'),
};

const state = {
    log: [], findings: [], agents: null, score: null, metrics: null, txns: null,
    findingFilter: 'all', txnFilter: 'all',
    seenLog: 0, drawer: null, online: null,
    cache: {},
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (n) => (n == null || isNaN(n)) ? '—' : `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const pct = (n) => (n == null || isNaN(n)) ? '—' : `${Math.round(Number(n) * 100)}%`;
const icons = () => window.lucide && lucide.createIcons();

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

const missing = {};   // routes that 404'd: re-check once a minute instead of every poll

async function get(path) {
    if (missing[path] && Date.now() - missing[path] < 60_000) return null;
    const res = await fetch(state.cache.base + path, { cache: 'no-store' });
    if (res.status === 404) { missing[path] = Date.now(); return null; }   // route not deployed yet
    delete missing[path];
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
}

async function post(path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (settings.token) headers.Authorization = `Bearer ${settings.token}`;
    const res = await fetch(state.cache.base + path, { method: 'POST', headers, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
}

// Re-render only when the payload actually changed, so polling never flickers
function changed(key, data) {
    const s = JSON.stringify(data);
    if (state.cache[key] === s) return false;
    state.cache[key] = s;
    return true;
}

function setOnline(ok, msg) {
    if (state.online === ok && !msg) return;
    state.online = ok;
    $('liveStatus').innerHTML = ok
        ? '<span class="w-2 h-2 rounded-full bg-emerald-500 pulse-dot"></span> Live'
        : `<span class="w-2 h-2 rounded-full bg-red-500"></span> ${esc(msg || 'Offline')}`;
}

async function pollFast() {
    try {
        const [log, agents] = await Promise.all([get('/api/log'), get('/api/agents')]);
        setOnline(true);
        if (log && changed('log', log)) { state.log = log; renderLog(); }
        if (agents && changed('agents', agents)) { state.agents = agents; renderAgents(); }
        else renderAgents();   // keep "x s ago" fresh
        if (state.drawer) refreshDrawer();
    } catch (e) {
        setOnline(false, 'API unreachable');
    } finally {
        setTimeout(pollFast, FAST_MS);
    }
}

async function pollSlow() {
    try {
        const [findings, score, metrics, txns] = await Promise.all([
            get('/api/findings'), get('/api/score'),
            get('/api/metrics').catch(() => null), get('/api/transactions').catch(() => null),
        ]);
        if (findings && changed('findings', findings)) { state.findings = findings; renderFindings(); }
        const m = { score, metrics };
        if (changed('metrics', m)) { state.score = score; state.metrics = metrics; renderMetrics(); }
        if (changed('txns', txns)) { state.txns = txns; renderTxns(); }
    } catch { /* pollFast reports connectivity */ }
    finally {
        setTimeout(pollSlow, SLOW_MS);
    }
}

/* ---------------- metric cards ---------------- */

// /api/metrics may arrive as an array of cards, {cards: [...]}, or {key: card}
function metricCards(metrics) {
    if (!metrics) return null;
    const list = Array.isArray(metrics) ? metrics
        : Array.isArray(metrics.cards) ? metrics.cards
            : Object.entries(metrics).filter(([, v]) => v && typeof v === 'object').map(([k, v]) => ({ key: k, ...v }));
    if (!list.length) return null;
    return list.map((c) => ({
        label: c.label || c.title || c.name || c.key,
        value: c.display ?? c.value_display ?? fmtMetric(c),
        sub: c.compare || c.subtitle || c.detail || '',
        formula: c.formula || '',
        source: Array.isArray(c.source) ? c.source.join(', ') : (c.source || ''),
    }));
}

function fmtMetric(c) {
    const v = c.value;
    if (v == null) return '—';
    if (c.unit === '$' || c.unit === 'usd' || /cost|saved|money|dollar/i.test(c.key || c.label || '')) return money(v);
    if (c.unit === '%' || /accuracy/i.test(c.key || c.label || '')) return v <= 1 ? pct(v) : `${v}%`;
    if (c.unit) return `${v} ${c.unit}`;
    return String(v);
}

function scoreCards(s) {
    if (!s) return [];
    return [
        { label: 'Traps caught', value: `${s.caught}/${s.total}`, sub: `${s.missed} missed`, formula: 'caught ÷ planted traps', source: 'answer key vs findings' },
        { label: 'Recovered', value: money(s.recovered), sub: 'recoverable gaps found', formula: 'Σ gap of findings judged recoverable', source: '/api/score' },
        { label: 'Sent to a human', value: s.to_human, sub: 'judges disagreed', formula: 'findings where the panel did not agree', source: '/api/score' },
        { label: 'False alarms', value: s.false_alarms, sub: 'flagged but correct', formula: 'findings on orders the answer key marks fine', source: 'answer key' },
    ];
}

function renderMetrics() {
    const cards = metricCards(state.metrics) || scoreCards(state.score);
    $('metrics').innerHTML = cards.map((c, i) => `
        <div class="${i === 0 ? 'bg-cardDark text-white shadow-md' : 'bg-white text-gray-900 card-shadow border border-gray-100'} p-4 sm:p-5 rounded-3xl flex flex-col justify-between gap-2 group relative">
            <p class="${i === 0 ? 'text-gray-400' : 'text-gray-500'} text-xs font-semibold">${esc(c.label)}</p>
            <h2 class="text-xl sm:text-2xl xl:text-3xl font-extrabold tracking-tight">${esc(c.value)}</h2>
            ${c.sub ? `<p class="${i === 0 ? 'text-limeAccent' : 'text-gray-500'} text-xs font-semibold">${esc(c.sub)}</p>` : ''}
            ${(c.formula || c.source) ? `
            <details class="text-[11px] ${i === 0 ? 'text-gray-400' : 'text-gray-500'}">
                <summary class="cursor-pointer font-semibold select-none">How we got this</summary>
                ${c.formula ? `<p class="mt-1"><span class="font-bold">Formula:</span> ${esc(c.formula)}</p>` : ''}
                ${c.source ? `<p class="mt-0.5"><span class="font-bold">Source:</span> ${esc(c.source)}</p>` : ''}
            </details>` : ''}
        </div>`).join('');
}

/* ---------------- agent chain ---------------- */

function renderAgents() {
    const a = state.agents;
    if (!a) return;
    const list = a.agents || [];
    const byId = Object.fromEntries(list.map((x) => [x.actor, x]));
    $('agentChain').innerHTML = list.map((ag, i) => {
        const fresh = ag.last_at && (Date.now() - new Date(ag.last_at).getTime()) < 60_000;
        const working = ag.status === 'working' || ag.running || fresh;
        const parent = ag.spawned_by || ag.parent;
        const parallel = ag.parallel;
        return `
        ${i ? '<i data-lucide="chevron-right" class="w-4 h-4 text-gray-600 shrink-0 self-center"></i>' : ''}
        <button data-agent="${esc(ag.actor)}" class="shrink-0 text-left w-44 rounded-2xl p-3 transition border ${working ? 'bg-limeAccent/10 border-limeAccent/60' : 'bg-white/5 border-white/5 hover:bg-white/10'}">
            <div class="flex items-center gap-2 mb-1">
                <span class="w-2 h-2 rounded-full ${working ? 'bg-limeAccent pulse-dot' : ag.actions ? 'bg-purpleAccent' : 'bg-gray-600'}"></span>
                <span class="font-bold text-sm truncate">${esc(ag.name)}</span>
            </div>
            <p class="text-[11px] text-gray-400 leading-snug line-clamp-2 h-8">${esc(ag.job)}</p>
            <div class="flex items-center justify-between mt-2 text-[11px] font-semibold">
                <span class="${working ? 'text-limeAccent' : 'text-gray-400'}">${working ? 'working' : ag.actions ? `${ag.actions} actions` : 'waiting'}</span>
                <span class="text-gray-500">${esc(ago(ag.last_at))}</span>
            </div>
            ${parent || parallel ? `<p class="text-[10px] text-gray-500 mt-1 truncate">${parent ? `spawned by ${esc(byId[parent]?.name || parent)}` : ''}${parent && parallel ? ' · ' : ''}${parallel ? 'parallel' : ''}</p>` : ''}
        </button>`;
    }).join('');
    icons();
}

/* ---------------- findings ---------------- */

const OUTCOME = {
    recoverable: 'bg-emerald-100 text-emerald-800',
    conflict: 'bg-amber-100 text-amber-800',
    correct: 'bg-gray-100 text-gray-600',
    disputed: 'bg-red-100 text-red-700',
};
const STATE_PILL = {
    NEW: 'bg-gray-100 text-gray-600', CHECKED: 'bg-gray-100 text-gray-700', JUDGED: 'bg-purpleAccent/10 text-purpleAccent',
    APPROVED: 'bg-emerald-100 text-emerald-800', REJECTED: 'bg-red-100 text-red-700', FILED: 'bg-sky-100 text-sky-800', RECOVERED: 'bg-limeAccent text-black',
};
const pill = (cls, text) => `<span class="inline-block rounded-full px-2 py-0.5 text-[11px] font-bold whitespace-nowrap ${cls}">${esc(text)}</span>`;
const fid = (f) => f.finding_id || f.id;
const riskOf = (f) => f.risk_score ?? f.risk ?? null;

function riskPill(r) {
    if (r == null) return '<span class="text-gray-300">—</span>';
    const cls = r >= 70 ? 'bg-red-100 text-red-700' : r >= 40 ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-600';
    return pill(cls, Math.round(r));
}

function filterBar(el, options, current, onPick) {
    el.innerHTML = options.map(([key, label, n]) => `
        <button data-key="${esc(key)}" class="rounded-full px-3 py-1 transition ${current === key ? 'bg-cardDark text-white' : 'bg-lightCardBg text-gray-600 hover:bg-gray-200'}">${esc(label)}${n != null ? ` <span class="opacity-60">${n}</span>` : ''}</button>`).join('');
    el.querySelectorAll('button').forEach((b) => b.onclick = () => onPick(b.dataset.key));
}

function renderFindings() {
    const all = state.findings;
    const needsHuman = (f) => f.state === 'JUDGED';
    const count = (fn) => all.filter(fn).length;
    filterBar($('findingFilters'), [
        ['all', 'All', all.length],
        ['review', 'Needs review', count(needsHuman)],
        ['recoverable', 'Recoverable', count((f) => f.outcome === 'recoverable')],
        ['conflict', 'Conflict', count((f) => f.outcome === 'conflict')],
    ], state.findingFilter, (k) => { state.findingFilter = k; renderFindings(); });

    const rows = all.filter((f) => state.findingFilter === 'all' ? true
        : state.findingFilter === 'review' ? needsHuman(f) : f.outcome === state.findingFilter);
    rows.sort((a, b) => (riskOf(b) ?? -1) - (riskOf(a) ?? -1));

    $('findingRows').innerHTML = rows.length ? rows.map((f) => `
        <tr data-finding="${esc(fid(f))}" class="cursor-pointer hover:bg-lightCardBg transition">
            <td class="py-2.5 pr-3 font-bold text-gray-900 whitespace-nowrap">${esc(fid(f))}</td>
            <td class="py-2.5 pr-3 capitalize whitespace-nowrap">${esc(f.channel)}</td>
            <td class="py-2.5 pr-3 whitespace-nowrap">${esc((f.check_type || '').replace(/_/g, ' '))}</td>
            <td class="py-2.5 pr-3 text-right tabular-nums">${money(f.expected)}</td>
            <td class="py-2.5 pr-3 text-right tabular-nums">${money(f.actual)}</td>
            <td class="py-2.5 pr-3 text-right tabular-nums font-bold ${f.gap > 0 ? 'text-red-600' : ''}">${money(f.gap)}</td>
            <td class="py-2.5 pr-3">${pill(OUTCOME[f.outcome] || 'bg-gray-100 text-gray-600', f.outcome || '—')} <span class="text-gray-400 text-[11px]">${pct(f.confidence)}</span></td>
            <td class="py-2.5 pr-3 text-right">${riskPill(riskOf(f))}</td>
            <td class="py-2.5">${pill(STATE_PILL[f.state] || 'bg-gray-100 text-gray-600', f.state || '—')}</td>
        </tr>`).join('')
        : `<tr><td colspan="9" class="py-8 text-center text-gray-400">No findings ${state.findingFilter === 'all' ? 'yet' : 'in this view'}.</td></tr>`;
}

/* ---------------- transactions ---------------- */

const TXN_STATUS = {
    matched: 'bg-emerald-100 text-emerald-800', short: 'bg-amber-100 text-amber-800',
    missing: 'bg-red-100 text-red-700', disputed: 'bg-purpleAccent/10 text-purpleAccent',
};

function renderTxns() {
    const raw = state.txns;
    const rows = Array.isArray(raw) ? raw : raw && (raw.transactions || raw.rows || raw.items);
    if (!rows) {
        $('txnFilters').innerHTML = '';
        $('txnBody').innerHTML = `<div class="py-10 text-center text-gray-400 text-sm">
            <i data-lucide="hourglass" class="w-5 h-5 mx-auto mb-2"></i>
            The transactions feed isn't on the backend yet. This table fills itself once <code class="text-gray-500">/api/transactions</code> ships.</div>`;
        return icons();
    }
    const statusOf = (r) => String(r.status || r.match_status || '').toLowerCase();
    const counts = {};
    rows.forEach((r) => { const s = statusOf(r); counts[s] = (counts[s] || 0) + 1; });
    filterBar($('txnFilters'), [['all', 'All', rows.length], ...['matched', 'short', 'missing', 'disputed'].filter((s) => counts[s]).map((s) => [s, s[0].toUpperCase() + s.slice(1), counts[s]])],
        state.txnFilter, (k) => { state.txnFilter = k; renderTxns(); });

    const shown = rows.filter((r) => state.txnFilter === 'all' || statusOf(r) === state.txnFilter);
    const cols = Object.keys(rows[0] || {}).filter((k) => !['status', 'match_status'].includes(k) && typeof rows[0][k] !== 'object');
    const isMoney = (k) => /amount|payout|deposit|gross|net|fee|gap|total|expected|actual/i.test(k);
    $('txnBody').innerHTML = `
        <table class="w-full text-left text-xs sm:text-sm">
            <thead class="text-gray-400 text-[11px] uppercase tracking-wide sticky top-0 bg-white">
                <tr>${cols.map((k) => `<th class="py-2 pr-3 font-bold whitespace-nowrap ${isMoney(k) ? 'text-right' : ''}">${esc(k.replace(/_/g, ' '))}</th>`).join('')}<th class="py-2 font-bold">Status</th></tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
                ${shown.slice(0, 500).map((r) => `<tr class="hover:bg-lightCardBg">
                    ${cols.map((k) => `<td class="py-2 pr-3 whitespace-nowrap ${isMoney(k) ? 'text-right tabular-nums' : ''}">${isMoney(k) && typeof r[k] === 'number' ? money(r[k]) : esc(r[k] ?? '—')}</td>`).join('')}
                    <td class="py-2">${pill(TXN_STATUS[statusOf(r)] || 'bg-gray-100 text-gray-600', statusOf(r) || '—')}</td>
                </tr>`).join('')}
            </tbody>
        </table>
        ${shown.length > 500 ? `<p class="text-center text-xs text-gray-400 py-2">Showing first 500 of ${shown.length}</p>` : ''}`;
}

/* ---------------- audit log ---------------- */

function logRow(e, fresh) {
    const refused = e.allowed === false;
    const move = e.before || e.after ? `<span class="text-gray-500">${esc(e.before || '∅')} → </span><span class="${refused ? 'text-red-400 line-through' : 'text-white'}">${esc(e.after || '∅')}</span>` : '';
    return `
        <button data-finding="${e.subject_type === 'finding' ? esc(e.subject_id) : ''}" class="w-full text-left rounded-2xl p-2.5 ${refused ? 'bg-red-500/10 border border-red-500/30' : 'bg-white/5 hover:bg-white/10'} ${fresh ? 'flash' : ''}">
            <div class="flex items-center justify-between gap-2 text-[11px]">
                <span class="font-bold ${e.role === 'human' ? 'text-limeAccent' : refused ? 'text-red-300' : 'text-purple-300'}">${esc(e.actor)} <span class="text-gray-500 font-medium">· ${esc(e.role)}</span></span>
                <span class="text-gray-500 tabular-nums">${esc(clock(e.at))}</span>
            </div>
            <div class="text-xs mt-0.5 flex flex-wrap gap-x-1.5">
                ${refused ? '<span class="font-bold text-red-400">REFUSED</span>' : ''}
                <span class="font-bold text-white">${esc(e.action)}</span>
                <span class="text-gray-400">${esc(e.subject_id)}</span>
                ${move}
            </div>
            ${e.reason ? `<p class="text-[11px] text-gray-400 mt-0.5 leading-snug">${esc(e.reason)}</p>` : ''}
        </button>`;
}

function renderLog() {
    const entries = [...state.log].reverse();
    const fresh = state.seenLog ? state.log.length - state.seenLog : 0;
    state.seenLog = state.log.length;
    const refused = state.log.filter((e) => e.allowed === false).length;
    $('logCount').textContent = `${state.log.length} entries${refused ? ` · ${refused} refused` : ''}`;
    $('logList').innerHTML = entries.length ? entries.map((e, i) => logRow(e, i < fresh)).join('')
        : '<p class="text-gray-500 text-sm text-center py-8">Nothing has happened yet.</p>';
}

/* ---------------- detail drawer ---------------- */

function openDrawer(kind, id) {
    state.drawer = { kind, id };
    state.cache.drawer = null;
    $('drawer').classList.remove('hidden');
    $('drawerTitle').textContent = id;
    $('drawerBody').innerHTML = '<p class="text-gray-400 text-sm">Loading…</p>';
    refreshDrawer();
}

function closeDrawer() {
    state.drawer = null;
    $('drawer').classList.add('hidden');
}

async function refreshDrawer() {
    const d = state.drawer;
    if (!d) return;
    try {
        if (d.kind === 'finding') {
            const data = await get(`/api/findings/${encodeURIComponent(d.id)}`);
            if (state.drawer !== d || !changed('drawer', data)) return;
            renderFindingDetail(data);
        } else {
            const data = await get(`/api/agents/${encodeURIComponent(d.id)}`).catch(() => null);
            if (state.drawer !== d) return;
            const payload = { data, log: data ? null : state.log.filter((e) => e.actor === d.id) };
            if (!changed('drawer', payload)) return;
            renderAgentDetail(d.id, payload);
        }
        icons();
    } catch (e) {
        $('drawerBody').innerHTML = `<p class="text-red-600 text-sm">Couldn't load: ${esc(e.message)}</p>`;
    }
}

const card = (title, body, extra = '') => `<section class="bg-white rounded-3xl p-4 sm:p-5 card-shadow border border-gray-100 ${extra}"><h4 class="font-bold text-gray-900 text-sm mb-3">${title}</h4>${body}</section>`;

function parseMemo(m) {
    if (!m) return null;
    if (typeof m === 'object') return m;
    try { return JSON.parse(m); } catch { return { memo: m }; }
}

function renderFindingDetail(data) {
    if (!data) { $('drawerBody').innerHTML = '<p class="text-gray-400 text-sm">This finding no longer exists.</p>'; return; }
    const f = data.finding || data;
    const log = data.log || [];
    $('drawerTitle').textContent = `${fid(f)} · ${(f.check_type || '').replace(/_/g, ' ')} on ${f.channel}`;

    const rounds = {};
    (f.votes || []).forEach((v) => (rounds[v.round || 1] ||= []).push(v));
    const memo = parseMemo(f.review_memo);
    const canDecide = f.state === 'JUDGED';
    const judges = Object.fromEntries((state.agents?.judges || []).map((j) => [j.id, j]));
    const typed = $('decisionReason')?.value || '';   // keep a half-typed reason across live refreshes

    $('drawerBody').innerHTML = [
        `<div class="grid grid-cols-3 gap-3">
            ${[['Deal says', f.expected, 'text-gray-900'], ['Was charged', f.actual, 'text-gray-900'], ['Gap', f.gap, f.gap > 0 ? 'text-red-600' : 'text-gray-900']].map(([l, v, c]) => `
            <div class="bg-white rounded-2xl p-3 sm:p-4 card-shadow border border-gray-100">
                <p class="text-gray-500 text-[11px] font-semibold">${l}</p>
                <p class="text-lg sm:text-2xl font-extrabold tabular-nums ${c}">${money(v)}</p>
            </div>`).join('')}
        </div>`,
        `<div class="flex flex-wrap items-center gap-2 text-xs">
            ${pill(OUTCOME[f.outcome] || 'bg-gray-100 text-gray-600', `${f.outcome} · ${pct(f.confidence)}`)}
            ${pill(STATE_PILL[f.state] || 'bg-gray-100', f.state)}
            ${riskOf(f) != null ? `<span class="text-gray-500 font-semibold">Risk</span> ${riskPill(riskOf(f))}` : ''}
            ${f.attention ? `<span class="text-gray-500 font-semibold">Queue rank #${esc(f.attention.rank)} · ${esc(f.attention.status)}</span>` : ''}
            <span class="text-gray-500 font-semibold">Orders: ${esc((f.order_ids || []).join(', '))}</span>
        </div>`,
        canDecide ? card('Your call', `
            <p class="text-xs text-gray-500 mb-3">Approving hands it to Clicky to file in the sandbox portal. Rejecting closes it. Both are written to the audit log under your name.</p>
            <textarea id="decisionReason" rows="2" class="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-purpleAccent/40" placeholder="Why? (recorded in the log)"></textarea>
            <div class="flex gap-2">
                <button data-decide="approve" class="flex-1 rounded-xl bg-limeAccent hover:bg-limeAccentHover text-black font-bold text-sm py-2.5 flex items-center justify-center gap-1.5"><i data-lucide="check" class="w-4 h-4"></i> Approve</button>
                <button data-decide="reject" class="flex-1 rounded-xl bg-cardDark hover:bg-black text-white font-bold text-sm py-2.5 flex items-center justify-center gap-1.5"><i data-lucide="x" class="w-4 h-4"></i> Reject</button>
            </div>
            <p class="text-[11px] text-gray-400 mt-2">${settings.token ? `Acting as <b>${esc(settings.actor || 'reviewer')}</b>.` : 'No token set; the backend will refuse until you add one in settings.'}</p>`, 'ring-2 ring-purpleAccent/30') : '',
        f.reason ? card('Panel verdict', `<p class="text-sm text-gray-700 leading-relaxed">${esc(f.reason)}</p>`) : '',
        memo && memo.memo ? card('Reviewer memo', `<p class="text-sm text-gray-700 leading-relaxed">${esc(memo.memo)}</p>`) : '',
        card('Evidence', (f.evidence || []).map((ev) => `
            <div class="rounded-2xl bg-lightCardBg p-3 mb-2 last:mb-0">
                <div class="flex items-center justify-between gap-2 text-[11px] font-semibold text-gray-500">
                    <span class="truncate font-mono">${esc(ev.source_file)}${ev.row != null ? `:${esc(ev.row)}` : ''}</span>
                    ${ev.authority ? pill(ev.authority === 'contract' ? 'bg-emerald-100 text-emerald-800' : /unauth/.test(ev.authority) ? 'bg-red-100 text-red-700' : 'bg-gray-200 text-gray-700', ev.authority.replace(/_/g, ' ')) : ''}
                </div>
                <p class="text-sm text-gray-800 mt-1">${ev.quote ? `“${esc(ev.quote)}”` : esc(ev.what)}</p>
            </div>`).join('') || '<p class="text-sm text-gray-400">No evidence attached.</p>'),
        card('Judge votes', Object.keys(rounds).sort().map((r) => `
            <p class="text-[11px] font-bold uppercase tracking-wide text-gray-400 mb-2 ${r > 1 ? 'mt-4' : ''}">Round ${esc(r)}${r > 1 ? ' · after debate' : ''}</p>
            <div class="grid gap-2">${rounds[r].map((v) => `
                <div class="rounded-2xl border border-gray-100 p-3">
                    <div class="flex items-center justify-between gap-2">
                        <span class="text-sm font-bold">${esc(judges[v.judge]?.name || v.judge)}${v.revised ? ' <span class="text-[11px] text-amber-600 font-semibold">changed vote</span>' : ''}</span>
                        ${pill(OUTCOME[v.outcome] || 'bg-gray-100', `${v.outcome} · ${pct(v.confidence)}`)}
                    </div>
                    <p class="text-xs text-gray-600 mt-1 leading-snug">${esc(v.reason)}</p>
                </div>`).join('')}</div>`).join('') || '<p class="text-sm text-gray-400">Not judged yet.</p>'),
        f.dispute_draft ? card('Dispute draft', `<pre class="text-xs whitespace-pre-wrap text-gray-700 font-sans">${esc(typeof f.dispute_draft === 'string' ? f.dispute_draft : JSON.stringify(f.dispute_draft, null, 2))}</pre>`) : '',
        card('Case file', `<div class="bg-cardDark rounded-2xl p-2 space-y-1.5">${[...log].reverse().map((e) => logRow(e)).join('') || '<p class="text-gray-500 text-sm p-2">No moves yet.</p>'}</div>`),
    ].join('');

    if ($('decisionReason')) $('decisionReason').value = typed;
    $('drawerBody').querySelectorAll('[data-decide]').forEach((b) => b.onclick = () => decide(fid(f), b.dataset.decide));
}

const IMG_RE = /\.(png|jpe?g|gif|webp)(\?|$)/i;
const absUrl = (p) => /^https?:|^data:/.test(p) ? p : `${state.cache.base}/${String(p).replace(/^\//, '')}`;

function collectImages(obj, out = []) {
    if (typeof obj === 'string' && (IMG_RE.test(obj) || obj.startsWith('data:image'))) out.push(obj);
    else if (Array.isArray(obj)) obj.forEach((x) => collectImages(x, out));
    else if (obj && typeof obj === 'object') Object.values(obj).forEach((x) => collectImages(x, out));
    return out;
}

function renderAgentDetail(id, { data, log }) {
    const ag = (state.agents?.agents || []).find((a) => a.actor === id) || {};
    $('drawerTitle').textContent = ag.name || id;
    const entries = data ? (data.log || data.activity || data.actions || []) : log;
    const shots = data ? collectImages(data.screenshots || data.screens || data.screen || []) : [];
    $('drawerBody').innerHTML = [
        card('What it does', `<p class="text-sm text-gray-700">${esc(ag.job || data?.job || '')}</p>
            <p class="text-xs text-gray-500 mt-2">Owner: ${esc(ag.owner || data?.owner || '—')} · ${esc(ag.actions ?? entries.length)} actions · last ${esc(ago(ag.last_at))}</p>`),
        shots.length ? card('Screen', `<div class="grid gap-3">${shots.slice(-6).reverse().map((s) => `<img src="${esc(absUrl(s))}" alt="${esc(ag.name)} screenshot" class="w-full rounded-2xl border border-gray-100" loading="lazy">`).join('')}</div>`) : '',
        card('Activity', `<div class="bg-cardDark rounded-2xl p-2 space-y-1.5">${[...entries].reverse().map((e) => logRow(e)).join('') || '<p class="text-gray-500 text-sm p-2">No activity yet.</p>'}</div>`),
    ].join('');
}

async function decide(id, action) {
    if (!settings.actor) { openSettings(); return toast('Add your name first so the log knows who decided.'); }
    const reason = $('decisionReason')?.value.trim() || '';
    try {
        await post(`/api/findings/${encodeURIComponent(id)}/${action}`, { actor: settings.actor, reason });
        toast(`${id} ${action === 'approve' ? 'approved' : 'rejected'}.`);
        state.cache.findings = state.cache.drawer = null;
        refreshDrawer();
    } catch (e) {
        toast(`Refused: ${e.message}`);
    }
}

/* ---------------- settings + toast ---------------- */

function openSettings() {
    $('actorInput').value = settings.actor;
    $('tokenInput').value = settings.token;
    $('apiInput').value = settings.api;
    $('settingsModal').classList.replace('hidden', 'flex');
}
function closeSettings() { $('settingsModal').classList.replace('flex', 'hidden'); }

let toastTimer;
function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 3500);
}

/* ---------------- wiring ---------------- */

document.addEventListener('click', (e) => {
    const f = e.target.closest('[data-finding]');
    if (f && f.dataset.finding && !e.target.closest('#drawer')) return openDrawer('finding', f.dataset.finding);
    const a = e.target.closest('[data-agent]');
    if (a) return openDrawer('agent', a.dataset.agent);
    if (e.target.closest('[data-close-drawer]')) return closeDrawer();
    if (e.target.closest('[data-open-settings]')) return openSettings();
    if (e.target.closest('[data-close-settings]')) return closeSettings();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeDrawer(); closeSettings(); } });

$('settingsForm').onsubmit = (e) => {
    e.preventDefault();
    settings.actor = $('actorInput').value.trim();
    settings.token = $('tokenInput').value.trim();
    const api = $('apiInput').value.trim().replace(/\/$/, '') || DEFAULT_API;
    const apiChanged = api !== settings.api;
    settings.api = api;
    save('flock.actor', settings.actor);
    save('flock.token', settings.token);
    save('flock.api', settings.api);
    closeSettings();
    if (apiChanged) location.reload();
    else toast('Saved.');
};

state.cache.base = settings.api;
$('apiBase').textContent = `· API: ${settings.api}`;
renderTxns();
icons();
pollFast();
pollSlow();
