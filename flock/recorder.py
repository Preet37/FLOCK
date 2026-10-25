"""Demonstration recorder (owner: Preet). Watches a human file ONE dispute in the sandbox portal.

  python -m flock.agents.clicky record            # opens the localhost portal in a real (headed) browser

A small script is injected into every page (context.add_init_script) and reports through an exposed binding:
every fill, select and click, with a locator for the element built from what a person sees (its label, its
role and text, its name attribute; id and CSS last), plus the page it happened on. Python adds navigations,
a screenshot per step, a Playwright video and a trace. The finding the human should file is shown in a panel on
the page (with copy buttons); the panel lives in a shadow root and is never recorded.
Recording ends when a reference number (SBX-...) appears on the page, or when the window is closed.
Passwords are never stored: a password field is recorded as "secret".

Output: data/demos/<demo_id>/ demo.json, step-NN.png, video.webm, trace.zip (gitignored).
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from flock import workflow

REF_RE = r"\bSBX-[A-Z0-9]{6}\b"

RECORDER_JS = r"""
(() => {
  if (window.__flockRecorderInstalled) return;
  window.__flockRecorderInstalled = true;
  const layout = () => (document.querySelector("meta[name=portal-layout]") || {}).content || null;
  const send = (ev) => { try { window.__flockRecord({...ev, path: location.pathname, layout: layout(), at: Date.now()}); } catch (e) {} };
  const inPanel = (e) => (e.composedPath ? e.composedPath() : []).some((n) => n && n.id === "__flock_panel_host");
  const text = (s) => (s || "").replace(/\s+/g, " ").trim();
  function labelOf(el) {
    if (el.labels && el.labels.length) return text(el.labels[0].innerText);
    const wrap = el.closest && el.closest("label");
    if (wrap) return text(wrap.innerText);
    return el.getAttribute("aria-label") || null;
  }
  function roleOf(el) {
    const t = el.tagName.toLowerCase(), type = (el.getAttribute("type") || "").toLowerCase();
    if (el.getAttribute("role")) return el.getAttribute("role");
    if (t === "button" || (t === "input" && ["submit", "button"].includes(type))) return "button";
    if (t === "a") return "link";
    if (t === "select") return "combobox";
    if (t === "textarea" || t === "input") return "textbox";
    return null;
  }
  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && parts.length < 5; n = n.parentElement) {
      let p = n.tagName.toLowerCase();
      const sib = n.parentElement ? [...n.parentElement.children].filter((c) => c.tagName === n.tagName) : [];
      if (sib.length > 1) p += `:nth-of-type(${sib.indexOf(n) + 1})`;
      parts.unshift(p);
    }
    return parts.join(" > ");
  }
  function target(el) {
    const t = el.tagName.toLowerCase();
    const role = roleOf(el);
    return {
      tag: t, type: (el.getAttribute("type") || null), label: labelOf(el), role,
      text: (role === "button" || role === "link") ? text(el.innerText || el.value) : null,
      name: el.getAttribute("name"), id: el.id || null, placeholder: el.getAttribute("placeholder"), css: cssPath(el),
    };
  }
  function flash(el) {
    const old = el.style.outline;
    el.style.outline = "3px solid #f59e0b";
    setTimeout(() => { el.style.outline = old; }, 700);
  }
  const pending = new Map(), sent = new WeakMap();
  function flush() {
    for (const el of pending.values()) {
      if (sent.get(el) === el.value) continue;
      sent.set(el, el.value);
      const secret = (el.getAttribute("type") || "").toLowerCase() === "password";
      flash(el);
      if (el.tagName === "SELECT") {
        const opt = el.options[el.selectedIndex];
        send({kind: "select", target: target(el), value: el.value, option_text: opt ? text(opt.text) : null});
      } else {
        send({kind: "fill", target: target(el), value: secret ? null : el.value, secret});
      }
    }
    pending.clear();
  }
  const watch = (e) => {
    if (inPanel(e)) return;
    const el = e.target;
    if (!el || !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
    if (["submit", "button", "checkbox", "radio", "hidden"].includes((el.getAttribute("type") || "").toLowerCase())) return;
    pending.set(el, el);
    clearTimeout(watch.t); watch.t = setTimeout(flush, 300);   // a pause in typing counts as done with the field
    if (e.type === "change") flush();
  };
  document.addEventListener("input", watch, true);
  document.addEventListener("change", watch, true);
  document.addEventListener("focusout", (e) => { if (!inPanel(e)) flush(); }, true);
  let lastClick = 0;
  document.addEventListener("click", (e) => {
    if (inPanel(e)) return;
    const el = e.target.closest && e.target.closest("button, a, input[type=submit], input[type=button], [role=button]");
    if (!el) return;
    flush();
    lastClick = Date.now();
    flash(el);
    send({kind: "click", target: target(el)});
  }, true);
  document.addEventListener("submit", (e) => {
    flush();
    if (Date.now() - lastClick < 1000 || !e.submitter) return;   // Enter key: record it as a click on the button
    send({kind: "click", target: target(e.submitter)});
  }, true);
  window.addEventListener("pagehide", flush);
  function scanRef() {
    const re = /\bSBX-[A-Z0-9]{6}\b/;
    const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const m = n.nodeValue && n.nodeValue.match(re);
      const el = n.parentElement;
      if (!m || !el || el.closest("a, td, #__flock_panel_host")) continue;   // a list row is not a fresh receipt
      if (text(el.innerText) !== m[0]) continue;
      send({kind: "reference", target: target(el), value: m[0]});
      return;
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scanRef); else scanRef();
})();
"""

PANEL_JS = r"""
window.__flockPanel = (shown) => {
  if (document.getElementById("__flock_panel_host")) return;
  const host = document.createElement("div");
  host.id = "__flock_panel_host";
  host.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:2147483647";
  const root = host.attachShadow({mode: "open"});
  const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const rows = shown.rows.map(([k, v]) => `<div class="r"><span class="k">${esc(k)}</span>
    <span class="v">${esc(v.length > 40 ? v.slice(0, 37) + "..." : v)}</span><button data-v="${esc(v)}">copy</button></div>`).join("");
  root.innerHTML = `<style>
    .p{font:13px system-ui,sans-serif;background:#111827;color:#f9fafb;border-radius:10px;padding:10px 12px;width:224px;
       box-shadow:0 6px 24px #0006}
    .h{font-weight:700;margin-bottom:6px}.s{color:#9ca3af;font-size:12px;margin-bottom:8px}
    .r{display:grid;grid-template-columns:62px 1fr auto;gap:6px;align-items:center;padding:3px 0;border-top:1px solid #374151}
    .k{color:#9ca3af}.v{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}
    button{background:#374151;color:#f9fafb;border:0;border-radius:5px;padding:2px 7px;cursor:pointer;font-size:11px}
    .rec{display:inline-block;width:8px;height:8px;border-radius:50%;background:#ef4444;margin-right:6px}</style>
    <div class="p"><div class="h"><span class="rec"></span>Clicky is watching: file this one dispute</div>
    <div class="s">Fill the portal form the way you normally would. This panel is not recorded.</div>${rows}</div>`;
  root.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    try { await navigator.clipboard.writeText(b.dataset.v); }
    catch (err) {
      const t = document.createElement("textarea"); t.value = b.dataset.v; root.appendChild(t); t.select();
      document.execCommand("copy"); t.remove();
    }
    b.textContent = "copied";
    setTimeout(() => (b.textContent = "copy"), 1200);
  });
  document.documentElement.appendChild(host);
};
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shown_rows(f: dict[str, Any]) -> list[list[str]]:
    v = workflow.finding_values(f)
    return [["Finding", v["finding_id"]], ["Channel", v["channel"]], ["Order IDs", v["order_ids"]],
            ["Amount", v["amount"]], ["Category", v["category"]], ["Message", v["dispute_draft"]]]


def record(finding: dict[str, Any], url: str, headed: bool = True,
           driver: Callable[[Any, Callable[[], None]], None] | None = None, root: Path | None = None,
           timeout_s: float = 900, linger_s: float = 2.0) -> dict[str, Any]:
    """Record one demonstration. Returns demo.json's content (also written to <root>/<demo_id>/demo.json).

    driver(page, tick), if given, plays the human (tests and rehearsals); it calls tick() after each action so the
    step's screenshot is taken right away. Without a driver a person does it in the headed browser."""
    from playwright.sync_api import sync_playwright

    from flock.agents.clicky import LOCAL_HOSTS, _launch, _portal_up, _start_portal

    if urlparse(url).hostname not in LOCAL_HOSTS:
        raise ValueError(f"Clicky only records the localhost sandbox portal, not {url}")
    url = url.rstrip("/")
    if not _portal_up(url):
        _start_portal()
    demo_id = "D-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    out = workflow.demo_dir(demo_id, root)
    out.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = []   # events still waiting for their screenshot
    state: dict[str, Any] = {"done": None, "last_action_at": 0.0, "ref_at": None, "layout": None}

    # Callbacks only record; they never call back into Playwright (a screenshot inside a binding call deadlocks).
    def on_event(source, ev: dict[str, Any]) -> None:
        if ev.get("kind") == "reference":
            if state["ref_at"]:
                return
            state["ref_at"] = time.monotonic()
        state["layout"] = ev.pop("layout", None) or state["layout"]
        ev = {k: v for k, v in ev.items() if v is not None or k == "value"}
        ev["seq"] = len(events) + 1
        events.append(ev)
        todo.append(ev)
        if ev["kind"] == "click":
            state["last_action_at"] = time.monotonic()

    def on_nav(frame) -> None:
        if frame.parent_frame is not None:
            return
        u = urlparse(frame.url)
        if u.scheme not in ("http", "https"):
            return
        last = events[-1] if events else None
        if last and last["kind"] == "click" and time.monotonic() - state["last_action_at"] < 5 \
                and "navigated_to" not in last:
            last["navigated_to"] = u.path   # caused by the human's click: part of that step, not a new one
        elif not last or last.get("kind") != "goto" or last.get("path") != u.path:
            events.append({"kind": "goto", "path": u.path, "seq": len(events) + 1, "at": int(time.time() * 1000)})

    def tick() -> None:
        while todo:
            ev = todo.pop(0)
            name = f"step-{ev['seq']:02d}.png"
            try:
                page.screenshot(path=str(out / name), timeout=5000)
                ev["screenshot"] = name
            except Exception:
                pass

    panel = PANEL_JS + ("\n(() => { const go = () => window.__flockPanel(%s);\n"
                        "  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);"
                        " else go(); })();" % json.dumps({"rows": shown_rows(finding)}))

    def new_context(browser, video: bool):
        size = {"width": 1280, "height": 860}
        opts = {"record_video_dir": str(out / "video"), "record_video_size": size} if video else {}
        context = browser.new_context(viewport=size, **opts)
        context.tracing.start(screenshots=True, snapshots=True)
        context.expose_binding("__flockRecord", on_event)
        context.add_init_script(RECORDER_JS)
        context.add_init_script(panel)
        return context, context.new_page()

    with sync_playwright() as pw:
        browser = _launch(pw, headed)
        try:
            context, page = new_context(browser, video=True)
        except Exception as e:  # no ffmpeg for the video: record steps, screenshots and trace anyway
            if "ffmpeg" not in str(e):
                raise
            print("[recorder] no video (run: python -m playwright install ffmpeg); steps and trace still recorded")
            context, page = new_context(browser, video=False)
        page.on("framenavigated", on_nav)
        page.on("close", lambda p: state.update(done=state["done"] or "closed"))
        page.goto(url + "/login")
        try:
            if driver is not None:
                driver(page, tick)
            deadline = time.monotonic() + timeout_s
            while not state["done"] and time.monotonic() < deadline:
                tick()
                if state["ref_at"] and time.monotonic() - state["ref_at"] >= linger_s:
                    state["done"] = "reference_seen"
                    break
                page.wait_for_timeout(150)
            tick()
        except Exception as e:  # the human closed the window
            if "closed" not in str(e).lower():
                raise
            state["done"] = state["done"] or "closed"
        try:
            context.tracing.stop(path=str(out / "trace.zip"))
        except Exception:
            pass
        video = page.video
        context.close()
        browser.close()
        try:
            src = Path(video.path()) if video else None
            if src and src.exists():
                shutil.move(str(src), out / "video.webm")
                shutil.rmtree(out / "video", ignore_errors=True)
        except Exception:
            pass

    demo = {
        "demo_id": demo_id, "recorded_at": _now(), "portal_url": url, "portal_layout": state["layout"],
        "finished": state["done"] or "timeout",
        "shown": {k: finding.get(k) for k in ("finding_id", "channel", "order_ids", "gap", "check_type",
                                                "dispute_draft", "expected", "actual")},
        "events": events,
        "video": "video.webm" if (out / "video.webm").exists() else None,
        "trace": "trace.zip" if (out / "trace.zip").exists() else None,
    }
    (out / "demo.json").write_text(json.dumps(demo, indent=2))
    return demo
