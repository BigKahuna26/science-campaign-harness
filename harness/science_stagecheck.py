#!/usr/bin/env python3
"""Stage gate checks over SCHEMA FIELDS, not prose.

WHY THIS EXISTS SEPARATELY FROM stagecheck.py
----------------------------------------------
`stagecheck.py` reads the host harness's `result.md` — prose — and every gate
failure in the 2026-10-01/02 campaign was a parsing problem wearing a science
problem's clothes:

  * a verdict written `NOT_EVALUABLE` slipped past `--absent "NOT EVALUABLE"`
    on one underscore;
  * a result reading "no biological control verdict is issued" SATISFIED
    `--mentions "CONTROL VERDICT"` and helped pass a gate it should have
    failed;
  * three patches landed on the same wound: `--absent`, then whitespace and
    underscore normalisation, then `--issues` for line-initial matching.

The Science harness writes `result.json` with a host-validated schema, so a
verdict is a field. This script asks `verdict.evaluable is true`, and there is
nothing left to spell wrong. `--mentions`, `--absent` and `--issues` have no
equivalent here on purpose: a gate that matches text is the bug class.

A gate is still a FACT ABOUT THE OUTPUT, not an opinion about the work.

Reads `analysis/campaigns/<id>/tasks/*/result.json` — the MIRRORED vault copy
written by `science_campaign_sync.py mirror`, never the workspace, so a gate
can be re-run later and get the same answer.

Usage::

    python3 analysis/scripts/harness/science_stagecheck.py --verdict CONTROL --evaluable
    python3 analysis/scripts/harness/science_stagecheck.py --verdict CONTROL \
        --outcome SUPPORTED MIXED
    python3 analysis/scripts/harness/science_stagecheck.py --min-tasks 10 --all-traced
    python3 analysis/scripts/harness/science_stagecheck.py --produced "*.csv" --min-tasks 2
    python3 analysis/scripts/harness/science_stagecheck.py --no-stop-condition

Exits 0 when every requested check passes, 1 otherwise, printing one line per
check. Chain them with `&&` exactly as the host briefs do.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parents[3]


def load_results(campaign: str, stage=None):
    cdir = VAULT / "analysis/campaigns" / campaign
    if not cdir.exists():
        sys.exit("no campaign directory at %s" % cdir)
    out = []
    for p in sorted(cdir.glob("tasks/*/result.json")):
        try:
            rec = json.loads(p.read_text())
        except Exception as exc:
            print("UNREADABLE %s: %s" % (p, exc))
            continue
        if stage is not None and rec.get("stage") != stage:
            continue
        rec["_path"] = p
        out.append(rec)
    return out


def accepted_ids(campaign: str):
    """Tasks a supervisor accepted. A result with no review has not been
    reviewed, and an unreviewed result is not evidence a stage is done."""
    cdir = VAULT / "analysis/campaigns" / campaign
    ids = set()
    for p in cdir.glob("tasks/*/review.json"):
        try:
            rec = json.loads(p.read_text())
        except Exception:
            continue
        if (rec.get("payload") or {}).get("decision") == "ACCEPT":
            ids.add(p.parent.name)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign", default=os.environ.get("HARNESS_CAMPAIGN", ""))
    ap.add_argument("--stage", type=int,
                    default=(int(os.environ["HARNESS_STAGE"])
                             if os.environ.get("HARNESS_STAGE") else None))
    ap.add_argument("--verdict", metavar="NAME",
                    help="a task carries this verdict name (e.g. CONTROL)")
    ap.add_argument("--evaluable", action="store_true",
                    help="...and verdict.evaluable is true")
    ap.add_argument("--outcome", nargs="+", metavar="OUTCOME",
                    help="...and verdict.outcome is one of these")
    ap.add_argument("--min-tasks", type=int, help="at least N accepted results")
    ap.add_argument("--produced", metavar="GLOB",
                    help="an accepted task produced a file matching this")
    ap.add_argument("--all-traced", action="store_true",
                    help="every finding's evidence_command is in that task's commands_run")
    ap.add_argument("--none-blocked", action="store_true",
                    help="no accepted task reported blocked=true")
    ap.add_argument("--no-stop-condition", action="store_true",
                    help="no verdict fired STOP_CONDITION_FIRED")
    ap.add_argument("--no-analyst-supplied", action="store_true",
                    help="no accepted result rests on data the worker supplied from its own "
                         "knowledge (citations, thresholds) rather than from a staged input")
    ap.add_argument("--gate-module", metavar="NAME",
                    help="apply a DOMAIN-SPECIFIC gate to the files accepted tasks "
                         "produced. NAME is an importable module on sys.path (add "
                         "yours with --gate-path) implementing the contract in "
                         "docs/GATE_PLUGIN.md: load_gate_constants() -> dict, "
                         "classify_csv(path) -> tuple|None, and check_records("
                         "records, constants) -> list[str] of violations. Prose is "
                         "deliberately not read: a claim backed by no table is "
                         "invisible here and must be caught by the supervisor.")
    ap.add_argument("--gate-path", metavar="DIR", action="append", default=[],
                    help="directory to add to sys.path before importing --gate-module; "
                         "repeatable")
    ap.add_argument("--accepted-only", action="store_true", default=True)
    args = ap.parse_args()

    if not args.campaign:
        sys.exit("--campaign (or HARNESS_CAMPAIGN) is required")

    results = load_results(args.campaign, args.stage)
    acc = accepted_ids(args.campaign)
    pool = [r for r in results if r.get("task_id") in acc] if args.accepted_only else results

    checks, failed = [], False

    def check(ok, label, detail=""):
        nonlocal failed
        checks.append("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                                    (" — " + detail) if detail else ""))
        if not ok:
            failed = True

    if args.min_tasks is not None:
        check(len(pool) >= args.min_tasks, "min-tasks %d" % args.min_tasks,
              "%d accepted results (of %d present)" % (len(pool), len(results)))

    if args.verdict:
        hits = [r for r in pool if ((r.get("payload") or {}).get("verdict") or {}).get("name")
                == args.verdict]
        check(bool(hits), "verdict %s present" % args.verdict,
              "%d accepted result(s)" % len(hits))
        if hits and args.evaluable:
            ev = [r for r in hits if (r["payload"]["verdict"] or {}).get("evaluable") is True]
            check(bool(ev), "verdict %s evaluable" % args.verdict,
                  "%d of %d evaluable" % (len(ev), len(hits)))
        if hits and args.outcome:
            want = set(args.outcome)
            got = [((r["payload"]["verdict"] or {}).get("outcome")) for r in hits]
            check(any(g in want for g in got),
                  "verdict %s outcome in %s" % (args.verdict, sorted(want)),
                  "observed %s" % got)

    if args.produced:
        hit = None
        for r in pool:
            files_dir = Path(r["_path"]).parent / "files"
            for f in (files_dir.iterdir() if files_dir.exists() else []):
                if fnmatch.fnmatch(f.name, args.produced):
                    hit = "%s/%s" % (r.get("task_id"), f.name)
                    break
            if hit:
                break
        check(hit is not None, "produced %s" % args.produced, hit or "no match")

    if args.all_traced:
        bad = []
        for r in pool:
            p = r.get("payload") or {}
            cmds = set(p.get("commands_run") or [])
            for i, f in enumerate(p.get("findings") or []):
                if f.get("evidence_command") not in cmds:
                    bad.append("%s findings[%d]" % (r.get("task_id"), i))
        check(not bad, "all findings traced to a command", "untraced: %s" % bad[:5])

    if args.none_blocked:
        blocked = [r.get("task_id") for r in pool if (r.get("payload") or {}).get("blocked")]
        check(not blocked, "none blocked", "blocked: %s" % blocked)

    if args.no_stop_condition:
        fired = [r.get("task_id") for r in pool
                 if ((r.get("payload") or {}).get("verdict") or {}).get("outcome")
                 == "STOP_CONDITION_FIRED"]
        check(not fired, "no stop condition fired", "fired: %s" % fired)

    if args.no_analyst_supplied:
        # --all-traced proves a number came from a command. It cannot see that
        # the command read a table the worker typed from memory, because that
        # table is a literal inside the script. This is the check that can.
        bad = []
        for r in pool:
            for a in (r.get("payload") or {}).get("analyst_supplied") or []:
                bad.append("%s: %s" % (r.get("task_id"), (a.get("what") or "")[:60]))
        check(not bad, "no analyst-supplied inputs", "; ".join(bad[:4]))

    if args.gate_module:
        # Domain gates are plugins: the harness supplies the traversal (which
        # tasks are in the pool, which files they produced, how violations are
        # reported) and the module supplies the rule. Nothing about the rule is
        # hardcoded here, so a gate for one field does not have to be deleted
        # before the harness can be used in another.
        import csv as _csv
        for d in args.gate_path:
            sys.path.insert(0, str(Path(d).expanduser().resolve()))
        try:
            dg = __import__(args.gate_module)
        except Exception as exc:
            check(False, "gate module %r importable" % args.gate_module,
                  "%s: %s — add its directory with --gate-path"
                  % (type(exc).__name__, exc))
        else:
            missing = [fn for fn in ("load_gate_constants", "classify_csv",
                                     "check_records")
                       if not callable(getattr(dg, fn, None))]
            if missing:
                check(False, "gate module implements the contract",
                      "%s lacks: %s (see docs/GATE_PLUGIN.md)"
                      % (args.gate_module, ", ".join(missing)))
            else:
                const = dg.load_gate_constants()
                bad, n_rec, n_tab = [], 0, 0
                for r in pool:
                    fdir = Path(r["_path"]).parent / "files"
                    for f in sorted(fdir.rglob("*")) if fdir.exists() else []:
                        if f.suffix != ".csv":
                            continue
                        cl = dg.classify_csv(f)
                        if not cl:
                            continue
                        n_tab += 1
                        with open(f, newline="", encoding="utf-8",
                                  errors="replace") as fh:
                            recs = list(_csv.DictReader(fh))
                        n_rec += len(recs)
                        for v in dg.check_records(recs, const) or []:
                            bad.append("%s/%s: %s" % (r.get("task_id"), f.name, v))
                check(not bad, "domain gate %r" % args.gate_module,
                      ("%d violation(s): %s" % (len(bad), bad[:3])) if bad else
                      "%d record(s) in %d table(s) passed" % (n_rec, n_tab))

    if not checks:
        sys.exit("no checks requested — give at least one")
    print("\n".join(checks))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
