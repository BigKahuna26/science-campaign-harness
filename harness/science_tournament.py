#!/usr/bin/env python3
"""Rank campaign reports by a pairwise-comparison tournament.

Ports the ranking the paper used (Methods p.30-31) to Claude Science:

    Every ordered pair of filed reports is compared once. In each game a
    language-model judge reads a fixed rubric and two reports under anonymized
    names, scores each from 1 to 5 for impact, novelty, soundness, and
    actionability (weights 0.35, 0.30, 0.25, 0.10). The report with the higher
    weighted score wins, and a report with a soundness score of 2 or less loses
    automatically. Ratings are fitted to all games with a Bradley-Terry model.

Two properties worth stating:

  * **Every ordered pair, both directions.** N reports give N*(N-1) games, so
    each unordered pair is judged as (A,B) and as (B,A). That cancels the
    position bias a single-direction comparison would bake in.
  * **The judge is a host.llm call, not a delegated agent.** It reads two
    reports and scores them; it needs no workspace. Games are shipped as one
    batched host.llm list call so they run concurrently.

Pure Python (no numpy/scipy), so it runs in either kernel. The Bradley-Terry
fit is the standard minorization-maximization iteration.

Usage (as a library, from the python kernel which can read the reports)::

    import science_tournament as T
    ranking = T.run_tournament(host, [{"id": "r001", "text": "..."}, ...])

Or over a mirrored campaign's filed reports::

    python3 analysis/scripts/harness/science_tournament.py --campaign <id>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

VAULT = Path(__file__).resolve().parents[3]

AXES = ("impact", "novelty", "soundness", "actionability")
WEIGHTS = {"impact": 0.35, "novelty": 0.30, "soundness": 0.25, "actionability": 0.10}
SOUNDNESS_FLOOR = 2  # a report scoring <= this on soundness loses automatically

RUBRIC = (
    "Score each report from 1 (poor) to 5 (excellent) on four axes:\n"
    "- impact: how much the finding would matter if correct.\n"
    "- novelty: how new it is against what is already known.\n"
    "- soundness: how well the evidence actually supports the claims — the load-bearing axis.\n"
    "- actionability: how clearly it points to a concrete next step.\n"
    "Judge the two reports against each other on the same scale. Be willing to use the full range; "
    "do not cluster everything at 3-4. Soundness is about whether the report's own evidence supports "
    "its own claims, independent of how exciting the claim is."
)

_JUDGE_SYSTEM = (
    "You are a judge in a tournament that ranks the reports of an autonomous research campaign. You "
    "read a fixed rubric and two anonymized reports, and you score each report on the four rubric "
    "axes. Score the reports relative to each other. You are not asked to pick a winner — just to "
    "score each axis honestly; the harness computes the winner from your scores."
)

JUDGE_TOOL = {
    "name": "score_pair",
    "description": "Score two reports on the four rubric axes, each 1-5.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["report_A", "report_B"],
        "properties": {
            "report_A": {
                "type": "object", "additionalProperties": False, "required": list(AXES),
                "properties": {a: {"type": "integer", "minimum": 1, "maximum": 5} for a in AXES},
            },
            "report_B": {
                "type": "object", "additionalProperties": False, "required": list(AXES),
                "properties": {a: {"type": "integer", "minimum": 1, "maximum": 5} for a in AXES},
            },
        },
    },
}


# --- pure functions: testable without any LLM -------------------------------

def weighted_score(scores):
    """Weighted 1-5 rubric score. `scores` is {axis: int}."""
    return sum(WEIGHTS[a] * float(scores[a]) for a in AXES)


def game_winner(scores_a, scores_b):
    """'A', 'B', or 'tie' for one game, applying the soundness auto-loss first.

    The auto-loss is the rule that keeps an exciting-but-unsound report from
    winning on impact alone: soundness <= 2 loses regardless of weighted score.
    If both are unsound, the higher weighted score breaks it (both are sinking
    anyway, and BT needs an outcome).
    """
    a_dq = scores_a["soundness"] <= SOUNDNESS_FLOOR
    b_dq = scores_b["soundness"] <= SOUNDNESS_FLOOR
    if a_dq and not b_dq:
        return "B"
    if b_dq and not a_dq:
        return "A"
    wa, wb = weighted_score(scores_a), weighted_score(scores_b)
    if wa > wb:
        return "A"
    if wb > wa:
        return "B"
    return "tie"


def bradley_terry(players, wins, iters=200, tol=1e-9):
    """Fit Bradley-Terry strengths by minorization-maximization.

    `players` is a list of ids. `wins[(i, j)]` is the win-credit i earned
    against j (1 per win, 0.5 per tie), summed over all games between them.
    Returns {id: strength}, normalized to sum to len(players).
    """
    p = {x: 1.0 for x in players}
    # total games between each unordered pair, and each player's win credit
    pair_games = {}
    win_credit = {x: 0.0 for x in players}
    for (i, j), c in wins.items():
        win_credit[i] += c
        key = frozenset((i, j))
        pair_games[key] = pair_games.get(key, 0.0) + c
    for _ in range(iters):
        newp = {}
        for i in players:
            denom = 0.0
            for j in players:
                if j == i:
                    continue
                nij = pair_games.get(frozenset((i, j)), 0.0)
                if nij:
                    denom += nij / (p[i] + p[j])
            newp[i] = (win_credit[i] / denom) if denom > 0 else p[i]
        s = sum(newp.values()) or 1.0
        scale = len(players) / s
        newp = {k: v * scale for k, v in newp.items()}
        if max(abs(newp[k] - p[k]) for k in players) < tol:
            p = newp
            break
        p = newp
    return p


def rank_from_games(players, games):
    """games: list of {A, B, winner in {'A','B','tie'}}. Returns ranked list."""
    wins = {}
    wl = {x: {"wins": 0.0, "games": 0} for x in players}
    for g in games:
        a, b, w = g["A"], g["B"], g["winner"]
        wl[a]["games"] += 1
        wl[b]["games"] += 1
        if w == "A":
            wins[(a, b)] = wins.get((a, b), 0.0) + 1.0
            wl[a]["wins"] += 1.0
        elif w == "B":
            wins[(b, a)] = wins.get((b, a), 0.0) + 1.0
            wl[b]["wins"] += 1.0
        else:
            wins[(a, b)] = wins.get((a, b), 0.0) + 0.5
            wins[(b, a)] = wins.get((b, a), 0.0) + 0.5
            wl[a]["wins"] += 0.5
            wl[b]["wins"] += 0.5
    strengths = bradley_terry(players, wins)
    ranked = sorted(players, key=lambda x: strengths[x], reverse=True)
    return [{"id": x, "bt_strength": round(strengths[x], 4),
             "wins": wl[x]["wins"], "games": wl[x]["games"]} for x in ranked]


# --- the judge (uses host.llm) ----------------------------------------------

def _judge_prompt(report_a, report_b):
    return ("%s\n\nREPORT A:\n%s\n\nREPORT B:\n%s\n\nScore both reports on the four axes."
            % (RUBRIC, report_a[:8000], report_b[:8000]))


def run_tournament(host, reports, model=None, max_concurrency=8):
    """Rank `reports` ([{id, text}, ...]). Returns a ranking dict.

    Every ordered pair is one game, shipped as one batched host.llm list call
    (concurrent). A game whose judge call fails is dropped with a note rather
    than failing the tournament.
    """
    reports = [r for r in reports if (r.get("text") or "").strip()]
    if len(reports) < 2:
        return {"ranking": [{"id": r["id"], "bt_strength": 1.0, "wins": 0, "games": 0}
                            for r in reports],
                "n_reports": len(reports), "n_games": 0,
                "note": "fewer than 2 reports with text — nothing to compare"}
    idx = {r["id"]: r for r in reports}
    pairs = [(a["id"], b["id"]) for a in reports for b in reports if a["id"] != b["id"]]
    requests = [{"prompt": _judge_prompt(idx[a]["text"], idx[b]["text"]),
                 "system": _JUDGE_SYSTEM, "tools": [JUDGE_TOOL],
                 "tool_choice": {"type": "tool", "name": "score_pair"},
                 "model": model, "max_tokens": 500} for (a, b) in pairs]
    results = host.llm(requests, max_concurrency=max_concurrency)
    games, dropped = [], 0
    for (a, b), r in zip(pairs, results):
        tu = r.get("tool_use") if isinstance(r, dict) else None
        inp = tu.get("input") if isinstance(tu, dict) else None
        if not isinstance(inp, dict) or "report_A" not in inp or "report_B" not in inp:
            dropped += 1
            continue
        try:
            w = game_winner(inp["report_A"], inp["report_B"])
        except (KeyError, TypeError):
            dropped += 1
            continue
        games.append({"A": a, "B": b, "winner": w,
                      "scores_A": inp["report_A"], "scores_B": inp["report_B"]})
    ranking = rank_from_games([r["id"] for r in reports], games)
    return {"ranking": ranking, "n_reports": len(reports), "n_games": len(games),
            "games_dropped": dropped, "games": games}


# --- CLI: rank a mirrored campaign's filed reports --------------------------

def _load_filed_reports(campaign):
    """Read a campaign's filed reports from the mirrored vault tree.

    A filed report's text is taken from its report artifact if present in
    tasks/<id>/files/, else from the result.json summary + findings.
    """
    cdir = VAULT / "analysis/campaigns" / campaign
    rep_path = None
    for p in sorted(cdir.glob("report_stage*.json")):
        rep_path = p  # latest stage report
    filed = []
    if rep_path:
        rec = json.loads(rep_path.read_text())
        filed = rec.get("filed_reports") or []
    out = []
    for fr in filed:
        tid = fr["task_id"]
        tdir = cdir / "tasks" / tid
        text = ""
        fdir = tdir / "files"
        if fdir.exists():
            for f in sorted(fdir.glob("*.md")):
                text += f.read_text()
        if not text and (tdir / "result.json").exists():
            pay = json.loads((tdir / "result.json").read_text()).get("payload", {})
            text = (pay.get("summary", "") + "\n\n"
                    + "\n".join("- %s" % (f.get("claim", "")) for f in pay.get("findings", [])))
        out.append({"id": tid, "text": text})
    return out, cdir


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--out", help="where to write ranking.json (default: campaign dir)")
    args = ap.parse_args()
    try:
        import host  # provided in-kernel; CLI is a convenience only
    except Exception:
        raise SystemExit("run_tournament needs the in-kernel host object; import this module "
                         "and call run_tournament(host, reports) from a python/repl cell instead")
    reports, cdir = _load_filed_reports(args.campaign)
    result = run_tournament(host, reports)
    out = Path(args.out) if args.out else (cdir / "ranking.json")
    out.write_text(json.dumps(result, indent=1))
    print(json.dumps({"n_reports": result["n_reports"], "n_games": result["n_games"],
                      "ranking": result["ranking"]}, indent=1))


if __name__ == "__main__":
    main()
