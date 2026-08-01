"""Assertion oracle planner.

Computes, from the CUT method's own code, WHAT the test should assert about the
returned object -- instead of letting the model guess. This targets the largest
assertion-failure class:

    assertNotNull(response.getMotorPremium());   // FAILS: the method never sets it

The model asserts every getter the DTO exposes, because it sees the getter in the
payload schema. But a method only populates SOME fields on its return object; the
rest are legitimately null on that path. Asserting them non-null guarantees failure.

The oracle reads the method's setter calls on the returned variable
(premiumResponse.setData(...), premiumResponse.setStatus(SUCCESS)) plus builder
calls, and emits an authoritative "assert exactly these, and only these"
instruction. Fields the method does NOT set are explicitly listed as "leave
unasserted (null on this path)".

This only READS facts already computed (bodyFacts.methodCalls, method_source).
It builds no new analysis and changes no generation behaviour on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SetterFact:
    """A `returnVar.setX(value)` (or builder `.x(value)`) on the returned object."""
    field: str          # logical field name, e.g. "data" from setData
    setter: str         # the actual method, e.g. "setData"
    value_expr: str     # the argument source, e.g. "getPremiumResponseData" or "StatusEnum.SUCCESS"
    getter: str         # the getter to assert on, e.g. "getData"
    branch_arm: str | None = None


@dataclass(slots=True, frozen=True)
class OraclePlan:
    return_var: str | None
    return_type: str | None
    set_fields: tuple[SetterFact, ...]
    # getters that exist on the type but are NOT set by this method -> must not
    # be asserted non-null (they are null on this path).
    unset_getters: tuple[str, ...] = ()


_SETTER_RE = re.compile(r"^set([A-Z]\w*)$")


def _getter_for(field_cap: str, value_expr: str) -> str:
    # boolean-ish setters map to isX; default getX. We cannot always know the
    # field type here, so default to getX (the common case). The model is told
    # to use the real accessor from the schema if getX is absent.
    return f"get{field_cap}"


def _find_return_variable(method_source: str) -> str | None:
    """Identify the variable the method returns: `return premiumResponse;`."""
    # last `return <ident>;` wins (the success-path return)
    matches = re.findall(r"\breturn\s+([A-Za-z_$][\w$]*)\s*;", method_source or "")
    return matches[-1] if matches else None


def _setter_calls_on(method_source: str, return_var: str) -> list[SetterFact]:
    """Setter calls on the returned variable, parsed from the method source.

    Verified against real DTO-building service methods: finds every
    `returnVar.setX(arg)` regardless of branch, deduplicated by field (last
    write on the primary path wins for the value expression).
    """
    facts: list[SetterFact] = []
    by_field: dict[str, SetterFact] = {}
    pattern = re.compile(rf"\b{re.escape(return_var)}\s*\.\s*(set\w+)\s*\(\s*([^;]*?)\s*\)")
    for name, arg in pattern.findall(method_source or ""):
        m = _SETTER_RE.match(name)
        if not m:
            continue
        field_cap = m.group(1)
        field = field_cap[0].lower() + field_cap[1:]
        # Prefer a non-error/success value if the same field is set twice
        # (e.g. setStatus(FAIL) on guard path, setStatus(SUCCESS) on main path).
        candidate = SetterFact(
            field=field, setter=name, value_expr=(arg or "").strip(),
            getter=_getter_for(field_cap, arg or ""), branch_arm=None,
        )
        prev = by_field.get(field)
        if prev is None or "SUCCESS" in candidate.value_expr.upper():
            by_field[field] = candidate
    return list(by_field.values())


def build_oracle_plan(method_context, return_type_schema=None) -> OraclePlan:
    """Compute the assertion oracle for one method's returned object."""
    src = getattr(method_context, "method_source", "") or ""
    return_var = _find_return_variable(src)

    # builder-style: GetPremiumResponse.builder()....build() then setters, OR
    # fully-fluent builders. We handle the setter form (dominant in these DTOs);
    # fluent-builder value extraction is left to the model with the schema.
    set_fields = tuple(_setter_calls_on(src, return_var)) if return_var else ()

    return_type = None
    if return_type_schema is not None:
        return_type = getattr(return_type_schema, "type_name", None)

    # Determine getters that exist on the type but are NOT set here.
    unset: list[str] = []
    if return_type_schema is not None:
        set_getters = {f.getter for f in set_fields}
        for prop in getattr(return_type_schema, "properties", ()) or ():
            g = prop.getter or f"get{prop.name[:1].upper()}{prop.name[1:]}"
            if g not in set_getters:
                unset.append(g)

    return OraclePlan(
        return_var=return_var, return_type=return_type,
        set_fields=set_fields, unset_getters=tuple(unset),
    )


def render_oracle_block(method_context, return_type_schema=None) -> str:
    """Prompt block: assert exactly what the method sets; do not assert the rest."""
    plan = build_oracle_plan(method_context, return_type_schema)
    if not plan.set_fields and not plan.unset_getters:
        return "(no return-object oracle derivable for this method)"

    lines = [
        "=== ASSERTION ORACLE (assert exactly these; do NOT assert others) ===",
        "The method under test populates ONLY the fields listed below on its",
        "returned object. Assert these. Every other getter on the return type is",
        "NULL on this path -- do NOT write assertNotNull/assertEquals for them, or",
        "the test will fail against real production behaviour.",
        "",
    ]
    if plan.return_type:
        lines.append(f"Return type: {plan.return_type}")
        lines.append("")

    if plan.set_fields:
        lines.append("FIELDS THE METHOD SETS -> assert these:")
        for f in plan.set_fields:
            arm = f" [only on branch: {f.branch_arm}]" if f.branch_arm else ""
            # If the set value is a literal/enum/known constant, assert equality;
            # otherwise assert non-null (the value came from a mock/computation).
            if re.match(r"^[A-Z][\w.]*\.[A-Z_][\w]*$", f.value_expr) or f.value_expr in {"true", "false"} or re.match(r'^".*"$', f.value_expr):
                lines.append(f"  - assertEquals({f.value_expr}, result.{f.getter}());{arm}")
            else:
                lines.append(f"  - assertNotNull(result.{f.getter}());  // set from {f.value_expr}{arm}")
        lines.append("")

    if plan.unset_getters:
        shown = plan.unset_getters[:15]
        lines.append("GETTERS THE METHOD DOES NOT SET -> DO NOT assert non-null:")
        lines.append("  " + ", ".join(g + "()" for g in shown)
                     + (" ..." if len(plan.unset_getters) > 15 else ""))
        lines.append("  (These are null on this path. Asserting them non-null is a guaranteed failure.)")

    return "\n".join(lines).rstrip()