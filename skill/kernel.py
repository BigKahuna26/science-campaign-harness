"""python-kernel helpers for the science-campaign skill.

These run in the `python` kernel (host.llm + file I/O). The campaign LOOP runs
in the `repl` kernel from the staged module — see SKILL.md. Every helper locates
the engine in the harness repo and raises a clear error if it is not reachable.
"""

HARNESS_DIR = os.environ.get(
    "SCIENCE_HARNESS_DIR",
    os.path.join(os.path.expanduser("~"), "science-campaign-harness",
                 "harness"),
)


def harness_dir():
    """Absolute path to the vault's harness scripts, or a clear error."""
    import os
    if not os.path.isdir(HARNESS_DIR):
        raise RuntimeError(
            "science-campaign: engine not found at %s — this skill expects the "
            "repo mounted. Set SCIENCE_HARNESS_DIR, or edit HARNESS_DIR in this file."
            % HARNESS_DIR)
    return HARNESS_DIR


def load_engine(module_name):
    """Import a harness module (e.g. 'science_tournament', 'science_campaign') from the vault."""
    import sys
    import importlib
    d = harness_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(module_name)


def stage_campaign(campaign_id, workspace=None):
    """Copy the loop module into the workspace and record its hash. Returns the
    staged record (source path, sha256). Run before importing science_campaign
    in the repl kernel."""
    import os
    import subprocess
    import json
    ws = workspace or os.getcwd()
    sync = os.path.join(harness_dir(), "science_campaign_sync.py")
    r = subprocess.run(["python3", sync, "stage", "--campaign", campaign_id, "--workspace", ws],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("stage failed: %s" % (r.stderr or r.stdout))
    return json.loads(r.stdout)


def mirror_campaign(host, campaign_id, workspace=None):
    """Mirror a workspace campaign into the vault, resolving report/table
    artifacts into tasks/<id>/files/. Returns the mirror record."""
    import os
    import glob
    import json
    import subprocess
    ws = workspace or os.getcwd()
    cd = os.path.join(ws, "campaigns", campaign_id)
    amap = {}
    for p in glob.glob(os.path.join(cd, "tasks", "*", "result.json")):
        try:
            payload = json.load(open(p)).get("payload", {})
        except Exception:
            continue
        for a in payload.get("artifacts") or []:
            vid = a.get("version_id")
            if vid:
                try:
                    amap[vid] = host.artifact_path(vid)
                except Exception:
                    pass
    mapf = os.path.join(ws, "handoff_amap_%s.json" % campaign_id)
    os.makedirs(os.path.dirname(mapf) or ".", exist_ok=True)
    json.dump(amap, open(mapf, "w"))
    sync = os.path.join(harness_dir(), "science_campaign_sync.py")
    r = subprocess.run(["python3", sync, "mirror", "--campaign", campaign_id,
                        "--workspace", ws, "--artifact-map", mapf],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("mirror failed: %s" % (r.stderr or r.stdout))
    return json.loads(r.stdout)


def reports_from_campaign(campaign_id, workspace=None):
    """Load the filed reports' text ([{id, text}]) from a campaign's workspace
    (or vault, if the workspace copy is gone). Text is the report artifact if
    present, else the accepted result's summary + findings."""
    import os
    import glob
    import json
    ws = workspace or os.getcwd()
    candidates = [os.path.join(ws, "campaigns", campaign_id),
                  os.path.join(harness_dir(), "..", "..", "campaigns", campaign_id)]
    cd = next((c for c in candidates if os.path.isdir(c)), candidates[0])
    filed_ids = []
    reps = sorted(glob.glob(os.path.join(cd, "report_stage*.json")))
    if reps:
        rec = json.load(open(reps[-1]))
        filed_ids = [r["task_id"] for r in rec.get("filed_reports") or []]
    out = []
    for tid in filed_ids:
        tdir = os.path.join(cd, "tasks", tid)
        text = ""
        for md in sorted(glob.glob(os.path.join(tdir, "files", "*.md"))):
            text += open(md).read()
        if not text and os.path.exists(os.path.join(tdir, "result.json")):
            pay = json.load(open(os.path.join(tdir, "result.json"))).get("payload", {})
            text = (pay.get("summary", "") + "\n\n"
                    + "\n".join("- " + f.get("claim", "") for f in pay.get("findings", [])))
        out.append({"id": tid, "text": text})
    return out


def rank_campaign(host, campaign_id, workspace=None, model=None):
    """Run the tournament over a campaign's filed reports; write ranking.json
    into the campaign dir and return the ranking dict."""
    import os
    import json
    T = load_engine("science_tournament")
    reports = reports_from_campaign(campaign_id, workspace)
    result = T.run_tournament(host, reports, model=model)
    ws = workspace or os.getcwd()
    cd = os.path.join(ws, "campaigns", campaign_id)
    if os.path.isdir(cd):
        json.dump(result, open(os.path.join(cd, "ranking.json"), "w"), indent=1)
    return result


def rank_reports(host, reports, model=None):
    """Rank an explicit [{id, text}] list (bypasses campaign loading)."""
    return load_engine("science_tournament").run_tournament(host, reports, model=model)
