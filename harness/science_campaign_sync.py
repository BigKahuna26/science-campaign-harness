#!/usr/bin/env python3
"""Stage the Science harness loop into the workspace; mirror its results back.

WHY THIS EXISTS
---------------
Claude Science has two kernels and they have opposite privileges (measured
2026-10-09):

  * the **control-plane kernel** can call `host.delegate()` but CANNOT read or
    write any host path — `PermissionError: [Errno 1] Operation not permitted`
    on the vault and on the Desktop alike, for read, stat, list and write;
  * the **Python analysis kernel** can read and write the vault but has no
    `host.delegate` at all (`host.delegate() has no surface in the python
    kernel`).

They share one directory: the session workspace. So the loop module cannot be
imported from the vault by the kernel that runs it, and the results it writes
cannot be put in the vault by that kernel either. This script is the bridge,
and it runs in the Python kernel from its own saved file.

    stage   vault file -> workspace copy, hash recorded
    mirror  workspace results -> analysis/campaigns/<id>/, artifacts resolved

WHAT "RUN FROM THAT FILE" MEANS HERE
------------------------------------
The vault's rule is that every number comes from a script saved in
`analysis/scripts/` and run from that file. What the control-plane kernel runs
is a byte-identical COPY of `science_campaign.py`, so `stage` records the
source path, the SHA-256 and the time, and the loop stamps that same hash into
every result.json it writes. `verify` re-reads both and reports whether the
copy still matches the vault file — which is the question that actually matters
afterwards: not "was it the file" but "was it this code".

The gate scripts and this script are ordinary vault files run from the vault,
with no copy involved.

Usage::

    python3 analysis/scripts/harness/science_campaign_sync.py stage \
        --campaign smoke --workspace /path/to/workspace
    python3 analysis/scripts/harness/science_campaign_sync.py verify \
        --campaign smoke --workspace /path/to/workspace
    python3 analysis/scripts/harness/science_campaign_sync.py mirror \
        --campaign smoke --workspace /path/to/workspace [--artifact-map map.json]

`--artifact-map` is a JSON object mapping artifact version_id -> readable path,
written by the caller from `host.artifact_path()` (which lives in the Python
kernel). Without it, artifacts are recorded by id and not copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

VAULT = Path(__file__).resolve().parents[3]
LOOP_MODULE = Path(__file__).resolve().parent / "science_campaign.py"
CAMPAIGNS = VAULT / "analysis/campaigns"


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def workspace_campaign_dir(workspace: Path, campaign: str) -> Path:
    return Path(workspace) / "campaigns" / campaign


def do_stage(workspace: Path, campaign: str) -> dict:
    """Copy the loop module into the workspace and record what was copied."""
    if not LOOP_MODULE.exists():
        sys.exit("loop module missing: %s" % LOOP_MODULE)
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    dest = ws / LOOP_MODULE.name
    shutil.copy2(LOOP_MODULE, dest)
    src_hash, dst_hash = sha256(LOOP_MODULE), sha256(dest)
    if src_hash != dst_hash:
        sys.exit("copy mismatch: %s != %s" % (src_hash, dst_hash))
    rec = {
        "campaign": campaign,
        "source_path": str(LOOP_MODULE),
        "staged_path": str(dest),
        "sha256": src_hash,
        "staged_at": time.time(),
        "staged_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "vault_commit": _git_head(),
        "vault_dirty": _git_dirty(LOOP_MODULE),
    }
    cdir = workspace_campaign_dir(ws, campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "staged.json").write_text(json.dumps(rec, indent=1))
    return rec


def do_verify(workspace: Path, campaign: str) -> dict:
    """Does the staged copy still match the vault file?"""
    cdir = workspace_campaign_dir(Path(workspace), campaign)
    staged_file = cdir / "staged.json"
    if not staged_file.exists():
        sys.exit("no staged.json at %s — run `stage` first" % staged_file)
    rec = json.loads(staged_file.read_text())
    dest = Path(rec["staged_path"])
    out = {
        "campaign": campaign,
        "staged_sha256": rec["sha256"],
        "staged_copy_present": dest.exists(),
        "staged_copy_sha256": sha256(dest) if dest.exists() else None,
        "vault_sha256": sha256(LOOP_MODULE),
    }
    out["copy_matches_record"] = out["staged_copy_sha256"] == out["staged_sha256"]
    out["vault_matches_record"] = out["vault_sha256"] == out["staged_sha256"]
    out["ok"] = bool(out["copy_matches_record"] and out["vault_matches_record"])
    return out


def do_mirror(workspace: Path, campaign: str, artifact_map=None) -> dict:
    """Copy the workspace campaign tree into the vault; resolve artifacts.

    The workspace is swept after long idle gaps, so the vault copy is the
    durable record. Artifacts land in `tasks/<id>/files/`, matching the host
    harness layout that `stagecheck.py` already assumes.
    """
    src = workspace_campaign_dir(Path(workspace), campaign)
    if not src.exists():
        sys.exit("nothing to mirror: %s does not exist" % src)
    dst = CAMPAIGNS / campaign
    dst.mkdir(parents=True, exist_ok=True)
    amap = dict(artifact_map or {})
    copied, artifacts, missing = [], [], []

    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith(".tmp"):
                continue
            shutil.copy2(Path(root) / name, dst / rel / name)
            copied.append(str(rel / name))

    for result_path in sorted(dst.glob("tasks/*/result.json")):
        rec = json.loads(result_path.read_text())
        files_dir = result_path.parent / "files"
        for art in (rec.get("payload") or {}).get("artifacts") or []:
            vid, fname = art.get("version_id"), art.get("filename")
            srcf = amap.get(vid)
            if srcf and Path(srcf).exists():
                files_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srcf, files_dir / Path(fname or Path(srcf).name).name)
                artifacts.append({"task": result_path.parent.name, "version_id": vid,
                                  "filename": fname})
            else:
                missing.append({"task": result_path.parent.name, "version_id": vid,
                                "filename": fname})

    rec = {"campaign": campaign, "mirrored_at": time.time(), "dest": str(dst),
           "files_copied": len(copied), "artifacts_copied": len(artifacts),
           "artifacts_unresolved": missing}
    (dst / "mirror.json").write_text(json.dumps(rec, indent=1))
    return rec


def _git_head():
    try:
        import subprocess
        return subprocess.run(["git", "-C", str(VAULT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip() or None
    except Exception:
        return None


def _git_dirty(path):
    """True when the file has uncommitted changes — that run is not reproducible
    from any commit, which is worth knowing before the numbers are used."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(VAULT), "status", "--porcelain", str(path)],
                           capture_output=True, text=True, timeout=20)
        return bool(r.stdout.strip())
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["stage", "verify", "mirror"])
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--workspace", required=True,
                    help="session workspace dir (shared by both kernels)")
    ap.add_argument("--artifact-map", help="JSON: {version_id: readable_path}")
    args = ap.parse_args()

    if args.mode == "stage":
        out = do_stage(Path(args.workspace), args.campaign)
    elif args.mode == "verify":
        out = do_verify(Path(args.workspace), args.campaign)
    else:
        amap = json.loads(Path(args.artifact_map).read_text()) if args.artifact_map else {}
        out = do_mirror(Path(args.workspace), args.campaign, amap)

    print(json.dumps(out, indent=1))
    if args.mode == "verify" and not out["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
