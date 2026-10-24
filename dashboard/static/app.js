// Flock dashboard. Plain JS, hash routes: #/  #/queue  #/finding/<id>  #/score
// Polls /api/summary every second; re-renders the current view when the audit log grows.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const CHANNEL = { doordash: "DoorDash", ubereats: "Uber Eats", grubhub: "Grubhub", direct_app: "Direct app" };
const STRONG = new Set(["contract", "amendment", "auth_email"]);
let lastVersion = null;
let lastSummary = null;
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
    toast(`${id} ${action === "approve" ? "approved" : "rejected"} by ${actorName()}. Logged.`);
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

document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-act]");
  if (b) {
    const id = b.dataset.id;
    const box = document.querySelector(`textarea[data-reason="${CSS.escape(id || "")}"]`);
    const reason = box ? box.value.trim() : "";
    if (b.dataset.act === "approve" || b.dataset.act === "reject") decide(id, b.dataset.act, reason);
    if (b.dataset.act === "agent-try") decide(id, "approve", "Agent tried to approve its own call", "agent");
    if (b.dataset.act === "file") fileApproved();
    if (b.dataset.act === "rule") approveRule(id);
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
        <a href="#/finding/${esc(e.subject_id)}" class="mono">${esc(e.subject_id)}</a></span>
      <span class="meta">${esc(e.before)} → ${esc(e.after)} · ${when(e.at)}</span></li>`).join("")}</ul>`;
}

async function renderLive() {
  const [s, rows] = await Promise.all([api("/api/summary"), api("/api/findings")]);
  const prev = {};
  view.querySelectorAll(".stat").forEach((el) => (prev[el.dataset.k] = el.dataset.v));
  view.innerHTML = `
    <div class="page-head"><div><h1>Live board</h1>
      <div class="sub">Every move below went through the gatekeeper and is in the audit log.</div></div></div>
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
        <div style="text-align:right"><div class="gap">${usd(f.gap)}</div>${badge(f.outcome, f.outcome === "conflict" ? "conflict: judges split" : "dispute drafted")}</div>
      </div>
      ${evidenceInline(f)}
      <div style="margin-top:8px">${voteChips(f)}</div>
      ${decideBox(f)}
    </article>`;
  const conflicts = rows.filter((f) => f.outcome === "conflict");
  const drafts = rows.filter((f) => f.outcome !== "conflict");
  view.innerHTML = `
    <div class="page-head"><div><h1>Human queue</h1>
      <div class="sub">Agents propose. You decide. Approve or reject goes through <code>gatekeeper.transition()</code> as <b>${esc(actorName())}</b> (human).</div></div>
      ${approved.length ? `<button class="btn-ok" data-act="file">File ${approved.length} approved via Clicky</button>` : ""}</div>
    <h3>Conflicts (${conflicts.length})</h3>
    <div class="queue">${conflicts.map(item).join("") || `<div class="card empty">No conflicts waiting. The panel and the learned rules handled everything.</div>`}</div>
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
          ${(f.votes || []).map((v) => `<div class="vote"><b>${esc(v.judge)}${v.round ? ` <span class="muted">${esc(v.round === "audit" ? "audit" : v.round === 2 ? "after debate" : "round 1")}</span>` : ""}</b>
              <span>${badge(v.outcome, v.outcome)} <span class="num">${pct(v.confidence)}</span><div class="bar"><i style="width:${Math.round(v.confidence * 100)}%"></i></div></span>
              <span class="why muted">${esc(v.reason)}</span></div>`).join("")}
        </section>

        ${f.dispute_draft ? `<section class="card"><div class="card-head"><h2>Dispute draft</h2><span class="muted">dispute_writer · not sent until a human approves</span></div><pre class="draft">${esc(f.dispute_draft)}</pre></section>` : ""}
        ${d.filed ? `<section class="card"><div class="card-head"><h2>Filed</h2><span class="muted">sandbox portal (never a real one)</span></div>
          <div class="card-pad"><b class="mono">${esc(d.filed.portal_ref)}</b> · ${usd(d.filed.amount)} to ${chan(d.filed.channel)} · ${when(d.filed.at)}</div></section>` : ""}

        <section><h2>Audit trail</h2>${timeline(d.log)}
          ${d.needs_human ? `<div class="card card-pad decide"><b>Your call, ${esc(actorName())}.</b>
            <span class="muted">${f.outcome === "conflict" ? "Approve = dispute it. Reject = accept the charge." : "Approve sends the draft to Clicky for filing."}</span>
            ${decideBox(f, true)}</div>` : ""}
        </section>
      </div>
      <aside class="side">
        <div class="side-block"><h3>Decided by</h3><div class="v">${human ? `<b>${esc(human.actor)}</b> ${badge("human", "human")}<div class="muted">${human.action === "approve" ? "approved" : "rejected"} ·${when(human.at)}</div><div>${esc(human.reason)}</div>` : '<span class="muted">No human decision yet</span>'}</div></div>
        <div class="side-block"><h3>Outcome</h3><div class="v">${f.outcome ? badge(f.outcome, f.outcome) : '<span class="muted">pending</span>'}</div></div>
        <div class="side-block"><h3>Filed as</h3><div class="v mono">${esc(f.portal_ref || "") || '<span class="muted">not filed</span>'}</div></div>
        <div class="side-block"><h3>Rank score</h3><div class="v num">${f.rank_score != null ? esc(f.rank_score) : '<span class="muted">n/a</span>'}</div></div>
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

// ---------- router + polling ----------
function route() {
  const h = location.hash.replace(/^#/, "") || "/";
  const m = h.match(/^\/finding\/(.+)$/);
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on",
    (a.dataset.view === "live" && (h === "/" || m)) || (a.dataset.view === "queue" && h === "/queue") || (a.dataset.view === "score" && h === "/score")));
  if (m) return renderFinding(decodeURIComponent(m[1]));
  if (h === "/queue") return renderQueue();
  if (h === "/score") return renderScore();
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
setInterval(refresh, 1000);
