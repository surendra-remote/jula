"""Deterministic Mockito stub planner.

Decides, for each observed dependency invocation on the selected path, exactly
how the generated test must stub it -- WITHOUT asking the model to guess. This
targets two proven runtime-error classes:

  1. Dead stub (PotentialStubbingProblem / silent null):
     The test stubs `when(dep.m(x)).thenReturn(...)` with a local object `x`,
     but the CUT constructs its OWN argument internally, so Mockito's equals()
     match never binds. The stub is dead; the mock returns null; an NPE surfaces
     later in production code.
     FIX: if an argument is NOT sourced from a test-controllable value, the stub
     MUST use a matcher (any(Type.class)), never an exact instance.

  2. Overload ambiguity on a null literal:
     dep.m(null) matches multiple overloads because a null argument has no
     inferred type. The generated stub picks the wrong overload.
     FIX: emit an explicit cast -- any(Type.class) or (Type) null -- so the
     overload is unambiguous.

The planner only READS execution facts already computed by the analyzer
(InvocationArgument.source_parameter / .transformed / .expression_kind,
DependencyMethodContract). It changes no analysis. Its output is a text block
appended to the method-wise prompt and (optionally) a structured plan the
validator can check against.
"""

from __future__ import annotations

from dataclasses import dataclass
from junitforge.execution.dependencies import simple_type_name

# Argument value types where an exact literal is always safe to match on.
_LITERAL_KINDS = {"literal", "string", "number", "boolean", "char", "null-literal"}


@dataclass(slots=True, frozen=True)
class ArgPlan:
    index: int
    declared_type: str          # the parameter type from the contract (authoritative)
    strategy: str               # "exact" | "matcher" | "matcher-cast" | "isNull"
    rendered: str               # what to write, e.g. eq("email-url") / any(Foo.class)
    reason: str


@dataclass(slots=True, frozen=True)
class StubPlan:
    dependency_field: str
    method_name: str
    return_type: str | None
    args: tuple[ArgPlan, ...]
    matcher_required: bool      # if any arg is a matcher, ALL must be (Mockito rule)
    void: bool
    note: str = ""


def _arg_is_test_controllable(arg) -> bool:
    """True if the test can supply this exact value, so exact match is safe.

    Controllable == the argument traces back to a CUT method parameter (the test
    passes it in) AND was not transformed on the way. Anything constructed inside
    the CUT, or derived/transformed, is NOT controllable -> matcher required.
    """
    if arg.transformed:
        return False
    if getattr(arg, "source_parameter", None):
        return True
    kind = (getattr(arg, "expression_kind", None) or "").lower()
    if kind in _LITERAL_KINDS:
        return True
    # A literal_value captured by the analyzer is also directly reproducible.
    if getattr(arg, "literal_value", None) is not None:
        return True
    return False


def _is_null_literal(arg) -> bool:
    kind = (getattr(arg, "expression_kind", None) or "").lower()
    if kind == "null-literal":
        return True
    return (arg.expression or "").strip() == "null"


def _matcher_for(type_name: str) -> str:
    simple = simple_type_name(type_name) or "Object"
    common = {
        "String": "anyString()",
        "int": "anyInt()", "Integer": "anyInt()",
        "long": "anyLong()", "Long": "anyLong()",
        "double": "anyDouble()", "Double": "anyDouble()",
        "boolean": "anyBoolean()", "Boolean": "anyBoolean()",
        "List": "anyList()", "Map": "anyMap()", "Set": "anySet()",
    }
    if simple in common:
        return common[simple]
    return f"any({simple}.class)"


def _exact_for(arg, type_name: str) -> str:
    # Prefer a captured literal; else echo the argument expression.
    literal = getattr(arg, "literal_value", None)
    value = literal if literal is not None else (arg.expression or "").strip()
    return f"eq({value})"


def plan_argument(index: int, arg, declared_type: str) -> ArgPlan:
    dtype = declared_type or arg.inferred_type or "Object"

    if _is_null_literal(arg):
        # Null literals are the overload-ambiguity trap. Force an unambiguous
        # matcher carrying the declared type, so Mockito binds the right overload.
        simple = simple_type_name(dtype) or "Object"
        return ArgPlan(
            index=index, declared_type=dtype, strategy="matcher-cast",
            rendered=f"any({simple}.class)",
            reason="null argument: cast to declared type to disambiguate overloads",
        )

    if _arg_is_test_controllable(arg):
        return ArgPlan(
            index=index, declared_type=dtype, strategy="exact",
            rendered=_exact_for(arg, dtype),
            reason="value is supplied by the test (CUT parameter or literal)",
        )

    return ArgPlan(
        index=index, declared_type=dtype, strategy="matcher",
        rendered=_matcher_for(dtype),
        reason="argument is constructed/derived inside the CUT; the test cannot "
               "supply this exact instance, so an exact stub would never match",
    )


def plan_invocation(invocation) -> StubPlan:
    contract = invocation.contract
    ptypes = contract.parameter_types or ()
    args = invocation.arguments or ()

    arg_plans: list[ArgPlan] = []
    for i, arg in enumerate(args):
        declared = ptypes[i] if i < len(ptypes) else (arg.inferred_type or "Object")
        arg_plans.append(plan_argument(i, arg, declared))

    matcher_required = any(a.strategy != "exact" for a in arg_plans)

    # Mockito rule: if ANY argument is a matcher, ALL must be matchers. Promote
    # exact args to eq(...) wrappers (they already render as eq(...), so this is
    # automatically satisfied -- but flag it so the prompt states the rule).
    return_type = contract.return_type
    void = (return_type or "").strip() in ("", "void")

    return StubPlan(
        dependency_field=invocation.dependency_field,
        method_name=invocation.method_name,
        return_type=return_type,
        args=tuple(arg_plans),
        matcher_required=matcher_required,
        void=void,
    )


def build_stub_plans(method_context) -> list[StubPlan]:
    plans: list[StubPlan] = []
    seen: set[tuple] = set()
    for invocation in getattr(method_context, "dependency_invocations", ()) or ():
        key = (invocation.dependency_field, invocation.method_name,
               tuple(a.expression for a in (invocation.arguments or ())))
        if key in seen:
            continue
        seen.add(key)
        plans.append(plan_invocation(invocation))
    return plans


def _sibling_self_calls(method_context, execution_context):
    """Public sibling methods the CUT method calls on `this`.

    When method A calls sibling B on the same class (this.getQuotation(...)), the
    test must NOT stub B -- B is a real method on the class under test. Instead B
    should RUN, and the collaborators B uses must be stubbed. This returns
    (sibling_name, [that sibling's dependency invocations]) so the caller's stub
    plan can include B's collaborators and the prompt can tell the model to let B
    run.
    """
    if execution_context is None:
        return []
    src = getattr(method_context, "method_source", "") or ""
    # names called on `this` (bare name( or this.name()
    import re as _re
    called = set(_re.findall(r"(?:\bthis\s*\.\s*|\b)([a-z][A-Za-z0-9_]*)\s*\(", src))
    siblings = []
    by_name = {}
    for m in getattr(execution_context, "methods", ()) or ():
        nm = (m.method_id or "").split("(")[0]
        by_name.setdefault(nm, m)
    for name in called:
        sib = by_name.get(name)
        if sib is None or sib is method_context:
            continue
        # only siblings that themselves call collaborators are worth propagating
        if getattr(sib, "dependency_invocations", ()):
            siblings.append((name, sib))
    return siblings


def render_stub_plan_block(method_context, execution_context=None) -> str:
    """Prompt block: exactly how to stub each collaborator call on this path."""
    plans = build_stub_plans(method_context)
    siblings_pre = _sibling_self_calls(method_context, execution_context)
    if not plans and not siblings_pre:
        return "(no collaborator invocations on this path)"

    lines = [
        "=== EXACT STUB PLAN (use these matchers/values verbatim) ===",
        "Each collaborator call the target path makes is listed with the REQUIRED",
        "stub form. Use it exactly. Do NOT stub with a locally constructed object:",
        "the CUT builds its own arguments, so an exact-instance stub will never",
        "match at runtime (the mock then returns null and the test throws an NPE).",
        "",
    ]
    for p in plans:
        arg_render = ", ".join(a.rendered for a in p.args) if p.args else ""
        lines.append(f"- {p.dependency_field}.{p.method_name}({arg_render})")
        for a in p.args:
            lines.append(f"    arg{a.index + 1} [{a.declared_type}]: {a.rendered}  // {a.reason}")
        if p.void:
            if getattr(method_context, "selected_primary_path", None) is not None:
                lines.append("    return: void -> REQUIRED success stub: "
                             "doNothing().when(mock).method(...); never use when(...).thenReturn(...)")
            else:
                lines.append("    return: void -> do NOT write when(...).thenReturn(...); "
                             "use doNothing()/doThrow() only if the path needs it")
        else:
            lines.append(f"    return: {p.return_type} -> when(...).thenReturn(<real object, "
                         f"all fields read on this path populated>)")
        if p.matcher_required:
            lines.append("    MATCHER RULE: this call mixes matchers -> ALL arguments must be "
                         "matchers; wrap any exact value as eq(value).")
        lines.append("")

    siblings = _sibling_self_calls(method_context, execution_context)
    if siblings:
        lines.append("=== SELF-CALLS (sibling methods of the class under test) ===")
        lines.append("This method calls other methods OF THE SAME CLASS. Do NOT stub")
        lines.append("them -- they are real methods on the class under test and cannot be")
        lines.append("stubbed on an @InjectMocks instance. Let them RUN, and stub the")
        lines.append("collaborators THEY use, listed here:")
        for name, sib in siblings:
            lines.append(f"  - {name}(...) runs for real. Stub its collaborators:")
            for inv in sib.dependency_invocations:
                arg_plans = [plan_argument(i, a, (inv.contract.parameter_types or (None,)*len(inv.arguments))[i] if i < len(inv.contract.parameter_types or ()) else (a.inferred_type or "Object")) for i, a in enumerate(inv.arguments or ())]
                args = ", ".join(a.rendered for a in arg_plans)
                lines.append(f"      when({inv.dependency_field}.{inv.method_name}({args})).thenReturn(...);")
        lines.append("")

    return "\n".join(lines).rstrip()
