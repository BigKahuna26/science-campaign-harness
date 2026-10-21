# science-campaign-harness

An autonomous multi-agent research campaign loop for
[Claude Science](https://claude.ai) — worker, supervisor, triage, curator and
editor roles over `host.delegate` sub-agents, with **scripted** stage gates and
a pairwise report tournament.

Reimplemented from the agentic harness described in Yoon et al. 2026
(*Array-Associated Reverse Transcriptases*, Fig. 1A). The architecture is
theirs; this code is an independent implementation on Claude Science
primitives, and it **deviates from the paper in three documented ways** — see
[Relationship to the source design](#relationship-to-the-source-design).

Packaged with its selftest: **44/44 checks passing**.

---

## What it does

You write a research brief and a list of stages. Each stage is a list of
tasks. For every task the harness:

1. dispatches a **worker** sub-agent, which plans, runs the analysis, saves
   its scripts and submits a **schema-typed** result;
2. dispatches a **supervisor** sub-agent that reviews that result **without
   having seen the worker's reasoning** — `fork` is never passed to
   `host.delegate`, and the selftest enforces it;
3. loops ACCEPT / REVISE up to `max_revisions`;
4. lets a worker propose **follow-up tasks**, which **triage** releases or
   rejects — with depth and count caps enforced in code *before* the model is
   consulted, so a release-everything model cannot run away;
5. has a **curator** distil accepted results into a shared knowledge base
   carried into later tasks.

Then a **scripted gate** over schema fields — not a model judgement — decides
whether the next stage opens. On failure the loop persists state and is
resumable; accepted work is never repeated.

## Requirements

- **Delegation enabled** on the account. Check `host.capabilities()["delegate"]`
  in a `repl` cell; the loop raises otherwise. This is a platform setting.
- A directory you have granted Claude Science access to, containing this repo.
- **`pandas`** in the environment your `python` kernel uses. Everything else
  the engine imports is standard library.

## Install

```bash
git clone <this repo> ~/science-campaign-harness
cd ~/science-campaign-harness
python3 harness/science_campaign_selftest.py        # expect 44/44
```

Then register the skill in your own account — skills are per-account and there
is no import/export, so this is a copy-paste. In a `repl` cell:

```python
root = "/absolute/path/to/science-campaign-harness"
host.skills.edit("science-campaign", "SKILL.md",
                 open(f"{root}/skill/SKILL.md").read())
host.skills.edit("science-campaign", "kernel.py",
                 open(f"{root}/skill/kernel.py").read())
host.skills.publish("science-campaign")
```

`kernel.py` finds the engine via `SCIENCE_HARNESS_DIR`, falling back to
`~/science-campaign-harness/harness`. If it is wrong the helpers raise naming
the path they looked for.

## Why there are two kernels

The kernel that can spawn sub-agents (`repl`) **cannot read or write host
paths**. The kernel that can read your files (`python`) **cannot spawn
sub-agents**. They share only the session workspace.

So the loop runs in `repl` from a *staged copy* of the engine in the
workspace, and that copy's SHA-256 is stamped into every result — you can
always tell which engine produced a record. Staging, mirroring back, and the
tournament run in `python`. If `host.delegate` is missing in one kernel or the
vault unreadable in the other, this is why.

## Running one

```python
# python kernel
stage_campaign("my-campaign")

# repl kernel
import sys, os, importlib; sys.path.insert(0, os.getcwd())
import science_campaign as sc; importlib.reload(sc)

stages = [
  {"n": 1,
   "tasks": [{"id": "t001", "stage": 1, "task": "..."}],
   "gate": sc.stage_gate(min_accepted=2, require_no_stop_condition=True)},
  {"n": 2,
   "tasks": [{"id": "r001", "stage": 2, "kind": "report",
              "source_task_ids": ["t001"], "task": "write the report ..."}],
   "gate": sc.stage_gate(min_filed=1)},
]
camp = sc.run_campaign(host, campaign_id="my-campaign", stages=stages,
                       out_root="campaigns",
                       root_frame_id=sc.detect_root_frame_id(host),
                       brief=open("brief.md").read(),
                       cost_ceiling_usd=50.0, wave_size=6,
                       max_revisions=2, collect_timeout=3600,
                       enable_triage=True, curate=True, enable_editor=True,
                       max_followups=12, max_followup_depth=1,
                       script_dir="/abs/path/to/your/scripts")
```

Halted? Re-run the same call with `resume=True`. Then, in the `python` kernel,
`mirror_campaign(host, "my-campaign")` and `rank_campaign(host, "my-campaign")`.

## Domain gates

Generic gates read schema fields. A gate that reads the *tables* a task
produced and applies a rule from your field is a **plugin** — see
[docs/GATE_PLUGIN.md](docs/GATE_PLUGIN.md) and
[examples/example_gate.py](examples/example_gate.py).

```bash
python3 harness/science_stagecheck.py --campaign my-campaign \
    --gate-module example_gate --gate-path examples/
```

## Relationship to the source design

Yoon et al. 2026, Fig. 1A: *"Instances of Claude act as workers to plan and run
tasks, supervisors to review them and open follow-up tasks (98 of 119 tasks),
curators to record findings in a shared knowledge base, and editors to review
reports."*

1. **Curator and editor are full agents there; single `host.llm` calls here.**
   A cost decision — five delegated roles per task is expensive, and these are
   stateless judgements over text.
2. **The supervisor opens follow-ups there; the worker proposes them here.**
3. **Triage is an addition.** The source has no gate between a proposed
   follow-up and its execution.

Cite the paper for the architecture. Do not cite this repo as an
implementation of it without reading the three points above.

## Sharp edges

- **Supervisors review by reading unless their environment has your analysis
  stack.** In one measured run only 2 of 6 independently recomputed anything;
  one reported that no environment available to it carried the packages the
  work needed. The review layer is only as strong as what the reviewer can
  import — **the highest-value thing to fix before relying on it.**
- **A stop condition requires a *named* verdict.** `name: "NONE"` is refused
  and the task recorded as a failure, even when the work was good. Tested
  behaviour (`test_stop_condition_allowed_on_any_verdict_but_none`), not a
  bug; survey tasks with no hypothesis still need a verdict name.
- **The cost brake undercounts.** It sums recorded per-session cost before each
  wave, and the platform's own per-output review carries no visible cost row.
  Treat `cost_ceiling_usd` as a floor.
- **Wave cap is 48** per `delegate()` call; oversize waves are refused before
  anything spawns.
- **Slow is not failed; parked is not failed.** A child past the collect
  deadline keeps its id and stays collectable; one parked on a user approval
  halts the stage and is never killed.
- **Sub-agents do not themselves delegate** in practice. Nothing prevents it,
  but a focused analysis task rarely has a seam to split, so expect a flat
  fan-out.

## Layout

```
harness/
  science_campaign.py            the loop: schemas, guards, triage, curator,
                                 editor, run_stage, run_campaign, resume
  science_campaign_sync.py       stage / verify / mirror (the kernel bridge)
  science_stagecheck.py          scripted gates + the domain-gate plugin point
  science_tournament.py          pairwise LLM judge + Bradley-Terry ranking
  science_campaign_selftest.py   44 deterministic checks
skill/      SKILL.md + kernel.py to register in your account
docs/       GATE_PLUGIN.md
examples/   example_gate.py
```

## License

MIT. See [LICENSE](LICENSE).
