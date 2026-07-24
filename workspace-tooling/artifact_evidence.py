#!/usr/bin/env python3
"""Self-describing gate evidence: threshold, observed value, and verdict together.

F-16 recorded the defect this module closes. A gate that emits only an
aggregate ``passed: true`` — with its acceptance bar living in ADR prose — is
unreadable after the fact: a later reader cannot distinguish "0.93 cleared a
declared bar of 0.80" from "0.93 was accepted post hoc because it was what the
run happened to produce". Evidence has to carry its own bar.

Every check built here records four things in the artifact itself: the
``observed`` value, the ``operator``, the ``threshold`` it was compared
against, and the resulting ``verdict``. The verdict is *computed* from the
other three rather than passed in, so evidence cannot record a verdict that
contradicts its own threshold.

Two shapes exist deliberately:

``check()``
    A gated criterion. Contributes to the aggregate status.

``observation()``
    A number the run measures and reports but does *not* gate on. Naming these
    explicitly keeps an ungated metric from reading like a silently-passing
    check — the failure mode where an artifact shows nine numbers and gates on
    six without saying which six.

This module deliberately depends on nothing beyond the standard library so it
can be imported by any evidence producer without pulling in a vector client.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1

#: Comparison operators a gate may declare. Restricting the set keeps the
#: operator field machine-comparable rather than free text.
OPERATORS: Mapping[str, Callable[[Any, Any], bool]] = {
    ">=": lambda observed, threshold: observed >= threshold,
    ">": lambda observed, threshold: observed > threshold,
    "<=": lambda observed, threshold: observed <= threshold,
    "<": lambda observed, threshold: observed < threshold,
    "==": lambda observed, threshold: observed == threshold,
    "!=": lambda observed, threshold: observed != threshold,
}

PASS = "pass"
FAIL = "fail"


class EvidenceError(ValueError):
    """A gate description is malformed and must not be recorded as evidence."""


def _coerce(value: Any, *, label: str) -> Any:
    """Normalize a comparable value, rejecting the ones that break a gate."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceError(f"{label} must be finite, got {value!r}")
        return value
    raise EvidenceError(f"{label} must be a real number or bool, got {type(value).__name__}")


def check(
    name: str,
    *,
    observed: Any,
    operator: str,
    threshold: Any,
    basis: Mapping[str, Any] | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """Build one gated criterion whose verdict is derived, never asserted.

    ``basis`` documents a *derived* threshold — one computed from the run
    rather than fixed in advance (for example "98% of the best simple
    baseline"). Recording the resolved number alone would leave a future
    reader unable to tell a moving bar from a fixed one, so the expression and
    its inputs are carried alongside the resolved value.
    """
    if not name or not isinstance(name, str):
        raise EvidenceError("check name must be a non-empty string")
    if operator not in OPERATORS:
        raise EvidenceError(
            f"unsupported operator {operator!r}; expected one of: "
            + ", ".join(sorted(OPERATORS))
        )
    observed_value = _coerce(observed, label=f"check {name!r} observed")
    threshold_value = _coerce(threshold, label=f"check {name!r} threshold")
    verdict = PASS if OPERATORS[operator](observed_value, threshold_value) else FAIL
    record: dict[str, Any] = {
        "name": name,
        "observed": observed_value,
        "operator": operator,
        "threshold": threshold_value,
        "verdict": verdict,
    }
    if basis is not None:
        if not isinstance(basis, Mapping) or "expression" not in basis:
            raise EvidenceError(
                f"check {name!r} basis must be a mapping containing 'expression'"
            )
        record["threshold_basis"] = dict(basis)
    if unit is not None:
        record["unit"] = unit
    return record


def observation(
    name: str,
    *,
    observed: Any,
    reason: str,
    unit: str | None = None,
) -> dict[str, Any]:
    """Record a measured value that is explicitly NOT gated, and say why.

    An ungated number sitting beside gated ones is ambiguous; ``reason`` forces
    the producer to state that the omission is intentional.
    """
    if not reason or not isinstance(reason, str):
        raise EvidenceError(f"observation {name!r} must state why it is ungated")
    record: dict[str, Any] = {
        "name": name,
        "observed": _coerce(observed, label=f"observation {name!r} observed"),
        "gated": False,
        "reason": reason,
    }
    if unit is not None:
        record["unit"] = unit
    return record


def summarize(
    checks: Iterable[Mapping[str, Any]],
    *,
    observations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Fold checks into an aggregate verdict that shows its own work."""
    ordered = [dict(item) for item in checks]
    if not ordered:
        raise EvidenceError("a gate must declare at least one check")
    names = [item["name"] for item in ordered]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise EvidenceError(f"duplicate check names: {', '.join(duplicates)}")
    failed = [item["name"] for item in ordered if item["verdict"] != PASS]
    return {
        "evidence_schema_version": SCHEMA_VERSION,
        "status": "passed" if not failed else "failed",
        "checks_total": len(ordered),
        "checks_failed": failed,
        "checks": ordered,
        "observations": [dict(item) for item in observations],
    }


def legacy_boolean_map(summary: Mapping[str, Any]) -> dict[str, bool]:
    """Project a summary back to the flat ``{name: bool}`` shape.

    Retained for producers that must keep emitting the historical key beside
    the enriched one during a format transition.
    """
    return {item["name"]: item["verdict"] == PASS for item in summary["checks"]}
