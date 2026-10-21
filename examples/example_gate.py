#!/usr/bin/env python3
"""example_gate.py -- a worked domain gate for science_stagecheck.py.

The rule: any table reporting an effect size per group must carry that group's
N, and any effect computed from a group below `min_n` must be marked
"not assessable at this size" rather than reported as a null result. This is
the shape of a real power-precondition gate, with neutral column names.

Try it:
    python3 harness/science_stagecheck.py --campaign <id> \
        --gate-module example_gate --gate-path examples/
"""
from __future__ import annotations
import csv, json, os

NOT_ASSESSABLE = "not assessable at this size"


def load_gate_constants() -> dict:
    """Prefer a staged audit file; fall back to documented defaults.

    Reading from the audit is the point: a gate carrying its own literals can
    disagree with the analysis it is supposed to be gating.
    """
    path = os.environ.get("GATE_CONSTANTS")
    if path and os.path.exists(path):
        with open(path) as fh:
            d = json.load(fh)
        return {"effect_threshold": float(d["effect_threshold"]),
                "min_n": int(d["min_n"]), "source": path}
    return {"effect_threshold": 0.10, "min_n": 50, "source": "defaults"}


def classify_csv(path):
    """In scope if the header has a group column, an N column and an effect."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            header = next(csv.reader(fh), [])
    except OSError:
        return None
    low = [h.strip().lower() for h in header]
    gcol = next((h for h in low if h in ("group", "cluster", "bin")), None)
    ncol = next((h for h in low if h in ("n", "n_cells", "n_records", "size")), None)
    ecol = next((h for h in low if "effect" in h or "eta2" in h), None)
    return ("per_group", gcol, ncol, ecol) if gcol and ecol else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_records(records: list[dict], constants: dict) -> list[str]:
    if not records:
        return []
    low = {k.strip().lower(): k for k in records[0]}
    gcol = next((low[h] for h in ("group", "cluster", "bin") if h in low), None)
    ncol = next((low[h] for h in ("n", "n_cells", "n_records", "size") if h in low), None)
    ecol = next((low[k] for k in low if "effect" in k or "eta2" in k), None)
    out = []
    for row in records:
        eff = _num(row.get(ecol)) if ecol else None
        if eff is None:
            continue
        g = row.get(gcol, "?")
        if ncol is None:
            out.append("group %s: effect reported with no N column" % g)
            continue
        n = _num(row.get(ncol))
        if n is None:
            out.append("group %s: N missing" % g)
        elif n < constants["min_n"] and NOT_ASSESSABLE not in \
                " ".join(str(v).lower() for v in row.values()):
            out.append("group %s: N=%d below min_n=%d but not marked %r"
                       % (g, n, constants["min_n"], NOT_ASSESSABLE))
    return out
