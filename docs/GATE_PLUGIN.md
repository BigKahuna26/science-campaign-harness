# Domain gate plugins

`science_stagecheck.py` ships generic gates that read **schema fields**:
`--verdict`, `--produced`, `--all-traced`, `--none-blocked`,
`--no-stop-condition`, `--no-analyst-supplied`, `--min-tasks`.

A *domain* gate is different: it reads the **tables an accepted task
produced** and applies a rule from your field. That rule cannot be general, so
it is a plugin rather than a flag.

```bash
python3 harness/science_stagecheck.py --campaign my-campaign \
    --gate-module example_gate --gate-path examples/
```

## The contract

Your module implements three callables.

```python
def load_gate_constants() -> dict:
    """Thresholds the rule needs.

    Read them from a staged audit file rather than hardcoding literals, so the
    gate and the analysis cannot drift apart.
    """

def classify_csv(path) -> tuple | None:
    """Return a non-None tuple if this CSV is in scope, else None.

    Called for every .csv under an accepted task's files/ directory. The
    tuple's contents are yours — stagecheck only tests truthiness.
    """

def check_records(records: list[dict], constants: dict) -> list[str]:
    """Return one short string per violation; empty list means pass.

    `records` is the CSV parsed with csv.DictReader. Each returned string is
    reported with its task id and filename prefixed.
    """
```

## What a domain gate should and should not do

- **Read tables, not prose.** A claim stated only in `findings[].claim` with no
  table behind it is invisible here, and that is deliberate — catching it is
  the supervisor's job, not a gate's. If your rule needs prose, it is not a
  gate.
- **Fail loudly on a missing threshold.** `load_gate_constants` raising is a
  better outcome than a gate that silently passes everything because a
  constants file moved.
- **Keep violations short.** They are joined into a one-line check result;
  only the first three are shown.

## Why this is a plugin

The gate that motivated this interface tested a statistical-power
precondition: that every per-group record in a produced table carried its
group size, and that any null computed from a group below the attainable
ceiling was marked *not assessable at this size* rather than reported as a
negative. Useful, and specific to one analysis. Wiring it in as a flag made
the harness unusable anywhere else; it is the reason the plugin point exists.
