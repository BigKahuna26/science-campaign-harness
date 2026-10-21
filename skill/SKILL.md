---
name: science-campaign
description: "Run an autonomous multi-agent research campaign inside Claude Science — the Yoon et al. worker / supervisor / triage / curator / editor loop with scripted stage gates and a pairwise report tournament, built entirely on host.delegate sub-agents. Use when the user wants to run a campaign from a research brief, dispatch a self-extending wave of analysis tasks with review, rank the resulting reports, or explore a campaign's ranked reports. Triggers: 'run a campaign', 'autonomous research harness', 'worker/supervisor loop', 'triage follow-ups', 'rank the reports', 'science harness'."
---

# Science campaign — the autonomous research harness, on Claude Science primitives

A worker/supervisor/triage/curator/editor campaign loop, reimplemented from
Yoon et al. 2026 on `host.delegate` sub-agents instead of host `claude -p`
subprocesses. The engine lives at
`harness/` (git-tracked, reviewed, self-tested); this skill is
the runbook for driving it in a session.

## The five roles (and where each runs)

| role | what it does | how it runs |
|---|---|---|
| worker | plans + runs one task, submits a schema-typed result | **delegated sub-agent** (fresh context, own kernel + workspace) |
| supervisor | reviews that result without having seen the worker's reasoning | **delegated sub-agent** |
| triage | releases or rejects each proposed follow-up, with a reason | `host.llm` call |
| curator | distills each accepted task into a shared knowledge base | `host.llm` call |
| editor | files or holds each report before it can be ranked | `host.llm` call |

Worker and supervisor are delegated because they must *run and independently
verify* analysis. Triage, curator, and editor are stateless judgements over
text, so they are `host.llm` calls — faithful to the paper and far cheaper than
five delegated roles. The tournament judge is also `host.llm`.

## Prerequisites

- **Delegation (ultra mode) must be on** — `host.capabilities()["delegate"]`
  is `True` (the loop raises otherwise).
- The vault must be mounted (the engine scripts live under
  `harness/`).

## Why two kernels

The kernel that can call `host.delegate` (the **control-plane / `repl`
kernel**) cannot read or write any host path; the **`python` kernel** can read
the vault but has no `host.delegate`. They share the workspace directory. So:

- the campaign LOOP runs in the `repl` kernel, from a **staged copy** of the
  engine module (its SHA-256 is stamped into every result);
- **staging, mirroring to the vault, and the tournament** run in the `python`
  kernel.

This skill's `kernel.py` helpers load into the `python` kernel.

## Run a campaign — the five steps

**1. Stage the engine (`python`/`bash`).** `stage_campaign("<id>")` copies the
loop module into the workspace and records its hash (or run
`science_campaign_sync.py stage` directly).

**2. Run the campaign (`repl` tool).** Import the staged module, define a brief
and stages, and call `run_campaign`:

```python
import sys, os, importlib
sys.path.insert(0, os.getcwd())
import science_campaign as sc; importlib.reload(sc)

ROOT = sc.detect_root_frame_id(host)
stages = [
    {"n": 1, "tasks": [{"id": "t001", "stage": 1, "task": "..."}],
     "gate": sc.stage_gate(min_accepted=2, require_no_stop_condition=True)},
    {"n": 2, "tasks": [{"id": "r001", "stage": 2, "task": "write a report ...",
                        "kind": "report", "source_task_ids": ["t001"]}],
     "gate": sc.stage_gate(min_filed=1)},
]
camp = sc.run_campaign(
    host, campaign_id="<id>", stages=stages, out_root="campaigns",
    root_frame_id=ROOT, brief="<the research brief text>",
    cost_ceiling_usd=<ceiling>, context_ceiling_tokens=400_000,
    wave_size=4, max_revisions=2, collect_timeout=1800,
    enable_triage=True, curate=True, enable_editor=True,
    max_followups=50, max_followup_depth=2)
```

Key knobs: `cost_ceiling_usd` is the brake (summed recorded cost across the
whole session tree, checked before each wave); `context_ceiling_tokens` stops
the orchestrator before silent compaction; `wave_size` ≤ 48; `script_dir` tells
workers where to save the scripts their numbers come from (the vault
`analysis/scripts/` for a real campaign).

**3. If it halted, resume (`repl` tool).** The brake, the context guard, a
parked approval, and consecutive-failure halts all stop the stage and persist
state. Re-run the same call with `resume=True` to continue — accepted work is
not repeated; released follow-ups and the knowledge base carry forward.

**4. Mirror + rank (`python` tool).** `mirror_campaign(host, "<id>")` copies the
campaign into the vault (resolving report artifacts); `rank_campaign(host,
"<id>")` runs the tournament over the filed reports and writes `ranking.json`.

**5. Explore.** The ranked reports and their tables are artifacts; open the
top-ranked report to read it.

## Guardrails this harness enforces (each from a real failure)

- **Only a schema-valid submission counts** — a child that talks instead of
  submitting is a failure, never read as a result.
- **The cost brake** is recorded per-session cost, checked before each wave (it
  undercounts by the platform's own per-output review, which has no visible
  cost row — treat it as a floor).
- **Slow ≠ failed; parked ≠ failed** — a child past the collect deadline keeps
  its id and stays collectable; a child parked on a user approval halts the
  stage and is never stopped.
- **Triage caps before the LLM** — follow-up depth and campaign caps are
  enforced structurally, so a release-everything model cannot run away.
- **`analyst_supplied`** — data a worker supplies from its own knowledge must
  be declared; a verdict that leans on unverifiable self-supplied data is a
  REVISE.
- **`fork` is never passed** to `host.delegate` (it would break reviewer
  independence).

## Measured numbers (2026-10-11)

48 concurrent children in 27 s, zero failures — concurrency is free up to the
cap; the limit is cost (each child pays a ~50k-token context tax). Substantive
stage-2 workers run ~$5–21 each. A two-stage smoke chain (triage → curator →
gate → report → editor → tournament) that self-extended 2 seeds to 4 tasks cost
~$8.

## The engine files (in the vault)

- `science_campaign.py` — the loop: schemas, classifier, cost/context/failure
  guards, resume, triage, curator, editor, `run_stage`, `run_campaign`.
- `science_tournament.py` — pairwise `host.llm` judge (paper rubric,
  soundness-≤2 auto-loss) + Bradley–Terry ranking.
- `science_campaign_sync.py` — stage / verify / mirror (the two-kernel bridge).
- `science_stagecheck.py` — vault-side scripted stage gates over schema fields.
- `science_campaign_selftest.py` — deterministic checks; run after any edit.

Never auto-apply a campaign's conclusions. A filed, top-ranked report is a
candidate for review, not a settled finding — the same rule the vault applies
to every reviewer.
