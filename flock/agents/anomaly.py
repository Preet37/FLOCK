"""Anomaly Detector (owner: Thabhelo). Learns what normal looks like for each store and app, then flags jumps no rule covers.

The checkers compare a payout to a deal term. When there is no term (the direct app's processor charges a percentage plus a
fixed amount, so the Deal Keeper emits no flat fee to compare; a platform whose contract never mentions a charge), nothing
watches that money. This agent does: for each store x channel it fits the normal relationship from earlier weeks, in plain
Python, and raises a finding when a later week breaks it.

IN:  order stories (matcher) + terms_for (deal_keeper.TermIndex), the same inputs the checkers get
OUT: findings with check_type "anomaly", state NEW, through the normal panel like any other finding

What it learns, per (store, channel), from the weeks before the one under test ("history"):
  fee         other_fees ~ a x gross + b   (least squares; the processor's percent-plus-fixed shape falls out of the fit)
  commission  commission ~ r x gross
  lag         payout_date - placed_at, in days (timing)
A metric is only watched when no deal term governs it: a "fee" term means the Checkers own fees for that story, a
"commission" term means they own commission. Timing has no term, so it is always watched.

How a week is judged: baseline = all earlier, unflagged weeks with at least MIN_HISTORY orders. The week needs MIN_WEEK
orders. Its median residual against the fitted baseline must exceed max(FLOOR, K x MAD of the baseline residuals), so a
real jump is needed, not noise. The orders that carry the jump become one finding: expected = what the baseline predicts,
actual = what was charged, gap = actual - expected (positive = the store is owed). A flagged week stays out of later
baselines, so a persistent change is reported every week until a human settles it (and the Learner can turn two of
those decisions into a rule).

No model touches money. Every number is flock.models.money(); every evidence row carries source_file + row.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

from flock.models import Finding, money

CHECK_TYPE = "anomaly"        # proposed addition to CONTRACT.md check_type
MIN_HISTORY = 15              # orders in the baseline before we trust it
MIN_WEEK = 5                  # orders in the week under test
K = 4.0                       # MADs above baseline noise
FLOOR = {"fee": 0.05, "commission": 0.05, "lag": 2.0}   # dollars, dollars, days: below this nobody cares
TERM_FOR = {"fee": "fee", "commission": "commission"}   # metric -> the deal term that would make it the Checkers' job


# ---- metrics ---------------------------------------------------------------------------------------------------------
def _ts(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None) if s else None
    except ValueError:
        return None


def _line(story: dict[str, Any]) -> dict[str, Any] | None:
    lines = [ln for ln in story.get("payout_lines", []) if ln.get("gross")]
    return max(lines, key=lambda ln: ln["gross"]) if lines else None


def _xy(story: dict[str, Any], metric: str) -> tuple[float, float] | None:
    """(x, y) for a story: gross -> fees, gross -> commission, or 0 -> lag days."""
    ln = _line(story)
    if ln is None:
        return None
    if metric == "fee":
        return float(ln["gross"]), float(ln.get("other_fees") or 0.0)
    if metric == "commission":
        return float(ln["gross"]), float(ln.get("commission") or 0.0)
    placed, paid = _ts(story.get("placed_at")), _ts(ln.get("payout_date"))
    if not placed or not paid:
        return None
    return 0.0, (paid - placed).total_seconds() / 86400


def _fit(points: list[tuple[float, float]], metric: str) -> tuple[float, float]:
    """y ~ a x + b (fee), y ~ r x (commission), y ~ b (lag). Plain least squares."""
    n = len(points)
    if metric == "commission":
        sxx = sum(x * x for x, _ in points)
        return (sum(x * y for x, y in points) / sxx if sxx else 0.0), 0.0
    if metric == "lag":
        return 0.0, _median([y for _, y in points])
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    a = sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else 0.0
    return a, my - a * mx


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _mad(xs: list[float]) -> float:
    m = _median(xs)
    return _median([abs(x - m) for x in xs])


def _predict(fit: tuple[float, float], x: float) -> float:
    return fit[0] * x + fit[1]


# ---- the detector ----------------------------------------------------------------------------------------------------
def _monday(d: datetime) -> datetime:
    """Weeks are calendar weeks starting on the Monday of the first order, like the platforms' payout periods."""
    return datetime.combine(d.date(), datetime.min.time()) - timedelta(days=d.weekday())


def _week(story: dict[str, Any], start: datetime) -> int:
    return ((_ts(story["placed_at"]) or start) - start).days // 7


def detect(stories: list[dict[str, Any]], terms_for: Callable[[dict[str, Any]], dict[str, Any]]) -> list[Finding]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for s in stories:
        if s.get("kind", "order") == "order" and s.get("payout_lines") and _ts(s.get("placed_at")):
            groups[(s.get("store_id", "all"), s["channel"])].append(s)
    out: list[Finding] = []
    for (store, channel), group in sorted(groups.items()):
        for metric in ("fee", "commission", "lag"):
            out += _watch(group, metric, terms_for, store, channel)
    return out


def _watch(group: list[dict[str, Any]], metric: str, terms_for, store: str, channel: str) -> list[Finding]:
    governed = TERM_FOR.get(metric)
    rows = []
    start = _monday(min(_ts(s["placed_at"]) for s in group))
    for s in group:
        if governed and governed in (terms_for(s) or {}):
            continue  # a deal term covers this metric here: the Checkers' job, not ours
        xy = _xy(s, metric)
        if xy:
            rows.append((_week(s, start), s, xy))
    if not rows:
        return []
    by_week: dict[int, list] = defaultdict(list)
    for w, s, xy in rows:
        by_week[w].append((s, xy))
    found: list[Finding] = []
    baseline: list[tuple[dict[str, Any], tuple[float, float]]] = []
    for w in sorted(by_week):
        week = by_week[w]
        if len(baseline) >= MIN_HISTORY and len(week) >= MIN_WEEK:
            fit = _fit([xy for _, xy in baseline], metric)
            noise = _mad([y - _predict(fit, x) for _, (x, y) in baseline])
            threshold = max(FLOOR[metric], K * noise)
            resid = [(s, xy, xy[1] - _predict(fit, xy[0])) for s, xy in week]
            med = _median([r for _, _, r in resid])
            if med > threshold:
                found.append(_finding(metric, store, channel, w, fit, noise, threshold, len(baseline),
                                      [(s, xy, r) for s, xy, r in resid if r > threshold / 2], med))
                continue  # a flagged week never joins the baseline
        baseline += week
    return found


def _finding(metric: str, store: str, channel: str, week: int, fit: tuple[float, float], noise: float,
             threshold: float, n_hist: int, hits: list, med: float) -> Finding:
    stories = [s for s, _, _ in hits]
    unit = "days" if metric == "lag" else "$"
    if metric == "lag":
        expected, actual, gap = money(fit[1]), money(fit[1] + med), 0.0  # timing: nothing to recover, a human looks
    else:
        expected = money(sum(_predict(fit, x) for _, (x, _y), _ in hits))
        actual = money(sum(y for _, (_x, y), _ in hits))
        gap = money(actual - expected)
    first = _line(stories[0])
    sample = _line(stories[min(1, len(stories) - 1)])
    normal = (f"{fit[0] * 100:.2f}% of gross + ${fit[1]:.2f}" if metric == "fee"
              else f"{fit[0] * 100:.2f}% of gross" if metric == "commission" else f"{fit[1]:.1f} days to payout")
    label = {"fee": "processing and other fees", "commission": "commission", "lag": "payout timing"}[metric]
    evidence = [
        {"source_file": first["source_file"], "row": first["source_row"],
         "what": f"baseline from {n_hist} earlier orders at {store} on {channel}: {label} normally {normal} "
                 f"(noise {noise:.3f} {unit}, threshold {threshold:.2f} {unit}); learned from history, no deal term covers it"},
        {"source_file": first["source_file"], "row": first["source_row"],
         "what": f"week {week + 1}: {len(stories)} orders charged {label} {med:.2f} {unit} above normal each"},
    ]
    if sample is not first:
        evidence.append({"source_file": sample["source_file"], "row": sample["source_row"],
                         "what": f"another order in the same week, {label} above normal"})
    reason = (f"{label.capitalize()} at {store} on {channel} jumped in week {week + 1}: {len(stories)} orders ran "
              f"{med:.2f} {unit} above the pattern learned from {n_hist} earlier orders ({normal}). "
              f"No contract term covers this charge, so no rule caught it; the history did.")
    return Finding(order_ids=[s["order_id"] for s in stories], channel=channel, check_type=CHECK_TYPE,
                   expected=expected, actual=actual, gap=gap, evidence=evidence, reason=reason)
