/* ==========================================================================
   Flock evidence viewer: open the exact cell a finding points at. Self-contained (injects its own DOM + CSS).
     FlockEvidence.open(ref, api?)          ref = "path:row" | {file|source_file, row, column, quote, what}
     FlockEvidence.openFinding(fid, api?)   every evidence row of one finding, first one opened
     FlockEvidence.openAll(api?)            every source file the run touched (GET /api/evidence/index)
   Backend: GET /api/evidence?ref=<json>  ->  flock/evidence.py resolve(); GET /api/evidence/index -> index().
   ========================================================================== */
(() => {
    if (window.FlockEvidence) return;
    const DEFAULT_API = 'https://web-production-812b4.up.railway.app';
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const css = `
    .fev-back{position:fixed;inset:0;background:rgba(15,18,32,.62);backdrop-filter:blur(3px);z-index:9998;display:flex;align-items:center;justify-content:center;padding:16px}
    .fev{font-family:"Plus Jakarta Sans",ui-sans-serif,system-ui,sans-serif;background:#fff;color:#1f2337;width:min(1180px,100%);max-height:92vh;border-radius:24px;
      box-shadow:0 30px 80px rgba(1,47,138,.35);display:flex;flex-direction:column;overflow:hidden}
    .fev-h{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid #e6ecf7;background:linear-gradient(90deg,#012F8A,#0159C4);color:#fff}
    .fev-h .t{font-weight:800;font-size:17px}.fev-h .s{font-size:12px;color:#D2F5FF}
    .fev-x{margin-left:auto;background:rgba(255,255,255,.14);border:0;color:#fff;width:34px;height:34px;border-radius:50%;font-size:18px;cursor:pointer}
    .fev-crumb{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#97E2FF;word-break:break-all}
    .fev-body{display:flex;min-height:0;flex:1}
    .fev-side{width:270px;flex:none;border-right:1px solid #e6ecf7;overflow:auto;background:#f6f9ff}
    .fev-side button{display:block;width:100%;text-align:left;border:0;background:none;padding:10px 14px;font-size:12.5px;cursor:pointer;border-bottom:1px solid #eaf0fb;color:#24304f}
    .fev-side button:hover{background:#eaf3ff}.fev-side button.on{background:#D2F5FF;font-weight:700}
    .fev-side .k{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#4A628A;font-weight:800}
    .fev-main{flex:1;min-width:0;display:flex;flex-direction:column}
    .fev-what{padding:12px 18px;font-size:13.5px;background:#f2f7ff;border-bottom:1px solid #e6ecf7}
    .fev-what b{color:#0146A9}
    .fev-grid{overflow:auto;flex:1}
    .fev table{border-collapse:separate;border-spacing:0;font-size:12.5px;width:max-content;min-width:100%}
    .fev th{position:sticky;top:0;background:#eef3fb;color:#3E445C;font-weight:800;text-align:left;padding:8px 12px;border-bottom:1px solid #d7e2f3;border-right:1px solid #e4eaf5;white-space:nowrap;z-index:2}
    .fev td{padding:7px 12px;border-bottom:1px solid #eef2f8;border-right:1px solid #f1f4f9;white-space:nowrap;font-family:ui-monospace,Menlo,monospace;max-width:420px;overflow:hidden;text-overflow:ellipsis}
    .fev td.rn,.fev th.rn{position:sticky;left:0;background:#f6f9ff;color:#8090b0;text-align:right;font-weight:700;z-index:1}
    .fev th.rn{z-index:3}
    .fev tr.hit td{background:#eaf5ff}.fev tr.hit td.rn{background:#0084FF;color:#fff}
    .fev td.cell{background:#0084FF!important;color:#fff;font-weight:800;box-shadow:inset 0 0 0 2px #012F8A;animation:fevpulse 1.6s ease-in-out 2}
    .fev th.col{background:#0084FF;color:#fff}
    .fev pre{margin:0;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;white-space:pre-wrap}
    .fev-f{display:flex;align-items:center;gap:10px;padding:12px 18px;border-top:1px solid #e6ecf7;font-size:12px;color:#4A628A}
    .fev-f button{margin-left:auto;background:#0084FF;color:#fff;border:0;border-radius:999px;padding:8px 14px;font-weight:700;cursor:pointer}
    .fev-f button.sec{margin-left:0;background:#e8f0fd;color:#0146A9}
    .fev-err{padding:28px;color:#b42318;font-size:14px}
    .fev-files td{font-family:inherit}.fev-files tr{cursor:pointer}.fev-files tr:hover td{background:#eaf3ff}
    @keyframes fevpulse{0%,100%{box-shadow:inset 0 0 0 2px #012F8A}50%{box-shadow:inset 0 0 0 2px #012F8A,0 0 0 6px rgba(0,132,255,.35)}}
    @media(max-width:760px){.fev-side{display:none}}`;

    let root = null, apiBase = '', current = null;
    function base(api) {
        if (api) return api.replace(/\/$/, '');
        const p = new URLSearchParams(location.search).get('api');
        if (p) return p.replace(/\/$/, '');
        if (/^https?:$/.test(location.protocol)) return location.origin;
        try { return (localStorage.getItem('flock.api') || DEFAULT_API).replace(/\/$/, ''); } catch { return DEFAULT_API; }
    }
    async function getJSON(path) {
        const r = await fetch(apiBase + path, { cache: 'no-store' });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
        return body;
    }
    function shell(title, sub) {
        close();
        if (!document.getElementById('fev-css')) {
            const s = document.createElement('style'); s.id = 'fev-css'; s.textContent = css; document.head.appendChild(s);
        }
        root = document.createElement('div'); root.className = 'fev-back';
        root.innerHTML = `<div class="fev" role="dialog" aria-modal="true" aria-label="Evidence">
            <div class="fev-h"><div style="min-width:0"><div class="t">${esc(title)}</div><div class="s" data-sub>${sub || ''}</div></div>
            <button class="fev-x" aria-label="Close">×</button></div>
            <div class="fev-body"><div class="fev-side" data-side></div><div class="fev-main" data-main><div class="fev-err" style="color:#4A628A">Loading…</div></div></div>
            <div class="fev-f"><span data-foot></span><button class="sec" data-all>All evidence</button><button data-dl>Download CSV slice</button></div></div>`;
        root.addEventListener('click', (e) => { if (e.target === root) close(); });
        root.querySelector('.fev-x').onclick = close;
        root.querySelector('[data-all]').onclick = () => openAll();
        root.querySelector('[data-dl]').onclick = download;
        document.addEventListener('keydown', onKey);
        document.body.appendChild(root);
        return root;
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    function close() {
        if (root) root.remove();
        root = null; document.removeEventListener('keydown', onKey);
    }
    const q = (sel) => root && root.querySelector(sel);

    function refKey(ref) {
        if (typeof ref === 'string') return ref;
        const r = { file: ref.file || ref.source_file };
        ['row', 'column', 'quote', 'what'].forEach((k) => ref[k] != null && ref[k] !== '' && (r[k] = ref[k]));
        return JSON.stringify(r);
    }
    async function show(ref) {
        const main = q('[data-main]');
        main.innerHTML = '<div class="fev-err" style="color:#4A628A">Opening the file…</div>';
        let d;
        try { d = await getJSON(`/api/evidence?ref=${encodeURIComponent(refKey(ref))}`); } catch (e) {
            main.innerHTML = `<div class="fev-err">Couldn't open this evidence: ${esc(e.message)}</div>`; return;
        }
        current = d;
        q('[data-sub]').innerHTML = `<span class="fev-crumb">${esc(String(d.file).split('/').join(' / '))}</span>`;
        const hl = d.highlight || {};
        const what = d.what || (typeof ref === 'object' && ref.what) || '';
        const quote = d.quote || (typeof ref === 'object' && ref.quote) || '';
        main.innerHTML = `<div class="fev-what">${hl.row ? `<b>Row ${esc(hl.row)}</b>${hl.column_name ? `, column <b>${esc(hl.column_name)}</b>` : ''}` : '<b>Source</b>'}
            ${what ? ` · ${esc(what)}` : ''}${quote ? ` · “${esc(quote)}”` : ''}</div><div class="fev-grid" data-grid></div>`;
        const grid = q('[data-grid]');
        if (d.kind === 'csv') {
            const head = `<tr><th class="rn">#</th>${(d.header || []).map((h, i) => `<th class="${i === hl.column ? 'col' : ''}">${esc(h)}</th>`).join('')}</tr>`;
            const body = (d.rows || []).map((r) => {
                const hit = r.row === hl.row;
                return `<tr class="${hit ? 'hit' : ''}" ${hit ? 'data-hit' : ''}><td class="rn">${r.row}</td>${r.cells.map((c, i) =>
                    `<td class="${hit && i === hl.column ? 'cell' : ''}" title="${esc(c)}">${esc(c)}</td>`).join('')}</tr>`;
            }).join('');
            grid.innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
        } else {
            grid.innerHTML = `<table><tbody>${(d.rows || []).map((r) => {
                const hit = r.row === hl.row;
                return `<tr class="${hit ? 'hit' : ''}" ${hit ? 'data-hit' : ''}><td class="rn">${r.row}</td><td class="${hit ? 'cell' : ''}" style="white-space:pre-wrap;max-width:none"><pre>${esc(r.cells[0])}</pre></td></tr>`;
            }).join('')}</tbody></table>`;
        }
        q('[data-foot]').textContent = `${d.kind.toUpperCase()} · ${Number(d.total_rows || 0).toLocaleString()} ${d.kind === 'csv' ? 'rows' : 'lines'} in the file · showing ${d.rows.length}`;
        q('[data-dl]').style.display = d.kind === 'csv' ? '' : 'none';
        const hitRow = grid.querySelector('[data-hit]');
        if (hitRow) requestAnimationFrame(() => {
            hitRow.scrollIntoView({ block: 'center', inline: 'nearest' });
            const cell = hitRow.querySelector('td.cell');
            if (cell) cell.scrollIntoView({ block: 'nearest', inline: 'center' });
        });
    }
    function download() {
        if (!current || current.kind !== 'csv') return;
        const line = (cells) => cells.map((c) => /[",\n]/.test(c) ? `"${String(c).replace(/"/g, '""')}"` : c).join(',');
        const text = [line(['excel_row', ...(current.header || [])]), ...current.rows.map((r) => line([String(r.row), ...r.cells]))].join('\n');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
        a.download = `${String(current.file).split('/').pop().replace(/\.csv$/, '')}_rows_${current.rows[0].row}-${current.rows[current.rows.length - 1].row}.csv`;
        a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }
    function side(items, onPick) {
        const el = q('[data-side]');
        el.innerHTML = items.map((it, i) => `<button data-i="${i}">${it.html}</button>`).join('') || '<div style="padding:14px;font-size:12px;color:#4A628A">No evidence references.</div>';
        el.querySelectorAll('button').forEach((b) => b.onclick = () => {
            el.querySelectorAll('button').forEach((x) => x.classList.remove('on')); b.classList.add('on');
            onPick(items[Number(b.dataset.i)]);
        });
        const first = el.querySelector('button'); if (first) first.click();
    }
    const refLabel = (r) => `<div class="k">${esc(r.conflict ? 'conflicting claim' : r.authority || (r.row ? `row ${r.row}` : 'quote'))}</div>
        <div>${esc(String(r.file || r.source_file).split('/').pop())}</div><div style="color:#5d6b8a;font-size:11.5px;margin-top:2px">${esc(r.what || r.quote || '')}</div>`;

    async function open(ref, api) {
        apiBase = base(api);
        shell('Evidence', '');
        q('[data-side]').style.display = 'none';
        await show(ref);
    }
    async function openFinding(fid, api) {
        apiBase = base(api);
        shell(`Evidence · ${fid}`, 'Every number traced to the cell it came from');
        let f;
        try { f = await getJSON(`/api/findings/${encodeURIComponent(fid)}`); } catch {
            try { const all = await getJSON('/api/findings'); f = (all.findings || all).find((x) => (x.finding_id || x.id) === fid); } catch { /* below */ }
        }
        f = f && (f.finding || f);
        const refs = ((f && f.evidence) || []).filter((e) => e && e.source_file).map((e) => ({ ...e, file: e.source_file }));
        if (!refs.length) { q('[data-main]').innerHTML = `<div class="fev-err">No evidence rows on ${esc(fid)}.</div>`; return; }
        side(refs.map((r) => ({ ref: r, html: refLabel(r) })), (it) => show(it.ref));
    }
    async function openAll(api) {
        if (api || !apiBase) apiBase = base(api);
        if (!root) shell('All evidence', 'Every source file this run touched');
        else { q('.t').textContent = 'All evidence'; q('[data-sub]').textContent = 'Every source file this run touched'; }
        q('[data-side]').style.display = '';
        let idx;
        try { idx = await getJSON('/api/evidence/index'); } catch (e) {
            q('[data-main]').innerHTML = `<div class="fev-err">The evidence index isn't available on this server yet (${esc(e.message)}).</div>`; return;
        }
        const main = q('[data-main]');
        const files = idx.files || [];
        main.innerHTML = `<div class="fev-what"><b>${files.length} source files</b> · ${idx.count || 0} evidence references across ${(idx.findings || []).length} findings. Click a file or a reference.</div>
            <div class="fev-grid"><table class="fev-files"><thead><tr><th>File</th><th>Kind</th><th>References</th><th>Findings</th></tr></thead><tbody>
            ${files.map((f, i) => `<tr data-i="${i}"><td>${esc(f.file)}</td><td>${esc(f.kind)}</td><td>${f.refs}</td><td>${esc(f.findings.slice(0, 6).join(', '))}${f.findings.length > 6 ? ` +${f.findings.length - 6}` : ''}</td></tr>`).join('')}
            </tbody></table></div>`;
        main.querySelectorAll('tr[data-i]').forEach((tr) => tr.onclick = () => {
            const f = files[Number(tr.dataset.i)];
            const first = (idx.findings || []).flatMap((x) => x.refs).find((r) => r.file === f.file);
            show(first || { file: f.file });
        });
        const items = (idx.findings || []).flatMap((x) => x.refs.map((r) => ({ ref: r, html: `<div class="k">${esc(x.finding_id)} · ${esc(x.check_type || '')}</div>${refLabel(r)}` })));
        const el = q('[data-side]');
        el.innerHTML = items.map((it, i) => `<button data-i="${i}">${it.html}</button>`).join('');
        el.querySelectorAll('button').forEach((b) => b.onclick = () => { el.querySelectorAll('button').forEach((x) => x.classList.remove('on')); b.classList.add('on'); show(items[Number(b.dataset.i)].ref); });
    }
    window.FlockEvidence = { open, openFinding, openAll, close };
})();
