#!/usr/bin/env python3
"""The Science harness dispatch loop — workers and supervisors on host.delegate().

WHAT THIS IS
------------
The campaign loop from `analysis/science-harness.md`, reimplemented on Claude
Science's sub-agent primitive instead of `claude -p` subprocesses. Design,
briefs, disqualifiers and stop conditions stay as `analysis/harness.md`
describes them; only the venue changes.

HOW IT IS RUN, AND WHY THAT IS ODD
----------------------------------
The kernel that can call `host.delegate()` (the control-plane kernel) CANNOT
read or write any host path — measured 2026-10-09, `PermissionError: [Errno 1]
Operation not permitted` on the vault AND on the Desktop, for read, stat, list
and write alike. The Python analysis kernel is the reverse: it can write the
vault but has no `host.delegate`.

So this file cannot be imported from the vault by the kernel that runs it.
`science_campaign_sync.py` copies it byte-for-byte into the session workspace
and records its SHA-256; the control-plane kernel imports THAT copy and stamps
the hash into every result it writes. The vault file is the source; the hash is
what makes "which version actually ran" answerable afterwards.

Two consequences that shape the API:

  * **Stdlib only.** The control-plane kernel runs `python -I -S` — no
    site-packages, so no pandas, no numpy, no third-party anything.
  * **A stage gate cannot run here.** Gates are vault-side scripts over vault
    files, and this kernel can reach neither. `run_stage()` therefore stops at
    the stage boundary and hands back the gate command; the caller runs it from
    the Python kernel (where it is a real file in `analysis/scripts/` executed
    from that file) and then calls `run_stage()` for the next stage.

WHAT THE LOOP GUARDS AGAINST, AND WHERE EACH GUARD CAME FROM
------------------------------------------------------------
1. **Only a schema-valid submission counts.** A child that never calls
   submit_output still returns `status="completed"`, with
   `structured_output_unsatisfied=True` and prose in `response`. Measured: the
   runtime re-prompts such a child three times and then lets it finish. Reading
   `response` as a result is how a stage closes on work that was never done, so
   `classify_result()` treats anything but a valid payload as a failure and the
   loop never reads `response` as data.
2. **The brake is denominated in a recorded unit.** Per-frame `total_cost` is
   readable the moment `collect()` returns and does not move afterwards
   (measured: identical at +1 s, +14 s and +90 s). The ceiling is checked
   BEFORE each wave, against the campaign-wide sum. CAVEAT: the platform runs
   its own review of each delegated output and that review's cost appears in no
   row this can see, so the sum is a floor, not a bill.
3. **The context guard.** The orchestrator's context compacts silently with no
   error; on the host that silent degradation cost $800 and 4.5 unattended
   hours. `_context_used` is readable, so the loop stops and hands back rather
   than carrying on degraded.
4. **Consecutive same-role failures.** Five in a row is that role's
   infrastructure being gone, not a task problem.
5. **`fork` is never passed.** `host.delegate()` accepts an undocumented
   `fork` flag whose host-side behaviour is unknown; if it copies the parent's
   context into the child it destroys the one property the design depends on —
   a reviewer that has not read the worker's reasoning. `_dispatch()` asserts
   it is absent from every request.

WHAT IT DOES NOT FIX
--------------------
Children run as a fresh context but the SAME model family, so a delegated
supervisor is an independent reader, not a cross-family one. And sub-agents are
LEAVES here: `host.delegate` is absent inside a child
(`AttributeError: host.delegate() is root-only and this frame is a sub-agent
(leaf)`), so a supervisor cannot delegate a verification sub-task — verification
is its own wave, dispatched from the top.

Usage (from the control-plane kernel, against the staged copy)::

    import science_campaign as sc
    report = sc.run_stage(
        host,
        campaign_id="demo-stage2",
        tasks=[{"id": "t001", "stage": 2, "task": "..."}],
        out_root="campaigns",
        root_frame_id=sc.detect_root_frame_id(host),
        cost_ceiling_usd=25.0,
        context_ceiling_tokens=400_000,
    )
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

MODULE_VERSION = "1.0"

# --- the vocabulary -------------------------------------------------------
# Verdict names are NOT invented here: these are the exact phrases
# analysis/campaigns/_brief_campaign-grammar-v1.md requires each task to carry
# (stage 2 lines 226-231, stage 3 lines 237-240). "NONE" is for the tasks that
# owe no named verdict — building the null, the discriminating test, the
# robustness recompute.
VERDICT_NAMES = [
    "CONTROL",
    "FAMILY",
    "DECOUPLING",
    "ENVELOPE",
    "DUAL-CONSTRAINT",
    "STATE-SWITCH",
    "EFFECTOR-CLASS",
    "PATCH",
    "PRE-REGISTRATION",
    "NONE",
]

# The outcome a verdict can carry. Deliberately small and typed: every field a
# gate reads must be a boolean or an enum, because the bug class this harness
# exists to remove is a gate matching prose. MIXED is required by the
# discriminating test, which partitions items into four classes and can be
# genuinely two-directional. STOP_CONDITION_FIRED is only meaningful for
# CONTROL and is rejected elsewhere by check_worker_consistency().
OUTCOMES = ["SUPPORTED", "NOT_SUPPORTED", "MIXED", "STOP_CONDITION_FIRED"]

CONFIDENCE = ["high", "medium", "low"]

MAX_DELEGATE_BATCH = 48  # host-side hard cap; 49 raises and spawns nothing


def worker_schema(verdict_names=None, outcomes=None):
    """JSON Schema for a worker's submission.

    Nested objects and arrays-of-objects are enforced — measured: every one of
    eight deliberate violations was rejected at submit, including a nested type
    error (`/verdict/evaluable: must be boolean`), a nested missing field, an
    enum miss inside an array item, and an extra key at both levels.

    `additionalProperties: false` at BOTH levels is load-bearing: it is what
    turns a misspelled field into a rejection instead of a silently ignored
    one.
    """
    names = list(verdict_names or VERDICT_NAMES)
    outs = list(outcomes or OUTCOMES)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "task_id", "summary", "commands_run", "artifacts", "findings",
            "verdict", "blocked", "blocked_reason", "followups",
            "analyst_supplied",
        ],
        "properties": {
            "task_id": {"type": "string"},
            "summary": {"type": "string"},
            # Every number in a result must come from a command actually run.
            "commands_run": {"type": "array", "items": {"type": "string"}},
            # Data the worker supplied from its OWN KNOWLEDGE rather than from a
            # staged input — citations, literature calls, parameter values,
            # thresholds. Measured 2026-10-11: a worker hand-entered 56
            # "experimental" entity x partner literature pairings with
            # citations, no database queried and no staged table; the reviewer
            # spot-checked one and found it mis-assigned, leaving 55 unverified.
            # Every mechanical check still passed, because the table was a
            # literal INSIDE the script the findings trace to. commands_run
            # proves a number came from a command; it says nothing about where
            # that command's inputs came from. This field is what a gate can
            # actually see.
            "analyst_supplied": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["what", "why_not_staged", "verifiable_how"],
                    "properties": {
                        "what": {"type": "string"},
                        "why_not_staged": {"type": "string"},
                        "verifiable_how": {"type": "string"},
                    },
                },
            },
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["filename", "version_id"],
                    "properties": {
                        "filename": {"type": "string"},
                        "version_id": {"type": "string"},
                    },
                },
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["claim", "evidence", "evidence_command", "confidence"],
                    "properties": {
                        "claim": {"type": "string"},
                        "evidence": {"type": "string"},
                        # An exact element of commands_run, not prose that
                        # mentions one. check_worker_consistency() enforces
                        # membership; a free-text "evidence names a command"
                        # rule is the prose-matching bug in another costume.
                        "evidence_command": {"type": "string"},
                        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                    },
                },
            },
            "verdict": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "evaluable", "outcome", "value", "reason"],
                "properties": {
                    "name": {"type": "string", "enum": names},
                    # "I could not run this" is a different outcome from "the
                    # answer is no". Conflating them is what let a stage close
                    # on a panel of NOT EVALUABLE results.
                    "evaluable": {"type": "boolean"},
                    "outcome": {"type": ["string", "null"], "enum": outs + [None]},
                    "value": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
            },
            "blocked": {"type": "boolean"},
            "blocked_reason": {"type": ["string", "null"]},
            "followups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task", "why"],
                    "properties": {"task": {"type": "string"}, "why": {"type": "string"}},
                },
            },
        },
    }


SUPERVISOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "decision", "checked", "unsupported_claims",
                 "required_changes", "notes", "followups", "confidence"],
    "properties": {
        "task_id": {"type": "string"},
        "decision": {"type": "string", "enum": ["ACCEPT", "REVISE"]},
        "checked": {"type": "array", "items": {"type": "string"}},
        # Non-blocking observations. Without this field the ONLY channel a
        # reviewer has is required_changes, which forces REVISE — and measured
        # 2026-10-10, that is exactly what happens: three reviewers in a row
        # returned REVISE on a correct trivial result (byte count 1371, right
        # every time, independently confirmed by each reviewer) over wording
        # and provenance phrasing, and the task stalled at max_revisions having
        # burned six children and $2.64. An unbounded ACCEPT bar makes a
        # correct result unacceptable.
        "notes": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["quote", "why"],
                # The quote, because the reviews that earned their keep on the
                # host all quoted: "the result's headline is 51 and 53" against
                # a shipped log reading 18 and 20.
                "properties": {"quote": {"type": "string"}, "why": {"type": "string"}},
            },
        },
        "required_changes": {"type": "array", "items": {"type": "string"}},
        # Follow-ups the REVIEW surfaced. The paper attributes its 98 follow-up
        # tasks to a mix of worker proposals and supervisor reviews, so the
        # supervisor gets the same channel the worker has. These go to triage
        # alongside the worker's own followups; default [] when the review
        # opens nothing.
        "followups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "why"],
                "properties": {"task": {"type": "string"}, "why": {"type": "string"}},
            },
        },
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
    },
}

WORKER_SCHEMA = worker_schema()


# --- cross-field rules the JSON Schema cannot express ---------------------
# Schema validation is enforced host-side at submit and covers types, enums,
# required fields and unknown keys. It cannot express "if evaluable is false
# then outcome must be null". Those rules live here and run parent-side, so a
# payload that is well-typed but incoherent is still a failure.

def check_worker_consistency(payload, expected_task_id=None):
    """Return a list of problems. Empty list means coherent."""
    p = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    v = payload.get("verdict") or {}
    evaluable = v.get("evaluable")
    outcome = v.get("outcome")
    name = v.get("name")

    # An evaluable task should normally say what the answer was. But the verdict
    # schema assumes a HYPOTHESIS-TESTING task, and not every scripted task is one:
    # a staging job that downloads, converts and audits a dataset has no categorical
    # outcome to report, and forcing one invites a worker to invent an enum value.
    # This cost two complete staging jobs: campaign-a/m000a (ACC000111,
    # 127,782 cells, deviations ["none"]) and m000d (ACC000222, 93,028 cells) were
    # both rejected as `incoherent` with their matrices written, verified and
    # hash-recorded on disk, purely for leaving outcome null.
    # So the rule now fires only when the payload claims an analytic result -- i.e.
    # it reported findings. A task that did work and simply has no outcome to name
    # is coherent; a task that asserts findings without an outcome is not. This
    # cannot hide a blocked task, because blocked work sets evaluable False.
    # name == "NONE" is the worker declaring there is no hypothesis-verdict here,
    # which is the correct call for a staging or audit task. Demanding an outcome
    # from a task that has explicitly said it has no verdict is self-contradictory,
    # and the rule below already treats NONE as incompatible WITH an outcome. Both
    # staging jobs set name NONE, outcome null, evaluable true, and reported their
    # measurements as findings -- internally coherent, and rejected anyway.
    if (evaluable is True and outcome is None and name != "NONE"
            and (payload.get("findings") or [])):
        p.append("verdict.evaluable is true and findings were reported under "
                 "verdict.name %r, but verdict.outcome is null" % (name,))
    if evaluable is False and outcome is not None:
        p.append("verdict.evaluable is false but verdict.outcome is %r" % (outcome,))
    # A stop condition is a PRE-REGISTERED rule ("if X, stop"), and such a rule can
    # be declared by, and fire on, any kind of task -- a pre-registration that fails
    # its own viability check, a patch that finds the thing it was patching is
    # unsalvageable. Restricting it to CONTROL cost this project two complete
    # analyses: campaign-b/d004 (verdict PATCH) and campaign-c/v003
    # (verdict PRE-REGISTRATION) were both rejected as `incoherent` with sound work
    # and correct numbers, purely on the enum pairing. Rejecting finished analysis
    # over a naming choice is a worse failure than an imprecise verdict name.
    # Only NONE remains incompatible: no verdict cannot carry an outcome.
    if outcome == "STOP_CONDITION_FIRED" and name == "NONE":
        p.append("STOP_CONDITION_FIRED requires a verdict, got name 'NONE'")
    if evaluable is False and not (v.get("reason") or "").strip():
        p.append("verdict.evaluable is false with an empty verdict.reason")

    blocked = payload.get("blocked")
    reason = payload.get("blocked_reason")
    if blocked is True and not (reason or "").strip():
        p.append("blocked is true with an empty blocked_reason")
    if blocked is False and reason is not None:
        p.append("blocked is false but blocked_reason is set")
    if blocked is True and evaluable is True:
        p.append("blocked is true but verdict.evaluable is true — could not run, yet evaluated")

    cmds = payload.get("commands_run") or []
    if not isinstance(cmds, list):
        p.append("commands_run is not a list")
        cmds = []
    for i, f in enumerate(payload.get("findings") or []):
        ec = f.get("evidence_command")
        if ec not in cmds:
            p.append("findings[%d].evidence_command is not in commands_run: %r" % (i, ec))
    if (payload.get("findings") and not cmds):
        p.append("findings present with an empty commands_run")

    if expected_task_id is not None and payload.get("task_id") != expected_task_id:
        p.append("task_id %r does not match the dispatched task %r"
                 % (payload.get("task_id"), expected_task_id))
    return p


def check_supervisor_consistency(payload, expected_task_id=None):
    p = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    decision = payload.get("decision")
    changes = payload.get("required_changes") or []
    if decision == "REVISE" and not changes:
        p.append("decision is REVISE with no required_changes")
    if decision == "ACCEPT" and changes:
        p.append("decision is ACCEPT but required_changes is non-empty")
    for i, c in enumerate(payload.get("unsupported_claims") or []):
        if not (c.get("quote") or "").strip():
            p.append("unsupported_claims[%d] has an empty quote" % i)
    if expected_task_id is not None and payload.get("task_id") != expected_task_id:
        p.append("task_id %r does not match the reviewed task %r"
                 % (payload.get("task_id"), expected_task_id))
    return p


# --- result classification -------------------------------------------------

def classify_result(result, schema_kind="worker", expected_task_id=None):
    """Map a delegate/collect result dict onto a single outcome word.

    Returns (state, problems). `state` is one of:
        ok            — completed AND carries a schema-valid, coherent payload
        unsatisfied   — completed WITHOUT calling submit_output (prose only)
        incoherent    — payload passed the schema but broke a cross-field rule
        failed        — the slot errored, was cancelled, or was orphaned
        running       — still running at the collect deadline

    Only "ok" counts as success anywhere in this loop. `response` is never
    read as data: a child that talked instead of submitting has not done the
    work, however plausible the prose.
    """
    if not isinstance(result, dict):
        return "failed", ["result is not a dict"]
    status = result.get("status")
    if status == "running":
        # PARKED IS NOT SLOW. A child that calls ask_user or hits a network /
        # host-access gate sits at "awaiting_user_response" until a human acts
        # on a card in the session UI. Measured 2026-10-11: a worker finished
        # a real stage-2 analysis, asked approval to Trash four files its own
        # test run had left in the vault root, and parked. The loop could not
        # tell that from a slow child, so at the deadline it stopped the child
        # — which DENIES the pending approval and discarded a completed
        # 16-minute analysis. Waiting longer would not have helped; only a
        # human could clear it.
        if result.get("child_status") == "awaiting_user_response":
            return "blocked_on_user", [
                "parked on a user approval card (ask_user / network / host access) — "
                "blocked on a human, not slow; do not stop it, that denies the approval"]
        return "running", ["still running at the collect deadline (child_status=%s)"
                           % result.get("child_status")]
    if status in ("failed", "cancelled", "orphaned"):
        return "failed", ["status=%s error=%s" % (status, result.get("error"))]
    if result.get("error"):
        return "failed", [str(result["error"])]
    if result.get("structured_output_unsatisfied"):
        return "unsatisfied", ["child completed without calling submit_output"]
    payload = result.get("structured_output")
    if payload is None:
        return "unsatisfied", ["no structured_output on a %s slot" % status]
    checker = check_worker_consistency if schema_kind == "worker" else check_supervisor_consistency
    problems = checker(payload, expected_task_id=expected_task_id)
    if problems:
        return "incoherent", problems
    return "ok", []


# --- the brake and the guards ---------------------------------------------

def detect_root_frame_id(host):
    """The orchestrating session's own frame id.

    There is no `host.frame_id`, so this infers it: the single root frame in
    this project currently processing. REFUSES when that is ambiguous rather
    than guessing, because a wrong root makes the cost brake sum the wrong
    tree — and a brake measuring the wrong thing is the $800 failure again.
    """
    r = host.query(
        "SELECT id FROM frames WHERE parent_frame_id IS NULL AND status='processing'"
        " ORDER BY updated_at DESC"
    )
    rows = [x[0] for x in r["rows"]]
    if len(rows) != 1:
        raise RuntimeError(
            "cannot infer root frame id unambiguously (%d candidates: %s) — pass "
            "root_frame_id= explicitly" % (len(rows), rows[:5])
        )
    return rows[0]


def campaign_cost(host, root_frame_id):
    """Recorded cost of the whole session tree, in USD.

    Includes the orchestrator's own spend and every child's. Measured: a
    child's total_cost is readable while it is still running and is final by
    the time collect() returns. UNDERCOUNTS by the platform's own review of
    each delegated output, which has no visible cost row.
    """
    r = host.query(
        "SELECT COUNT(*), COALESCE(SUM(total_cost), 0) FROM frames WHERE root_frame_id=?",
        params=[root_frame_id],
    )
    n, total = r["rows"][0]
    return float(total or 0.0), int(n or 0)


def context_used(host, root_frame_id):
    """(tokens_in_context, compaction_count) for the orchestrator itself."""
    r = host.query(
        "SELECT json_extract(context_data,'$._context_used'),"
        "       json_extract(context_data,'$._compaction_count')"
        " FROM frames WHERE id=?",
        params=[root_frame_id],
    )
    if not r["rows"]:
        return None, None
    used, comp = r["rows"][0]
    return (int(used) if used is not None else None,
            int(comp) if comp is not None else None)


class Halt(Exception):
    """Raised to stop a stage. `kind` is machine-readable; `detail` is for a human."""

    def __init__(self, kind, detail, state=None):
        super().__init__("%s: %s" % (kind, detail))
        self.kind = kind
        self.detail = detail
        self.state = state or {}


# --- dispatch --------------------------------------------------------------

def _dispatch(host, requests):
    """Spawn a wave. The only place host.delegate is called.

    Asserts `fork` is absent from every request and from the call. The flag is
    undocumented, its host-side behaviour unverified, and if it copies the
    parent's context into the child then every supervisor in this loop has
    already read the worker's reasoning and the reviews are worthless.
    """
    for i, r in enumerate(requests):
        assert "fork" not in r, "requests[%d] carries fork=; forbidden in this harness" % i
    if len(requests) > MAX_DELEGATE_BATCH:
        raise ValueError(
            "wave of %d exceeds the host cap of %d per delegate() call"
            % (len(requests), MAX_DELEGATE_BATCH)
        )
    descs = host.delegate(requests, wait=False)
    if isinstance(descs, dict):  # single-request form mirrors the input
        descs = [descs]
    return descs


def _collect(host, frame_ids, timeout, slice_seconds=60):
    """Collect in short slices so the kernel frees between them.

    collect() is deadline-bounded (max 1800 s) and a long blocking collect
    holds the control-plane kernel, which blocks steering. Short slices keep
    the loop interruptible.
    """
    pending = list(frame_ids)
    done = {}
    last_seen = {}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        window = min(slice_seconds, max(1, int(deadline - time.time())))
        for res in host.collect(pending, timeout=window):
            fid = res.get("frame_id")
            if res.get("status") != "running":
                done[fid] = res
            else:
                # KEEP the running descriptor. It carries child_status, which
                # is the ONLY way to tell a child parked on a user approval
                # from a merely slow one. Synthesizing a placeholder here threw
                # that away and made the two indistinguishable — measured in
                # the 2026-10-11 pilot, where the parked child was stopped and
                # its finished analysis lost.
                last_seen[fid] = res
        pending = [f for f in pending if f not in done]
    for fid in pending:  # still running at the deadline
        done[fid] = last_seen.get(fid) or {"frame_id": fid, "status": "running",
                                           "child_status": "unknown-at-deadline"}
    return [done[f] for f in frame_ids]


# --- persistence -----------------------------------------------------------

def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, default=str)
    os.replace(tmp, path)


def _load_state(base):
    """Read a campaign's state.json, or None when there is nothing to resume."""
    path = os.path.join(base, "state.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _module_hash():
    """SHA-256 of the running copy of this module, stamped into every result."""
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        return None


# --- prompts ---------------------------------------------------------------

_WORKER_PREAMBLE = """You are a worker on a research campaign. Do the task below and submit a structured result.

Hard rules, from the vault this campaign belongs to:
- Every number you report must come from a command you actually ran, from a script saved under {script_dir}. List each command verbatim in commands_run, and give every finding an evidence_command that is EXACTLY one of those strings.
- A negative result is a result. "The grammar is not conserved here" and "the ortholog set is too shallow to test this" are both good answers.
- If you could not run the work at all, set blocked=true with a reason and verdict.evaluable=false. Do not dress a blocked task as a negative finding.
- Do not re-derive staged inputs. If you believe a staged input is wrong, say so in a finding rather than rebuilding it.
- Save tables as artifacts and list them in artifacts as {{filename, version_id}}; do not paste large tables into the payload.
- ANY data you supply from your own knowledge rather than from a staged input — a citation, a literature call, a threshold, a parameter — MUST be listed in analyst_supplied, including when you embed it as a literal in your script. Hardcoding a table into the script does NOT make it traceable: commands_run shows a number came from a command, not where that command's inputs came from. If analyst_supplied is empty, you are asserting every input came from a staged file. Prefer declaring it to omitting it.
  (Braces doubled: this string goes through .format() for script_dir, so a literal brace must be escaped or it is read as a field name.)

TASK
"""

_SUPERVISOR_PREAMBLE = """You are a supervisor reviewing one worker's result on a research campaign. You have NOT seen the worker's reasoning — only its submitted result and the task it was given. Judge what is in front of you.

What to check, in this order:
1. Does every number in the result trace to a command in commands_run?
2. Does the verdict follow from the evidence given, or does it overreach?
3. Is anything asserted that the stated method cannot establish (mechanism from sequence, recruitment from a motif match, a causal order from a static measurement)?
4. Is a blocked or non-evaluable outcome being reported as a negative finding?

Quote the exact text of anything you flag — a review that paraphrases cannot be checked.

PROPORTIONALITY — read this before you decide, it is the part reviewers get wrong.
REVISE is for defects that change something: a number that does not trace to a command, a verdict the evidence does not support, a claim the stated method cannot establish, or a blocked/non-evaluable outcome dressed as a finding. If none of those is present, return ACCEPT.

Wording, phrasing, how a command is labelled in commands_run, how precisely verdict.reason is scoped, formatting, and anything you would describe as tightening or clarifying are NOT grounds for REVISE. Put them in `notes`, which travels with an ACCEPT and blocks nothing. A revision round costs a full worker and a full review, so spend one only to fix something that is wrong.

A result can be accepted while imperfect. Returning REVISE on a correct result because it could be phrased better is itself a defect: a task that cannot be accepted stalls, and the campaign loses the answer it already had.

UNVERIFIABLE DATA THE WORKER SUPPLIED ITSELF (the analyst_supplied field). A worker may hand in data from its own knowledge — a citation, a literature call, a threshold, a parameter — rather than from a staged input. The rule turns on whether a verdict or finding DEPENDS on it:
- If a verdict or a finding rests on analyst_supplied data that you cannot independently verify from the staged inputs, return REVISE and require either that the claim be dropped, that the verdict be re-scoped so it no longer depends on that data, or that the data be grounded in a staged/verifiable source. A conclusion resting on an unverifiable table the worker typed from memory is exactly the trap this harness exists to catch — a hardcoded literal is not evidence.
- If the analyst_supplied data is declared and NO verdict or finding leans on it, that is a note, not a REVISE. Say in `notes` that it is unverified and non-load-bearing.
Spot-check what you can: if you check one analyst_supplied item and it is wrong, say so with the quote and treat the rest of that layer as unverified.

THE TASK THE WORKER WAS GIVEN
{task}

THE WORKER'S SUBMITTED RESULT (JSON)
{payload}
"""


_KNOWLEDGE_BLOCK = (
    "\n\nSHARED KNOWLEDGE BASE — findings earlier tasks in this campaign already established. "
    "Use them; do not re-derive or contradict them without saying why:\n%s\n")


def build_worker_request(task, schema, model=None, script_dir="analysis/scripts/", knowledge=""):
    """`script_dir` is where the worker must save the scripts its numbers come from.

    It is a parameter because a worker obeys this preamble over the task text,
    and that was measured: in the 2026-10-09 smoke run a task that said "do not
    write to the vault" still produced `analysis/scripts/r001_input_hash.py`
    and a CSV under `data/derived/`, because the preamble told it to save a
    script there and the preamble won. For real campaigns the vault path is
    correct; an infrastructure or smoke run must pass its own workspace instead
    rather than relying on the task text to countermand this line.

    `knowledge` is the curator's running knowledge base, injected so a worker
    builds on what earlier tasks found (paper: entries included in later
    workers' prompts).
    """
    kb = (_KNOWLEDGE_BLOCK % knowledge) if knowledge else ""
    req = {
        "task": _WORKER_PREAMBLE.format(script_dir=script_dir) + kb + task["task"]
        + "\n\nSet task_id to exactly: %s" % task["id"],
        "name": task.get("name") or task["id"],
        "output_schema": schema,
    }
    if model:
        req["model"] = model
    return req


def build_supervisor_request(task, payload, model=None, knowledge=""):
    kb = (_KNOWLEDGE_BLOCK % knowledge) if knowledge else ""
    req = {
        "task": _SUPERVISOR_PREAMBLE.format(
            task=task["task"], payload=json.dumps(payload, indent=1, default=str)
        ) + kb + "\n\nSet task_id to exactly: %s" % task["id"],
        "name": "sv-%s" % task["id"],
        "output_schema": SUPERVISOR_SCHEMA,
    }
    if model:
        req["model"] = model
    return req


# --- triage: the engine that makes a campaign self-extend --------------------
#
# The paper's harness is 98 follow-ups out of 119 tasks — the work is mostly
# what the agents proposed as they went, not what the brief seeded. A proposed
# follow-up enters a triage queue "from which the harness released or rejected
# with a written reason" (Methods p.28; 10 of 119 were rejected here). This is
# that step.
#
# Triage is a host.llm call, not a delegated sub-agent: it is a stateless
# judgement over text (brief + proposal + knowledge base), with no workspace
# and no tools, so a delegated session would be pure overhead. Worker and
# supervisor stay delegated because they must RUN and independently VERIFY
# analysis; triage, curator and editor only read and decide.

TRIAGE_TOOL = {
    "name": "triage",
    "description": "Release a proposed follow-up task into the work queue, or reject it, with a reason.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "reason"],
        "properties": {
            "decision": {"type": "string", "enum": ["RELEASE", "REJECT"]},
            "reason": {"type": "string"},
            # Triage may tighten a vague proposal into a runnable task; null
            # keeps the proposal as written.
            "refined_task": {"type": ["string", "null"]},
        },
    },
}

_TRIAGE_SYSTEM = (
    "You are the triage step of an autonomous research campaign. You decide whether a PROPOSED "
    "follow-up task enters the work queue (RELEASE) or does not (REJECT), and you give a written "
    "reason either way. RELEASE only a proposal that advances the campaign's own question, is not "
    "already answered by the knowledge base, stays inside the brief's scope, and is specific enough "
    "to hand to a worker as written. REJECT duplicates, work the knowledge base shows is already "
    "done, scope creep beyond the brief, and proposals too vague to run. Rejecting is normal and "
    "expected: most proposals should not run, and a campaign that releases everything never finishes. "
    "If a proposal is sound but loosely worded, RELEASE it and put a tightened version in refined_task."
)


def _llm_tool(host, system, prompt, tool, model=None, max_tokens=800):
    """One structured host.llm call forced through `tool`; returns the input dict.

    Verified 2026-10-11: host.llm(prompt=, system=, tools=[tool],
    tool_choice={"type":"tool","name":tool["name"]}) returns
    {"tool_use": {"name", "input"}, "text", ...}; input is the parsed object.
    Falls back to parsing JSON out of `text` if tool_use is absent, and returns
    {} if neither works — callers treat {} as fail-closed.
    """
    r = host.llm(prompt=prompt, system=system, tools=[tool],
                 tool_choice={"type": "tool", "name": tool["name"]},
                 model=model, max_tokens=max_tokens)
    if not isinstance(r, dict):
        return {}
    tu = r.get("tool_use")
    if isinstance(tu, dict) and isinstance(tu.get("input"), dict):
        return tu["input"]
    txt = r.get("text") or ""
    try:
        return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    except Exception:
        return {}


_REFERENT_RE = re.compile(
    r"""(?:
          [A-Za-z0-9_./-]+\.(?:json|csv|tsv|txt|h5ad|rds|RDS|py|md|gz|parquet)\b  # files
        | \bGSE\d+\b | \bGSM\d+\b                                                # accessions
        | \b[A-Z]\d{1,2}\b                                                       # variant ids (A1, S7)
    )""",
    re.X,
)


def unprovenanced_referents(task_text, provenance_text):
    """Referents a follow-up names that appear nowhere in its own provenance.

    Triage composes follow-up text with an LLM, and in three consecutive campaigns it
    invented a referent that did not exist in the parent's work:
      * campaign-b  -- an axis variant "A5"; only A1-A4 were ever fitted.
      * campaign-d   -- "annotation_scheme.json"; the file written was
                             state_definition_preregistration.json.
      * campaign-c -- "local_pilot.h5ad" named as ACC000333's source; that
                             file is a retired earlier corpus, a different
                             dataset entirely.
    Existence checking alone catches only the second: the third path exists and is simply
    the wrong referent. What catches all three is provenance -- a follow-up may only name
    things that appear in the parent result, the brief, or the knowledge base. Anything
    else was invented between the parent's output and the proposal.

    Returns the sorted list of unprovenanced referents (empty when clean).
    """
    found = {m.group(0) for m in _REFERENT_RE.finditer(task_text or "")}
    if not found:
        return []
    hay = provenance_text or ""
    return sorted(r for r in found if r not in hay)


def triage_followups(host, proposals, brief, knowledge="", model=None, max_tokens=800,
                     declared_tasks=None):
    """Judge each proposed follow-up. Returns one decision dict per proposal:
    {proposal, decision ('RELEASE'|'REJECT'), reason, refined_task}.

    Fail-closed: an LLM call that returns nothing usable becomes a REJECT with a
    stated reason, never a silent RELEASE of an unjudged task.

    `declared_tasks` is [(task_id, task_text), ...] for the campaign's own scripted
    stages. Triage is shown them and told to reject a proposal that restates one.
    Without this, triage proposed a duplicate of its own declared stage-2 validation
    in THREE consecutive campaigns (campaign-d/c001.f1, campaign-c/v001.f1,
    campaign-e/w001.f1). That is a correctness problem, not a cost problem: the
    declared stage task validates a FROZEN scheme against a criterion fixed before any
    result existed, and a follow-up re-doing it hands the scheme a second attempt under
    looser framing. Duplication is a judgement about meaning, so it belongs in the
    triage model rather than in the structural caps above it.
    """
    kb = (("\n\nKNOWLEDGE BASE — findings already recorded; REJECT anything these already answer:\n"
           + knowledge) if knowledge else "")
    dt = ""
    if declared_tasks:
        dt = ("\n\nTASKS THIS CAMPAIGN ALREADY HAS SCRIPTED. REJECT any proposal that asks for "
              "the same deliverable as one of these, even if it is worded differently or adds "
              "detail — the scripted task is the registered one and must stay the single "
              "authority on its question:\n"
              + "\n".join("  [%s] %s" % (tid, (txt or "").strip()[:400].replace("\n", " "))
                          for tid, txt in declared_tasks))
    out = []
    for p in proposals:
        prompt = ("CAMPAIGN BRIEF:\n%s%s%s\n\nPROPOSED FOLLOW-UP (proposed by the %s of task %s):\n"
                  "task: %s\nwhy: %s\n\nRelease or reject it."
                  % (brief or "(no brief text supplied)", kb, dt, p.get("source", "worker"),
                     p.get("parent_id", "?"), p.get("task", ""), p.get("why", "")))
        d = _llm_tool(host, _TRIAGE_SYSTEM, prompt, TRIAGE_TOOL, model=model, max_tokens=max_tokens)
        decision = d.get("decision")
        if decision not in ("RELEASE", "REJECT"):
            decision, d = "REJECT", {"reason": "triage returned no usable decision — rejected by default"}
        out.append({"proposal": p, "decision": decision,
                    "reason": d.get("reason", ""), "refined_task": d.get("refined_task")})
    return out


# --- curator: the shared knowledge base that feeds findings forward ---------
#
# Paper (Methods p.28): "After each completed task, a curator agent read the
# task brief and the worker's summary and entered the findings into a shared
# knowledge base. Relevant entries were included in the prompts of later
# workers and supervisors." Without it, every wave starts blind and a later
# worker re-derives (or contradicts) what an earlier one already established.
#
# Like triage, the curator is a host.llm call — it reads text and extracts
# findings; it needs no workspace. The knowledge base is a JSON file in the
# campaign dir, so it survives a resume.

CURATOR_TOOL = {
    "name": "curate",
    "description": "Extract the durable, reusable findings from a completed task.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entries"],
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["claim", "evidence"],
                    "properties": {"claim": {"type": "string"}, "evidence": {"type": "string"}},
                },
            },
        },
    },
}

_CURATOR_SYSTEM = (
    "You are the curator of an autonomous research campaign. Read a completed task's brief and the "
    "worker's result summary and verdict, and extract the durable, reusable findings — the facts a "
    "LATER worker or reviewer would need and should not have to re-derive. Each entry is one specific "
    "claim with its evidence. Record negatives and limits too: what did NOT hold, what could not be "
    "tested, which input was unusable. Do NOT record process narration, restated task text, or vague "
    "generalities — only findings a future task would act on. Return an empty list if the task "
    "produced nothing reusable."
)


def curate_task(host, task, payload, model=None, max_tokens=900):
    """Extract knowledge-base entries from an accepted task. Each entry is
    tagged with the task id that produced it, for provenance."""
    v = payload.get("verdict") or {}
    prompt = ("TASK BRIEF:\n%s\n\nWORKER SUMMARY:\n%s\n\nVERDICT: %s (evaluable=%s, outcome=%s) — %s\n\n"
              "Extract the durable findings a later task would reuse."
              % (task.get("task", ""), payload.get("summary", ""), v.get("name"),
                 v.get("evaluable"), v.get("outcome"), v.get("reason", "")))
    d = _llm_tool(host, _CURATOR_SYSTEM, prompt, CURATOR_TOOL, model=model, max_tokens=max_tokens)
    entries = d.get("entries") or []
    out = []
    for e in entries:
        if isinstance(e, dict) and e.get("claim"):
            out.append({"claim": e["claim"], "evidence": e.get("evidence", ""),
                        "provenance_task_id": task["id"]})
    return out


def render_knowledge(kb_entries, max_entries=40, max_chars=4000):
    """A bounded rendering of the knowledge base for prompt injection.

    Bounded on purpose: every child pays the ~50k-token context tax already
    (measured), and an unbounded knowledge base would grow that per-child cost
    without limit as the campaign extends. Newest entries first, capped.
    """
    if not kb_entries:
        return ""
    lines = []
    for e in reversed(kb_entries[-max_entries:]):
        line = "- %s [evidence: %s] (task %s)" % (
            e.get("claim", ""), (e.get("evidence", "") or "")[:200], e.get("provenance_task_id", "?"))
        lines.append(line)
    text = "\n".join(lines)
    return text[:max_chars]


def _load_knowledge(base):
    path = os.path.join(base, "knowledge.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_knowledge(base, kb_entries):
    _write_json(os.path.join(base, "knowledge.json"), kb_entries)


# --- editor: reviews a report before it is filed ----------------------------
#
# Paper (Methods p.28): "Reports were reviewed by an editor agent before
# filing." 22 report tasks produced 19 filed reports. The editor is the last
# guard before a report becomes a tournament entry, and it checks the one thing
# a supervisor's per-task review cannot: does the REPORT tell the truth about
# the results it is built on, taken together. A report task is an ordinary
# worker task tagged kind="report"; after its supervisor accepts it, the editor
# decides FILE or HOLD. Only filed reports reach the tournament.

EDITOR_TOOL = {
    "name": "edit_decision",
    "description": "File a campaign report, or hold it, with a reason.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "reason"],
        "properties": {
            "decision": {"type": "string", "enum": ["FILE", "HOLD"]},
            "reason": {"type": "string"},
        },
    },
}

_EDITOR_SYSTEM = (
    "You are the editor of an autonomous research campaign, deciding whether a report may be FILED "
    "or must be HELD, with a reason. FILE only if the report tells the truth about the results it is "
    "built on: every headline claim is supported by an accepted result it cites, nothing overstates "
    "what the analysis actually showed, and a negative or limited result is reported as such rather "
    "than dressed up as a positive. HOLD a report that overstates its evidence, asserts what its "
    "stated method cannot establish, reports a blocked or non-evaluable outcome as a finding, or "
    "whose numbers do not match the results it cites. Holding is a normal outcome; a filed report is "
    "one you would stand behind."
)


def editor_review(host, task, payload, sources_text="", model=None, max_tokens=700):
    """Decide FILE/HOLD for an accepted report task. `sources_text` is a bounded
    rendering of the accepted results the report draws on."""
    v = payload.get("verdict") or {}
    findings = payload.get("findings") or []
    ftext = "\n".join("- %s" % (f.get("claim", "")) for f in findings[:30])
    src = ("\n\nACCEPTED RESULTS THIS REPORT DRAWS ON:\n" + sources_text) if sources_text else ""
    prompt = ("REPORT TASK:\n%s\n\nREPORT SUMMARY:\n%s\n\nREPORT FINDINGS:\n%s\n\nVERDICT: %s "
              "(evaluable=%s, outcome=%s) — %s%s\n\nFile it or hold it."
              % (task.get("task", ""), payload.get("summary", ""), ftext, v.get("name"),
                 v.get("evaluable"), v.get("outcome"), v.get("reason", ""), src))
    d = _llm_tool(host, _EDITOR_SYSTEM, prompt, EDITOR_TOOL, model=model, max_tokens=max_tokens)
    decision = d.get("decision")
    if decision not in ("FILE", "HOLD"):
        decision, d = "HOLD", {"reason": "editor returned no usable decision — held by default"}
    return {"decision": decision, "reason": d.get("reason", "")}


# --- the loop --------------------------------------------------------------

def run_stage(host, *, campaign_id, tasks, out_root, root_frame_id,
              cost_ceiling_usd, context_ceiling_tokens,
              stage=None, wave_size=4, max_revisions=2,
              max_consecutive_role_failures=5, collect_timeout=1800,
              collect_slice=60, worker_model=None, supervisor_model=None,
              verdict_names=None, gate_command=None, dry_run=False,
              on_timeout="stop", max_consecutive_timeouts=3,
              script_dir="analysis/scripts/", resume=False,
              brief="", enable_triage=True, triage_model=None,
              max_followup_depth=2, max_followups=50, knowledge="",
              curate=True, curator_model=None,
              enable_editor=True, editor_model=None,
              declared_tasks=None):
    """Run one stage to exhaustion, or until a guard halts it.

    Returns a report dict; never raises on a guard trip — the halt is recorded
    in report["halt"] so the caller can act on it. Writes one result.json per
    task and a state.json per campaign under
    `<out_root>/<campaign_id>/`, which `science_campaign_sync.py` mirrors into
    the vault.
    """
    schema = worker_schema(verdict_names=verdict_names)
    base = os.path.join(out_root, campaign_id)
    started = time.time()
    mod_hash = _module_hash()

    queue = [dict(t) for t in tasks if stage is None or t.get("stage") == stage]

    # A non-empty task list that filters to an empty queue is always a caller
    # error, and silently returning an empty report hides it: the stage reports
    # 0 waves, $0.00 and "gate_failed", which reads like a scientific outcome
    # rather than a dispatch that never happened. Fail loudly instead, naming the
    # mismatch. (Resume legitimately empties the queue -- every task is done --
    # so this only guards the pre-resume construction.)
    if tasks and not queue:
        raise ValueError(
            "run_stage(stage=%r) matched 0 of %d supplied tasks: their stage keys are "
            "%r. Tasks are filtered by t['stage'] == stage; set it, or call via "
            "run_campaign which stamps it from the stage's 'n'."
            % (stage, len(tasks), sorted({t.get("stage") for t in tasks}, key=str)))

    prior = _load_state(base) if resume else None
    report = {
        "campaign_id": campaign_id,
        "stage": stage,
        "module_version": MODULE_VERSION,
        "module_sha256": mod_hash,
        "root_frame_id": root_frame_id,
        "started_at": started,
        "cost_ceiling_usd": cost_ceiling_usd,
        "context_ceiling_tokens": context_ceiling_tokens,
        "wave_size": wave_size,
        "dry_run": bool(dry_run),
        "waves": [],
        "accepted": [],
        "stalled": [],
        # Children still running when collect gave up. Their frame ids are kept
        # so the work is retrievable with host.collect([...]) afterwards —
        # losing the id is what makes a slow child an abandoned one.
        "timed_out": [],
        # Tasks parked on a human approval card. Never stopped, never counted
        # as a failure — the stage halts and these ids stay collectable.
        "blocked_on_user": [],
        # Tasks whose worker failed. Retried on resume — a failure is usually
        # transient, and the consecutive-failure halt is what catches a dead role.
        "failed": [],
        # Follow-ups proposed during the stage: released ones became new tasks,
        # rejected ones carry triage's written reason. The paper's self-extension
        # (98 of 119 tasks) and its 10 triage rejections live here.
        "released_followups": [],
        "rejected_followups": [],
        # Report tasks the editor filed (the tournament's input set) and held
        # (kept with the editor's reason). Paper: 22 report tasks -> 19 filed.
        "filed_reports": [],
        "held_reports": [],
        "on_timeout": on_timeout,
        "enable_triage": enable_triage,
        "script_dir": script_dir,
        "halt": None,
        "gate_command": gate_command,
        "gate_state": "not_run_here",  # gates are vault-side; see module docstring
        "resumed_from": None,
    }

    # RESUME. Every guard in this loop halts mid-stage by design — the cost
    # ceiling in particular is expected to fire on a real campaign — and
    # without a way back in, a halt means reconstructing the queue by hand and
    # re-running work that was already accepted and already paid for.
    # state.json carries the pending tasks with their full (possibly
    # revision-augmented) text, so a resume continues where the halt left off.
    if prior:
        done = (set(prior.get("accepted") or [])
                | {s["task_id"] for s in (prior.get("stalled") or [])})
        # Failed tasks are retried on resume, ahead of anything still queued.
        pending = prior.get("queued_tasks")
        if pending is not None:
            pending = list(prior.get("failed_tasks") or []) + list(pending)
        if pending is not None:
            # Prefer the saved text: a task halted mid-revision carries the
            # reviewer's required changes appended, and dropping that would
            # silently re-dispatch the unrevised version.
            # Dedupe by id, first occurrence wins — defence in depth against a
            # task appearing in both failed_tasks and queued_tasks.
            seen, deduped = set(), []
            for t in pending:
                if t["id"] in seen or t["id"] in done:
                    continue
                seen.add(t["id"])
                deduped.append(dict(t))
            queue = deduped
        else:
            queue = [t for t in queue if t["id"] not in done]
        report["accepted"] = list(prior.get("accepted") or [])
        report["stalled"] = list(prior.get("stalled") or [])
        report["resumed_from"] = {
            "accepted_carried": len(report["accepted"]),
            "stalled_carried": len(report["stalled"]),
            "queued": [t["id"] for t in queue],
            "state_updated_at": prior.get("updated_at"),
            "halted_on": prior.get("halted_on"),
        }

    if dry_run:
        report["planned_tasks"] = [t["id"] for t in queue]
        report["finished_at"] = time.time()
        _write_json(os.path.join(base, "report_stage%s.json" % stage), report)
        return report

    revisions = {t["id"]: 0 for t in queue}
    if prior and prior.get("revisions"):
        revisions.update({k: v for k, v in prior["revisions"].items() if k in revisions})
    consecutive = {"worker": 0, "supervisor": 0, "timeout": 0}
    wave = []  # referenced by the Halt handler; a halt can fire before wave 1
    failed_tasks = []  # task dicts whose worker failed, kept so resume can retry them
    # Monotonic across the campaign AND across a resume, so a released follow-up
    # never collides with an id from before the halt.
    n_followups_released = int((prior or {}).get("n_followups") or 0)
    # The curator's running knowledge base: seeded from any prior run's
    # knowledge.json (resume) and from a caller-supplied `knowledge` string,
    # grown by curate_task after each accepted task, rendered into later prompts.
    kb_entries = _load_knowledge(base)
    if knowledge and not kb_entries:
        kb_entries = [{"claim": knowledge, "evidence": "campaign seed", "provenance_task_id": "seed"}]
    # One-line summary of every accepted analysis task, so an editor reviewing a
    # report can see the results the report claims to draw on.
    accepted_summaries = {}
    cost0, _ = campaign_cost(host, root_frame_id)
    report["cost_at_start_usd"] = cost0

    try:
        while queue:
            # --- guards, before anything is spawned ---
            cost_now, n_frames = campaign_cost(host, root_frame_id)
            if cost_now >= cost_ceiling_usd:
                raise Halt("cost_ceiling",
                           "recorded campaign cost $%.4f >= ceiling $%.4f across %d frames "
                           "(excludes the platform's own per-output review, which has no "
                           "visible cost row)" % (cost_now, cost_ceiling_usd, n_frames),
                           {"cost_usd": cost_now, "queued": [t["id"] for t in queue]})
            used, compactions = context_used(host, root_frame_id)
            if used is not None and used >= context_ceiling_tokens:
                raise Halt("context_ceiling",
                           "orchestrator context %d tokens >= ceiling %d (compactions so far: %s)"
                           % (used, context_ceiling_tokens, compactions),
                           {"context_used": used, "queued": [t["id"] for t in queue]})

            wave, queue = queue[:wave_size], queue[wave_size:]
            wave_rec = {"n": len(report["waves"]) + 1,
                        "task_ids": [t["id"] for t in wave],
                        "cost_before_usd": cost_now,
                        "context_before_tokens": used,
                        "started_at": time.time()}
            wave_proposals = []  # follow-ups accepted tasks propose this wave

            # --- workers ---
            kb_text = render_knowledge(kb_entries)
            descs = _dispatch(host, [build_worker_request(t, schema, worker_model, script_dir,
                                                          knowledge=kb_text)
                                     for t in wave])
            results = _collect(host, [d.get("frame_id") for d in descs],
                               collect_timeout, collect_slice)

            payloads, wave_rec["workers"] = {}, []
            for t, res in zip(wave, results):
                state, problems = classify_result(res, "worker", expected_task_id=t["id"])
                rec = {"task_id": t["id"], "frame_id": res.get("frame_id"),
                       "state": state, "problems": problems,
                       "deviations": res.get("deviations")}
                wave_rec["workers"].append(rec)
                if state == "ok":
                    consecutive["worker"] = 0
                    consecutive["timeout"] = 0
                    payloads[t["id"]] = res["structured_output"]
                    _write_json(os.path.join(base, "tasks", t["id"], "result.json"), {
                        "task_id": t["id"], "stage": stage, "campaign_id": campaign_id,
                        "module_sha256": mod_hash, "frame_id": res.get("frame_id"),
                        "collected_at": time.time(), "deviations": res.get("deviations"),
                        "payload": res["structured_output"],
                    })
                elif state == "blocked_on_user":
                    # Never stopped: stopping denies the card and destroys the
                    # child's work. Halt the stage instead — a campaign that
                    # needs a human cannot be unattended, and spending on the
                    # remaining tasks while nobody is there to clear the card
                    # is the failure this guard exists to prevent. The frame id
                    # survives, so once the user acts the result is still
                    # retrievable with host.collect([frame_id]).
                    report["blocked_on_user"].append(
                        {"task_id": t["id"], "frame_id": res.get("frame_id")})
                    _write_json(os.path.join(base, "tasks", t["id"], "blocked_on_user.json"), rec)
                    report["waves"].append(wave_rec)
                    raise Halt("blocked_on_user",
                               "task %s is parked on a user approval card (frame %s). It was NOT "
                               "stopped — stopping denies the approval and discards its work. Clear "
                               "the card in the session UI, then collect it with "
                               "host.collect(['%s'])." % (t["id"], res.get("frame_id"),
                                                          res.get("frame_id")),
                               {"blocked_on_user": report["blocked_on_user"]})
                elif state == "running":
                    # SLOW IS NOT FAILED. Measured in the 2026-10-09 smoke run: a
                    # revision worker passed the collect deadline, the loop counted
                    # it as a worker failure, and the child went on running and
                    # spending with nothing holding its id. It had in fact finished
                    # — its full payload came back from stop_child afterwards. So a
                    # deadline must never feed the infrastructure halt (which exists
                    # to catch a dead role, not a slow one), and the frame id must
                    # survive so the work can still be collected.
                    consecutive["timeout"] += 1
                    rec["frame_id_for_later_collect"] = res.get("frame_id")
                    report["timed_out"].append(
                        {"task_id": t["id"], "frame_id": res.get("frame_id"),
                         "on_timeout": on_timeout})
                    _write_json(os.path.join(base, "tasks", t["id"], "timeout.json"), rec)
                    if on_timeout == "stop":
                        try:
                            host.stop_child(res.get("frame_id"),
                                            reason="collect deadline reached in campaign %s"
                                                   % campaign_id)
                        except Exception as exc:  # never let cleanup kill the run
                            rec["stop_child_error"] = "%s: %s" % (type(exc).__name__, exc)
                    if consecutive["timeout"] >= max_consecutive_timeouts:
                        report["waves"].append(wave_rec)
                        raise Halt("consecutive_timeouts",
                                   "%d consecutive workers past the %ds collect deadline — "
                                   "raise collect_timeout or cut the task size"
                                   % (consecutive["timeout"], collect_timeout),
                                   {"timed_out": report["timed_out"][-3:]})
                else:
                    consecutive["worker"] += 1
                    # Keep the task dict, not just the record. A failed task was
                    # being dropped from the queue and written only to
                    # failure.json, so nothing in state.json could see it and a
                    # resume silently continued without it — a transient worker
                    # failure quietly removed a task from the campaign.
                    report["failed"].append({"task_id": t["id"], "problems": problems})
                    failed_tasks.append(dict(t))
                    _write_json(os.path.join(base, "tasks", t["id"], "failure.json"), rec)
                    if consecutive["worker"] >= max_consecutive_role_failures:
                        report["waves"].append(wave_rec)
                        raise Halt("consecutive_worker_failures",
                                   "%d consecutive worker failures — this is the role's "
                                   "infrastructure, not the tasks" % consecutive["worker"],
                                   {"last_problems": problems})

            # --- supervisors: one per worker payload, fresh context each ---
            wave_rec["supervisors"] = []
            sv_tasks = [t for t in wave if t["id"] in payloads]
            if sv_tasks:
                sv_descs = _dispatch(host, [
                    build_supervisor_request(t, payloads[t["id"]], supervisor_model,
                                             knowledge=kb_text)
                    for t in sv_tasks
                ])
                sv_results = _collect(host, [d.get("frame_id") for d in sv_descs],
                                      collect_timeout, collect_slice)
                for t, res in zip(sv_tasks, sv_results):
                    state, problems = classify_result(res, "supervisor", expected_task_id=t["id"])
                    verdict = res.get("structured_output") if state == "ok" else None
                    rec = {"task_id": t["id"], "frame_id": res.get("frame_id"),
                           "state": state, "problems": problems,
                           "decision": (verdict or {}).get("decision")}
                    wave_rec["supervisors"].append(rec)
                    if state == "running":
                        # Same rule as workers: a deadline is not a dead role.
                        report["timed_out"].append(
                            {"task_id": t["id"], "role": "supervisor",
                             "frame_id": res.get("frame_id"), "on_timeout": on_timeout})
                        if on_timeout == "stop":
                            try:
                                host.stop_child(res.get("frame_id"),
                                                reason="collect deadline reached in campaign %s"
                                                       % campaign_id)
                            except Exception as exc:
                                rec["stop_child_error"] = "%s: %s" % (type(exc).__name__, exc)
                        report["stalled"].append(
                            {"task_id": t["id"],
                             "why": "supervisor past the %ds collect deadline" % collect_timeout,
                             "frame_id": res.get("frame_id")})
                        continue
                    if state != "ok":
                        consecutive["supervisor"] += 1
                        if consecutive["supervisor"] >= max_consecutive_role_failures:
                            report["waves"].append(wave_rec)
                            raise Halt("consecutive_supervisor_failures",
                                       "%d consecutive supervisor failures — this is the "
                                       "role's infrastructure, not the tasks"
                                       % consecutive["supervisor"], {"last_problems": problems})
                        report["stalled"].append({"task_id": t["id"],
                                                  "why": "supervisor %s: %s" % (state, problems)})
                        continue
                    consecutive["supervisor"] = 0
                    _write_json(os.path.join(base, "tasks", t["id"], "review.json"), {
                        "task_id": t["id"], "frame_id": res.get("frame_id"),
                        "module_sha256": mod_hash, "payload": verdict,
                    })
                    if verdict["decision"] == "ACCEPT":
                        report["accepted"].append(t["id"])
                        # Collect follow-ups ONLY from accepted tasks — a
                        # rejected or stalled task's proposals are noise. Both
                        # the worker and the accepting supervisor may propose
                        # (paper: follow-ups came from both roles).
                        pdepth = t.get("followup_depth", 0)
                        for fu in (payloads[t["id"]].get("followups") or []):
                            wave_proposals.append({"task": fu.get("task", ""), "why": fu.get("why", ""),
                                                   "source": "worker", "parent_id": t["id"],
                                                   "parent_depth": pdepth})
                        for fu in (verdict.get("followups") or []):
                            wave_proposals.append({"task": fu.get("task", ""), "why": fu.get("why", ""),
                                                   "source": "supervisor", "parent_id": t["id"],
                                                   "parent_depth": pdepth})
                        acc_payload = payloads[t["id"]]
                        is_report = t.get("kind") == "report"
                        if not is_report:
                            accepted_summaries[t["id"]] = (acc_payload.get("summary") or "")[:300]
                        # A REPORT task goes to the editor before filing; an
                        # ANALYSIS task is curated into the knowledge base.
                        # (A report is a synthesis, not a new finding, so it is
                        # not curated.)
                        if is_report and enable_editor:
                            cited = t.get("source_task_ids") or list(accepted_summaries)
                            src = "\n".join("- %s: %s" % (tid, accepted_summaries.get(tid, ""))
                                            for tid in cited if tid in accepted_summaries)[:4000]
                            ed = editor_review(host, t, acc_payload, src, model=editor_model)
                            arts = [a.get("version_id") for a in (acc_payload.get("artifacts") or [])]
                            if ed["decision"] == "FILE":
                                report["filed_reports"].append(
                                    {"task_id": t["id"], "reason": ed["reason"], "artifacts": arts})
                            else:
                                report["held_reports"].append(
                                    {"task_id": t["id"], "reason": ed["reason"], "artifacts": arts})
                            wave_rec.setdefault("edited", []).append(
                                {"task_id": t["id"], "decision": ed["decision"]})
                        elif curate:
                            new_entries = curate_task(host, t, acc_payload, model=curator_model)
                            if new_entries:
                                kb_entries.extend(new_entries)
                                _save_knowledge(base, kb_entries)
                                wave_rec.setdefault("curated", []).append(
                                    {"task_id": t["id"], "entries": len(new_entries)})
                    # setdefault, not [...]: `revisions` is built from the INITIAL
                    # queue (see above), so triage-released follow-ups and
                    # follow-ups-of-follow-ups have no entry. Indexing raised
                    # KeyError the first time a follow-up was returned for
                    # revision, which killed the loop mid-campaign.
                    elif revisions.setdefault(t["id"], 0) < max_revisions:
                        revisions[t["id"]] += 1
                        t2 = dict(t)
                        t2["task"] = (t["task"] + "\n\nREVISION %d. A reviewer returned this "
                                      "task. Address every item, then resubmit:\n- "
                                      % revisions[t["id"]]
                                      + "\n- ".join(verdict["required_changes"]))
                        queue.append(t2)
                    else:
                        # The worker's payload is still on disk at
                        # tasks/<id>/result.json and may be perfectly good — a
                        # stall means no reviewer signed it off, NOT that the
                        # work is absent. Gates default to accepted-only, so a
                        # stalled task is invisible to them by design; say so
                        # here rather than leaving it to be inferred.
                        report["stalled"].append({
                            "task_id": t["id"],
                            "why": "REVISE after %d revisions" % revisions[t["id"]],
                            "required_changes": verdict["required_changes"],
                            "unreviewed_result_on_disk":
                                os.path.join(base, "tasks", t["id"], "result.json")})

            # --- triage: release or reject this wave's proposed follow-ups ---
            # This is what makes the campaign self-extend (paper: 98 of 119
            # tasks were follow-ups). Depth and campaign caps are enforced
            # BEFORE any LLM call, so a runaway chain is stopped structurally,
            # not by hoping the triage model says no.
            if enable_triage and wave_proposals:
                runnable, pre_rejected = [], []
                for p in wave_proposals:
                    if p["parent_depth"] >= max_followup_depth:
                        pre_rejected.append((p, "depth cap: parent at follow-up depth %d >= %d"
                                             % (p["parent_depth"], max_followup_depth)))
                    elif n_followups_released + len(runnable) >= max_followups:
                        pre_rejected.append((p, "campaign follow-up cap %d reached" % max_followups))
                    else:
                        runnable.append(p)
                for p, why in pre_rejected:
                    report["rejected_followups"].append(
                        {"proposal": p, "decision": "REJECT", "reason": why, "by": "cap"})
                decisions = (triage_followups(host, runnable, brief, render_knowledge(kb_entries),
                                              model=triage_model,
                                              declared_tasks=declared_tasks)
                             if runnable else [])
                released = 0
                for d in decisions:
                    if d["decision"] == "RELEASE":
                        p = d["proposal"]
                        n_followups_released += 1
                        nid = "%s.f%d" % (p["parent_id"], n_followups_released)
                        ftask = d.get("refined_task") or p.get("task", "")

                        # Guard against invented referents (see
                        # unprovenanced_referents). The follow-up is still released --
                        # a proposal can be scientifically good and sloppy about a
                        # filename, and campaign-d/c001.f2 found that campaign's
                        # decisive failure -- but the worker is told explicitly which
                        # names have no provenance, so it cannot silently operate on a
                        # file or an entity that its parent never produced.
                        prov = [brief, render_knowledge(kb_entries), p.get("task", "")]
                        try:
                            with open(os.path.join(base, "tasks", p["parent_id"],
                                                   "result.json")) as _fh:
                                prov.append(_fh.read())
                        except OSError:
                            pass
                        bad = unprovenanced_referents(ftask, "\n".join(prov))
                        if bad:
                            ftask += (
                                "\n\n[ORCHESTRATOR WARNING -- UNVERIFIED REFERENTS] "
                                "These names appear in the task above but in neither your "
                                "parent task's result, the brief, nor the knowledge base: "
                                + ", ".join(bad) +
                                ". They may be transcription errors or may not exist. Before "
                                "relying on any of them, verify it resolves to what the task "
                                "implies; if it does not, say so in your result and proceed "
                                "with the correct referent rather than inventing data. Do not "
                                "treat this warning as a reason to abandon the task."
                            )

                        queue.append({"id": nid, "stage": stage, "name": nid,
                                      "task": ftask,
                                      "followup_depth": p["parent_depth"] + 1,
                                      "parent_id": p["parent_id"]})
                        report["released_followups"].append(
                            {"task_id": nid, "parent_id": p["parent_id"], "reason": d["reason"],
                             "source": p.get("source"),
                             "unprovenanced_referents": bad})
                        released += 1
                    else:
                        report["rejected_followups"].append(
                            {"proposal": d["proposal"], "decision": "REJECT",
                             "reason": d["reason"], "by": "triage"})
                wave_rec["triage"] = {"proposed": len(wave_proposals), "released": released,
                                      "rejected": len(wave_proposals) - released}

            wave_rec["finished_at"] = time.time()
            wave_rec["cost_after_usd"] = campaign_cost(host, root_frame_id)[0]
            report["waves"].append(wave_rec)
            # queued_tasks carries FULL task dicts, not just ids: a task sitting
            # in the queue after a REVISE has the reviewer's required changes
            # appended to its text, and resuming from ids alone would silently
            # re-dispatch the unrevised version.
            _write_json(os.path.join(base, "state.json"), {
                "campaign_id": campaign_id, "stage": stage, "updated_at": time.time(),
                "accepted": report["accepted"], "stalled": report["stalled"],
                "queued": [t["id"] for t in queue], "queued_tasks": queue,
                "failed_tasks": failed_tasks, "n_followups": n_followups_released,
                "revisions": revisions, "module_sha256": mod_hash,
            })
    except Halt as h:
        report["halt"] = {"kind": h.kind, "detail": h.detail, "state": h.state}
        # Persist the EXACT remaining work so resume=True picks up here. A halt
        # can fire mid-wave, and those in-flight tasks are no longer in `queue`
        # — without this they would be silently dropped on resume, which is the
        # quiet way to lose a task from a campaign.
        settled = set(report["accepted"]) | {s["task_id"] for s in report["stalled"]}
        still_queued = {t["id"] for t in queue}
        # Failed tasks are carried in failed_tasks and re-queued from there on
        # resume; including them here too would dispatch them twice.
        already_failed = {t["id"] for t in failed_tasks}
        leftover = [t for t in wave
                    if t["id"] not in settled and t["id"] not in still_queued
                    and t["id"] not in already_failed]
        _write_json(os.path.join(base, "state.json"), {
            "campaign_id": campaign_id, "stage": stage, "updated_at": time.time(),
            "accepted": report["accepted"], "stalled": report["stalled"],
            "queued": [t["id"] for t in leftover + queue],
            "queued_tasks": leftover + queue,
            "failed_tasks": failed_tasks, "n_followups": n_followups_released,
            "revisions": revisions, "module_sha256": mod_hash,
            "halted_on": h.kind,
        })

    report["finished_at"] = time.time()
    report["cost_at_end_usd"] = campaign_cost(host, root_frame_id)[0]
    report["cost_spent_usd"] = report["cost_at_end_usd"] - report["cost_at_start_usd"]
    report["context_at_end_tokens"] = context_used(host, root_frame_id)[0]
    _write_json(os.path.join(base, "report_stage%s.json" % stage), report)
    return report


def summarize(report):
    """One short line per wave — what the orchestrator prints, instead of payloads.

    The parent stays thin on purpose: it dispatches, writes results to disk and
    reasons about nothing. A parent holding conclusions is both the thing that
    compacts silently and the only thing a leaked transcript could give away.
    """
    out = ["campaign=%s stage=%s waves=%d accepted=%d stalled=%d spent=$%.4f"
           % (report["campaign_id"], report["stage"], len(report["waves"]),
              len(report["accepted"]), len(report["stalled"]),
              report.get("cost_spent_usd") or 0.0)]
    for w in report["waves"]:
        states = {}
        for r in w.get("workers", []):
            states[r["state"]] = states.get(r["state"], 0) + 1
        decs = {}
        for r in w.get("supervisors", []):
            key = r.get("decision") or r["state"]
            decs[key] = decs.get(key, 0) + 1
        out.append("  wave %d: %d tasks | workers %s | supervisors %s"
                   % (w["n"], len(w["task_ids"]), states or "-", decs or "-"))
    if report.get("halt"):
        out.append("  HALT %s: %s" % (report["halt"]["kind"], report["halt"]["detail"]))
    return "\n".join(out)


# --- the stage chain --------------------------------------------------------
#
# Paper (Methods p.28): the brief's stages run "as a chain, with each stage
# closed by scripted completion checks ... No task of a later stage was opened
# until the preceding stage was completed." That ordering is the campaign's
# spine — it is what stops a deep-dive stage from running on a census that
# failed its own check.
#
# The gate here is a callable, gate(base, rep) -> (ok: bool, detail: str), run
# in THIS kernel over the stage's workspace output. A callable IS a scripted
# check — it is code over the produced files — and keeping it in-process lets
# the whole chain be one call. The vault-side science_stagecheck.py is the same
# idea in its durable home: run it over the MIRRORED campaign for the
# git-tracked audit record. Use both — the in-process gate to drive the chain,
# the vault script to leave a checkable trail.

def stage_gate(min_accepted=0, min_filed=0, require_no_stop_condition=False):
    """Build a gate callable that reads the stage's produced result.json files.

    Faithful to 'a check is a fact about the output': it reads the files on
    disk, not the in-memory report, so it checks what the stage actually
    produced.
    """
    def _gate(base, rep):
        import glob
        stage = rep.get("stage")
        accepted = set(rep.get("accepted") or [])
        results = []
        for p in glob.glob(os.path.join(base, "tasks", "*", "result.json")):
            try:
                with open(p) as fh:
                    r = json.load(fh)
            except Exception:
                continue
            if (stage is None or r.get("stage") == stage) and r.get("task_id") in accepted:
                results.append(r)
        n_acc = len(results)
        n_filed = len(rep.get("filed_reports") or [])
        problems = []
        if n_acc < min_accepted:
            problems.append("accepted %d < required %d" % (n_acc, min_accepted))
        if n_filed < min_filed:
            problems.append("filed reports %d < required %d" % (n_filed, min_filed))
        if require_no_stop_condition:
            fired = [r["task_id"] for r in results
                     if ((r.get("payload") or {}).get("verdict") or {}).get("outcome")
                     == "STOP_CONDITION_FIRED"]
            if fired:
                problems.append("stop condition fired: %s" % fired)
        return (not problems), ("; ".join(problems) if problems else
                                "accepted=%d filed=%d" % (n_acc, n_filed))
    return _gate


def run_campaign(host, *, campaign_id, stages, out_root, root_frame_id,
                 cost_ceiling_usd, context_ceiling_tokens, brief="", **run_stage_kwargs):
    """Run a multi-stage campaign. `stages` is a list of dicts:

        {"n": int, "tasks": [task, ...], "gate": callable|None,
         "gate_command": str|None}

    Each stage runs to exhaustion via run_stage; then its gate is evaluated
    over the workspace output. A later stage opens ONLY if the prior stage did
    not halt AND its gate passed. The shared cost ceiling and knowledge base
    span the whole chain. Returns a campaign-level record (also written to
    campaign.json).
    """
    base = os.path.join(out_root, campaign_id)
    camp = {"campaign_id": campaign_id, "module_sha256": _module_hash(),
            "started_at": time.time(), "stages": [], "stopped_at": None,
            "brief": brief}
    for st in stages:
        n = st["n"]
        # Every scripted task in the WHOLE campaign, so triage can recognise a
        # proposal that duplicates a stage it has not reached yet — which is how all
        # three observed duplications happened (a stage-1 follow-up restating the
        # stage-2 validation).
        declared = [(t.get("id"), t.get("task"))
                    for s in stages for t in (s.get("tasks") or [])]
        # Stamp the stage number onto each task. run_stage filters its queue by
        # t["stage"] == stage, so a task dict without the key silently produces an
        # EMPTY queue: the stage then "completes" in 0.0s having run nothing, with
        # waves 0 and $0.00, and only the gate reports a problem. That is defect #8.
        # The caller already declares the stage via st["n"], so requiring them to
        # repeat it in every task dict is a footgun with no upside.
        for t in st["tasks"]:
            t.setdefault("stage", n)
        rep = run_stage(host, campaign_id=campaign_id, tasks=st["tasks"], out_root=out_root,
                        root_frame_id=root_frame_id, cost_ceiling_usd=cost_ceiling_usd,
                        context_ceiling_tokens=context_ceiling_tokens, stage=n, brief=brief,
                        gate_command=st.get("gate_command"), declared_tasks=declared,
                        **run_stage_kwargs)
        rec = {"stage": n, "accepted": list(rep.get("accepted") or []),
               "stalled": [s.get("task_id") for s in rep.get("stalled") or []],
               "filed_reports": [r.get("task_id") for r in rep.get("filed_reports") or []],
               "released_followups": len(rep.get("released_followups") or []),
               "halt": rep.get("halt"), "spent_usd": rep.get("cost_spent_usd")}
        if rep.get("halt"):
            rec["gate"] = {"passed": False, "detail": "stage halted before its gate"}
            camp["stages"].append(rec)
            camp["stopped_at"] = {"stage": n, "why": "halt:" + rep["halt"]["kind"]}
            break
        gate = st.get("gate")
        if gate:
            ok, detail = gate(base, rep)
            rec["gate"] = {"passed": bool(ok), "detail": detail}
            camp["stages"].append(rec)
            if not ok:
                camp["stopped_at"] = {"stage": n, "why": "gate_failed: " + detail}
                break
        else:
            rec["gate"] = {"passed": True, "detail": "no gate for this stage"}
            camp["stages"].append(rec)
    camp["finished_at"] = time.time()
    camp["completed_all_stages"] = camp["stopped_at"] is None
    _write_json(os.path.join(base, "campaign.json"), camp)
    return camp
