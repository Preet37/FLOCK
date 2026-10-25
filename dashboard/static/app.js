// Flock dashboard. Plain JS, hash routes: #/  #/queue  #/flock  #/finding/<id>  #/clicky  #/journal  #/journal/<id>  #/score  #/market
// Polls /api/summary every second; re-renders the current view when the audit log grows.
// Also polls /api/progress and updates the live board's progress bar in place (no re-render), and
// /api/agents?since=<cursor> for the Flock view (only agents that changed come back).
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const CHANNEL = { doordash: "DoorDash", ubereats: "Uber Eats", grubhub: "Grubhub", direct_app: "Direct app" };
const STRONG = new Set(["contract", "amendment", "auth_email"]);
let lastVersion = null;
let lastSummary = null;
let lastProgress = null;
let busy = false;

// ---------- helpers ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (n) => (n == null ? "" : (n < 0 ? "-$" : "$") + Math.abs(Number(n)).toFixed(2));
const pct = (n) => (n == null ? "n/a" : Math.round(Number(n) * 100) + "%");
const label = (s) => esc(String(s ?? "").replace(/_/g, " "));
const chan = (c) => esc(CHANNEL[c] || c);
const badge = (cls, text) => `<span class="badge b-${esc(cls)}">${esc(text)}</span>`;
const when = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); } catch { return esc(iso); } };
const initials = (e) => (e.allowed ? (e.role === "human" ? "H" : e.role === "gatekeeper" ? "GK" : "AI") : "!");

function actorName() {
  const v = $("#actor").value.trim();
  return v || "controller";
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const err = new Error(data.error || res.statusText); err.status = res.status; err.data = data; throw err; }
  return data;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3200);
}

// ---------- actions (all writes go through the gatekeeper on the server) ----------
async function decide(id, action, reason, role = "human") {
  busy = true;
  try {
    await api(`/api/findings/${encodeURIComponent(id)}/${action}`, {
      method: "POST", body: JSON.stringify({ actor: role === "human" ? actorName() : "panel", role, reason }),
    });
    const did = { approve: "approved", reject: "rejected", recover: "marked recovered (platform paid)" }[action] || action;
    toast(`${id} ${did} by ${actorName()}. Logged; judges' bets on it settled.`);
  } catch (e) {
    toast(e.status === 403 ? `Gatekeeper denied it: ${e.message}. The attempt is in the audit log.` : e.message, true);
  } finally { busy = false; await refresh(true); }
}

async function fileApproved() {
  try {
    const r = await api("/api/file-approved", { method: "POST" });
    toast(r.filed.length ? `Clicky filed ${r.filed.length} in the sandbox portal: ${r.filed.join(", ")}` : "Nothing approved to file.");
  } catch (e) { toast(e.message, true); }
  await refresh(true);
}

async function approveRule(id) {
  try {
    const r = await api(`/api/rules/${encodeURIComponent(id)}/approve`, { method: "POST", body: JSON.stringify({ actor: actorName() }) });
    toast(`Rule ${r.rule.rule_id} approved by ${actorName()}. The panel applies it next run.`);
  } catch (e) { toast(e.message, true); }
  await refresh(true);
}

async function decideJe(id, action, reason, role = "human") {
  busy = true;
  try {
    await api(`/api/journal/${encodeURIComponent(id)}/${action}`, {
      method: "POST", body: JSON.stringify({ actor: role === "human" ? actorName() : "journal", role, reason }),
    });
    toast(`${id} ${action === "approve" ? "approved" : "rejected"} by ${actorName()}. Logged.`);
  } catch (e) {
    toast(e.status === 403 ? `Gatekeeper denied it: ${e.message}. The attempt is in the audit log.` : e.message, true);
  } finally { busy = false; await refresh(true); }
}

async function exportJournal() {
  try {
    const r = await api("/api/journal/export", { method: "POST" });
    toast(r.posted.length ? `Gatekeeper posted ${r.posted.length} entries to ${r.file}` : "Nothing approved to export.");
  } catch (e) { toast(e.message, true); }
  await refresh(true);
}

async function showSource(btn) {
  const box = btn.closest("td").querySelector(".src-row");
  try {
    const r = await api(`/api/source?file=${encodeURIComponent(btn.dataset.file)}&row=${encodeURIComponent(btn.dataset.row)}`);
    box.innerHTML = `<table><tr>${r.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>
      <tr>${r.values.map((v) => `<td class="mono">${esc(v)}</td>`).join("")}</tr></table>`;
  } catch (e) { box.textContent = `${btn.dataset.file} row ${btn.dataset.row}: ${e.message}`; }
}

document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-act]");
  if (b) {
    const id = b.dataset.id;
    const box = document.querySelector(`textarea[data-reason="${CSS.escape(id || "")}"]`);
    const reason = box ? box.value.trim() : "";
    if (b.dataset.act === "approve" || b.dataset.act === "reject") decide(id, b.dataset.act, reason);
    if (b.dataset.act === "recover") decide(id, "recover", "Platform paid the dispute");
    if (b.dataset.act === "agent-try") decide(id, "approve", "Agent tried to approve its own call", "agent");
    if (b.dataset.act === "file") fileApproved();
    if (b.dataset.act === "rule") approveRule(id);
    if (b.dataset.act === "je-approve" || b.dataset.act === "je-reject") decideJe(id, b.dataset.act.slice(3), reason);
    if (b.dataset.act === "je-agent-try") decideJe(id, "approve", "Agent tried to approve its own journal entry", "agent");
    if (b.dataset.act === "je-export") exportJournal();
    if (b.dataset.act === "src") showSource(b);
    if (b.dataset.act === "wf-approve") decideWorkflow("approve");
    if (b.dataset.act === "wf-reject") decideWorkflow("reject");
    if (b.dataset.act === "wf-agent-try") decideWorkflow("approve", "agent");
    return;
  }
  const row = ev.target.closest("tr[data-href]");
  if (row) location.hash = row.dataset.href;
});

// ---------- views ----------
function counters(s) {
  const cards = [
    ["findings", "Findings", s.findings, `${s.by_state.CHECKED + s.by_state.JUDGED} checked or judged`],
    ["dollars", "Dollars found", usd(s.dollars_found), `${usd(s.dollars_filed)} filed so far`, "accent"],
    ["to_human", "Sent to a human", s.to_human, "judges disagreed or low confidence"],
    ["awaiting", "Awaiting a decision", s.awaiting_human, "in the human queue"],
    ["log", "Audit log entries", s.log_entries, `${s.denied} denied attempt${s.denied === 1 ? "" : "s"}`],
  ];
  return `<div class="stats">${cards.map(([k, l, v, h, cls]) => `
    <div class="card stat ${cls || ""}" data-k="${k}" data-v="${esc(v)}">
      <div class="label">${l}</div><div class="value">${esc(v)}</div><div class="hint">${esc(h)}</div>
    </div>`).join("")}</div>`;
}

function stateStrip(s) {
  const order = ["NEW", "CHECKED", "JUDGED", "APPROVED", "REJECTED", "FILED", "RECOVERED"];
  return `<div class="strip">${order.map((k) => `<div class="st"><span>${k}</span><b>${s.by_state[k] || 0}</b></div>`).join("")}</div>`;
}

function findingsTable(rows) {
  if (!rows.length) return `<div class="empty">No findings yet. Run <code>python -m flock.pipeline --reset --pace 0.4</code> and watch this fill in.</div>`;
  return `<div class="table-scroll"><table><thead><tr>
      <th>Finding</th><th>Channel</th><th>Check</th><th class="r">Gap</th><th>Outcome</th><th>State</th></tr></thead><tbody>
    ${rows.map((f) => `<tr class="link" data-href="#/finding/${esc(f.finding_id)}">
      <td><a href="#/finding/${esc(f.finding_id)}" class="mono">${esc(f.finding_id)}</a></td>
      <td>${chan(f.channel)}</td><td>${label(f.check_type)}</td><td class="r num">${usd(f.gap)}</td>
      <td>${f.outcome ? badge(f.outcome, f.outcome) : '<span class="muted">pending</span>'}</td>
      <td>${badge(f.state, f.state)}${f.needs_human ? ' <span class="badge b-conflict">needs you</span>' : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function activity(entries) {
  if (!entries.length) return `<div class="empty">Nothing logged yet.</div>`;
  return `<ul class="feed">${entries.map((e) => `<li>
      <span class="dot ${e.allowed ? esc(e.role) : "denied"}"></span>
      <span><b>${esc(e.actor)}</b> ${e.allowed ? esc(e.action) : `tried to <b>${esc(e.action)}</b>, denied`}
        <a href="${e.subject_type === "workflow" ? "#/clicky" : (e.subject_type === "journal_entry" ? "#/journal/" : "#/finding/") + esc(e.subject_id)}" class="mono">${esc(e.subject_id)}</a></span>
      <span class="meta">${esc(e.before)} → ${esc(e.after)} · ${when(e.at)}</span></li>`).join("")}</ul>`;
}

// ---------- run progress (flock.progress -> /api/progress) ----------
const big = (n) => (n == null ? "?" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n)));

function progressBar(p) {
  if (!p || !p.status || p.status === "idle") return "";
  const running = p.status === "running";
  const frac = running && p.total ? Math.min(1, p.done / p.total) : running ? null : 1;
  const stages = (p.stages || []).map((s) => `<span class="pstage" title="${esc(big(s.done))} ${esc(s.unit)} in ${esc(s.seconds)}s">${esc(s.name)} <b>${esc(s.seconds)}s</b></span>`).join("");
  const head = running
    ? `<b>${esc(p.stage || "starting")}</b> <span class="num">${esc(big(p.done))}${p.total ? " / " + esc(big(p.total)) : ""} ${esc(p.unit || "rows")}</span> <span class="muted">· ${esc(big(p.rate))}/s · ${esc(Math.round(p.elapsed || 0))}s</span>`
    : `<b>${p.status === "failed" ? "Run failed" : "Run finished"}</b> <span class="muted">in ${esc(Math.round(p.elapsed || 0))}s${p.run ? " · " + esc(p.run) : ""}</span>`;
  return `<section class="card card-pad progress ${esc(p.status)}" aria-label="Run progress">
      <div class="phead">${head}<span class="pct num">${frac == null ? "" : Math.round(frac * 100) + "%"}</span></div>
      <div class="ptrack" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${frac == null ? 0 : Math.round(frac * 100)}">
        <i class="${frac == null ? "indeterminate" : ""}" style="width:${frac == null ? 30 : Math.round(frac * 100)}%"></i></div>
      ${stages ? `<div class="pstages">${stages}</div>` : ""}
    </section>`;
}

function renderProgress(p) {
  lastProgress = p;
  const el = document.getElementById("run-progress");
  if (el) el.innerHTML = progressBar(p);
}

async function pollProgress() {
  try { renderProgress(await api("/api/progress")); } catch { /* the board works without it */ }
}

async function renderLive() {
  const [s, rows] = await Promise.all([api("/api/summary"), api("/api/findings")]);
  const prev = {};
  view.querySelectorAll(".stat").forEach((el) => (prev[el.dataset.k] = el.dataset.v));
  view.innerHTML = `
    <div class="page-head"><div><h1>Live board</h1>
      <div class="sub">Every move below went through the gatekeeper and is in the audit log.</div></div></div>
    <div id="run-progress">${progressBar(lastProgress)}</div>
    ${counters(s)}
    ${stateStrip(s)}
    <div class="grid-2">
      <section class="card"><div class="card-head"><h2>Findings</h2><span class="muted">${rows.length} total</span></div>${findingsTable(rows)}</section>
      <section class="card"><div class="card-head"><h2>Agent activity</h2><span class="muted">latest first</span></div>${activity(s.activity)}</section>
    </div>`;
  view.querySelectorAll(".stat").forEach((el) => {
    if (prev[el.dataset.k] !== undefined && prev[el.dataset.k] !== el.dataset.v) el.classList.add("flash");
  });
}

function evidenceInline(f) {
  return (f.evidence || []).map((e) => {
    const a = e.authority ? ` ${authBadge(e.authority)}` : "";
    return e.quote ? `<div>“${esc(e.quote)}”${a}</div>` : `<div class="muted mono">${esc(e.source_file)}${e.row != null ? ` row ${esc(e.row)}` : ""}</div>`;
  }).join("");
}

function authBadge(a) {
  const cls = STRONG.has(a) ? "strong" : (a === "unauth_email" || a === "call_note") ? "weak" : "";
  return `<span class="badge b-auth ${cls}">${label(a)}</span>`;
}

function voteChips(f) {
  if (!f.votes || !f.votes.length) return "";
  return `<div class="votes">${f.votes.map((v) => `<span class="vote-chip">${esc(v.judge.replace("judge.", ""))}: <b>${esc(v.outcome)}</b> ${pct(v.confidence)}</span>`).join("")}</div>`;
}

function decideBox(f, withAgentTry = false) {
  const conflict = f.outcome === "conflict";
  return `<div class="q-actions">
      <textarea data-reason="${esc(f.finding_id)}" rows="1" placeholder="Reason (goes in the audit log)"></textarea>
      <button class="btn-ok" data-act="approve" data-id="${esc(f.finding_id)}" title="${conflict ? "Dispute it" : "Approve the dispute"}">Approve</button>
      <button class="btn-bad" data-act="reject" data-id="${esc(f.finding_id)}" title="${conflict ? "Accept the charge" : "Don't dispute"}">Reject</button>
      ${withAgentTry ? `<button class="btn-ghost" data-act="agent-try" data-id="${esc(f.finding_id)}" title="Shows the gatekeeper refusing an agent">Try as an agent</button>` : ""}
    </div>`;
}

async function renderQueue() {
  const [rows, all, rules] = await Promise.all([api("/api/queue"), api("/api/findings"), api("/api/rules")]);
  const approved = all.filter((f) => f.state === "APPROVED");
  const item = (f) => `<article class="card card-pad q-item">
      <div class="q-top">
        <div><div class="q-title"><a href="#/finding/${esc(f.finding_id)}" class="mono">${esc(f.finding_id)}</a>
          · ${chan(f.channel)} ${label(f.check_type)} · ${esc(f.order_ids.join(", "))}</div>
          <div class="reason">${esc(f.reason)}</div></div>
        <div style="text-align:right"><div class="gap">${usd(f.gap)}</div>${badge(f.outcome, f.outcome === "conflict" ? "conflict: judges split" : "dispute drafted")}
          ${attentionTag(f)}</div>
      </div>
      ${evidenceInline(f)}
      <div style="margin-top:8px">${voteChips(f)}</div>
      ${decideBox(f)}
    </article>`;
  const isDeferred = (f) => (f.attention || {}).status === "deferred";
  const conflicts = rows.filter((f) => f.outcome === "conflict" && !isDeferred(f));
  const deferred = rows.filter((f) => f.outcome === "conflict" && isDeferred(f));
  const drafts = rows.filter((f) => f.outcome !== "conflict");
  view.innerHTML = `
    <div class="page-head"><div><h1>Human queue</h1>
      <div class="sub">Agents propose. You decide. Approve or reject goes through <code>gatekeeper.transition()</code> as <b>${esc(actorName())}</b> (human).</div></div>
      ${approved.length ? `<button class="btn-ok" data-act="file">File ${approved.length} approved via Clicky</button>` : ""}</div>
    <h3>Conflicts (${conflicts.length}) <span class="muted h-note">ordered by value of your attention: |gap| × panel uncertainty</span></h3>
    <div class="queue">${conflicts.map(item).join("") || `<div class="card empty">No conflicts waiting. The panel and the learned rules handled everything.</div>`}</div>
    ${deferred.length ? `<h3 class="section-gap">Deferred: over this week's attention budget (${deferred.length})</h3>
    <div class="sub" style="margin:-4px 0 8px">Still conflicts. Nothing here is decided automatically; they wait for you.</div>
    <div class="queue deferred">${deferred.map(item).join("")}</div>` : ""}
    <h3 class="section-gap">Disputes ready to approve (${drafts.length})</h3>
    <div class="queue">${drafts.map(item).join("") || `<div class="card empty">No drafted disputes waiting.</div>`}</div>
    ${rulesCard(rules)}`;
}

function rulesCard(rules) {
  const p = rules.proposed || [], a = rules.approved || [];
  return `<section class="card section-gap"><div class="card-head"><h2>Learned rules</h2>
      <span class="muted">from human decisions, applied by the panel next run</span></div>
    ${p.map((r) => `<div class="rule"><div><b>${esc(r.rule_id)}</b> proposed · ${esc(r.description)}
        <div class="muted">${esc(r.evidence_count)} precedents: ${esc((r.precedents || []).join(", "))}</div></div>
        <button data-act="rule" data-id="${esc(r.rule_id)}">Approve rule</button></div>`).join("")}
    ${a.map((r) => `<div class="rule"><div><b>${esc(r.rule_id)}</b> ${badge("APPROVED", "approved")} · ${esc(r.description)}
        <div class="muted">approved by ${esc(r.approved_by)} · ${esc(r.evidence_count)} precedents</div></div></div>`).join("")}
    ${!p.length && !a.length ? `<div class="empty">No rules yet. Decide a few conflicts the same way and the Learner proposes one.</div>` : ""}
  </section>`;
}

function diffBlock(diff) {
  const keys = Object.keys(diff || {});
  if (!keys.length) return "";
  const show = (v) => esc(typeof v === "string" ? v : JSON.stringify(v));
  return `<details><summary>${keys.length} field${keys.length > 1 ? "s" : ""} changed: ${esc(keys.join(", "))}</summary>
    <div class="diff">${keys.map((k) => `<div class="del">- ${esc(k)}: ${show(diff[k][0])}</div><div class="add">+ ${esc(k)}: ${show(diff[k][1])}</div>`).join("")}</div></details>`;
}

function timeline(log) {
  return `<ol class="timeline">${log.map((e) => `<li class="tl ${e.allowed ? esc(e.role) : "denied"}">
      <span class="node">${initials(e)}</span>
      <div class="box"><div class="box-head">
          <b>${esc(e.actor)}</b> ${badge(e.role, e.role)}
          ${e.allowed ? `<span>${esc(e.action)}</span>` : `<span>tried to <b>${esc(e.action)}</b></span> ${badge("denied", "denied")}`}
          <span class="mono muted">${esc(e.before)} → ${esc(e.after)}</span>
          <span class="when">${when(e.at)}</span></div>
        <div class="box-body">${esc(e.reason) || '<span class="muted">no reason given</span>'}${diffBlock(e.diff)}</div></div></li>`).join("")}</ol>`;
}

async function renderFinding(id) {
  let d;
  try { d = await api(`/api/findings/${encodeURIComponent(id)}`); }
  catch (e) { view.innerHTML = `<div class="card empty section-gap">${esc(e.message)}. <a href="#/">Back to the board</a></div>`; return; }
  const f = d.finding;
  const human = d.decision;
  const stateWord = { FILED: "Filed", RECOVERED: "Recovered", APPROVED: "Approved", REJECTED: "Rejected", JUDGED: d.needs_human ? "Needs a human" : "Judged" }[f.state] || f.state;
  view.innerHTML = `
    <div class="pr-head">
      <div class="muted"><a href="#/">Live board</a> / finding</div>
      <h1 style="margin-top:6px">${chan(f.channel)} ${label(f.check_type)} on ${esc(f.order_ids.join(", "))} <span class="id">${esc(f.finding_id)}</span></h1>
      <div class="pr-meta">
        <span class="badge pr-state b-${esc(f.state)}">${esc(stateWord)}</span>
        ${f.outcome ? badge(f.outcome, f.outcome) : ""}
        <span>${d.log.length} audit entries${d.denied.length ? `, <b class="bad-t">${d.denied.length} denied</b>` : ""}</span>
      </div>
    </div>
    <div class="pr-layout">
      <div class="stack">
        <section class="card">
          <div class="card-head"><h2>Numbers</h2><span class="muted">computed in Python, never by a model</span></div>
          <div class="numbers"><div><div class="k">Expected</div><div class="v">${usd(f.expected)}</div></div>
            <div><div class="k">Actual</div><div class="v">${usd(f.actual)}</div></div>
            <div><div class="k">Gap</div><div class="v ${f.outcome === "recoverable" ? "ok-t" : ""}">${usd(f.gap)}</div></div></div>
        </section>

        <section class="card"><div class="card-head"><h2>Sources and claims</h2><span class="muted">${(f.evidence || []).length} pieces of evidence</span></div>
          ${(f.evidence || []).map((e) => `<div class="ev">
            <div class="file">${esc(e.source_file)}${e.row != null ? ` <span class="muted">row ${esc(e.row)}</span>` : ""} ${e.authority ? authBadge(e.authority) : ""}</div>
            ${e.what ? `<div>${esc(e.what)}</div>` : ""}${e.quote ? `<blockquote>${esc(e.quote)}</blockquote>` : ""}</div>`).join("") || '<div class="empty">No evidence attached.</div>'}
        </section>

        <section class="card"><div class="card-head"><h2>${d.rule ? "Rule applied" : "Judge panel"}</h2>
            <span class="muted">${f.confidence != null ? `${pct(f.confidence)} confidence` : ""}</span></div>
          <div class="card-pad" style="padding-bottom:${f.votes && f.votes.length ? 0 : 16}px">${esc(f.reason)}
            ${d.rule ? `<div class="muted" style="margin-top:6px">${esc(d.rule.rule_id)}, approved by <b>${esc(d.rule.approved_by)}</b>, learned from ${esc(d.rule.evidence_count)} human decisions (${esc((d.rule.precedents || []).join(", "))})</div>` : ""}</div>
          ${f.review_memo ? `<div class="card-pad"><b>Reviewer memo for the human</b> <span class="muted">(the reviewer explains the split; it does not decide)</span><div>${esc(f.review_memo)}</div></div>` : ""}
          ${(f.votes || []).map((v) => `<div class="vote"><b>${esc(v.judge)}${v.round ? ` <span class="muted">${esc(v.round === "audit" ? "audit" : v.round === 2 ? "after debate" : "round 1")}</span>` : ""}${v.weight != null ? `<div class="muted" style="font-weight:400">weight ${pct(v.weight)}</div>` : ""}</b>
              <span>${badge(v.outcome, v.outcome)} <span class="num">${pct(v.confidence)}</span><div class="bar"><i style="width:${Math.round(v.confidence * 100)}%"></i></div></span>
              <span class="why muted">${esc(v.reason)}</span></div>`).join("")}
        </section>

        ${caseFileCard(f.case_file)}
        ${f.dispute_draft ? `<section class="card"><div class="card-head"><h2>Dispute draft</h2><span class="muted">dispute_writer · not sent until a human approves</span></div><pre class="draft">${esc(f.dispute_draft)}</pre></section>` : ""}
        ${d.filed ? `<section class="card"><div class="card-head"><h2>Filed</h2><span class="muted">sandbox portal (never a real one)</span></div>
          <div class="card-pad"><b class="mono">${esc(d.filed.portal_ref)}</b> · ${usd(d.filed.amount)} to ${chan(d.filed.channel)} · ${when(d.filed.at)}</div></section>` : ""}

        <section><h2>Audit trail</h2>${timeline(d.log)}
          ${d.needs_human ? `<div class="card card-pad decide"><b>Your call, ${esc(actorName())}.</b>
            <span class="muted">${f.outcome === "conflict" ? "Approve = dispute it. Reject = accept the charge." : "Approve sends the draft to Clicky for filing."}</span>
            ${decideBox(f, true)}</div>` : ""}
          ${f.state === "FILED" ? `<div class="card card-pad decide"><b>Did ${chan(f.channel)} pay it?</b>
            <span class="muted">Marking it recovered is ground truth: the judges' open bets on this finding settle.</span>
            <div class="q-actions"><button class="btn-ok" data-act="recover" data-id="${esc(f.finding_id)}">Mark recovered (platform paid)</button></div></div>` : ""}
        </section>
      </div>
      <aside class="side">
        <div class="side-block"><h3>Decided by</h3><div class="v">${human ? `<b>${esc(human.actor)}</b> ${badge("human", "human")}<div class="muted">${human.action === "approve" ? "approved" : "rejected"} ·${when(human.at)}</div><div>${esc(human.reason)}</div>` : '<span class="muted">No human decision yet</span>'}</div></div>
        <div class="side-block"><h3>Outcome</h3><div class="v">${f.outcome ? badge(f.outcome, f.outcome) : '<span class="muted">pending</span>'}</div></div>
        <div class="side-block"><h3>Filed as</h3><div class="v mono">${esc(f.portal_ref || "") || '<span class="muted">not filed</span>'}</div></div>
        <div class="side-block"><h3>Rank score</h3><div class="v num">${f.rank_score != null ? esc(f.rank_score) : '<span class="muted">n/a</span>'}</div></div>
        <div class="side-block"><h3>Agents on this finding</h3><div class="v">${findingAgents(d.agents)}</div></div>
        <div class="side-block"><h3>Denied attempts</h3><div class="v">${d.denied.length ? d.denied.map((e) => `<div><b>${esc(e.actor)}</b> (${esc(e.role)}) tried to ${esc(e.action)}</div>`).join("") : '<span class="muted">None</span>'}</div></div>
      </aside>
    </div>`;
}

async function renderScore() {
  const [s, rules] = await Promise.all([api("/api/score"), api("/api/rules")]);
  const max = Math.max(1, ...s.runs.map((r) => r.to_human));
  view.innerHTML = `
    <div class="page-head"><div><h1>Scoreboard</h1><div class="sub">Flock's calls vs <code>data/answer_key.csv</code> (same logic as <code>scripts/score.py</code>).</div></div></div>
    <div class="stats">
      <div class="card stat"><div class="label">Traps caught</div><div class="big ${s.caught === s.total ? "ok-t" : ""}">${s.caught}/${s.total}</div></div>
      <div class="card stat"><div class="label">Missed</div><div class="big ${s.missed ? "bad-t" : ""}">${s.missed}</div></div>
      <div class="card stat"><div class="label">False alarms</div><div class="big ${s.false_alarms ? "bad-t" : ""}">${s.false_alarms}</div></div>
      <div class="card stat accent"><div class="label">Recoverable dollars found</div><div class="big" style="color:var(--accent)">${usd(s.recovered)}</div></div>
      <div class="card stat"><div class="label">Sent to a human (this run)</div><div class="big">${s.to_human}</div><div class="hint">${s.by_rule} settled by learned rules</div></div>
    </div>
    <div class="grid-2">
      <section class="card"><div class="card-head"><h2>Traps vs answer key</h2></div>
        <div class="table-scroll"><table><thead><tr><th>Order</th><th>Trap</th><th>Expected</th><th>Flock said</th><th class="r">Amount</th><th>Result</th></tr></thead><tbody>
        ${s.traps.map((t) => `<tr ${t.finding_id ? `class="link" data-href="#/finding/${esc(t.finding_id)}"` : ""}>
          <td class="mono">${esc(t.order_id)}</td><td>${label(t.trap_type)}</td><td>${badge(t.expected_outcome, t.expected_outcome)}</td>
          <td>${t.got ? badge(t.got, t.got) : '<span class="muted">not found</span>'}${t.by_rule ? ' <span class="muted">(rule)</span>' : ""}</td>
          <td class="r num">${usd(t.amount)}</td>
          <td>${t.caught ? '<b class="ok-t">caught</b>' : '<b class="bad-t">missed</b>'}${t.false_alarm ? ' <b class="bad-t">false alarm</b>' : ""}</td></tr>`).join("")}
        </tbody></table></div></section>
      <section class="card"><div class="card-head"><h2>Human hand-offs by week</h2><span class="muted">same traps, after learning</span></div>
        ${s.runs.length ? `<div class="weeks">${s.runs.slice(-6).map((r) => `<div class="week">
            <span class="n">${r.to_human}</span><div class="col" style="height:${Math.round((r.to_human / max) * 120)}px"></div>
            <span class="lbl">${esc(r.label)}<br>${r.learned_rules} rule${r.learned_rules === 1 ? "" : "s"}</span></div>`).join("")}</div>`
          : `<div class="empty">No runs recorded. Run the Quickstart: week 1 with <code>--forget</code>, approve R-001, then week 2.</div>`}
      </section>
    </div>
    ${rulesCard(rules)}`;
}

// ---------- judge market ----------
function attentionTag(f) {
  const a = f.attention;
  if (!a) return "";
  const cls = a.status === "deferred" ? "deferred" : "queued";
  return `<div class="att ${cls}" title="value of a human looking = |gap| × (1 − weighted confidence)">
    ${a.status === "deferred" ? "deferred" : `attention #${esc(a.rank)} of ${esc(a.budget)}`} · value ${usd(a.value)}</div>`;
}

function sparkline(values, w = 120, h = 28) {
  if (!values || values.length < 2) return `<svg class="spark" width="${w}" height="${h}" aria-hidden="true"><line x1="0" y1="${h / 2}" x2="${w}" y2="${h / 2}"/></svg>`;
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * (w - 4) + 2).toFixed(1)},${(h - 3 - ((v - lo) / span) * (h - 6)).toFixed(1)}`);
  const up = values[values.length - 1] >= values[0];
  return `<svg class="spark ${up ? "up" : "down"}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
    aria-label="balance from ${values[0]} to ${values[values.length - 1]}"><polyline points="${pts.join(" ")}"/></svg>`;
}

function ledgerFeed(rows) {
  if (!rows.length) return `<div class="empty">No bets yet. Run the pipeline: every judge vote is a bet.</div>`;
  const signed = (n) => (n > 0 ? "+" : "") + Number(n).toFixed(2);
  return `<ul class="feed">${rows.map((e) => {
    const who = esc(e.judge.replace("judge.", ""));
    const link = `<a href="#/finding/${esc(e.finding_id)}" class="mono">${esc(e.finding_id)}</a>`;
    if (e.kind === "stake") return `<li><span class="dot agent"></span>
      <span><b>${who}</b> bets <b>${esc(e.outcome)}</b> ${pct(e.confidence)} on ${link}</span>
      <span class="meta">stake ${esc(e.stake)} · to win ${esc(e.to_win)}, at risk ${esc(e.at_risk)} · ${when(e.at)}</span></li>`;
    if (e.kind === "settle") return `<li><span class="dot ${e.payout > 0 ? "human" : e.payout < 0 ? "denied" : ""}"></span>
      <span><b>${who}</b> ${esc(e.result)} <b class="${e.payout > 0 ? "ok-t" : e.payout < 0 ? "bad-t" : "muted"}">${signed(e.payout)}</b> on ${link}</span>
      <span class="meta">called ${esc(e.called)}, truth ${esc(e.truth)} (${esc(e.source)} by ${esc(e.decided_by)}) · balance ${Number(e.balance).toFixed(2)} · ${when(e.at)}</span></li>`;
    return `<li><span class="dot"></span><span><b>${who}</b> bet on ${link} voided</span><span class="meta">${esc(e.reason)} · ${when(e.at)}</span></li>`;
  }).join("")}</ul>`;
}

async function renderMarket() {
  const m = await api("/api/market");
  const a = m.attention;
  const leader = m.leaderboard[0];
  view.innerHTML = `
    <div class="page-head"><div><h1>Judge market</h1>
      <div class="sub">Every vote is a bet. Bets settle when a human approves or rejects, or the platform pays. Balances weight the panel next week.</div></div></div>
    <div class="stats">
      <div class="card stat accent"><div class="label">Attention budget used</div><div class="value">${a.used}/${a.budget}</div><div class="hint">${a.remaining} token${a.remaining === 1 ? "" : "s"} left this week</div></div>
      <div class="card stat"><div class="label">Deferred</div><div class="value">${a.deferred.length}</div><div class="hint">over budget, still waiting for a human</div></div>
      <div class="card stat"><div class="label">Bets settled</div><div class="value">${m.settled}</div><div class="hint">${m.open_bets} still open</div></div>
      <div class="card stat"><div class="label">Most trusted judge</div><div class="value" style="font-size:22px">${esc(leader.judge.replace("judge.", ""))}</div><div class="hint">${pct(leader.weight)} of the panel's weight</div></div>
    </div>
    <div class="grid-2">
      <section class="card"><div class="card-head"><h2>Balances</h2><span class="muted">start ${m.start}, Brier-scored</span></div>
        <div class="table-scroll"><table><thead><tr><th>Judge</th><th class="r">Balance</th><th class="r">Weight</th><th>History</th><th>Record</th></tr></thead><tbody>
        ${m.leaderboard.map((r) => `<tr><td><b>${esc(r.judge.replace("judge.", ""))}</b></td>
          <td class="r num"><b>${Number(r.balance).toFixed(2)}</b><div class="${r.pnl >= 0 ? "ok-t" : "bad-t"}" style="font-size:12px">${r.pnl >= 0 ? "+" : ""}${Number(r.pnl).toFixed(2)}</div></td>
          <td class="r num">${pct(r.weight)}</td><td>${sparkline(r.history)}</td>
          <td class="muted">${r.won} won · ${r.lost} lost · ${r.abstained} abstained · ${r.open} open</td></tr>`).join("")}
        </tbody></table></div>
        <div class="card-pad muted" style="font-size:12.5px;border-top:1px solid var(--border)">
          Payout = 10 × (1 − 4 × (p − y)²), p = the judge's probability it's recoverable, y = what really happened.
          Confident and right: up to +10. "Conflict" (no lean): 0. Confident and wrong: down to −30. Honest confidence pays best.</div>
      </section>
      <section class="card"><div class="card-head"><h2>Recent bets and settlements</h2><span class="muted">append-only ledger</span></div>${ledgerFeed(m.recent)}</section>
    </div>
    <section class="card section-gap"><div class="card-head"><h2>Human attention this week</h2>
        <span class="muted">value = |gap| × (1 − weighted confidence)</span></div>
      ${a.escalations ? `<div class="table-scroll"><table><thead><tr><th>#</th><th>Finding</th><th>Check</th><th class="r">Gap</th><th class="r">Value</th><th>Status</th></tr></thead><tbody>
        ${[...a.queued, ...a.deferred].map((d) => `<tr class="link" data-href="#/finding/${esc(d.finding_id)}">
          <td class="num">${esc(d.rank)}</td><td><a href="#/finding/${esc(d.finding_id)}" class="mono">${esc(d.finding_id)}</a></td>
          <td>${chan(d.channel)} ${label(d.check_type)}</td><td class="r num">${usd(d.gap)}</td><td class="r num">${usd(d.value)}</td>
          <td>${d.status === "deferred" ? '<span class="badge b-conflict">deferred</span>' : '<span class="badge b-JUDGED">queued</span>'}
            ${d.decided ? badge("human", "decided") : ""}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">Nothing escalated this week. Budget untouched.</div>`}
    </section>`;
}

// ---------- journal entries ----------
const balancedMark = (j) => (j.balanced ? '<b class="ok-t">balanced</b>' : '<b class="bad-t">does not balance</b>');

async function renderJournal() {
  const d = await api("/api/journal");
  const rows = d.entries;
  const approved = rows.filter((j) => j.state === "APPROVED").length;
  const groups = {};
  rows.forEach((j) => ((groups[`${j.store_id} · ${j.week || "no week"}`] ||= []).push(j)));
  const table = (list) => `<div class="table-scroll"><table><thead><tr>
      <th>Journal</th><th>Channel</th><th>Payout</th><th>Date</th><th class="r">Debits</th><th class="r">Credits</th>
      <th class="r">Disputed</th><th>Check</th><th>State</th><th></th></tr></thead><tbody>
    ${list.map((j) => `<tr class="link" data-href="#/journal/${esc(j.je_id)}">
      <td><a href="#/journal/${esc(j.je_id)}" class="mono">${esc(j.journal_no)}</a></td><td>${chan(j.channel)}</td>
      <td class="mono">${esc(j.payout_id)}</td><td>${esc(j.date)}</td>
      <td class="r num">${usd(j.total_debit)}</td><td class="r num">${usd(j.total_credit)}</td>
      <td class="r num">${j.disputed ? usd(j.disputed) : ""}</td><td>${balancedMark(j)}</td><td>${badge(j.state, j.state)}</td>
      <td>${j.state === "DRAFT" ? `<button class="btn-ok" data-act="je-approve" data-id="${esc(j.je_id)}">Approve</button>` : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
  view.innerHTML = `
    <div class="page-head"><div><h1>Journal entries</h1>
      <div class="sub">One draft entry per store, channel and payout. The agent drafts; you approve as <b>${esc(actorName())}</b> (human);
        only the gatekeeper posts, by writing the GL import file.</div></div>
      ${approved ? `<button class="btn-ok" data-act="je-export">Export ${approved} approved to the GL import</button>` : ""}</div>
    <div class="stats">
      <div class="card stat"><div class="label">Entries</div><div class="value">${rows.length}</div>
        <div class="hint">${d.by_state.DRAFT} draft · ${d.by_state.APPROVED} approved · ${d.by_state.POSTED} posted</div></div>
      <div class="card stat"><div class="label">Debits = credits</div><div class="value num">${usd(d.debits)}</div>
        <div class="hint">${d.unbalanced ? `<b class="bad-t">${d.unbalanced} unbalanced</b>` : "every entry balances to the cent"}</div></div>
      <div class="card stat accent"><div class="label">Due from platforms (disputed)</div>
        <div class="value num">${usd(rows.reduce((a, j) => a + j.disputed, 0))}</div><div class="hint">booked as a receivable, not expense</div></div>
    </div>
    ${rows.length ? Object.entries(groups).map(([k, list]) => `<section class="card je-group"><div class="card-head"><h2>${esc(k)}</h2>
        <span class="muted">${list.length} entr${list.length === 1 ? "y" : "ies"}</span></div>${table(list)}</section>`).join("")
      : `<div class="card empty section-gap">No journal entries. They are drafted on real data: run the generator, collector and formatter, then <code>python -m flock.pipeline --reset</code>.</div>`}`;
}

function sourceLinks(ln) {
  const out = [];
  (ln.sources || []).forEach((s) => s.rows.slice(0, 3).forEach((r) => out.push(
    `<button class="btn-ghost src mono" data-act="src" data-file="${esc(s.source_file)}" data-row="${esc(r)}">${esc(s.source_file.split("/").pop())}:${esc(r)}</button>`)));
  const n = (ln.sources || []).reduce((a, s) => a + s.rows.length, 0);
  return out.join("") + (n > out.length ? `<span class="muted">+${n - out.length} more</span>` : "");
}

async function renderJournalEntry(id) {
  let d;
  try { d = await api(`/api/journal/${encodeURIComponent(id)}`); }
  catch (e) { view.innerHTML = `<div class="card empty section-gap">${esc(e.message)}. <a href="#/journal">Back to the journal</a></div>`; return; }
  const j = d.entry;
  view.innerHTML = `
    <div class="pr-head">
      <div class="muted"><a href="#/journal">Journal</a> / entry</div>
      <h1 style="margin-top:6px">${esc(j.journal_no)} · ${chan(j.channel)} payout ${esc(j.payout_id)} · store ${esc(j.store_id)} <span class="id">${esc(j.je_id)}</span></h1>
      <div class="pr-meta">${badge(j.state, j.state)} ${balancedMark(j)}
        <span>dated ${esc(j.date)}${j.deposit_id ? ` · deposit <span class="mono">${esc(j.deposit_id)}</span>` : ""} · tax rule ${esc(j.tax_rule || "none in effect")}</span>
        ${j.export_file ? `<span>posted to <span class="mono">${esc(j.export_file)}</span></span>` : ""}</div>
    </div>
    <div class="stack">
      <section class="card"><div class="card-head"><h2>Lines</h2><span class="muted">location ${esc(j.store_id)} · class ${chan(j.channel)}</span></div>
        <div class="table-scroll"><table class="je-lines"><thead><tr><th>#</th><th>Account</th><th class="r">Debit</th><th class="r">Credit</th><th>Memo and sources</th></tr></thead><tbody>
        ${j.lines.map((ln) => `<tr><td class="num">${ln.line_no}</td><td><span class="mono">${esc(ln.account)}</span> ${esc(ln.account_name)}</td>
          <td class="r num">${ln.debit ? usd(ln.debit) : ""}</td><td class="r num">${ln.credit ? usd(ln.credit) : ""}</td>
          <td class="memo">${esc(ln.memo)}${ln.finding_id ? ` <a class="mono" href="#/finding/${esc(ln.finding_id)}">${esc(ln.finding_id)}</a>` : ""}
            <div>${sourceLinks(ln)}</div><div class="src-row"></div></td></tr>`).join("")}
        </tbody><tfoot><tr><td></td><td>Total</td><td class="r num">${usd(j.total_debit)}</td><td class="r num">${usd(j.total_credit)}</td><td>${balancedMark(j)}</td></tr></tfoot></table></div>
      </section>
      ${j.notes && j.notes.length ? `<section class="card"><div class="card-head"><h2>Notes</h2></div><ul class="notes">${j.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></section>` : ""}
      <section><h2>Audit trail</h2>${timeline(d.log)}
        ${j.state === "DRAFT" ? `<div class="card card-pad decide"><b>Your call, ${esc(actorName())}.</b>
          <span class="muted">Approve = ready to post to the GL. Reject = do not post.</span>
          <div class="q-actions">
            <textarea data-reason="${esc(j.je_id)}" rows="1" placeholder="Reason (goes in the audit log)"></textarea>
            <button class="btn-ok" data-act="je-approve" data-id="${esc(j.je_id)}">Approve</button>
            <button class="btn-bad" data-act="je-reject" data-id="${esc(j.je_id)}">Reject</button>
            <button class="btn-ghost" data-act="je-agent-try" data-id="${esc(j.je_id)}" title="Shows the gatekeeper refusing an agent">Try as an agent</button>
          </div></div>` : ""}
        ${j.state === "APPROVED" ? `<div class="card card-pad decide"><button class="btn-ok" data-act="je-export">Export approved entries to the GL import</button></div>` : ""}
      </section>
    </div>`;
}

// ---------- the live flock (flock/agents_live.py -> /api/agents) ----------
// Keeps every agent of the latest run in `flock.agents`, merged from incremental polls (?since=<cursor>).
const ROLES = ["worker", "checker", "judge", "investigator", "writer", "clicky", "learner", "journal"];
const ROLE_JOB = {
  worker: "matches one store's orders to payouts and runs the checkers",
  checker: "reconciles payouts with bank deposits",
  judge: "three votes on one finding",
  investigator: "builds a case file for an unexplained finding",
  writer: "drafts one dispute",
  clicky: "files approved disputes in the sandbox portal",
  learner: "proposes rules from human decisions",
  journal: "drafts the journal entries",
};
const flock = { cursor: 0, run: null, agents: new Map(), counts: {}, active: [], activeCount: 0, total: 0 };

async function pollAgents() {
  const d = await api(`/api/agents?since=${flock.cursor}`);
  if (d.reset) flock.agents.clear();
  for (const a of d.agents) flock.agents.set(a.agent_id, a);
  const changed = d.reset || d.agents.length > 0 || d.cursor !== flock.cursor;
  Object.assign(flock, { cursor: d.cursor, run: d.run, counts: d.counts, active: d.active, activeCount: d.active_count, total: d.total });
  const fc = $("#flock-count");
  if (fc) { fc.textContent = big(d.total); fc.classList.toggle("hot", d.active_count > 0); }
  return changed;
}

const stateWord = { spawned: "spawned", working: "working", done: "done", failed: "failed" };
const agentTitle = (a) => `${a.role}${a.subject ? " · " + a.subject : ""} · ${stateWord[a.state] || a.state}${a.summary ? "\n" + a.summary : ""}`;
const subjectLink = (s) => (/^F-/.test(String(s || "")) ? `<a href="#/finding/${esc(s)}" class="mono">${esc(s)}</a>` : `<span class="mono">${esc(s ?? "")}</span>`);

function agentChip(a, parentSubject) {
  const text = a.pooled ? `${a.role} ×${big(a.count)}` : (a.subject != null && a.subject === parentSubject ? a.role : (a.subject ?? a.agent_id));
  const href = /^F-/.test(String(a.subject || "")) ? `#/finding/${esc(a.subject)}` : null;
  const inner = `<span class="fl-dot"></span>${esc(text)}`;
  return href
    ? `<a class="fl-chip r-${esc(a.role)} s-${esc(a.state)}" href="${href}" title="${esc(agentTitle(a))}">${inner}</a>`
    : `<span class="fl-chip r-${esc(a.role)} s-${esc(a.state)}" title="${esc(agentTitle(a))}">${inner}</span>`;
}

function agentNode(a, kids, depth = 0) {
  const mine = kids.get(a.agent_id) || [];
  const leaves = mine.filter((k) => !(kids.get(k.agent_id) || []).length);
  const branches = mine.filter((k) => (kids.get(k.agent_id) || []).length);
  const pool = a.pooled ? ` <span class="num">${big(a.done)}/${big(a.count)} done${a.failed ? `, ${big(a.failed)} failed` : ""}</span>` : "";
  return `<div class="fl-node r-${esc(a.role)} s-${esc(a.state)}" style="--depth:${depth}">
      <div class="fl-head"><span class="fl-dot"></span><b>${esc(a.role)}</b> ${a.pooled ? '<span class="muted">pooled</span>' : subjectLink(a.subject)}
        <span class="fl-state">${esc(stateWord[a.state] || a.state)}</span>${pool}
        <span class="fl-sum muted">${esc(a.summary || "")}</span></div>
      ${branches.length ? `<div class="fl-branches">${branches.map((k) => agentNode(k, kids, depth + 1)).join("")}</div>` : ""}
      ${leaves.length ? `<div class="fl-kids">${leaves.map((k) => agentChip(k, a.subject)).join("")}</div>` : ""}
    </div>`;
}

function flockTree() {
  const all = [...flock.agents.values()];
  const ids = new Set(all.map((a) => a.agent_id));
  const kids = new Map();
  const roots = [];
  for (const a of all) {
    if (a.parent_id && ids.has(a.parent_id)) {
      if (!kids.has(a.parent_id)) kids.set(a.parent_id, []);
      kids.get(a.parent_id).push(a);
    } else roots.push(a);
  }
  const order = (a) => [ROLES.indexOf(a.role), a.seq || 0];
  const cmp = (x, y) => { const a = order(x), b = order(y); return a[0] - b[0] || a[1] - b[1]; };
  roots.sort(cmp);
  kids.forEach((v) => v.sort(cmp));
  return roots.map((a) => agentNode(a, kids)).join("");
}

function roleCards() {
  return `<div class="fl-roles">${ROLES.map((r) => {
    const c = flock.counts[r] || { total: 0, working: 0, spawned: 0, done: 0, failed: 0 };
    const live = (c.working || 0) + (c.spawned || 0);
    return `<div class="card fl-role r-${r}${c.total ? "" : " idle"}" title="${esc(ROLE_JOB[r])}">
        <div class="fl-role-head"><span class="fl-dot"></span>${esc(r)}</div>
        <div class="value num">${big(c.total)}</div>
        <div class="hint">${live ? `<b>${big(live)} active</b> · ` : ""}${big(c.done)} done${c.failed ? ` · <span class="bad-t">${big(c.failed)} failed</span>` : ""}</div>
      </div>`;
  }).join("")}</div>`;
}

async function renderFlock() {
  try { await pollAgents(); } catch (e) { view.innerHTML = `<div class="card empty section-gap">${esc(e.message)}</div>`; return; }
  drawFlock();
}

function drawFlock() {
  const inv = flock.counts.investigator || { total: 0 };
  view.innerHTML = `
    <div class="page-head"><div><h1>Flock</h1>
      <div class="sub">Every agent with a job: one worker per store, a judge per finding, and an investigator only when a finding can't be explained.
        ${flock.run ? `<span class="mono">${esc(flock.run)}</span>` : ""}</div></div>
      <div class="muted num">${big(flock.total)} agents · ${big(flock.activeCount)} active · ${big(inv.total)} investigator${inv.total === 1 ? "" : "s"}</div></div>
    ${roleCards()}
    <section class="card section-gap"><div class="card-head"><h2>Working now</h2><span class="muted">${flock.activeCount ? `${big(flock.activeCount)} active` : "nobody, the run is idle"}</span></div>
      ${flock.active.length ? `<div class="fl-kids fl-active">${flock.active.map((a) => agentChip(a)).join("")}</div>` : '<div class="empty">No agent is working. Start a run: <code>python -m flock.pipeline --reset --pace 0.3</code></div>'}
    </section>
    <section class="card section-gap"><div class="card-head"><h2>Who spawned whom</h2>
        <span class="muted fl-legend"><span class="fl-chip s-spawned"><span class="fl-dot"></span>spawned</span><span class="fl-chip s-working"><span class="fl-dot"></span>working</span><span class="fl-chip s-done"><span class="fl-dot"></span>done</span><span class="fl-chip s-failed"><span class="fl-dot"></span>failed</span></span></div>
      <div class="fl-tree">${flock.agents.size ? flockTree() : '<div class="empty">No agents yet for this store. Run the pipeline to see the flock.</div>'}</div>
    </section>`;
}

function caseFileCard(cf) {
  if (!cf) return "";
  const rows = (items, head, row) => (items && items.length ? `<div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${items.map(row).join("")}</tbody></table></div>` : "");
  return `<section class="card cf"><div class="card-head"><h2>Case file</h2>
      <span class="muted">investigator · ${esc(cf.why)} · ${esc(cf.summary_by === "template" ? "template summary" : cf.summary_by)} · adds evidence, never changes the outcome or the money</span></div>
    <div class="card-pad">${cf.summary_by === "template" ? `<b>${esc(cf.why[0].toUpperCase() + cf.why.slice(1))}.</b>` : esc(cf.summary)}
      <ul class="cf-facts">${(cf.facts || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <div><b>Check next:</b> ${esc(cf.next_step)}</div></div>
    ${rows(cf.payouts, "<th>Payout</th><th>Date</th><th>Store</th><th class=\"r\">Lines</th><th class=\"r\">Net</th>",
      (p) => `<tr><td class="mono">${esc(p.payout_id)}</td><td>${esc(p.payout_date)}</td><td>${esc(p.store_id ?? "")}</td><td class="r num">${esc(p.lines)}</td><td class="r num">${usd(p.net)}</td></tr>`)}
    ${rows(cf.nearby_deposits, "<th>Nearby deposit</th><th>Date</th><th class=\"r\">Amount</th><th class=\"r\">Off by</th><th>Memo names it</th>",
      (d) => `<tr><td class="mono">${esc(d.deposit_id)}</td><td>${esc(d.deposit_date)}</td><td class="r num">${usd(d.amount)}</td><td class="r num">${usd(d.off_by)}</td><td>${d.memo_names_payout ? "yes" : "no"}</td></tr>`)}
    ${rows(cf.claims, "<th>Claim</th><th>Authority</th><th>Value</th><th>Effective</th>",
      (c) => `<tr><td>${c.quote ? `“${esc(c.quote)}”` : ""}<div class="muted mono">${esc(c.source_file ?? "")}</div></td><td>${authBadge(c.authority)}${c.binding ? "" : ' <span class="muted">non-binding</span>'}</td>
        <td class="num">${esc(c.value ?? "")}</td><td class="muted">${esc(c.effective_from ?? "")}${c.effective_to ? " to " + esc(c.effective_to) : ""}</td></tr>`)}
  </section>`;
}

function findingAgents(agents) {
  if (!agents || !agents.length) return '<span class="muted">None recorded</span>';
  return agents.map((a) => `<div class="fl-mini"><span class="fl-chip r-${esc(a.role)} s-${esc(a.state)}"><span class="fl-dot"></span>${esc(a.role)}</span> <span class="muted">${esc(a.summary || stateWord[a.state] || "")}</span></div>`).join("");
}

// ---------- Clicky: the workflow it learned from watching a human (flock/workflow.py -> /api/clicky) ----------
const FEED_FIELD = { finding_id: "finding ID", order_ids: "order IDs", amount: "dispute amount", channel: "sales channel",
  category: "dispute category", dispute_draft: "approved dispute message" };
const WF_BADGE = { DRAFT: "JUDGED", APPROVED: "APPROVED", STALE: "REJECTED", REJECTED: "REJECTED", RETIRED: "NEW" };

async function decideWorkflow(action, role = "human") {
  busy = true;
  try {
    const r = await api(`/api/clicky/${action}`, {
      method: "POST", body: JSON.stringify({ actor: role === "human" ? actorName() : "clicky", role }),
    });
    toast(`${r.workflow.workflow_id} ${action === "approve" ? "approved" : "rejected"} by ${actorName()}. Logged with its hash.`);
  } catch (e) {
    toast(e.status === 403 ? `Gatekeeper denied it: ${e.message}. The attempt is in the audit log.` : e.message, true);
  } finally { busy = false; await refresh(true); }
}

function feedChip(s) {
  const src = s.source || {};
  if (src.kind === "finding") return `<span class="chip feed-f">finding · ${esc(FEED_FIELD[src.field] || src.field)}</span>`;
  if (src.kind === "env") return `<span class="chip feed-e">${src.secret ? "secret" : "setting"} · <span class="mono">${esc(src.name)}</span></span>`;
  if (src.kind === "constant") return `<span class="chip feed-c" title="${esc(src.value)}">constant</span>`;
  return "";
}

function wfStep(s, d) {
  const wf = d.workflow;
  const stale = wf.state === "STALE" && wf.stale_step === s.n;
  const t = s.target || {};
  const where = [t.label && `label “${t.label}”`, t.role && t.text && `${t.role} “${t.text}”`, t.name && `name=${t.name}`]
    .filter(Boolean).join(" · ");
  const shown = s.demo_value != null && !(s.source || {}).secret
    ? `<div class="wf-val mono" title="${esc(s.demo_value)}">typed in the demo: ${esc(s.demo_value.length > 90 ? s.demo_value.slice(0, 87) + "…" : s.demo_value)}</div>` : "";
  const img = s.screenshot && d.demo && d.demo.exists
    ? `<a href="${esc(d.demo.base + s.screenshot)}" target="_blank" rel="noopener"><img class="wf-thumb" loading="lazy" src="${esc(d.demo.base + s.screenshot)}" alt="screenshot of step ${s.n}"></a>`
    : `<div class="wf-thumb none">no screenshot</div>`;
  return `<li class="wf-step ${stale ? "stale" : ""}">
    <span class="wf-n">${s.n}</span>
    <div class="wf-main">
      <div class="wf-desc">${esc(s.describe)}</div>
      <div class="wf-meta"><span class="chip">${esc(s.phase === "login" ? "sign in (once)" : "per dispute")}</span>${feedChip(s)}
        ${where ? `<span class="muted mono">${esc(where)}</span>` : s.action === "goto" ? `<span class="muted mono">${esc(s.path)}</span>` : ""}</div>
      ${shown}
      ${stale ? `<div class="wf-stale">Clicky stopped here: ${esc(wf.stale_reason)}</div>` : ""}
    </div>${img}</li>`;
}

async function renderClicky() {
  const d = await api("/api/clicky");
  const wf = d.workflow;
  const head = `<div class="page-head"><div><h1>Clicky</h1>
      <div class="sub">Clicky watched a person file one dispute in the sandbox portal and learned the steps. Each typed value was matched
        to the finding field it came from, so Clicky can fill the form for any approved finding. It may only use the workflow after a
        human approves it; if the portal changes, it stops and asks for one new demonstration.</div></div></div>`;
  if (!wf) {
    view.innerHTML = head + `<div class="card empty section-gap">Nothing learned yet. Clicky files with its built-in selectors until someone shows it.
      <br><br>Record a demonstration: <code>python -m flock.agents.clicky record</code></div>`;
    return;
  }
  const fed = Object.keys(wf.fields || {}).length;
  let banner = "";
  if (wf.state === "DRAFT") {
    banner = `<div class="card card-pad wf-banner warn"><b>Waiting for your approval, ${esc(actorName())}.</b>
      <span>Check each step and which finding field feeds it. Clicky won't file in browser mode until a human approves.</span>
      ${d.missing_required.length ? `<div class="bad-t">Can't be approved: no step takes the ${esc(d.missing_required.map((m) => FEED_FIELD[m]).join(" or "))} from the finding.</div>` : ""}
      <div class="q-actions">
        <button class="btn-ok" data-act="wf-approve">Approve workflow</button>
        <button class="btn-bad" data-act="wf-reject">Reject</button>
        <button class="btn-ghost" data-act="wf-agent-try" title="Shows the gatekeeper refusing an agent">Try as an agent</button>
      </div></div>`;
  } else if (wf.state === "STALE") {
    banner = `<div class="card card-pad wf-banner bad"><b>The portal changed. Clicky stopped.</b>
      <span>${esc(wf.stale_reason)}. ${wf.stale_submitted ? "A dispute may have been submitted; check the portal." : "Nothing was typed or submitted."}
        Clicky never guesses on a money form.</span>
      <div><b>Show it once more:</b> <code>python -m flock.agents.clicky record</code> (file one dispute the new way), then approve the new version here.</div></div>`;
  } else if (wf.state === "APPROVED") {
    banner = `<div class="card card-pad wf-banner ok"><b>Approved by ${esc(wf.approved_by)}</b> <span class="muted">${when(wf.approved_at)}</span>
      <span>Hash <span class="mono">${esc((wf.approved_hash || "").slice(0, 12))}</span>. In browser mode (<code>FLOCK_CLICKY_MODE=browser</code>) Clicky replays these steps
        for every approved finding, and each filing's audit entry names this workflow and hash.</span></div>`;
  } else if (wf.state === "REJECTED") {
    banner = `<div class="card card-pad wf-banner">Rejected. Clicky uses its built-in selectors. Record again to propose a new workflow.</div>`;
  }
  const v = d.demo;
  const steps = wf.steps.map((s) => wfStep(s, d)).join("");
  view.innerHTML = head + `
    <div class="pr-meta" style="margin-bottom:12px">${badge(WF_BADGE[wf.state] || "NEW", wf.state)}
      <span class="mono">${esc(wf.workflow_id)}</span><span>learned ${when(wf.learned_at)} from demonstration <span class="mono">${esc(wf.learned_from)}</span></span>
      <span>portal form ${esc(wf.portal_layout || "?")} (serving ${esc(d.layout)})</span><span>labels: ${esc(wf.labeled_by)}</span>
      <span>Clicky mode now: ${esc(d.mode)}. In browser mode it ${esc(d.uses === "learned" ? "replays this workflow" : d.uses === "stopped" ? "files nothing until this is resolved" : "uses its built-in selectors")}</span></div>
    ${banner}
    <div class="stats section-gap">
      <div class="card stat"><div class="label">Steps</div><div class="value">${wf.steps.length}</div>
        <div class="hint">${wf.steps.filter((s) => s.phase === "login").length} to sign in, ${wf.steps.filter((s) => s.phase === "file").length} per dispute</div></div>
      <div class="card stat accent"><div class="label">Filled from the finding</div><div class="value">${fed}</div>
        <div class="hint">${esc(Object.keys(wf.fields || {}).map((k) => FEED_FIELD[k]).join(", ") || "none")}</div></div>
      <div class="card stat"><div class="label">Constants</div><div class="value">${(wf.constants || []).length}</div>
        <div class="hint">same value every time</div></div>
      <div class="card stat"><div class="label">Hash</div><div class="value mono" style="font-size:18px">${esc(wf.hash.slice(0, 12))}</div>
        <div class="hint">what a human approves</div></div>
    </div>
    ${(wf.warnings || []).length ? `<div class="card card-pad section-gap"><b>Check these</b><ul class="notes">${wf.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    <div class="grid-2">
      <section class="card"><div class="card-head"><h2>Learned steps</h2><span class="muted">in the order the person did them</span></div>
        <ol class="wf-steps">${steps}</ol></section>
      <div class="stack" style="align-content:start">
        <section class="card"><div class="card-head"><h2>The recording</h2></div><div class="card-pad">
          ${v && v.video ? `<video class="wf-video" controls preload="metadata" src="${esc(v.video)}"></video>
            <div><a href="${esc(v.video)}" target="_blank" rel="noopener">Open the video</a></div>` : `<div class="muted">No video for this demonstration.</div>`}
          ${v && v.trace ? `<div class="muted" style="margin-top:6px">Step-by-step trace: <a href="${esc(v.trace)}">trace.zip</a> · <code>python -m playwright show-trace ${esc(v.trace_path)}</code></div>` : ""}
        </div></section>
        <section class="card"><div class="card-head"><h2>Versions</h2></div><div class="table-scroll"><table>
          <thead><tr><th>Workflow</th><th>State</th><th>Form</th><th>Demo</th></tr></thead><tbody>
          ${d.history.map((h) => `<tr><td class="mono">${esc(h.workflow_id)}</td><td>${badge(WF_BADGE[h.state] || "NEW", h.state)}</td>
            <td>${esc(h.portal_layout || "?")}</td><td class="mono">${esc(h.learned_from)}</td></tr>`).join("")}
          </tbody></table></div></section>
        <section><h2>Audit trail</h2>${timeline(d.log)}</section>
      </div>
    </div>`;
}

async function pollClicky() {
  try {
    const d = await api("/api/clicky");
    const c = $("#clicky-count");
    const hot = d.needs_approval || d.needs_demo;
    c.hidden = !hot; c.classList.toggle("hot", hot);
    c.title = d.needs_demo ? "the portal changed: show Clicky once more" : d.needs_approval ? "a learned workflow waits for approval" : "";
  } catch { /* the board works without it */ }
}

// ---------- router + polling ----------
function route() {
  const h = location.hash.replace(/^#/, "") || "/";
  const m = h.match(/^\/finding\/(.+)$/);
  const jm = h.match(/^\/journal\/(.+)$/);
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on",
    (a.dataset.view === "live" && (h === "/" || m)) || (a.dataset.view === "queue" && h === "/queue")
    || (a.dataset.view === "journal" && (h === "/journal" || jm)) || (a.dataset.view === "score" && h === "/score")
    || (a.dataset.view === "market" && h === "/market") || (a.dataset.view === "flock" && h === "/flock")
    || (a.dataset.view === "clicky" && h === "/clicky")));
  if (h === "/clicky") return renderClicky();
  if (m) return renderFinding(decodeURIComponent(m[1]));
  if (jm) return renderJournalEntry(decodeURIComponent(jm[1]));
  if (h === "/journal") return renderJournal();
  if (h === "/queue") return renderQueue();
  if (h === "/score") return renderScore();
  if (h === "/market") return renderMarket();
  if (h === "/flock") return renderFlock();
  return renderLive();
}

function typing() {
  const a = document.activeElement;
  return (a && a.tagName === "TEXTAREA") || [...document.querySelectorAll("textarea[data-reason]")].some((t) => t.value.trim());
}

async function refresh(force = false) {
  try {
    const s = await api("/api/summary");
    lastSummary = s;
    const version = `${s.log_entries}:${s.findings}`;
    const qc = $("#queue-count");
    qc.textContent = s.awaiting_human; qc.classList.toggle("hot", s.awaiting_human > 0);
    $("#last-update").textContent = `updated ${new Date().toLocaleTimeString()}`;
    if (force || (version !== lastVersion && !busy && !typing())) {
      lastVersion = version;
      await route();
      pollClicky();
    }
  } catch (e) {
    $("#last-update").textContent = "can't reach the server, retrying…";
  }
}

// theme
(function theme() {
  const root = document.documentElement;
  try { const t = localStorage.getItem("flock-theme"); if (t) root.dataset.theme = t; } catch {}
  $("#theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("flock-theme", root.dataset.theme); } catch {}
  });
})();

// who am I
(function who() {
  const input = $("#actor");
  try { const v = localStorage.getItem("flock-actor"); if (v) input.value = v; } catch {}
  input.addEventListener("change", () => { try { localStorage.setItem("flock-actor", actorName()); } catch {} route(); });
})();

api("/api/meta").then((m) => ($("#store-name").textContent = `store: ${m.store}`)).catch(() => {});
window.addEventListener("hashchange", () => route());
refresh(true);
pollClicky();
pollProgress();
setInterval(refresh, 1000);
setInterval(pollProgress, 700);
// the Flock view redraws only when an agent changed; elsewhere the poll just keeps the tab's counter fresh
setInterval(async () => {
  const onFlock = (location.hash.replace(/^#/, "") || "/") === "/flock";
  try { if ((await pollAgents()) && onFlock && !busy) drawFlock(); } catch { /* the board works without it */ }
}, 1000);
