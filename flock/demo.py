"""Live seed for the demo: a judge picks a number, Flock generates a fresh dataset from it and runs everything.

  python -m flock.demo --seed 4821                                   # default company, 12 stores x 2 weeks
  python -m flock.demo --seed 4821 --profile ca_bagel_chain --weeks 1
  python -m flock.demo --seed 4821 --keep                            # leave the run on the dashboard afterwards
  python -m flock.demo --seed 4821 --confirm                         # accept the unseen layout without asking

What it does, in order (one progress bar the whole way, also on the dashboard's live board):
  1. generate   generator/generate.py into a temp folder (never generator/out, never data/answer_key.csv)
  2. collect    into a temp data/raw
  3. pipeline   format (temp copy of mappings.json, so the committed one is never touched; the unseen layout
                still asks a human y/N unless --confirm) -> match -> deal terms -> check -> judge -> rank -> draft
                -> demo human approves/rejects -> Clicky files in the sandbox portal -> learner proposes rules.
                Findings go to the real store (data/state) so the dashboard shows them live.
  4. score      against the answer key generated in step 1
  5. restore    data/state and the sandbox portal go back to what they were before (the fixtures demo),
                unless --keep. With --keep the generated key is left at data/state/answer_key.csv so the
                dashboard's scoreboard matches; the next `pipeline --reset` puts the fixtures key back.
Learned rules (rules.json) are read from a temp copy, never written. --forget starts with no learned rules.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from flock import pipeline
from flock.agents import clicky, collector, learner
from flock.progress import Progress, progress_path
from flock.store import ROOT, STATE_DIR, Store, get_store


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


generator = _load("flock_generator", ROOT / "generator" / "generate.py")
scoreboard = _load("flock_scoreboard", ROOT / "scripts" / "score.py")


class Snapshot:
    """Copies of the state folder and the sandbox portal, put back exactly (files that didn't exist are removed)."""

    def __init__(self, state_dir: Path, portal: Path, into: Path):
        self.state_dir, self.portal, self.into = Path(state_dir), Path(portal), Path(into)
        self.had_state = self.state_dir.exists()
        self.had_portal = self.portal.exists()
        if self.had_state:
            shutil.copytree(self.state_dir, self.into / "state")
        if self.had_portal:
            (self.into / "portal").mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.portal, self.into / "portal" / self.portal.name)

    def restore(self, keep: tuple[str, ...] = ()) -> None:
        kept = {n: (self.state_dir / n).read_bytes() for n in keep if (self.state_dir / n).exists()}
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        if self.had_state:
            shutil.copytree(self.into / "state", self.state_dir)
        for n, data in kept.items():
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / n).write_bytes(data)
        self.portal.unlink(missing_ok=True)
        if self.had_portal:
            self.portal.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.into / "portal" / self.portal.name, self.portal)


def run_demo(seed: int, profile: str = generator.DEFAULT_PROFILE, weeks: int = 2, days: int | None = None,
             stores: int | None = None, trap_rate: float | None = None, keep: bool = False, confirm: bool = False,
             forget: bool = False, approve: bool = True, pace: float = 0.0, store: Store | None = None,
             portal: Path | None = None, workdir: Path | None = None, interactive: bool = True) -> dict[str, Any]:
    """Generate -> collect -> pipeline -> score on a fresh seed; restore the previous demo state unless keep.
    store / portal default to the real ones (data/state, sandbox portal); tests pass temp ones."""
    store = store or get_store()
    state_dir = Path(getattr(store, "dir", STATE_DIR))
    portal = Path(portal or clicky.PORTAL)
    prof = generator.load_profile(profile)
    days = days or weeks * 7
    n_stores = stores or prof["default_stores"]
    ws = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="flock_demo_"))
    ws.mkdir(parents=True, exist_ok=True)
    out, raw, std = ws / "out", ws / "raw", ws / "std"
    mappings = ws / "mappings_temp_copy.json"
    shutil.copy(ROOT / "mappings.json", mappings)           # the committed file is only ever read
    rules = ws / "rules.json"
    if learner.RULES.exists() and not forget:
        shutil.copy(learner.RULES, rules)                    # learned rules: read from a copy, never written
    shutil.rmtree(ws / "backup", ignore_errors=True)
    snap = Snapshot(state_dir, portal, ws / "backup")
    runs = state_dir / "runs.jsonl" if keep else ws / "runs.jsonl"   # week-by-week history only if kept
    label = f"live demo: seed {seed}, {prof['name']}, {n_stores} stores x {days} days"
    progress = Progress(progress_path(store), run=label)
    t0 = time.monotonic()
    print(f"== {label} ==", flush=True)
    print(f"   {prof['description']}", flush=True)
    try:
        world = generator.generate(seed, n_stores, days, None, out, prof, trap_rate, progress)
        planted = sorted({r["trap_no"] for r in world["key"]})
        print(f"generated {len(world['orders']):,} orders, {len(world['deposits'])} bank deposits, "
              f"{len(world['emails'])} emails; {len(world['key'])} answer key rows covering traps {planted}")
        progress.stage("collect", total=1, unit="steps")
        collector.collect(out, raw)
        store_obj = pipeline.run(reset=True, demo_approve=approve, pace=pace, store=store, rules_path=rules,
                                 portal=portal, raw=raw, std=std, mappings=mappings, confirm=confirm,
                                 interactive=interactive, progress=progress, runs_path=runs,
                                 label=label)
        print(f"\n== Scoreboard vs the freshly generated answer key ({len(world['key'])} rows) ==")
        result = scoreboard.score(store_obj, runs_path=runs, answer_key=out / "answer_key.csv")
        result.update(seed=seed, profile=prof["name"], orders=len(world["orders"]),
                      seconds=round(time.monotonic() - t0, 1), workdir=str(ws))
        if keep:
            shutil.copy(out / "answer_key.csv", state_dir / pipeline.LIVE_KEY)
            print(f"\nkept: the dashboard shows this run (scoreboard uses its own answer key). "
                  f"Back to the fixtures demo: python -m flock.pipeline --reset --forget --demo-approve")
        print(f"done in {result['seconds']}s")
        return result
    finally:
        if not keep:
            snap.restore(keep=("progress.json",))
            print("restored the previous demo state (data/state, sandbox portal); --keep leaves this run instead")
        if not workdir:
            shutil.rmtree(ws, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Live demo: fresh data from a judge's seed, full Flock run, score.")
    ap.add_argument("--seed", type=int, required=True, help="any number; same seed = same data")
    ap.add_argument("--profile", default=generator.DEFAULT_PROFILE,
                    help=f"company profile: {', '.join(generator.list_profiles())}")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--days", type=int, default=None, help="overrides --weeks")
    ap.add_argument("--stores", type=int, default=None, help="default: the profile's store count")
    ap.add_argument("--trap-rate", type=float, default=None, help="instances of each trap per 100 store-weeks")
    ap.add_argument("--keep", action="store_true", help="leave this run in data/state for the dashboard")
    ap.add_argument("--confirm", action="store_true", help="accept the unseen layout's proposed mapping without asking")
    ap.add_argument("--forget", action="store_true", help="start with no learned rules")
    ap.add_argument("--no-approve", action="store_true", help="leave conflicts and drafts for a human on the dashboard")
    ap.add_argument("--pace", type=float, default=0.0, help="seconds per move, so the live board visibly ticks")
    ap.add_argument("--workdir", help="keep the generated files here instead of a temp folder")
    a = ap.parse_args(argv)
    r = run_demo(a.seed, a.profile, a.weeks, a.days, a.stores, a.trap_rate, keep=a.keep, confirm=a.confirm,
                 forget=a.forget, approve=not a.no_approve, pace=a.pace,
                 workdir=Path(a.workdir) if a.workdir else None)
    sys.exit(0 if r["total"] else 1)


if __name__ == "__main__":
    main()
