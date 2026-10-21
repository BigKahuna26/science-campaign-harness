#!/usr/bin/env python3
"""Exercise the Science harness loop against a stubbed host — no sub-agents, no spend.

WHY STUB IT
-----------
The guards are the part that must not be wrong, and every one of them is a
path a live run reaches only when something has already gone badly: the brake
tripping, five supervisors failing in a row, a revision cycle exhausting its
budget. Proving those with real sub-agents costs money per attempt and gives a
non-deterministic answer. A stub makes each one a fact.

What is NOT covered here, and must be checked live: that `host.delegate`
accepts these schemas, that a child can satisfy them, and that the platform
records cost where `campaign_cost()` reads it. Those are the live smoke run's
job — this file tests the loop's own decisions.

Run it after touching the loop:

    python3 analysis/scripts/harness/science_campaign_selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import science_campaign as sc  # noqa: E402

ROOT = "11111111-1111-1111-1111-111111111111"


def good_payload(task_id="t001", **over):
    p = {
        "task_id": task_id,
        "summary": "did the thing",
        "commands_run": ["python3 analysis/scripts/x.py --flag"],
        "artifacts": [{"filename": "out.csv", "version_id": "v-1"}],
        "findings": [{"claim": "c", "evidence": "e",
                      "evidence_command": "python3 analysis/scripts/x.py --flag",
                      "confidence": "high"}],
        "verdict": {"name": "CONTROL", "evaluable": True, "outcome": "SUPPORTED",
                    "value": "6/6", "reason": "r"},
        "blocked": False, "blocked_reason": None, "followups": [],
        "analyst_supplied": [],
    }
    p.update(over)
    return p


def sv_payload(task_id="t001", decision="ACCEPT", changes=None):
    return {"task_id": task_id, "decision": decision,
            "checked": ["recomputed the counts"],
            "unsupported_claims": ([] if decision == "ACCEPT"
                                   else [{"quote": "q", "why": "w"}]),
            # `changes if changes is not None` — NOT `changes or ...`: an
            # intentionally empty list is falsy, so `or` would substitute the
            # default and the "REVISE with no required_changes" case would
            # silently test a well-formed payload instead.
            "required_changes": (changes if changes is not None
                                 else ([] if decision == "ACCEPT" else ["state the null"])),
            "notes": [],
            "followups": [],
            "confidence": "high"}


class StubHost:
    """Minimal stand-in. `script` maps a child's name prefix to a canned result."""

    def __init__(self, cost=0.0, context=1000, worker_results=None, supervisor_results=None,
                 llm_decisions=None):
        self.cost = cost
        self.context = context
        self.worker_results = list(worker_results or [])
        self.supervisor_results = list(supervisor_results or [])
        # Canned triage/curator/editor decisions, popped per host.llm call and
        # wrapped in the real {"tool_use": {"input": ...}} shape so the loop's
        # own parsing (_llm_tool) is exercised, not bypassed.
        self.llm_decisions = list(llm_decisions or [])
        self.llm_calls = []
        self.spawns = []
        self.stopped = []
        self._pending = {}
        self._n = 0

    def llm(self, prompt=None, system=None, tools=None, tool_choice=None, model=None, **kw):
        self.llm_calls.append({"prompt": prompt, "system": system})
        payload = self.llm_decisions.pop(0) if self.llm_decisions else {}
        name = (tools[0]["name"] if tools else "decide")
        return {"tool_use": {"name": name, "input": payload}, "text": "", "stop_reason": "tool_use"}

    def stop_child(self, frame_id, reason=None):
        self.stopped.append(frame_id)
        return {"status": "stopped", "frame_id": frame_id}

    # -- the three queries the loop makes -------------------------------
    def query(self, sql, params=None):
        if "parent_frame_id IS NULL" in sql:
            return {"columns": ["id"], "rows": [[ROOT]]}
        if "SUM(total_cost)" in sql:
            # cost may be a callable so a test can make the ceiling trip at a
            # chosen point in the run (e.g. after wave 1) rather than up front.
            c = self.cost() if callable(self.cost) else self.cost
            return {"columns": ["n", "total"], "rows": [[len(self.spawns) + 1, c]]}
        if "_context_used" in sql:
            return {"columns": ["used", "comp"], "rows": [[self.context, 0]]}
        raise AssertionError("unexpected query: %s" % sql[:80])

    def delegate(self, requests, wait=True, **kw):
        assert wait is False, "the loop must dispatch async"
        assert "fork" not in kw
        descs = []
        for r in requests:
            self._n += 1
            fid = "frame-%03d" % self._n
            is_sv = r["name"].startswith("sv-")
            self.spawns.append({"frame_id": fid, "name": r["name"], "task": r["task"]})
            pool = self.supervisor_results if is_sv else self.worker_results
            self._pending[fid] = pool.pop(0) if pool else {"status": "failed",
                                                           "error": "stub exhausted"}
            descs.append({"frame_id": fid, "status": "running", "dispatched": True})
        return descs

    def collect(self, frame_ids, timeout=30):
        out = []
        for fid in frame_ids:
            res = dict(self._pending.get(fid) or {"status": "failed", "error": "unknown"})
            res["frame_id"] = fid
            out.append(res)
        return out


def ok(payload):
    return {"status": "completed", "structured_output": payload, "deviations": ["none"]}


def unsatisfied():
    return {"status": "completed", "structured_output_unsatisfied": True,
            "response": "I think the answer is probably yes."}


# --- the checks ------------------------------------------------------------

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def test_classify_states():
    cases = [
        (ok(good_payload()), "ok"),
        (unsatisfied(), "unsatisfied"),
        ({"status": "completed", "structured_output": None}, "unsatisfied"),
        ({"status": "failed", "error": "boom"}, "failed"),
        ({"status": "cancelled"}, "failed"),
        ({"status": "orphaned", "restart_casualty": True}, "failed"),
        ({"status": "running", "child_status": "processing"}, "running"),
        (ok(good_payload(verdict={"name": "CONTROL", "evaluable": False,
                                  "outcome": "SUPPORTED", "value": None,
                                  "reason": "no data"})), "incoherent"),
    ]
    bad = []
    for res, want in cases:
        got, _ = sc.classify_result(res, "worker", expected_task_id="t001")
        if got != want:
            bad.append("%s -> %s (want %s)" % (res.get("status"), got, want))
    return (not bad), "; ".join(bad)


@check
def test_consistency_rules():
    cases = [
        (good_payload(), 0, "clean payload"),
        (good_payload(verdict={"name": "CONTROL", "evaluable": True, "outcome": None,
                               "value": None, "reason": "r"}), 1, "evaluable w/o outcome"),
        # A stop condition off CONTROL is now ACCEPTED -- a pre-registered stop can
        # fire on any task kind, and the old restriction destroyed two complete
        # analyses (d004 PATCH, v003 PRE-REGISTRATION). Only NONE is still refused.
        (good_payload(verdict={"name": "FAMILY", "evaluable": True,
                               "outcome": "STOP_CONDITION_FIRED", "value": None,
                               "reason": "r"}), 0, "stop condition off CONTROL (now allowed)"),
        (good_payload(verdict={"name": "NONE", "evaluable": True,
                               "outcome": "STOP_CONDITION_FIRED", "value": None,
                               "reason": "r"}), 1, "stop condition with no verdict"),
        (good_payload(blocked=True, blocked_reason=None), 2, "blocked w/o reason + evaluable"),
        (good_payload(findings=[{"claim": "c", "evidence": "e",
                                 "evidence_command": "python3 other.py",
                                 "confidence": "low"}]), 1, "finding not traced"),
        (good_payload(task_id="WRONG"), 1, "task id mismatch"),
    ]
    bad = []
    for payload, want_n, label in cases:
        got = sc.check_worker_consistency(payload, expected_task_id="t001")
        if len(got) != want_n:
            bad.append("%s: %d problems %s (want %d)" % (label, len(got), got, want_n))
    sv = sc.check_supervisor_consistency(sv_payload(decision="REVISE", changes=[]),
                                         expected_task_id="t001")
    if len(sv) != 1:
        bad.append("REVISE with no required_changes: %s" % sv)
    return (not bad), "; ".join(bad)


@check
def test_cost_ceiling_halts_before_spawning():
    h = StubHost(cost=12.0)
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10.0,
                           context_ceiling_tokens=10 ** 9, stage=1)
    good = (rep["halt"] and rep["halt"]["kind"] == "cost_ceiling" and not h.spawns)
    return good, "halt=%s spawns=%d" % ((rep["halt"] or {}).get("kind"), len(h.spawns))


@check
def test_context_ceiling_halts_before_spawning():
    h = StubHost(cost=0.0, context=500_000)
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=400_000, stage=1)
    good = (rep["halt"] and rep["halt"]["kind"] == "context_ceiling" and not h.spawns)
    return good, "halt=%s spawns=%d" % ((rep["halt"] or {}).get("kind"), len(h.spawns))


@check
def test_consecutive_worker_failures_halt():
    tasks = [{"id": "t%03d" % i, "stage": 1, "task": "x"} for i in range(1, 7)]
    h = StubHost(worker_results=[unsatisfied() for _ in range(6)])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                           cost_ceiling_usd=10 ** 6, context_ceiling_tokens=10 ** 9,
                           stage=1, wave_size=2, max_consecutive_role_failures=2)
    good = (rep["halt"] and rep["halt"]["kind"] == "consecutive_worker_failures"
            and len(h.spawns) == 2 and not rep["accepted"])
    return good, "halt=%s spawns=%d accepted=%s" % (
        (rep["halt"] or {}).get("kind"), len(h.spawns), rep["accepted"])


@check
def test_unsatisfied_never_counts_as_success():
    """A child that talks instead of submitting has not done the work."""
    h = StubHost(worker_results=[unsatisfied()])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1)
        wrote_result = (Path(d) / "c/tasks/t001/result.json").exists()
        wrote_failure = (Path(d) / "c/tasks/t001/failure.json").exists()
    sv_spawned = [s for s in h.spawns if s["name"].startswith("sv-")]
    good = (not rep["accepted"] and not wrote_result and wrote_failure and not sv_spawned)
    return good, "accepted=%s result.json=%s failure.json=%s supervisors=%d" % (
        rep["accepted"], wrote_result, wrote_failure, len(sv_spawned))


@check
def test_revise_requeues_with_the_changes():
    h = StubHost(worker_results=[ok(good_payload()), ok(good_payload())],
                 supervisor_results=[ok(sv_payload(decision="REVISE",
                                                   changes=["state the multiple-testing correction"])),
                                     ok(sv_payload(decision="ACCEPT"))])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "orig"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, max_revisions=2)
    second = [s for s in h.spawns if not s["name"].startswith("sv-")][1]["task"]
    good = (rep["accepted"] == ["t001"] and "REVISION 1" in second
            and "multiple-testing correction" in second and not rep["stalled"])
    return good, "accepted=%s stalled=%s revision_prompt_carried=%s" % (
        rep["accepted"], rep["stalled"], "multiple-testing correction" in second)


@check
def test_max_revisions_stalls():
    h = StubHost(worker_results=[ok(good_payload()) for _ in range(5)],
                 supervisor_results=[ok(sv_payload(decision="REVISE")) for _ in range(5)])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, max_revisions=2)
    n_workers = len([s for s in h.spawns if not s["name"].startswith("sv-")])
    good = (not rep["accepted"] and len(rep["stalled"]) == 1 and n_workers == 3)
    return good, "accepted=%s stalled=%d worker_spawns=%d (want 3: original + 2 revisions)" % (
        rep["accepted"], len(rep["stalled"]), n_workers)


@check
def test_accept_can_carry_notes():
    """Regression: with no non-blocking channel, every nit forces REVISE.
    Measured 2026-10-10 — 3 reviewers, 3 REVISEs on a correct result, stalled."""
    p = sv_payload(decision="ACCEPT")
    p["notes"] = ["commands_run entry 2 could name the absolute path"]
    problems = sc.check_supervisor_consistency(p, expected_task_id="t001")
    preamble_ok = ("PROPORTIONALITY" in sc._SUPERVISOR_PREAMBLE
                   and "are NOT grounds for REVISE" in sc._SUPERVISOR_PREAMBLE)
    notes_in_schema = "notes" in sc.SUPERVISOR_SCHEMA["properties"]
    good = (not problems) and preamble_ok and notes_in_schema
    return good, "problems=%s proportionality_rule=%s notes_field=%s" % (
        problems, preamble_ok, notes_in_schema)


@check
def test_stall_points_at_the_unreviewed_result():
    h = StubHost(worker_results=[ok(good_payload()) for _ in range(5)],
                 supervisor_results=[ok(sv_payload(decision="REVISE")) for _ in range(5)])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, max_revisions=1)
        pointer = rep["stalled"][0].get("unreviewed_result_on_disk")
        good = bool(pointer) and Path(pointer).exists()
    return good, "pointer=%s exists=%s" % (bool(pointer), good)


@check
def test_timeout_is_not_a_failure():
    """Regression: the 2026-10-09 smoke run counted a slow worker as a failed
    role, abandoned its frame id, and left it running and spending. It had in
    fact finished. A deadline must not feed the infrastructure halt, the id
    must be kept, and the child must be stopped."""
    h = StubHost(worker_results=[{"status": "running", "child_status": "processing"}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1,
                           max_consecutive_role_failures=1, on_timeout="stop",
                           # The stub answers instantly, so _collect would spin
                           # to its real deadline; bound it.
                           collect_timeout=1, collect_slice=1)
        wrote_timeout = (Path(d) / "c/tasks/t001/timeout.json").exists()
        wrote_failure = (Path(d) / "c/tasks/t001/failure.json").exists()
    kept_id = bool(rep["timed_out"]) and rep["timed_out"][0].get("frame_id")
    good = (rep["halt"] is None and kept_id and wrote_timeout and not wrote_failure
            and h.stopped == [kept_id])
    return good, "halt=%s kept_id=%s timeout.json=%s failure.json=%s stopped=%s" % (
        (rep["halt"] or {}).get("kind"), bool(kept_id), wrote_timeout, wrote_failure, h.stopped)


@check
def test_parked_child_is_not_stopped():
    """Regression: the 2026-10-11 stage-2 pilot stopped a child that was parked
    on an approval card, which denies the approval and discarded a completed
    16-minute analysis. Parked must halt, never stop."""
    h = StubHost(worker_results=[{"status": "running",
                                  "child_status": "awaiting_user_response"}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, on_timeout="stop",
                           collect_timeout=1, collect_slice=1)
        wrote = (Path(d) / "c/tasks/t001/blocked_on_user.json").exists()
    kind = (rep["halt"] or {}).get("kind")
    good = (kind == "blocked_on_user" and h.stopped == [] and wrote
            and rep["blocked_on_user"] and rep["blocked_on_user"][0]["frame_id"])
    return good, "halt=%s stopped=%s blocked_on_user.json=%s id_kept=%s" % (
        kind, h.stopped, wrote, bool(rep["blocked_on_user"]))


@check
def test_consecutive_timeouts_halt_separately():
    tasks = [{"id": "t%03d" % i, "stage": 1, "task": "x"} for i in range(1, 6)]
    h = StubHost(worker_results=[{"status": "running"} for _ in range(5)])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                           cost_ceiling_usd=10 ** 6, context_ceiling_tokens=10 ** 9, stage=1,
                           wave_size=1, max_consecutive_timeouts=2,
                           max_consecutive_role_failures=99,
                           collect_timeout=1, collect_slice=1)
    good = (rep["halt"] and rep["halt"]["kind"] == "consecutive_timeouts"
            and len(h.spawns) == 2)
    return good, "halt=%s spawns=%d" % ((rep["halt"] or {}).get("kind"), len(h.spawns))


@check
def test_script_dir_reaches_the_worker_prompt():
    """The preamble beats the task text, measured — so the path must be settable."""
    req = sc.build_worker_request({"id": "t001", "task": "do it"}, sc.WORKER_SCHEMA,
                                  script_dir="the-task-workspace/")
    good = ("the-task-workspace/" in req["task"] and "analysis/scripts/" not in req["task"])
    return good, "script_dir honoured=%s" % good


@check
def test_resume_after_cost_halt():
    """A halt is expected on a real campaign — the brake guarantees one. Resume
    must continue from the saved state, not re-run accepted work."""
    tasks = [{"id": "t%03d" % i, "stage": 1, "task": "x"} for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as d:
        # wave 1 accepts t001 (worker + supervisor = 2 spawns), then the
        # ceiling trips at the wave-2 guard before anything else is dispatched.
        h1 = StubHost(worker_results=[ok(good_payload("t001"))],
                      supervisor_results=[ok(sv_payload("t001"))])
        h1.cost = lambda: 99.0 if len(h1.spawns) >= 2 else 0.0
        r1 = sc.run_stage(h1, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                          cost_ceiling_usd=5.0, context_ceiling_tokens=10 ** 9, stage=1,
                          wave_size=1)
        halted = (r1["halt"] or {}).get("kind")
        # resume with headroom; t001 must NOT be re-dispatched
        h2 = StubHost(cost=0.0,
                      worker_results=[ok(good_payload("t002")), ok(good_payload("t003"))],
                      supervisor_results=[ok(sv_payload("t002")), ok(sv_payload("t003"))])
        r2 = sc.run_stage(h2, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                          cost_ceiling_usd=10 ** 6, context_ceiling_tokens=10 ** 9, stage=1,
                          wave_size=1, resume=True)
    dispatched = [s["name"] for s in h2.spawns if not s["name"].startswith("sv-")]
    good = (halted == "cost_ceiling" and "t001" not in dispatched
            and set(r2["accepted"]) == {"t001", "t002", "t003"})
    return good, "halt=%s re-dispatched=%s final_accepted=%s" % (
        halted, dispatched, sorted(r2["accepted"]))


@check
def test_failed_task_is_retried_on_resume():
    """Regression: a failed worker dropped its task from the queue and recorded
    it only in failure.json, so resume silently continued without it."""
    tasks = [{"id": "t001", "stage": 1, "task": "x"}, {"id": "t002", "stage": 1, "task": "y"}]
    with tempfile.TemporaryDirectory() as d:
        h1 = StubHost(worker_results=[unsatisfied()])          # t001 fails
        h1.cost = lambda: 99.0 if len(h1.spawns) >= 1 else 0.0  # halt before t002
        r1 = sc.run_stage(h1, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                          cost_ceiling_usd=5.0, context_ceiling_tokens=10 ** 9, stage=1,
                          wave_size=1)
        state = json.loads((Path(d) / "c/state.json").read_text())
        h2 = StubHost(cost=0.0,
                      worker_results=[ok(good_payload("t001")), ok(good_payload("t002"))],
                      supervisor_results=[ok(sv_payload("t001")), ok(sv_payload("t002"))])
        r2 = sc.run_stage(h2, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                          cost_ceiling_usd=10 ** 6, context_ceiling_tokens=10 ** 9, stage=1,
                          wave_size=1, resume=True)
    persisted = [t["id"] for t in state.get("failed_tasks") or []]
    good = (r1["failed"] and persisted == ["t001"]
            and set(r2["accepted"]) == {"t001", "t002"})
    return good, "failed_persisted=%s resumed_accepted=%s" % (persisted, sorted(r2["accepted"]))


@check
def test_resume_keeps_revision_text():
    """A task halted mid-revision must resume with the reviewer's changes still
    appended — resuming from ids alone would re-run the unrevised version."""
    tasks = [{"id": "t001", "stage": 1, "task": "ORIGINAL"}]
    with tempfile.TemporaryDirectory() as d:
        h1 = StubHost(worker_results=[ok(good_payload("t001"))],
                      supervisor_results=[ok(sv_payload("t001", decision="REVISE",
                                                        changes=["declare the null"]))])
        # Halt at the wave-2 guard, while the revised task is still queued —
        # otherwise the stage runs on and the final state.json is empty.
        h1.cost = lambda: 99.0 if len(h1.spawns) >= 2 else 0.0
        sc.run_stage(h1, campaign_id="c", tasks=tasks, out_root=d, root_frame_id=ROOT,
                     cost_ceiling_usd=5.0, context_ceiling_tokens=10 ** 9, stage=1,
                     wave_size=1, max_revisions=1)
        state = json.loads((Path(d) / "c/state.json").read_text())
        saved = json.dumps(state.get("queued_tasks") or [])
    good = ("REVISION 1" in saved and "declare the null" in saved)
    return good, "revision text persisted=%s" % good


@check
def test_analyst_supplied_is_declarable_and_gateable():
    """Regression: a worker hand-entered 56 literature citations as a literal
    inside its script. Every mechanical check passed because the findings traced
    to the command that read it. Declaring it must be possible and gate-visible."""
    clean = sc.check_worker_consistency(good_payload(), expected_task_id="t001")
    declared = good_payload(analyst_supplied=[
        {"what": "56 entity x partner literature pairings with citations",
         "why_not_staged": "no staged literature table exists for this campaign",
         "verifiable_how": "spot-check each citation against the primary source"}])
    with_decl = sc.check_worker_consistency(declared, expected_task_id="t001")
    in_schema = "analyst_supplied" in sc.WORKER_SCHEMA["properties"]
    required = "analyst_supplied" in sc.WORKER_SCHEMA["required"]
    preamble = "analyst_supplied" in sc._WORKER_PREAMBLE
    # the supervisor must be told the load-bearing rule, not just given the field
    sv_rule = ("analyst_supplied" in sc._SUPERVISOR_PREAMBLE
               and "DEPENDS on it" in sc._SUPERVISOR_PREAMBLE
               and "non-load-bearing" in sc._SUPERVISOR_PREAMBLE)
    good = (not clean) and (not with_decl) and in_schema and required and preamble and sv_rule
    return good, "schema=%s required=%s worker_preamble=%s supervisor_rule=%s" % (
        in_schema, required, preamble, sv_rule)


@check
def test_editor_files_faithful_report():
    """A report task the editor FILEs lands in filed_reports and becomes the
    tournament's input; an analysis task is curated, not filed."""
    rpt = {**good_payload("r001"), "summary": "report synthesizing t-analyses",
           "artifacts": [{"filename": "report.md", "version_id": "v-rep"}]}
    h = StubHost(
        worker_results=[ok(rpt)], supervisor_results=[ok(sv_payload("r001"))],
        llm_decisions=[{"decision": "FILE", "reason": "every claim traces to an accepted result"}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c",
                           tasks=[{"id": "r001", "stage": 1, "task": "write a report",
                                   "kind": "report"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="b", enable_triage=False, curate=True, enable_editor=True)
    filed = [r["task_id"] for r in rep["filed_reports"]]
    good = (filed == ["r001"] and not rep["held_reports"]
            and rep["filed_reports"][0]["artifacts"] == ["v-rep"])
    return good, "filed=%s held=%d artifacts=%s" % (
        filed, len(rep["held_reports"]), rep["filed_reports"][0]["artifacts"] if filed else None)


@check
def test_editor_holds_overstating_report():
    """A report the editor HOLDs is not filed, and its reason survives verbatim.

    Corrected 2026-10-15 (reviewer finding bc25d16a). The previous assertion
    was `(A and B and C) or (A and D and E)` where C tested
    `"overstate" in reason`. Because the second disjunct needed only a
    non-empty reason, C was optional -- and in fact FALSE here, since the
    canned reason is "headline claims more than the results show", which does
    not contain the substring "overstate". The test passed entirely on the
    fallback branch and would have passed with any reason at all.

    Asserting the reason mentions overstating would only test the stub's own
    canned string. The real contract is that the editor's reason reaches the
    caller unaltered, so that is what is checked now.
    """
    hold_reason = "headline claims more than the results show"
    rpt = {**good_payload("r002"), "summary": "report that overstates"}
    h = StubHost(
        worker_results=[ok(rpt)], supervisor_results=[ok(sv_payload("r002"))],
        llm_decisions=[{"decision": "HOLD", "reason": hold_reason}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c",
                           tasks=[{"id": "r002", "stage": 1, "task": "write a report",
                                   "kind": "report"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="b", enable_triage=False, curate=True, enable_editor=True)
    good = (not rep["filed_reports"]
            and len(rep["held_reports"]) == 1
            and rep["held_reports"][0]["reason"] == hold_reason)
    return good, "filed=%d held=%s reason=%r" % (
        len(rep["filed_reports"]), len(rep["held_reports"]),
        rep["held_reports"][0]["reason"][:40] if rep["held_reports"] else None)


@check
def test_editor_fails_closed():
    """An editor LLM call that returns nothing usable HOLDs, never files blind."""
    import science_campaign as _sc
    h = StubHost(llm_decisions=[{}])
    out = _sc.editor_review(h, {"task": "t"}, good_payload("r"), "")
    good = (out["decision"] == "HOLD" and out["reason"])
    return good, "decision=%s" % out["decision"]


@check
def test_curator_writes_and_injects():
    """An accepted task's findings land in the knowledge base, and a later task's
    worker prompt carries them (paper: entries included in later workers' prompts)."""
    # curator runs at accept (before triage); llm_decisions are popped in call
    # order, so: task1 curator -> task1 triage(release t001.f1) -> task1.f1 curator -> f1 triage(none)
    h = StubHost(
        worker_results=[ok({**good_payload("t001"),
                            "followups": [{"task": "f", "why": "w"}], "summary": "found that X holds"}),
                        ok(good_payload("t001.f1"))],
        supervisor_results=[ok(sv_payload("t001")), ok(sv_payload("t001.f1"))],
        llm_decisions=[{"entries": [{"claim": "X holds in all 56 entities", "evidence": "per_entity.csv"}]},
                       {"decision": "RELEASE", "reason": "builds on X", "refined_task": None},
                       {"entries": []}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="find X", enable_triage=True, curate=True)
        kb = json.loads((Path(d) / "c/knowledge.json").read_text())
    # the follow-up worker's prompt must contain the curated finding
    f1_prompts = [c["prompt"] or "" for c in h.llm_calls]  # not the channel; check the dispatch
    # worker prompts are in spawns' tasks
    f1_task = [s["task"] for s in h.spawns if s["name"] == "t001.f1"][0]
    good = (len(kb) == 1 and kb[0]["provenance_task_id"] == "t001"
            and "X holds in all 56 entities" in f1_task
            and "SHARED KNOWLEDGE BASE" in f1_task)
    return good, "kb_entries=%d provenance=%s injected_into_followup=%s" % (
        len(kb), kb[0]["provenance_task_id"] if kb else None,
        "X holds in all 56 entities" in f1_task)


@check
def test_curator_persists_across_resume():
    """knowledge.json survives a halt so a resumed run keeps the findings."""
    h = StubHost(
        worker_results=[ok({**good_payload("t001"), "summary": "s"})],
        supervisor_results=[ok(sv_payload("t001"))],
        llm_decisions=[{"entries": [{"claim": "finding A", "evidence": "e"}]}])
    # halt after wave 1 so t002 is left queued
    h.cost = lambda: 99.0 if len(h.spawns) >= 2 else 0.0
    with tempfile.TemporaryDirectory() as d:
        sc.run_stage(h, campaign_id="c",
                     tasks=[{"id": "t001", "stage": 1, "task": "x"},
                            {"id": "t002", "stage": 1, "task": "y"}],
                     out_root=d, root_frame_id=ROOT, cost_ceiling_usd=5.0,
                     context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                     brief="b", curate=True, enable_triage=False)
        kb_on_disk = json.loads((Path(d) / "c/knowledge.json").read_text())
        # resume: t002's worker prompt should carry finding A
        h2 = StubHost(worker_results=[ok({**good_payload("t002"), "summary": "s"})],
                      supervisor_results=[ok(sv_payload("t002"))],
                      llm_decisions=[{"entries": []}])
        sc.run_stage(h2, campaign_id="c",
                     tasks=[{"id": "t001", "stage": 1, "task": "x"},
                            {"id": "t002", "stage": 1, "task": "y"}],
                     out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                     context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                     brief="b", curate=True, enable_triage=False, resume=True)
        t2_task = [s["task"] for s in h2.spawns if s["name"] == "t002"][0]
    good = (len(kb_on_disk) == 1 and kb_on_disk[0]["claim"] == "finding A"
            and "finding A" in t2_task)
    return good, "kb_persisted=%d injected_on_resume=%s" % (
        len(kb_on_disk), "finding A" in t2_task)


@check
def test_knowledge_render_is_bounded():
    """render_knowledge caps entries and chars so per-child context can't grow
    without limit as the campaign extends."""
    big = [{"claim": "c%d" % i, "evidence": "e" * 500, "provenance_task_id": "t%d" % i}
           for i in range(200)]
    out = sc.render_knowledge(big, max_entries=40, max_chars=4000)
    good = (len(out) <= 4000 and out.count("\n- ") <= 40 and "c199" in out and "c0 " not in out)
    return good, "len=%d newest_present=%s oldest_absent=%s" % (
        len(out), "c199" in out, "c0 " not in out)


@check
def test_triage_releases_and_rejects():
    """A released follow-up becomes a new task and runs; a rejected one does not,
    and its written reason is recorded. This is the self-extension engine."""
    wp = good_payload("t001")
    wp["followups"] = [{"task": "good follow-up", "why": "advances the question"},
                       {"task": "dup follow-up", "why": "re-do prior work"}]
    h = StubHost(
        worker_results=[ok(wp), ok(good_payload("t001.f1"))],
        supervisor_results=[ok(sv_payload("t001")), ok(sv_payload("t001.f1"))],
        llm_decisions=[{"decision": "RELEASE", "reason": "novel and specific", "refined_task": None},
                       {"decision": "REJECT", "reason": "duplicate of prior work", "refined_task": None}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="find X", enable_triage=True, curate=False)
        released = [r["task_id"] for r in rep["released_followups"]]
        rejected = rep["rejected_followups"]
        ran = [s["name"] for s in h.spawns if not s["name"].startswith("sv-")]
    good = (released == ["t001.f1"] and len(rejected) == 1
            and rejected[0]["reason"] == "duplicate of prior work"
            and "t001.f1" in ran and set(rep["accepted"]) == {"t001", "t001.f1"})
    return good, "released=%s rejected=%s ran=%s accepted=%s" % (
        released, [r["reason"] for r in rejected], ran, sorted(rep["accepted"]))


@check
def test_followup_returned_for_revision_does_not_crash():
    """Regression: `revisions` is built from the INITIAL queue, so a triage-released
    follow-up has no entry. Indexing it raised KeyError the first time a FOLLOW-UP
    (not a seed task) was returned for revision, killing the loop mid-campaign.
    Measured 2026-10-13 on campaign-b: KeyError 'd002.f5' after 23 children."""
    wp = good_payload("t001")
    wp["followups"] = [{"task": "the follow-up", "why": "advances the question"}]
    h = StubHost(
        worker_results=[ok(wp),
                        ok(good_payload("t001.f1")),   # first follow-up attempt
                        ok(good_payload("t001.f1"))],  # after revision
        supervisor_results=[ok(sv_payload("t001")),
                            # the follow-up is REVISEd -> enters the revision branch
                            ok(sv_payload("t001.f1", decision="REVISE",
                                          changes=["tighten the margin"])),
                            ok(sv_payload("t001.f1"))],
        llm_decisions=[{"decision": "RELEASE", "reason": "novel", "refined_task": None}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="find X", enable_triage=True, curate=False,
                           max_revisions=2)
    ran = [s["name"] for s in h.spawns if not s["name"].startswith("sv-")]
    good = (set(rep["accepted"]) == {"t001", "t001.f1"}
            and ran.count("t001.f1") == 2      # ran once, revised, ran again
            and not rep["stalled"])
    return good, "accepted=%s followup_attempts=%d stalled=%d" % (
        sorted(rep["accepted"]), ran.count("t001.f1"), len(rep["stalled"]))


@check
def test_triage_depth_cap_stops_runaway():
    """A proposal from a task already at the depth limit is capped BEFORE any
    triage LLM call, so a release-everything model cannot self-extend forever."""
    wp = good_payload("deep")
    wp["followups"] = [{"task": "even deeper", "why": "w"}]
    h = StubHost(worker_results=[ok(wp)], supervisor_results=[ok(sv_payload("deep"))],
                 # triage WOULD release if consulted — the cap must pre-empt it
                 llm_decisions=[{"decision": "RELEASE", "reason": "would release", "refined_task": None}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c",
                           tasks=[{"id": "deep", "stage": 1, "task": "x", "followup_depth": 2}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="b", enable_triage=True, curate=False, max_followup_depth=2)
    cap_rejects = [r for r in rep["rejected_followups"] if r.get("by") == "cap"]
    good = (not rep["released_followups"] and len(cap_rejects) == 1
            and "depth cap" in cap_rejects[0]["reason"] and len(h.llm_calls) == 0)
    return good, "released=%d cap_rejects=%d llm_calls=%d (0 proves cap pre-empts LLM)" % (
        len(rep["released_followups"]), len(cap_rejects), len(h.llm_calls))


@check
def test_triage_campaign_cap():
    """max_followups bounds total releases across the campaign; proposals past
    the cap are pre-rejected without an LLM call."""
    wp = good_payload("t001")
    wp["followups"] = [{"task": "a", "why": "w"}, {"task": "b", "why": "w"}, {"task": "c", "why": "w"}]
    h = StubHost(worker_results=[ok(wp), ok(good_payload("t001.f1"))],
                 supervisor_results=[ok(sv_payload("t001")), ok(sv_payload("t001.f1"))],
                 llm_decisions=[{"decision": "RELEASE", "reason": "ok", "refined_task": None}])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1, wave_size=1,
                           brief="b", enable_triage=True, curate=False, max_followups=1)
    cap_rejects = [r for r in rep["rejected_followups"] if r.get("by") == "cap"]
    good = (len(rep["released_followups"]) == 1 and len(cap_rejects) == 2)
    return good, "released=%d cap_rejected=%d" % (len(rep["released_followups"]), len(cap_rejects))


@check
def test_triage_fails_closed():
    """An LLM call that returns nothing usable becomes a REJECT with a reason,
    never a silent release of an unjudged task."""
    import science_campaign as _sc
    h = StubHost(llm_decisions=[{}])  # empty -> no decision
    out = _sc.triage_followups(h, [{"task": "t", "why": "w", "parent_id": "p", "parent_depth": 0}],
                               brief="b")
    good = (len(out) == 1 and out[0]["decision"] == "REJECT" and out[0]["reason"])
    return good, "decision=%s reason=%r" % (out[0]["decision"], out[0]["reason"][:40])


@check
def test_stage_chain_advances_only_through_a_passing_gate():
    """Stage 2 opens only because stage 1's gate passed; the stage-2 worker runs."""
    h = StubHost(
        worker_results=[ok(good_payload("s1a")), ok(good_payload("s2a"))],
        supervisor_results=[ok(sv_payload("s1a")), ok(sv_payload("s2a"))])
    with tempfile.TemporaryDirectory() as d:
        camp = sc.run_campaign(
            h, campaign_id="c", out_root=d, root_frame_id=ROOT,
            cost_ceiling_usd=10 ** 6, context_ceiling_tokens=10 ** 9, brief="b",
            enable_triage=False, curate=False, wave_size=1,
            stages=[{"n": 1, "tasks": [{"id": "s1a", "stage": 1, "task": "x"}],
                     "gate": sc.stage_gate(min_accepted=1)},
                    {"n": 2, "tasks": [{"id": "s2a", "stage": 2, "task": "y"}],
                     "gate": sc.stage_gate(min_accepted=1)}])
    ran = [s["name"] for s in h.spawns if not s["name"].startswith("sv-")]
    good = (camp["completed_all_stages"] and len(camp["stages"]) == 2
            and all(s["gate"]["passed"] for s in camp["stages"]) and "s2a" in ran)
    return good, "stages=%d all_passed=%s s2_ran=%s" % (
        len(camp["stages"]), camp["completed_all_stages"], "s2a" in ran)


@check
def test_stage_chain_stops_on_failed_gate():
    """A stage-1 gate that fails (needs 2 accepted, gets 1) stops the chain —
    the stage-2 worker is NEVER dispatched."""
    h = StubHost(
        worker_results=[ok(good_payload("s1a"))],
        supervisor_results=[ok(sv_payload("s1a"))])
    with tempfile.TemporaryDirectory() as d:
        camp = sc.run_campaign(
            h, campaign_id="c", out_root=d, root_frame_id=ROOT,
            cost_ceiling_usd=10 ** 6, context_ceiling_tokens=10 ** 9, brief="b",
            enable_triage=False, curate=False, wave_size=1,
            stages=[{"n": 1, "tasks": [{"id": "s1a", "stage": 1, "task": "x"}],
                     "gate": sc.stage_gate(min_accepted=2)},
                    {"n": 2, "tasks": [{"id": "s2a", "stage": 2, "task": "y"}],
                     "gate": sc.stage_gate(min_accepted=1)}])
    ran = [s["name"] for s in h.spawns if not s["name"].startswith("sv-")]
    good = (not camp["completed_all_stages"] and camp["stopped_at"]["stage"] == 1
            and "gate_failed" in camp["stopped_at"]["why"] and "s2a" not in ran)
    return good, "stopped_at=%s s2_never_ran=%s" % (camp["stopped_at"], "s2a" not in ran)


@check
def test_tournament_soundness_autoloss():
    """A report scoring <=2 on soundness loses regardless of how high it scores
    elsewhere — the rule that stops exciting-but-unsound work from winning."""
    import science_tournament as T
    sound = {"impact": 3, "novelty": 3, "soundness": 4, "actionability": 3}
    flashy_unsound = {"impact": 5, "novelty": 5, "soundness": 2, "actionability": 5}
    good = (T.game_winner(sound, flashy_unsound) == "A"
            and T.game_winner(flashy_unsound, sound) == "B"
            and T.game_winner(sound, sound) == "tie")
    return good, "autoloss + tie handled"


@check
def test_tournament_ranking_recovers_known_order():
    """Bradley-Terry recovers a known ordering from hand-built games.

    Scope note: this test covers rank_from_games ONLY. It previously also
    claimed to verify that N reports give N*(N-1) games, via the conjunct
    `len(players) * (len(players) - 1) == 12` — which is 4*3==12, a fixed
    arithmetic identity that never referenced the games list and would have
    passed no matter how many games were built. The pair count is now measured
    against production in test_tournament_pair_count_is_measured.
    """
    import science_tournament as T
    players = ["S", "M1", "M2", "W"]
    games = []
    for x in ["M1", "M2", "W"]:
        games += [{"A": "S", "B": x, "winner": "A"}, {"A": x, "B": "S", "winner": "B"}]
    for x in ["M1", "M2"]:
        games += [{"A": x, "B": "W", "winner": "A"}, {"A": "W", "B": x, "winner": "B"}]
    games += [{"A": "M1", "B": "M2", "winner": "A"}, {"A": "M2", "B": "M1", "winner": "A"}]
    order = [r["id"] for r in T.rank_from_games(players, games)]
    good = order[0] == "S" and order[-1] == "W" and set(order[1:3]) == {"M1", "M2"}
    return good, "order=%s" % order


class _JudgeHost:
    """Minimal host honouring the LIST form of host.llm that run_tournament uses.

    StubHost.llm handles only the single-request signature and returns one dict,
    so it cannot drive run_tournament (which passes a list and zips the result
    against its pair list). `payload_for` lets a test return a malformed judge
    payload for chosen pairs, to exercise the drop path.
    """

    def __init__(self, payload, payload_for=None):
        self.payload = payload
        self.payload_for = payload_for or (lambda i: payload)
        self.n_requests = 0

    def llm(self, requests, max_concurrency=8, **kw):
        if isinstance(requests, dict) or isinstance(requests, str):
            requests = [requests]
        out = []
        for i, _ in enumerate(requests):
            self.n_requests += 1
            out.append({"tool_use": {"name": "score_pair", "input": self.payload_for(i)},
                        "text": "", "stop_reason": "tool_use"})
        return out


_GOOD_JUDGE = {"report_A": {"impact": 4, "novelty": 3, "soundness": 4, "actionability": 3},
               "report_B": {"impact": 2, "novelty": 2, "soundness": 3, "actionability": 2}}


@check
def test_tournament_pair_count_is_measured():
    """run_tournament issues and returns N*(N-1) games -- COUNTED, not asserted.

    Counts three independent things and requires they agree: how many judge
    requests the host actually received, len(result['games']), and the reported
    n_games. A formula identity cannot satisfy this.
    """
    import science_tournament as T
    reports = [{"id": c, "text": "report %s" % c} for c in ["A", "B", "C", "D"]]
    h = _JudgeHost(_GOOD_JUDGE)
    res = T.run_tournament(h, reports)
    n = len(reports)
    expected = n * (n - 1)
    good = (h.n_requests == expected
            and len(res["games"]) == expected
            and res["n_games"] == expected
            and res["games_dropped"] == 0)
    return good, "requests=%d games=%d n_games=%d expected=%d" % (
        h.n_requests, len(res["games"]), res["n_games"], expected)


@check
def test_tournament_drops_malformed_judge_payloads():
    """A judge call returning no scores is dropped, and the drop is counted.

    This path was untested: a malformed payload must not raise and must not be
    silently counted as a game.
    """
    import science_tournament as T
    reports = [{"id": c, "text": "report %s" % c} for c in ["A", "B", "C"]]
    bad_every_third = lambda i: ({} if i % 3 == 0 else _GOOD_JUDGE)  # noqa: E731
    h = _JudgeHost(_GOOD_JUDGE, payload_for=bad_every_third)
    res = T.run_tournament(h, reports)
    expected_total = len(reports) * (len(reports) - 1)
    expected_bad = sum(1 for i in range(expected_total) if i % 3 == 0)
    good = (res["games_dropped"] == expected_bad
            and res["n_games"] == expected_total - expected_bad
            and len(res["games"]) == res["n_games"])
    return good, "dropped=%d n_games=%d of %d" % (
        res["games_dropped"], res["n_games"], expected_total)


@check
def test_fork_is_refused():
    h = StubHost()
    try:
        sc._dispatch(h, [{"task": "x", "name": "n", "fork": True}])
        return False, "fork passed through"
    except AssertionError as exc:
        return "fork" in str(exc), str(exc)[:80]


@check
def test_oversize_wave_refused_before_spawning():
    h = StubHost()
    try:
        sc._dispatch(h, [{"task": "x", "name": "n"} for _ in range(49)])
        return False, "49 requests accepted"
    except ValueError as exc:
        return (not h.spawns), "%s | spawns=%d" % (str(exc)[:60], len(h.spawns))


@check
def test_result_json_carries_module_hash():
    h = StubHost(worker_results=[ok(good_payload())],
                 supervisor_results=[ok(sv_payload())])
    with tempfile.TemporaryDirectory() as d:
        sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                     out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                     context_ceiling_tokens=10 ** 9, stage=1)
        rec = json.loads((Path(d) / "c/tasks/t001/result.json").read_text())
        rev = json.loads((Path(d) / "c/tasks/t001/review.json").read_text())
    good = (len(rec.get("module_sha256") or "") == 64
            and rec["payload"]["task_id"] == "t001"
            and rev["payload"]["decision"] == "ACCEPT")
    return good, "hash=%s..." % str(rec.get("module_sha256"))[:12]


@check
def test_gate_is_not_run_in_the_loop():
    """Gates are vault-side; this kernel can reach neither the files nor a shell."""
    h = StubHost(worker_results=[ok(good_payload())], supervisor_results=[ok(sv_payload())])
    with tempfile.TemporaryDirectory() as d:
        rep = sc.run_stage(h, campaign_id="c", tasks=[{"id": "t001", "stage": 1, "task": "x"}],
                           out_root=d, root_frame_id=ROOT, cost_ceiling_usd=10 ** 6,
                           context_ceiling_tokens=10 ** 9, stage=1,
                           gate_command="python3 analysis/scripts/harness/science_stagecheck.py --verdict CONTROL --evaluable")
    good = (rep["gate_state"] == "not_run_here" and rep["gate_command"])
    return good, "gate_state=%s" % rep["gate_state"]


@check
def test_stop_condition_allowed_on_any_verdict_but_none():
    """A pre-registered stop can fire on any task kind, not only CONTROL.

    Regression for two real losses: campaign-b/d004 (verdict PATCH) and
    campaign-c/v003 (verdict PRE-REGISTRATION) were both rejected as
    `incoherent` with sound work and correct numbers, purely on this enum pairing.
    """
    bad = []
    for name in ("CONTROL", "PATCH", "PRE-REGISTRATION"):
        pl = {"verdict": {"name": name, "evaluable": True,
                          "outcome": "STOP_CONDITION_FIRED", "value": "v", "reason": "r"}}
        probs = [p for p in sc.check_worker_consistency(pl) if "STOP_CONDITION" in p]
        if probs:
            bad.append("%s rejected: %s" % (name, probs[0]))
    # NONE must still be refused: no verdict cannot carry an outcome.
    pl = {"verdict": {"name": "NONE", "evaluable": True,
                      "outcome": "STOP_CONDITION_FIRED", "value": "v", "reason": "r"}}
    if not any("STOP_CONDITION" in p for p in sc.check_worker_consistency(pl)):
        bad.append("NONE was accepted")
    return not bad, ("; ".join(bad) if bad else "3 verdicts accepted, NONE refused")


@check
def test_unprovenanced_referents_catches_real_fabrications():
    """Triage invented a referent in three consecutive campaigns; catch all three.

    Existence checking alone catches only one of them -- local_pilot.h5ad
    exists and is simply the wrong dataset -- so the rule is provenance against the
    parent's own output, not presence on disk.
    """
    prov = ("wrote state_definition_preregistration.json; fitted variants A1 A2 A3 A4 "
            "on ACC000333 and ACC000444")
    cases = {
        "A5": "compare the A5 variant against A3",
        "data/derived/corpus/annotation_scheme.json":
            "load data/derived/corpus/annotation_scheme.json and score it",
        "local_pilot.h5ad":
            "score ACC000333 (local_pilot.h5ad subset, 16,599 cells)",
    }
    bad = []
    for expected, text in cases.items():
        got = sc.unprovenanced_referents(text, prov)
        if expected not in got:
            bad.append("missed %s (got %s)" % (expected, got))
    # A follow-up naming only provenanced things must come back clean.
    clean = sc.unprovenanced_referents(
        "reload state_definition_preregistration.json and refit A3 on ACC000444", prov)
    if clean:
        bad.append("false positives on a clean task: %s" % clean)
    return not bad, ("; ".join(bad) if bad else "3 fabrications caught, clean task clean")


@check
def test_triage_is_shown_the_declared_stage_tasks():
    """Triage must see the campaign's own scripted tasks, so it can reject a duplicate.

    Regression for three consecutive campaigns in which a stage-1 follow-up restated
    the stage-2 validation (campaign-d/c001.f1, campaign-c/v001.f1,
    campaign-e/w001.f1). Each would have handed a FROZEN scheme a second
    attempt under looser framing than the registered criterion. This asserts the
    mechanical property -- the declared tasks reach the prompt; whether to reject is
    then the triage model's judgement, which is the right place for a question about
    meaning.
    """
    seen = {}

    class H:
        def llm(self, *a, **k):
            seen["prompt"] = (a[0] if a else k.get("prompt")) or ""
            return {"text": "", "content": []}

    sc.triage_followups(
        H(), [{"parent_id": "w001", "parent_depth": 0, "source": "worker",
               "task": "Score both datasets and report depth_control", "why": "x"}],
        brief="b",
        declared_tasks=[("w002", "VALIDATION GATE: apply the frozen scheme to both "
                                 "labelled datasets, depth_control reported first.")])
    p = seen.get("prompt", "")
    bad = []
    if "ALREADY HAS SCRIPTED" not in p:
        bad.append("no declared-task block in the prompt")
    if "VALIDATION GATE" not in p:
        bad.append("declared task text absent from the prompt")
    # And with no declared tasks supplied, the block must not appear at all.
    sc.triage_followups(
        H(), [{"parent_id": "t", "parent_depth": 0, "source": "worker",
               "task": "x", "why": "y"}], brief="b")
    if "ALREADY HAS SCRIPTED" in seen.get("prompt", ""):
        bad.append("block appeared with no declared_tasks")
    return not bad, ("; ".join(bad) if bad else "declared stage tasks reach triage")


@check
def test_stage_mismatch_raises_instead_of_silently_running_nothing():
    """A task list that filters to an empty queue must raise, not report success.

    Regression for defect #8. run_stage filters its queue by t["stage"] == stage.
    A task dict without the key matched nothing, so the stage returned in 0.0s with
    waves 0, spent $0.00 and gate "accepted 0 < required 1" -- indistinguishable in
    the report from a stage whose work genuinely failed its gate. It cost a silent
    no-op launch of campaign-f before the cause was found.

    Two properties: the mismatch raises, and run_campaign stamps the stage itself so
    the caller cannot hit it.
    """
    bad = []
    try:
        sc.run_stage(object(), campaign_id="x", tasks=[{"id": "a001", "task": "t"}],
                     out_root="/tmp/sc_selftest_nonexistent", root_frame_id="f",
                     cost_ceiling_usd=1.0, context_ceiling_tokens=10, stage=1)
        bad.append("no raise on stage mismatch")
    except ValueError as e:
        if "matched 0 of 1 supplied tasks" not in str(e):
            bad.append("raised but message unhelpful: %s" % str(e)[:80])
    except Exception as e:
        bad.append("raised %s, expected ValueError" % type(e).__name__)

    # run_campaign must stamp 'stage' so the above is unreachable from that path.
    tasks = [{"id": "a001", "task": "t"}]
    stages = [{"n": 1, "tasks": tasks, "gate": None}]
    try:
        sc.run_campaign(object(), campaign_id="x", stages=stages,
                        out_root="/tmp/sc_selftest_nonexistent", root_frame_id="f",
                        cost_ceiling_usd=1.0, context_ceiling_tokens=10, dry_run=True)
    except Exception:
        pass  # it will fail later for other reasons; we only care about the stamp
    if tasks[0].get("stage") != 1:
        bad.append("run_campaign did not stamp stage onto the task dict")
    return not bad, ("; ".join(bad) if bad else
                     "stage mismatch raises; run_campaign stamps stage")


@check
def test_none_verdict_may_carry_a_null_outcome():
    """A task that declares no hypothesis-verdict must not be forced to name an outcome.

    Regression for campaign-a: m000a (ACC000111, 127,782 cells, deviations
    ["none"]) and m000d (ACC000222, 93,028 cells) both staged their matrices, verified
    them, hash-recorded them, reported 9 and 15 findings, and set verdict.name "NONE"
    with outcome null -- the honest encoding for a staging job, which has no hypothesis.
    Both were rejected `incoherent` on the outcome-null rule and their work was lost to
    the stage gate. This is the same class as the STOP_CONDITION_FIRED/CONTROL defect
    that destroyed d004 and v003: a well-formed verdict refused over an enum pairing.

    The rule must still fire when a task NAMES a verdict, reports findings, and leaves
    the outcome null -- that is a genuine omission.
    """
    staging = {"verdict": {"evaluable": True, "outcome": None, "name": "NONE"},
               "findings": [{"claim": "staged 127782 cells", "evidence_command": "c"}],
               "commands_run": ["c"], "blocked": False}
    analytic = {"verdict": {"evaluable": True, "outcome": None, "name": "PRE-REGISTRATION"},
                "findings": [{"claim": "x", "evidence_command": "c"}],
                "commands_run": ["c"], "blocked": False}
    bad = []
    if sc.check_worker_consistency(staging):
        bad.append("NONE-verdict staging payload still rejected: %s"
                   % sc.check_worker_consistency(staging))
    if not any("outcome is null" in s for s in sc.check_worker_consistency(analytic)):
        bad.append("named-verdict payload with null outcome no longer flagged")
    # and a blocked task must still be caught by the evaluable/blocked rule
    blocked = {"verdict": {"evaluable": True, "outcome": None, "name": "NONE"},
               "findings": [], "blocked": True, "blocked_reason": "no network"}
    if not sc.check_worker_consistency(blocked):
        bad.append("blocked+evaluable no longer flagged")
    return not bad, ("; ".join(bad) if bad else
                     "NONE carries null outcome; named verdicts still must not")


def main() -> int:
    width = max(len(c.__name__) for c in CHECKS)
    failures = 0
    for c in CHECKS:
        try:
            passed, detail = c()
        except Exception as exc:  # a crash is a failure, not a traceback
            passed, detail = False, "%s: %s" % (type(exc).__name__, exc)
        failures += (not passed)
        print("%s  %-*s  %s" % ("PASS" if passed else "FAIL", width, c.__name__, detail))
    print("\n%d/%d checks passed" % (len(CHECKS) - failures, len(CHECKS)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
