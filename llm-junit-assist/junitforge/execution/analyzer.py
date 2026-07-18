"""Build authoritative execution context for Controllers and ServiceImpl classes.

The analyzer is deterministic and deliberately bounded.  It consumes optional
JavaParser CLI body facts when present and falls back to source-range analysis.
It does not perform generalized symbolic execution.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from time import perf_counter

from junitforge.execution.dependencies import (
    resolve_dependency_refs,
    resolve_method_contract,
    simple_type_name,
)
from junitforge.execution.models import (
    AssignmentFact,
    BranchFact,
    ConfigurationField,
    ConfigurationRequirement,
    DereferenceRequirement,
    ControllerEndpoint,
    DependencyInvocation,
    DependencyRef,
    ExecutionContext,
    ExecutionTargetKind,
    InvocationArgument,
    LineFact,
    LocalVariableFact,
    MethodExecutionContext,
    MemberRead,
    NullGuard,
    ExitFact,
    PrivateHelperCall,
    RequestParameter,
    ResolutionStatus,
    ReturnObjectRead,
)
from junitforge.execution.payload_schema import (
    build_payload_schemas,
    extract_application_type_names,
    required_object_paths,
)
from junitforge.models import ClassSymbol, JavaSourceFile, MethodSig
from junitforge.timing import event as timing_event

LookupFn = Callable[[str], ClassSymbol | None]
MAX_PRIVATE_HELPER_DEPTH = int(os.getenv("JUNITFORGE_PRIVATE_HELPER_DEPTH", "4"))
MAX_ALIAS_RESOLUTION_ROUNDS = int(os.getenv("JUNITFORGE_ALIAS_RESOLUTION_ROUNDS", "32"))
MAX_HELPER_PROPAGATION_STATES = int(os.getenv("JUNITFORGE_HELPER_PROPAGATION_STATES", "128"))
MAX_PAYLOAD_TYPES_PER_METHOD = int(os.getenv("JUNITFORGE_PAYLOAD_MAX_TYPES", "25"))
MAX_PAYLOAD_DEPTH = int(os.getenv("JUNITFORGE_PAYLOAD_MAX_DEPTH", "4"))

_MAPPING_HTTP = {
    "GetMapping": ("GET",),
    "PostMapping": ("POST",),
    "PutMapping": ("PUT",),
    "DeleteMapping": ("DELETE",),
    "PatchMapping": ("PATCH",),
}
_BINDING_ANNOS = {
    "PathVariable": "PATH_VARIABLE",
    "RequestParam": "REQUEST_PARAM",
    "RequestHeader": "REQUEST_HEADER",
    "RequestBody": "REQUEST_BODY",
    "CookieValue": "COOKIE_VALUE",
    "ModelAttribute": "MODEL_ATTRIBUTE",
}


def _method_id(method: MethodSig) -> str:
    types = ",".join(t for t, _ in method.params)
    return f"{method.name}({types})"


def _line_for_offset(text: str, base_line: int, offset: int) -> int:
    return base_line + text.count("\n", 0, max(0, offset))


def _method_source(source: str, method: MethodSig) -> tuple[str, int]:
    """Return the real declaration/body range, correcting imprecise fallback lines."""
    lines = source.splitlines()
    if not lines:
        return "", 1
    hint = max(1, method.line or 1)
    name_re = re.compile(rf"\b{re.escape(method.name)}\s*\(")
    candidates: list[tuple[int, int]] = []
    return_text = re.sub(r"\s+", "", method.return_type or "")

    for index, line in enumerate(lines):
        match = name_re.search(line)
        if not match:
            continue
        prefix = line[:match.start()]
        score = -abs((index + 1) - hint)
        if return_text and return_text in re.sub(r"\s+", "", prefix):
            score += 200
        if any(re.search(rf"\b{modifier}\b", prefix) for modifier in method.modifiers):
            score += 50
        if prefix.rstrip().endswith("."):
            score -= 300
        if re.search(r"\breturn\b", prefix) or "=" in prefix:
            score -= 100
        candidates.append((score, index))

    if not candidates:
        start = min(len(lines), hint) - 1
        end = min(len(lines), method.end_line or hint)
        return "\n".join(lines[start:end]), start + 1

    _, start_index = max(candidates, key=lambda item: (item[0], -abs((item[1] + 1) - hint)))
    char_start = sum(len(line) + 1 for line in lines[:start_index])
    declaration_tail = source[char_start:]
    name_match = name_re.search(declaration_tail)
    if name_match is None:
        return lines[start_index], start_index + 1
    open_brace = declaration_tail.find("{", name_match.end())
    semicolon = declaration_tail.find(";", name_match.end())
    if open_brace < 0 or (semicolon >= 0 and semicolon < open_brace):
        return lines[start_index], start_index + 1
    close_brace = _find_matching(declaration_tail, open_brace, "{", "}")
    if close_brace is None:
        return "\n".join(lines[start_index:]), start_index + 1
    method_text = declaration_tail[:close_brace + 1]
    return method_text, start_index + 1


def _find_matching(text: str, open_index: int, open_char: str, close_char: str) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = open_index
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in {'"', "'"}:
            quote = ch
            i += 1
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_arguments(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    if not stripped:
        return ()
    out: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in {'"', "'"}:
            quote = ch
            continue
        if ch in depths:
            depths[ch] += 1
            continue
        if ch in pairs and depths[pairs[ch]] > 0:
            depths[pairs[ch]] -= 1
            continue
        if ch == "," and all(v == 0 for v in depths.values()):
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    return tuple(x for x in out if x)


def _resolve_expression_type(
    expression: str,
    known_types: dict[str, str],
    lookup: LookupFn,
) -> tuple[str | None, str | None, bool]:
    expr = expression.strip()
    if expr in known_types:
        return known_types[expr], expr, False

    chain = re.fullmatch(
        r"([A-Za-z_$][\w$]*)((?:\.(?:get|is)[A-Za-z_$][\w$]*\(\))+)",
        expr,
    )
    if chain and chain.group(1) in known_types:
        base = chain.group(1)
        current_type: str | None = known_types[base]
        for method_name in re.findall(r"\.((?:get|is)[A-Za-z_$][\w$]*)\(\)", chain.group(2)):
            sym = lookup(simple_type_name(current_type)) if current_type else None
            candidates = [
                m for m in (sym.methods or [])
                if m.name == method_name and m.arity == 0 and not m.is_static and "private" not in m.modifiers
            ] if sym is not None else []
            if len(candidates) != 1:
                current_type = None
                break
            current_type = candidates[0].return_type
        return current_type, base, True
    return None, None, bool(expr)


def _infer_argument(
    expression: str,
    params: dict[str, str],
    known_types: dict[str, str],
    lookup: LookupFn,
) -> InvocationArgument:
    expr = expression.strip()
    inferred, base_name, transformed = _resolve_expression_type(expr, known_types, lookup)
    if inferred is not None:
        return InvocationArgument(
            expression=expr,
            inferred_type=inferred,
            source_parameter=base_name if base_name in params else None,
            transformed=transformed,
        )
    if re.fullmatch(r'"(?:\\.|[^"\\])*"', expr):
        return InvocationArgument(expression=expr, inferred_type="String", literal_value=expr[1:-1])
    if re.fullmatch(r"-?\d+[lL]", expr):
        return InvocationArgument(expression=expr, inferred_type="long", literal_value=expr.rstrip("lL"))
    if re.fullmatch(r"-?\d+", expr):
        return InvocationArgument(expression=expr, inferred_type="int", literal_value=expr)
    if expr in {"true", "false"}:
        return InvocationArgument(expression=expr, inferred_type="boolean", literal_value=expr)
    if expr == "null":
        return InvocationArgument(expression=expr, literal_value="null")
    new_match = re.match(r"new\s+([A-Za-z_$][\w$.<>]*)", expr)
    if new_match:
        return InvocationArgument(expression=expr, inferred_type=new_match.group(1), transformed=True)
    mentioned = next((name for name in params if re.search(rf"\b{re.escape(name)}\b", expr)), None)
    return InvocationArgument(
        expression=expr,
        inferred_type=params.get(mentioned) if mentioned else None,
        source_parameter=mentioned,
        transformed=mentioned is not None or bool(expr),
    )


def _assignment_context(text: str, call_start: int) -> tuple[str | None, str | None]:
    statement_start = max(
        text.rfind(";", 0, call_start),
        text.rfind("{", 0, call_start),
        text.rfind("}", 0, call_start),
    ) + 1
    prefix = text[statement_start:call_start]
    match = re.search(
        r"(?:(?P<type>[A-Za-z_$][\w$]*(?:\s*<[^;=]+>)?(?:\[\])?)\s+)?"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*=\s*$",
        prefix.strip(),
    )
    if not match:
        return None, None
    return match.group("name"), match.group("type")


def _assigned_variable(text: str, call_start: int) -> str | None:
    return _assignment_context(text, call_start)[0]


def _parse_if_scopes(text: str, base_line: int, owner_method_id: str) -> list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]]:
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]] = []
    index = 0
    counter = 1
    while True:
        match = re.search(r"\bif\s*\(", text[index:])
        if not match:
            break
        if_start = index + match.start()
        open_paren = text.find("(", if_start)
        close_paren = _find_matching(text, open_paren, "(", ")")
        if close_paren is None:
            index = if_start + 2
            continue
        condition = " ".join(text[open_paren + 1:close_paren].split())
        cursor = close_paren + 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        true_range: tuple[int, int] | None = None
        if cursor < len(text) and text[cursor] == "{":
            end = _find_matching(text, cursor, "{", "}")
            if end is not None:
                true_range = (cursor + 1, end)
                after_true = end + 1
            else:
                after_true = cursor + 1
        else:
            semicolon = text.find(";", cursor)
            end = semicolon if semicolon >= 0 else cursor
            true_range = (cursor, end + 1)
            after_true = end + 1

        false_range: tuple[int, int] | None = None
        cursor = after_true
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if text.startswith("else", cursor):
            cursor += 4
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor < len(text) and text[cursor] == "{":
                end = _find_matching(text, cursor, "{", "}")
                if end is not None:
                    false_range = (cursor + 1, end)
            else:
                semicolon = text.find(";", cursor)
                end = semicolon if semicolon >= 0 else cursor
                false_range = (cursor, end + 1)

        if false_range is None and true_range is not None:
            true_text = text[true_range[0]:true_range[1]]
            if re.search(r"\b(return|throw)\b", true_text):
                false_range = (after_true, len(text))

        branch_id = f"{owner_method_id}:B{counter}"
        fact = BranchFact(
            branch_id=branch_id,
            owner_method_id=owner_method_id,
            kind="if",
            condition=condition,
            line=_line_for_offset(text, base_line, if_start),
            true_range=(
                _line_for_offset(text, base_line, true_range[0]),
                _line_for_offset(text, base_line, true_range[1]),
            ) if true_range else None,
            false_range=(
                _line_for_offset(text, base_line, false_range[0]),
                _line_for_offset(text, base_line, false_range[1]),
            ) if false_range else None,
        )
        scopes.append((fact, true_range, false_range))
        counter += 1
        index = close_paren + 1
    return scopes


def _branch_for_offset(scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]], offset: int) -> str | None:
    matches: list[tuple[int, str]] = []
    for fact, true_range, false_range in scopes:
        for suffix, span in (("true", true_range), ("false", false_range)):
            if span and span[0] <= offset <= span[1]:
                matches.append((span[1] - span[0], f"{fact.branch_id}:{suffix}"))
    return min(matches)[1] if matches else None


def _cli_call_hints(method: MethodSig) -> list[dict[str, object]]:
    facts = getattr(method, "body_facts", None) or {}
    for key in ("methodCalls", "calls", "invocations"):
        value = facts.get(key) if isinstance(facts, dict) else None
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []



def _body_fact_list(method: MethodSig, key: str) -> list[dict[str, object]]:
    facts = getattr(method, "body_facts", None) or {}
    value = facts.get(key) if isinstance(facts, dict) else None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _has_authoritative_body_facts(method: MethodSig) -> bool:
    facts = getattr(method, "body_facts", None)
    return isinstance(facts, dict) and "methodCalls" in facts and "branches" in facts


def _prefix_branch_id(owner_id: str, raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    return value if value.startswith(owner_id + ":") else f"{owner_id}:{value}"


def _range_from_fact(item: dict[str, object], start_key: str, end_key: str) -> tuple[int, int] | None:
    start = item.get(start_key)
    end = item.get(end_key)
    if isinstance(start, int) and isinstance(end, int):
        return (start, end)
    return None


def _cli_branches(method: MethodSig) -> list[BranchFact]:
    owner_id = _method_id(method)
    out: list[BranchFact] = []
    for item in _body_fact_list(method, "branches"):
        raw_id = item.get("branchId")
        branch_id = _prefix_branch_id(owner_id, raw_id)
        if branch_id is None:
            continue
        out.append(
            BranchFact(
                branch_id=branch_id,
                owner_method_id=owner_id,
                kind=str(item.get("kind") or "branch"),
                condition=(None if item.get("condition") is None else str(item.get("condition"))),
                line=_int_value(item.get("line")),
                end_line=_int_value(item.get("endLine")),
                parent_branch_id=_prefix_branch_id(owner_id, item.get("parentBranchId")),
                parent_arm=_str_or_none(item.get("parentArm")),
                true_range=_range_from_fact(item, "thenStartLine", "thenEndLine"),
                false_range=_range_from_fact(item, "elseStartLine", "elseEndLine"),
                body_range=_range_from_fact(item, "bodyStartLine", "bodyEndLine"),
            )
        )
    return out


def _int_value(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _local_declared_type(method_text: str, variable: str | None) -> str | None:
    if not variable:
        return None
    match = re.search(
        rf"\b([A-Za-z_$][\w$]*(?:\s*<[^;=]+>)?(?:\[\])?)\s+{re.escape(variable)}\s*=",
        method_text,
    )
    return match.group(1).strip() if match else None


def _cli_dependency_invocations(
    method: MethodSig,
    method_text: str,
    dependencies: list[DependencyRef],
    lookup: LookupFn,
    field_types: dict[str, str],
) -> list[DependencyInvocation]:
    owner_id = _method_id(method)
    params = {name: ptype for ptype, name in method.params}
    known_types = {**field_types, **params}
    dep_by_field = {
        field: dependency
        for dependency in dependencies
        for field in (dependency.field_name, dependency.parameter_name)
        if field
    }
    out: list[DependencyInvocation] = []
    counter = 1
    for item in _body_fact_list(method, "methodCalls"):
        field = _str_or_none(item.get("scope"))
        if field not in dep_by_field:
            continue
        method_name = _str_or_none(item.get("name"))
        if not method_name:
            continue
        raw_arguments = item.get("arguments")
        args: list[InvocationArgument] = []
        if isinstance(raw_arguments, list):
            for argument in raw_arguments:
                if not isinstance(argument, dict):
                    continue
                expression = str(argument.get("expr") or "").strip()
                inferred = _infer_argument(expression, params, known_types, lookup)
                args.append(replace(inferred, expression_kind=_str_or_none(argument.get("kind"))))
        dependency = dep_by_field[field]
        contract = resolve_method_contract(
            dependency,
            method_name,
            tuple(args),
            lookup,
        )
        assigned_to = _str_or_none(item.get("assignedTo"))
        # A chained dependency call may be assigned through Optional.orElseThrow;
        # the local variable type is therefore not a safe dependency return hint.
        if contract.return_type is None and assigned_to and _str_or_none(item.get("receiverKind")) != "chain":
            declared = _local_declared_type(method_text, assigned_to)
            if declared and not any(call.get("assignedTo") == assigned_to and call is not item for call in _body_fact_list(method, "methodCalls")):
                contract = replace(contract, return_type=declared, resolution_status=ResolutionStatus.PARTIAL)
        out.append(
            DependencyInvocation(
                invocation_id=f"{owner_id}:I{counter}",
                caller_method_id=owner_id,
                dependency_field=field,
                dependency_type=dependency.declared_type,
                method_name=method_name,
                arguments=tuple(args),
                contract=contract,
                assigned_to=assigned_to,
                assignment_chain=tuple(
                    str(value) for value in (item.get("assignmentChain") or [])
                ) if isinstance(item.get("assignmentChain"), list) else (),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
        counter += 1
    return out


def _cli_helper_calls(
    method: MethodSig,
    private_by_key: dict[tuple[str, int], str],
) -> list[PrivateHelperCall]:
    owner_id = _method_id(method)
    out: list[PrivateHelperCall] = []
    for item in _body_fact_list(method, "methodCalls"):
        receiver_kind = _str_or_none(item.get("receiverKind"))
        scope = _str_or_none(item.get("scope"))
        if scope is not None or receiver_kind != "this":
            continue
        name = _str_or_none(item.get("name"))
        args_value = item.get("arguments")
        arguments = tuple(
            str(arg.get("expr") or "")
            for arg in args_value
            if isinstance(arg, dict)
        ) if isinstance(args_value, list) else ()
        helper_id = private_by_key.get((name or "", len(arguments)))
        if helper_id is None:
            continue
        out.append(
            PrivateHelperCall(
                caller_method_id=owner_id,
                helper_method_id=helper_id,
                arguments=arguments,
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _cli_member_reads(method: MethodSig) -> list[MemberRead]:
    owner_id = _method_id(method)
    out: list[MemberRead] = []
    for item in _body_fact_list(method, "memberReads"):
        variable = _str_or_none(item.get("variable"))
        path = _str_or_none(item.get("propertyPath"))
        if not variable or not path:
            continue
        out.append(
            MemberRead(
                owner_method_id=owner_id,
                variable_name=variable,
                property_path=path,
                access_kind=str(item.get("accessKind") or "member").lower(),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out




def _cli_local_variables(method: MethodSig) -> list[LocalVariableFact]:
    owner_id = _method_id(method)
    out: list[LocalVariableFact] = []
    for item in _body_fact_list(method, "localVariables"):
        name = _str_or_none(item.get("name"))
        if not name:
            continue
        out.append(
            LocalVariableFact(
                name=name,
                declared_type=_str_or_none(item.get("declaredType")),
                initializer=_str_or_none(item.get("initializer")),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _cli_assignments(method: MethodSig) -> list[AssignmentFact]:
    owner_id = _method_id(method)
    out: list[AssignmentFact] = []
    for item in _body_fact_list(method, "assignments"):
        target = _str_or_none(item.get("target"))
        value = _str_or_none(item.get("value"))
        if not target or value is None:
            continue
        out.append(
            AssignmentFact(
                target=target,
                value=value,
                operator=str(item.get("operator") or "="),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _cli_line_facts(method: MethodSig) -> list[LineFact]:
    owner_id = _method_id(method)
    out: list[LineFact] = []
    for item in _body_fact_list(method, "lineFacts"):
        source = _str_or_none(item.get("source"))
        if not source:
            continue
        out.append(
            LineFact(
                kind=str(item.get("kind") or "Statement"),
                source=source,
                line=_int_value(item.get("line")),
                end_line=_int_value(item.get("endLine")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _cli_configuration_requirements(method: MethodSig) -> list[ConfigurationRequirement]:
    owner_id = _method_id(method)
    out: list[ConfigurationRequirement] = []
    for item in _body_fact_list(method, "configurationReads"):
        field_name = _str_or_none(item.get("fieldName"))
        if not field_name:
            continue
        out.append(
            ConfigurationRequirement(
                field_name=field_name,
                expression=str(item.get("expression") or field_name),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _getter_path_segment(segment: str) -> str:
    cleaned = segment.strip()
    if cleaned.endswith("()"):
        cleaned = cleaned[:-2]
    return _getter_property(cleaned)


def _parse_member_expression(expression: str | None) -> tuple[str, tuple[str, ...]] | None:
    text = (expression or "").strip()
    if not text:
        return None
    root_match = re.match(r"(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)", text)
    if not root_match:
        return None
    root = root_match.group(1)
    tail = text[root_match.end():]
    segments: list[str] = []
    position = 0
    pattern = re.compile(r"\s*\.\s*([A-Za-z_$][\w$]*)(\s*\(\s*\))?")
    while position < len(tail):
        match = pattern.match(tail, position)
        if not match:
            break
        name = match.group(1)
        segments.append(_getter_property(name) if match.group(2) else name)
        position = match.end()
    if position != len(tail.strip()) and tail[position:].strip():
        return None
    return root, tuple(segments)


def _alias_paths(local_variables: Iterable[LocalVariableFact]) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Resolve local aliases with hard convergence and cycle guards.

    The former fixed-point loop could grow indefinitely for cyclic aliases such
    as ``a = b`` / ``b = a``.  Resolution is now bounded and self/cyclic roots
    are ignored instead of repeatedly lengthening the path.
    """
    parsed_by_name: dict[str, tuple[str, tuple[str, ...]]] = {}
    for variable in local_variables:
        parsed = _parse_member_expression(variable.initializer)
        if parsed is not None and parsed[0] != variable.name:
            parsed_by_name[variable.name] = parsed

    aliases: dict[str, tuple[str, tuple[str, ...]]] = {}

    def resolve(name: str) -> tuple[str, tuple[str, ...]] | None:
        if name in aliases:
            return aliases[name]
        current = name
        segments: list[str] = []
        visited: set[str] = set()
        for _ in range(MAX_ALIAS_RESOLUTION_ROUNDS):
            if current in visited:
                return None
            visited.add(current)
            parsed = parsed_by_name.get(current)
            if parsed is None:
                result = (current, tuple(segments))
                aliases[name] = result
                return result
            root, tail = parsed
            segments[:0] = list(tail)
            current = root
        return None

    for name, (root, segments) in parsed_by_name.items():
        resolved = resolve(name)
        if resolved is None:
            continue
        final_root, final_segments = resolved
        # Preserve the direct alias path when the resolver stopped at the name
        # itself; this also avoids creating self-referential aliases.
        if final_root == name:
            continue
        aliases[name] = (final_root, final_segments)
    return aliases


def _cli_dereference_requirements(
    method: MethodSig,
    local_variables: list[LocalVariableFact],
    parameter_names: set[str],
    invocation_variables: set[str],
) -> list[DereferenceRequirement]:
    owner_id = _method_id(method)
    aliases = _alias_paths(local_variables)
    known_locals = {variable.name for variable in local_variables}
    out: list[DereferenceRequirement] = []
    seen: set[tuple[str, int | None, str | None, str | None]] = set()
    for item in _body_fact_list(method, "dereferenceChains"):
        raw_root = _str_or_none(item.get("rootVariable"))
        raw_path = _str_or_none(item.get("propertyPath"))
        if not raw_root or not raw_path:
            continue
        segments = tuple(_getter_path_segment(part) for part in raw_path.split(".") if part)
        root = raw_root
        prefix_segments: tuple[str, ...] = ()
        if root in aliases:
            root, prefix_segments = aliases[root]
        elif root not in parameter_names and root not in known_locals and root not in invocation_variables:
            # Ignore static utility/class receivers such as StringUtils and Objects.
            continue
        all_segments = (*prefix_segments, *segments)
        if not all_segments:
            continue
        full_path = ".".join((root, *all_segments))
        non_null: list[str] = [root]
        for index in range(max(0, len(all_segments) - 1)):
            non_null.append(".".join((root, *all_segments[: index + 1])))
        line = _int_value(item.get("line"))
        branch_id = _prefix_branch_id(owner_id, item.get("branchId"))
        branch_arm = _str_or_none(item.get("branchArm"))
        key = (full_path, line, branch_id, branch_arm)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            DereferenceRequirement(
                root_variable=root,
                full_path=full_path,
                non_null_prefixes=tuple(dict.fromkeys(non_null)),
                terminal_path=full_path,
                expression=str(item.get("expression") or full_path),
                line=line,
                branch_id=branch_id,
                branch_arm=branch_arm,
                consumer_kind=_str_or_none(item.get("consumerKind")),
                consumer_name=_str_or_none(item.get("consumerName")),
                consumer_scope=_str_or_none(item.get("consumerScope")),
                argument_index=_int_value(item.get("argumentIndex")),
            )
        )
    return out


_VALUE_PATTERN = re.compile(r"\$\{\s*([^}:\s]+)\s*(?::([^}]*))?\}")


def _java_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _configuration_test_value(type_name: str, default_value: str | None) -> str | None:
    simple = simple_type_name(type_name) or type_name.strip()
    raw = (default_value or "").strip()
    if simple == "String":
        return _java_string_literal(raw or "test-value")
    if simple in {"boolean", "Boolean"}:
        return "true" if raw.lower() not in {"false", "0", "no", "off"} else "false"
    if simple in {"byte", "Byte", "short", "Short", "int", "Integer"}:
        return raw if re.fullmatch(r"[-+]?\d+", raw) else "1"
    if simple in {"long", "Long"}:
        value = raw if re.fullmatch(r"[-+]?\d+", raw) else "1"
        return value + "L"
    if simple in {"float", "Float"}:
        value = raw if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", raw) else "1.0"
        return value + "f"
    if simple in {"double", "Double"}:
        value = raw if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", raw) else "1.0"
        return value + "d"
    if simple == "char" or simple == "Character":
        char = raw[0] if raw else "T"
        escaped = "\\'" if char == "'" else char
        return f"'{escaped}'"
    if simple == "BigDecimal":
        return f"new java.math.BigDecimal({_java_string_literal(raw or '1')})"
    if simple == "BigInteger":
        return f"new java.math.BigInteger({_java_string_literal(raw or '1')})"
    if simple == "Duration":
        return "java.time.Duration.ofSeconds(1)"
    if simple == "URI":
        return 'java.net.URI.create("http://localhost")'
    return None


def _configuration_fields(symbol: ClassSymbol, configuration_values: dict[str, str] | None = None) -> list[ConfigurationField]:
    out: list[ConfigurationField] = []
    for field in symbol.fields or []:
        if "Value" not in {annotation.split(".")[-1] for annotation in (field.annotations or [])}:
            continue
        annotation_expr = next(
            (expr for expr in (getattr(field, "annotation_exprs", None) or []) if "@Value" in expr),
            "@Value",
        )
        match = _VALUE_PATTERN.search(annotation_expr)
        property_key = match.group(1).strip() if match else None
        default_value = match.group(2).strip() if match and match.group(2) is not None else None
        configured_value = (configuration_values or {}).get(property_key) if property_key else None
        effective_value = configured_value if configured_value is not None else default_value
        out.append(
            ConfigurationField(
                field_name=field.name,
                type_name=field.type,
                property_key=property_key,
                default_value=default_value,
                test_value=_configuration_test_value(field.type, effective_value),
                annotation_expr=annotation_expr,
                static="static" in field.modifiers,
            )
        )
    return out

def _assigned_value_type(invocation: DependencyInvocation) -> str | None:
    return_type = invocation.contract.return_type
    if not return_type:
        return None
    if invocation.assignment_chain and invocation.assignment_chain[0] in {
        "orElseThrow", "orElse", "orElseGet", "get"
    }:
        match = re.search(r"<(.*)>", return_type)
        if match:
            inner = match.group(1).strip()
            if "," not in inner:
                return inner
    return return_type


def _cli_return_reads(
    method: MethodSig,
    invocations: list[DependencyInvocation],
) -> list[ReturnObjectRead]:
    owner_id = _method_id(method)
    invocation_by_variable = {
        invocation.assigned_to: invocation
        for invocation in invocations
        if invocation.assigned_to
    }
    out: list[ReturnObjectRead] = []
    for item in _body_fact_list(method, "returnReads"):
        variable = _str_or_none(item.get("variable"))
        path = _str_or_none(item.get("propertyPath"))
        invocation = invocation_by_variable.get(variable)
        if not variable or not path or invocation is None:
            continue
        last = path.split(".")[-1].removesuffix("()")
        out.append(
            ReturnObjectRead(
                owner_method_id=owner_id,
                invocation_id=invocation.invocation_id,
                variable_name=variable,
                declared_type=_assigned_value_type(invocation),
                access_kind=str(item.get("accessKind") or "member").upper(),
                member_name=_getter_property(last),
                property_path=path,
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return _dedupe_reads(out)


def _cli_null_guards(method: MethodSig) -> list[NullGuard]:
    owner_id = _method_id(method)
    out: list[NullGuard] = []
    for item in _body_fact_list(method, "nullGuards"):
        expression = _str_or_none(item.get("expression"))
        if not expression:
            continue
        out.append(
            NullGuard(
                guard_id=f"{owner_id}:{str(item.get('guardId') or 'G')}",
                owner_method_id=owner_id,
                expression=expression,
                operator=str(item.get("operator") or "=="),
                compared_with=str(item.get("comparedWith") or "null"),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _cli_exits(method: MethodSig) -> list[ExitFact]:
    owner_id = _method_id(method)
    out: list[ExitFact] = []
    for item in _body_fact_list(method, "exits"):
        kind = _str_or_none(item.get("kind"))
        if kind not in {"return", "throw"}:
            continue
        out.append(
            ExitFact(
                owner_method_id=owner_id,
                kind=kind,
                expression=_str_or_none(item.get("expression")),
                line=_int_value(item.get("line")),
                branch_id=_prefix_branch_id(owner_id, item.get("branchId")),
                branch_arm=_str_or_none(item.get("branchArm")),
            )
        )
    return out


def _populate_cli_branch_calls(
    branches: list[BranchFact],
    invocations: list[DependencyInvocation],
) -> list[BranchFact]:
    out: list[BranchFact] = []
    for branch in branches:
        then_calls = tuple(
            invocation.invocation_id
            for invocation in invocations
            if invocation.branch_id == branch.branch_id and invocation.branch_arm in {"then", "true"}
        )
        else_calls = tuple(
            invocation.invocation_id
            for invocation in invocations
            if invocation.branch_id == branch.branch_id and invocation.branch_arm in {"else", "false"}
        )
        body_calls = tuple(
            invocation.invocation_id
            for invocation in invocations
            if invocation.branch_id == branch.branch_id and invocation.branch_arm in {"body", "catch", "case"}
        )
        out.append(
            replace(
                branch,
                true_path_calls=then_calls,
                false_path_calls=else_calls,
                body_path_calls=body_calls,
            )
        )
    return out


def _matching_cli_hint(hints: list[dict[str, object]], field: str, name: str, line: int | None) -> dict[str, object]:
    for hint in hints:
        scope = str(hint.get("scope") or hint.get("dependencyField") or "")
        method_name = str(hint.get("name") or hint.get("methodName") or "")
        hint_line = hint.get("line")
        if scope == field and method_name == name and (line is None or hint_line is None or int(hint_line) == line):
            return hint
    return {}


def _extract_dependency_invocations(
    method: MethodSig,
    text: str,
    base_line: int,
    dependencies: list[DependencyRef],
    lookup: LookupFn,
    field_types: dict[str, str],
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]],
) -> list[DependencyInvocation]:
    owner_id = _method_id(method)
    params = {name: ptype for ptype, name in method.params}
    known_types = {**field_types, **params}
    hints = _cli_call_hints(method)
    invocations: list[DependencyInvocation] = []
    counter = 1

    for dependency in dependencies:
        field = dependency.field_name or dependency.parameter_name
        if not field:
            continue
        pattern = re.compile(rf"\b{re.escape(field)}\s*\.\s*([A-Za-z_$][\w$]*)\s*\(")
        for match in pattern.finditer(text):
            open_paren = text.find("(", match.start())
            close_paren = _find_matching(text, open_paren, "(", ")")
            if close_paren is None:
                continue
            method_name = match.group(1)
            raw_args = _split_arguments(text[open_paren + 1:close_paren])
            args = tuple(_infer_argument(arg, params, known_types, lookup) for arg in raw_args)
            line = _line_for_offset(text, base_line, match.start())
            hint = _matching_cli_hint(hints, field, method_name, line)
            assigned_to, assigned_type = _assignment_context(text, match.start())
            return_hint = str(hint.get("resolvedReturnType") or hint.get("returnType") or "").strip() or assigned_type
            declaring_hint = str(hint.get("resolvedDeclaringType") or hint.get("declaringType") or "").strip() or None
            parameter_hints_value = hint.get("resolvedParameterTypes") or hint.get("parameterTypes") or []
            parameter_hints = tuple(str(x) for x in parameter_hints_value) if isinstance(parameter_hints_value, list) else ()
            contract = resolve_method_contract(
                dependency,
                method_name,
                args,
                lookup,
                return_type_hint=return_hint,
                declaring_type_hint=declaring_hint,
                parameter_type_hints=parameter_hints,
            )
            invocation_id = f"{owner_id}:I{counter}"
            invocations.append(
                DependencyInvocation(
                    invocation_id=invocation_id,
                    caller_method_id=owner_id,
                    dependency_field=field,
                    dependency_type=dependency.declared_type,
                    method_name=method_name,
                    arguments=args,
                    contract=contract,
                    assigned_to=assigned_to,
                    line=line,
                    branch_id=_branch_for_offset(scopes, match.start()),
                )
            )
            counter += 1
    return invocations


def _helper_calls(
    method: MethodSig,
    text: str,
    base_line: int,
    private_by_key: dict[tuple[str, int], str],
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]],
) -> list[PrivateHelperCall]:
    owner_id = _method_id(method)
    body_start = text.find("{")
    body = text[body_start + 1:] if body_start >= 0 else text
    body_offset = body_start + 1 if body_start >= 0 else 0
    out: list[PrivateHelperCall] = []
    for (name, arity), helper_id in private_by_key.items():
        pattern = re.compile(rf"(?<![\w$])(?:this\s*\.\s*)?{re.escape(name)}\s*\(")
        for match in pattern.finditer(body):
            absolute = body_offset + match.start()
            open_paren = text.find("(", absolute)
            close_paren = _find_matching(text, open_paren, "(", ")")
            if close_paren is None:
                continue
            args = _split_arguments(text[open_paren + 1:close_paren])
            if len(args) != arity:
                continue
            out.append(
                PrivateHelperCall(
                    caller_method_id=owner_id,
                    helper_method_id=helper_id,
                    arguments=args,
                    line=_line_for_offset(text, base_line, absolute),
                    branch_id=_branch_for_offset(scopes, absolute),
                )
            )
    return out


def _getter_property(name: str) -> str:
    if name.startswith("get") and len(name) > 3:
        return name[3:4].lower() + name[4:]
    if name.startswith("is") and len(name) > 2:
        return name[2:3].lower() + name[3:]
    return name


def _reads_for_variable(
    owner_id: str,
    text: str,
    base_line: int,
    variable: str,
    invocation: DependencyInvocation,
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]],
) -> list[ReturnObjectRead]:
    out: list[ReturnObjectRead] = []
    chain_pattern = re.compile(
        rf"\b{re.escape(variable)}\b(?P<chain>(?:\s*\.\s*(?:get|is)[A-Za-z_$][\w$]*\s*\(\s*\))+)"
    )
    for match in chain_pattern.finditer(text):
        methods = re.findall(r"\.\s*((?:get|is)[A-Za-z_$][\w$]*)\s*\(", match.group("chain"))
        path: list[str] = []
        for method_name in methods:
            member = _getter_property(method_name)
            path.append(member)
            line = _line_for_offset(text, base_line, match.start())
            out.append(
                ReturnObjectRead(
                    owner_method_id=owner_id,
                    invocation_id=invocation.invocation_id,
                    variable_name=variable,
                    declared_type=invocation.contract.return_type,
                    access_kind="GETTER",
                    member_name=member,
                    property_path=".".join(path),
                    line=line,
                    branch_id=_branch_for_offset(scopes, match.start()),
                )
            )
    field_pattern = re.compile(rf"\b{re.escape(variable)}\s*\.\s*([a-zA-Z_$][\w$]*)\b(?!\s*\()")
    for match in field_pattern.finditer(text):
        member = match.group(1)
        if member in {"class"}:
            continue
        line = _line_for_offset(text, base_line, match.start())
        out.append(
            ReturnObjectRead(
                owner_method_id=owner_id,
                invocation_id=invocation.invocation_id,
                variable_name=variable,
                declared_type=invocation.contract.return_type,
                access_kind="FIELD",
                member_name=member,
                property_path=member,
                line=line,
                branch_id=_branch_for_offset(scopes, match.start()),
            )
        )
    return out


def _direct_invocation_reads(
    owner_id: str,
    text: str,
    base_line: int,
    invocation: DependencyInvocation,
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]],
) -> list[ReturnObjectRead]:
    pattern = re.compile(
        rf"\b{re.escape(invocation.dependency_field)}\s*\.\s*"
        rf"{re.escape(invocation.method_name)}\s*\("
    )
    for match in pattern.finditer(text):
        if invocation.line is not None and _line_for_offset(text, base_line, match.start()) != invocation.line:
            continue
        open_paren = text.find("(", match.start())
        close_paren = _find_matching(text, open_paren, "(", ")")
        if close_paren is None:
            continue
        tail = text[close_paren + 1:]
        chain = re.match(r"(?P<chain>(?:\s*\.\s*(?:get|is)[A-Za-z_$][\w$]*\s*\(\s*\))+)", tail)
        if not chain:
            return []
        method_names = re.findall(r"\.\s*((?:get|is)[A-Za-z_$][\w$]*)\s*\(", chain.group("chain"))
        path: list[str] = []
        reads: list[ReturnObjectRead] = []
        for method_name in method_names:
            member = _getter_property(method_name)
            path.append(member)
            reads.append(
                ReturnObjectRead(
                    owner_method_id=owner_id,
                    invocation_id=invocation.invocation_id,
                    variable_name="<direct-call>",
                    declared_type=invocation.contract.return_type,
                    access_kind="GETTER",
                    member_name=member,
                    property_path=".".join(path),
                    line=invocation.line,
                    branch_id=invocation.branch_id,
                )
            )
        return reads
    return []


def _return_reads(
    method: MethodSig,
    text: str,
    base_line: int,
    invocations: Iterable[DependencyInvocation],
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]],
) -> list[ReturnObjectRead]:
    owner_id = _method_id(method)
    out: list[ReturnObjectRead] = []
    for invocation in invocations:
        if invocation.assigned_to:
            out.extend(
                _reads_for_variable(
                    owner_id,
                    text,
                    base_line,
                    invocation.assigned_to,
                    invocation,
                    scopes,
                )
            )
        else:
            out.extend(_direct_invocation_reads(owner_id, text, base_line, invocation, scopes))
    return _dedupe_reads(out)


def _dedupe_reads(reads: Iterable[ReturnObjectRead]) -> list[ReturnObjectRead]:
    unique: dict[tuple[str, str, int | None, str], ReturnObjectRead] = {}
    for read in reads:
        key = (read.invocation_id, read.property_path, read.line, read.owner_method_id)
        unique[key] = read
    return list(unique.values())


def _propagated_return_reads(
    related_ids: tuple[str, ...],
    method_data: dict[str, dict[str, object]],
) -> list[ReturnObjectRead]:
    queue: list[tuple[str, str, DependencyInvocation]] = []
    reads: list[ReturnObjectRead] = []
    for related_id in related_ids:
        data = method_data.get(related_id, {})
        reads.extend(data.get("reads", []))  # type: ignore[arg-type]
        for invocation in data.get("invocations", []):  # type: ignore[assignment]
            if isinstance(invocation, DependencyInvocation) and invocation.assigned_to:
                queue.append((related_id, invocation.assigned_to, invocation))

    seen: set[tuple[str, str, str]] = set()
    while queue:
        owner_id, variable, invocation = queue.pop(0)
        key = (owner_id, variable, invocation.invocation_id)
        if key in seen:
            continue
        seen.add(key)
        data = method_data.get(owner_id, {})
        text = str(data.get("text", ""))
        base_line = int(data.get("base_line", 1))
        scopes = data.get("scopes", [])  # type: ignore[assignment]
        reads.extend(
            _reads_for_variable(
                owner_id,
                text,
                base_line,
                variable,
                invocation,
                scopes,  # type: ignore[arg-type]
            )
        )

        for helper in data.get("helpers", []):  # type: ignore[assignment]
            if not isinstance(helper, PrivateHelperCall):
                continue
            helper_data = method_data.get(helper.helper_method_id, {})
            helper_method = helper_data.get("method")
            if not isinstance(helper_method, MethodSig):
                continue
            for index, argument in enumerate(helper.arguments):
                if argument.strip() != variable or index >= len(helper_method.params):
                    continue
                _, parameter_name = helper_method.params[index]
                queue.append((helper.helper_method_id, parameter_name, invocation))
    return _dedupe_reads(reads)


def _populate_branch_calls(
    scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]],
    invocations: list[DependencyInvocation],
) -> list[BranchFact]:
    out: list[BranchFact] = []
    for fact, _, _ in scopes:
        true_prefix = f"{fact.branch_id}:true"
        false_prefix = f"{fact.branch_id}:false"
        out.append(
            replace(
                fact,
                true_path_calls=tuple(i.invocation_id for i in invocations if i.branch_id == true_prefix),
                false_path_calls=tuple(i.invocation_id for i in invocations if i.branch_id == false_prefix),
            )
        )
    return out


def _annotation_window(source: str, line: int | None, before: int = 12, after: int = 4) -> str:
    lines = source.splitlines()
    if not line:
        return ""
    start = max(0, line - before - 1)
    end = min(len(lines), line + after)
    return "\n".join(lines[start:end])


def _annotation_args(block: str, name: str) -> list[str]:
    out: list[str] = []
    pattern = re.compile(rf"@{re.escape(name)}\s*(?:\((.*?)\))?", re.DOTALL)
    for match in pattern.finditer(block):
        out.append(match.group(1) or "")
    return out


def _quoted_values(args: str) -> tuple[str, ...]:
    values = re.findall(r'"((?:\\.|[^"\\])*)"', args or "")
    return tuple(dict.fromkeys(values))


def _paths_from_mapping_args(args: str) -> tuple[str, ...]:
    if not args.strip():
        return ("",)
    path_match = re.search(r"(?:value|path)\s*=\s*(\{[^}]*\}|\"[^\"]*\")", args, re.DOTALL)
    values = _quoted_values(path_match.group(1) if path_match else args)
    return values or ("",)


def _combine_paths(class_paths: tuple[str, ...], method_paths: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for left in class_paths or ("",):
        for right in method_paths or ("",):
            combined = "/" + "/".join(x.strip("/") for x in (left, right) if x.strip("/"))
            if combined not in out:
                out.append(combined or "/")
    return tuple(out)


def _parse_request_parameters(method: MethodSig, method_text: str) -> tuple[RequestParameter, ...]:
    header = method_text.split("{", 1)[0]
    declaration = re.search(rf"\b{re.escape(method.name)}\s*\(", header)
    open_paren = header.find("(", declaration.start()) if declaration else -1
    if open_paren < 0:
        return tuple(RequestParameter(name, name, "UNANNOTATED", ptype) for ptype, name in method.params)
    close_paren = _find_matching(header, open_paren, "(", ")")
    raw_params = _split_arguments(header[open_paren + 1:close_paren] if close_paren is not None else "")
    by_name = {name: ptype for ptype, name in method.params}
    result: list[RequestParameter] = []
    for raw in raw_params:
        name_match = re.search(r"([A-Za-z_$][\w$]*)\s*$", raw.strip())
        if not name_match:
            continue
        java_name = name_match.group(1)
        type_name = by_name.get(java_name, "Object")
        source = "UNANNOTATED"
        wire_name = java_name
        required: bool | None = None
        default_value: str | None = None
        for anno, binding in _BINDING_ANNOS.items():
            anno_match = re.search(rf"@{anno}\s*(?:\((.*?)\))?", raw, re.DOTALL)
            if not anno_match:
                continue
            source = binding
            args = anno_match.group(1) or ""
            quoted = _quoted_values(args)
            if quoted:
                wire_name = quoted[0]
            req = re.search(r"required\s*=\s*(true|false)", args)
            if req:
                required = req.group(1) == "true"
            default = re.search(r'defaultValue\s*=\s*"((?:\\.|[^"\\])*)"', args)
            if default:
                default_value = default.group(1)
            break
        simple = simple_type_name(type_name)
        if source == "UNANNOTATED" and simple in {"HttpServletRequest", "ServletRequest"}:
            source = "SERVLET_REQUEST"
        elif source == "UNANNOTATED" and simple in {"HttpServletResponse", "ServletResponse"}:
            source = "SERVLET_RESPONSE"
        result.append(RequestParameter(java_name, wire_name, source, type_name, required, default_value))
    return tuple(result)


def _controller_endpoint(
    source_file: JavaSourceFile,
    symbol: ClassSymbol,
    method: MethodSig,
    method_text: str,
    method_line: int,
) -> ControllerEndpoint:
    class_block = _annotation_window(source_file.source, symbol.line, before=16, after=1)
    method_block = _annotation_window(source_file.source, method_line, before=10, after=2)
    class_paths: tuple[str, ...] = ("",)
    class_request_args = _annotation_args(class_block, "RequestMapping")
    if class_request_args:
        class_paths = _paths_from_mapping_args(class_request_args[-1])

    http_methods: tuple[str, ...] = ()
    method_paths: tuple[str, ...] = ("",)
    for mapping, methods in _MAPPING_HTTP.items():
        args_list = _annotation_args(method_block, mapping)
        if args_list:
            http_methods = methods
            method_paths = _paths_from_mapping_args(args_list[-1])
            break
    if not http_methods:
        args_list = _annotation_args(method_block, "RequestMapping")
        if args_list:
            args = args_list[-1]
            request_methods = re.findall(r"RequestMethod\.([A-Z]+)", args)
            http_methods = tuple(request_methods)
            method_paths = _paths_from_mapping_args(args)

    return ControllerEndpoint(
        method_id=_method_id(method),
        http_methods=http_methods,
        class_paths=class_paths,
        method_paths=method_paths,
        resolved_paths=_combine_paths(class_paths, method_paths),
        request_parameters=_parse_request_parameters(method, method_text),
        response_type=method.return_type,
    )


def _reachable_helpers(start: str, graph: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    found: list[str] = []
    visited: set[str] = set()

    def visit(node: str, depth: int) -> None:
        if depth >= MAX_PRIVATE_HELPER_DEPTH:
            return
        for nxt in graph.get(node, ()):
            if nxt in visited:
                continue
            visited.add(nxt)
            found.append(nxt)
            visit(nxt, depth + 1)

    visit(start, 0)
    return tuple(found)





def _fallback_configuration_requirements(
    method: MethodSig,
    method_text: str,
    configuration_fields: Iterable[ConfigurationField],
) -> list[ConfigurationRequirement]:
    owner_id = _method_id(method)
    out: list[ConfigurationRequirement] = []
    for field in configuration_fields:
        if re.search(rf"(?<![\w$])(?:this\s*\.\s*)?{re.escape(field.field_name)}(?![\w$])", method_text):
            out.append(
                ConfigurationRequirement(
                    field_name=field.field_name,
                    expression=field.field_name,
                    line=method.line,
                )
            )
    return out


def _replace_dereference_root(
    requirement: DereferenceRequirement,
    root: str,
    prefix_segments: tuple[str, ...],
) -> DereferenceRequirement:
    original_parts = requirement.full_path.split(".")
    suffix = tuple(original_parts[1:])
    all_segments = (*prefix_segments, *suffix)
    full_path = ".".join((root, *all_segments)) if all_segments else root
    non_null = [root]
    for index in range(max(0, len(all_segments) - 1)):
        non_null.append(".".join((root, *all_segments[: index + 1])))
    return replace(
        requirement,
        root_variable=root,
        full_path=full_path,
        terminal_path=full_path,
        non_null_prefixes=tuple(dict.fromkeys(non_null)),
    )


def _propagated_dereference_requirements(
    entry_method_id: str,
    method_data: dict[str, dict[str, object]],
) -> list[DereferenceRequirement]:
    """Propagate helper dereferences with depth/state caps.

    Recursive helper graphs and changing substitutions previously allowed an
    unbounded number of states.  The selected class is still analysed deeply,
    but propagation is capped to a deterministic, reportable budget.
    """
    queue: list[tuple[str, dict[str, tuple[str, tuple[str, ...]]], int]] = [
        (entry_method_id, {}, 0)
    ]
    seen_states: set[tuple[str, tuple[tuple[str, str, tuple[str, ...]], ...]]] = set()
    out: list[DereferenceRequirement] = []
    dedupe: set[tuple[str, int | None, str | None, str | None]] = set()
    processed = 0

    while queue and processed < MAX_HELPER_PROPAGATION_STATES:
        method_id, substitutions, depth = queue.pop(0)
        if depth > MAX_PRIVATE_HELPER_DEPTH:
            continue
        state_key = (
            method_id,
            tuple(sorted((name, root, segments) for name, (root, segments) in substitutions.items())),
        )
        if state_key in seen_states:
            continue
        seen_states.add(state_key)
        processed += 1
        data = method_data.get(method_id, {})
        for requirement in data.get("dereferences", []):  # type: ignore[assignment]
            if not isinstance(requirement, DereferenceRequirement):
                continue
            mapped = requirement
            if requirement.root_variable in substitutions:
                root, segments = substitutions[requirement.root_variable]
                mapped = _replace_dereference_root(requirement, root, segments)
            key = (mapped.full_path, mapped.line, mapped.branch_id, mapped.branch_arm)
            if key not in dedupe:
                dedupe.add(key)
                out.append(mapped)

        if depth >= MAX_PRIVATE_HELPER_DEPTH:
            continue
        caller_substitutions = substitutions
        for helper_call in data.get("helpers", []):  # type: ignore[assignment]
            if not isinstance(helper_call, PrivateHelperCall):
                continue
            helper_data = method_data.get(helper_call.helper_method_id, {})
            helper_method = helper_data.get("method")
            if not isinstance(helper_method, MethodSig):
                continue
            helper_mapping: dict[str, tuple[str, tuple[str, ...]]] = {}
            for index, argument in enumerate(helper_call.arguments):
                if index >= len(helper_method.params):
                    continue
                _, parameter_name = helper_method.params[index]
                parsed = _parse_member_expression(argument)
                if parsed is None:
                    continue
                root, segments = parsed
                if root in caller_substitutions:
                    parent_root, parent_segments = caller_substitutions[root]
                    helper_mapping[parameter_name] = (parent_root, (*parent_segments, *segments))
                else:
                    helper_mapping[parameter_name] = (root, segments)
            queue.append((helper_call.helper_method_id, helper_mapping, depth + 1))

    if queue:
        timing_event(
            "execution.helper_propagation.capped",
            method=entry_method_id,
            processed=processed,
            remaining=len(queue),
            max_states=MAX_HELPER_PROPAGATION_STATES,
            max_depth=MAX_PRIVATE_HELPER_DEPTH,
        )
    return out

def _method_declared_in_source(source: str, method: MethodSig) -> bool:
    return_type = re.escape(method.return_type or "void").replace(r"\ ", r"\s*")
    pattern = re.compile(rf"\b{return_type}\s+{re.escape(method.name)}\s*\(")
    return bool(pattern.search(source))

def build_execution_context(
    *,
    source_file: JavaSourceFile,
    symbol: ClassSymbol,
    lookup: LookupFn,
    target_kind: ExecutionTargetKind,
    configuration_values: dict[str, str] | None = None,
) -> ExecutionContext | None:
    """Build one isolated execution context per public Controller/ServiceImpl method."""
    if target_kind not in {ExecutionTargetKind.CONTROLLER, ExecutionTargetKind.SERVICE_IMPL}:
        return None

    context_started = perf_counter()
    timing_event(
        "execution.context.start",
        target=symbol.fqcn or symbol.name,
        target_kind=target_kind.value,
        total_methods=len(symbol.methods or ()),
    )
    dependency_started = perf_counter()
    dependencies, diagnostics = resolve_dependency_refs(source_file, symbol, lookup)
    timing_event(
        "execution.dependencies.end",
        elapsed=perf_counter() - dependency_started,
        target=symbol.name,
        dependencies=len(dependencies),
    )
    configuration_fields = _configuration_fields(symbol, configuration_values)
    all_methods = list(symbol.methods or [])
    private_methods = [method for method in all_methods if "private" in method.modifiers]
    private_by_key = {(method.name, method.arity): _method_id(method) for method in private_methods}
    field_types = {field.name: field.type for field in (symbol.fields or [])}

    method_data: dict[str, dict[str, object]] = {}
    graph: dict[str, tuple[str, ...]] = {}
    used_cli_facts = False

    for method_index, method in enumerate(all_methods, 1):
        method_started = perf_counter()
        text, base_line = _method_source(source_file.source, method)
        owner_id = _method_id(method)
        timing_event(
            "execution.method_facts.start",
            target=symbol.name,
            method=owner_id,
            index=method_index,
            total=len(all_methods),
        )
        authoritative = _has_authoritative_body_facts(method)
        used_cli_facts = used_cli_facts or authoritative

        if authoritative:
            branches = _cli_branches(method)
            invocations = _cli_dependency_invocations(
                method,
                text,
                dependencies,
                lookup,
                field_types,
            )
            helpers = _cli_helper_calls(method, private_by_key)
            member_reads = _cli_member_reads(method)
            reads = _cli_return_reads(method, invocations)
            null_guards = _cli_null_guards(method)
            exits = _cli_exits(method)
            local_variables = _cli_local_variables(method)
            assignments = _cli_assignments(method)
            configuration_requirements = _cli_configuration_requirements(method)
            line_facts = _cli_line_facts(method)
            dereferences = _cli_dereference_requirements(
                method,
                local_variables,
                {name for _, name in method.params},
                {invocation.assigned_to for invocation in invocations if invocation.assigned_to},
            )
            branches = _populate_cli_branch_calls(branches, invocations)
            scopes: list[tuple[BranchFact, tuple[int, int] | None, tuple[int, int] | None]] = []
        else:
            scopes = _parse_if_scopes(text, base_line, owner_id)
            invocations = _extract_dependency_invocations(
                method,
                text,
                base_line,
                dependencies,
                lookup,
                field_types,
                scopes,
            )
            helpers = _helper_calls(method, text, base_line, private_by_key, scopes)
            reads = _return_reads(method, text, base_line, invocations, scopes)
            branches = _populate_branch_calls(scopes, invocations)
            member_reads = []
            null_guards = []
            exits = []
            local_variables = []
            assignments = []
            configuration_requirements = _fallback_configuration_requirements(
                method, text, configuration_fields
            )
            line_facts = []
            dereferences = []

        method_data[owner_id] = {
            "method": method,
            "text": text,
            "base_line": base_line,
            "scopes": scopes,
            "invocations": invocations,
            "helpers": helpers,
            "reads": reads,
            "member_reads": member_reads,
            "null_guards": null_guards,
            "exits": exits,
            "branches": branches,
            "local_variables": local_variables,
            "assignments": assignments,
            "configuration_requirements": configuration_requirements,
            "line_facts": line_facts,
            "dereferences": dereferences,
            "authoritative": authoritative,
        }
        graph[owner_id] = tuple(helper.helper_method_id for helper in helpers)
        timing_event(
            "execution.method_facts.end",
            elapsed=perf_counter() - method_started,
            target=symbol.name,
            method=owner_id,
            authoritative=authoritative,
            invocations=len(invocations),
            helpers=len(helpers),
            branches=len(branches),
            dereferences=len(dereferences),
            locals=len(local_variables),
        )

    mapping_annotations = {
        "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
        "PatchMapping", "RequestMapping",
    }
    public_methods = [
        method for method in all_methods
        if "private" not in method.modifiers
        and not method.is_static
        and not method.is_abstract
        and not method.is_constructor
        and "LombokGenerated" not in {a.split(".")[-1] for a in (method.annotations or [])}
        and (
            method.is_public
            or (
                target_kind == ExecutionTargetKind.CONTROLLER
                and bool(set(method.annotations or []) & mapping_annotations)
            )
        )
        and _method_declared_in_source(source_file.source, method)
    ]

    contexts: list[MethodExecutionContext] = []
    global_schemas: dict[str, object] = {}
    schema_cache: dict[tuple[str, tuple[tuple[str, ...], ...], int], object] = {}

    timing_event(
        "execution.public_methods.selected",
        target=symbol.name,
        public_methods=len(public_methods),
        private_methods=len(private_methods),
    )

    for public_index, method in enumerate(public_methods, 1):
        public_started = perf_counter()
        owner_id = _method_id(method)
        timing_event(
            "execution.public_method.start",
            target=symbol.name,
            method=owner_id,
            index=public_index,
            total=len(public_methods),
        )
        data = method_data.get(owner_id, {})
        reachable = _reachable_helpers(owner_id, graph)
        related_ids = (owner_id, *reachable)

        invocations: list[DependencyInvocation] = []
        branches: list[BranchFact] = []
        reads: list[ReturnObjectRead] = []
        member_reads: list[MemberRead] = []
        null_guards: list[NullGuard] = []
        exits: list[ExitFact] = []
        local_variables: list[LocalVariableFact] = []
        assignments: list[AssignmentFact] = []
        configuration_requirements: list[ConfigurationRequirement] = []
        line_facts: list[LineFact] = []
        for related_id in related_ids:
            related = method_data.get(related_id, {})
            invocations.extend(related.get("invocations", []))  # type: ignore[arg-type]
            branches.extend(related.get("branches", []))  # type: ignore[arg-type]
            reads.extend(related.get("reads", []))  # type: ignore[arg-type]
            member_reads.extend(related.get("member_reads", []))  # type: ignore[arg-type]
            null_guards.extend(related.get("null_guards", []))  # type: ignore[arg-type]
            exits.extend(related.get("exits", []))  # type: ignore[arg-type]
            local_variables.extend(related.get("local_variables", []))  # type: ignore[arg-type]
            assignments.extend(related.get("assignments", []))  # type: ignore[arg-type]
            configuration_requirements.extend(related.get("configuration_requirements", []))  # type: ignore[arg-type]
            line_facts.extend(related.get("line_facts", []))  # type: ignore[arg-type]

        propagation_started = perf_counter()
        dereference_requirements = _propagated_dereference_requirements(owner_id, method_data)
        timing_event(
            "execution.dereference_propagation.end",
            elapsed=perf_counter() - propagation_started,
            target=symbol.name,
            method=owner_id,
            requirements=len(dereference_requirements),
            reachable_helpers=len(reachable),
        )

        input_types = tuple(type_name for type_name, _ in method.params)
        output_types = (method.return_type,) if method.return_type else ()
        parameter_types = {name: type_name for type_name, name in method.params}

        # Roots are retained, but nested schema expansion is authorized only by
        # property paths actually read by this public method or reachable helper.
        method_seed_types: list[str] = [*input_types, *output_types]
        path_requests: list[tuple[str, str]] = []
        for requirement in dereference_requirements:
            root_type = parameter_types.get(requirement.root_variable)
            if not root_type:
                continue
            prefix = requirement.root_variable + "."
            relative = (
                requirement.full_path[len(prefix):]
                if requirement.full_path.startswith(prefix)
                else requirement.full_path
            )
            path_requests.append((root_type, relative))
        for read in member_reads:
            root_type = parameter_types.get(read.variable_name)
            if root_type:
                path_requests.append((root_type, read.property_path))
        for read in reads:
            if read.declared_type:
                path_requests.append((read.declared_type, read.property_path))
        for invocation in invocations:
            # Collaborators are mocked. Only their returned payload root is
            # needed; their own parameter/dependency graphs are not expanded.
            if invocation.contract.return_type:
                method_seed_types.append(invocation.contract.return_type)

        method_seed_types = list(dict.fromkeys(method_seed_types))
        path_requests = list(dict.fromkeys(path_requests))
        timing_event(
            "execution.payload.start",
            target=symbol.name,
            method=owner_id,
            seed_types=len(method_seed_types),
            accessed_paths=len(path_requests),
        )
        payload_started = perf_counter()
        schemas, schema_diagnostics = build_payload_schemas(
            method_seed_types,
            lookup,
            path_requests=path_requests,
            max_depth=MAX_PAYLOAD_DEPTH,
            max_types=MAX_PAYLOAD_TYPES_PER_METHOD,
            schema_cache=schema_cache,  # type: ignore[arg-type]
            trace_label=owner_id,
        )
        timing_event(
            "execution.payload.end",
            elapsed=perf_counter() - payload_started,
            target=symbol.name,
            method=owner_id,
            schemas=len(schemas),
            diagnostics=len(schema_diagnostics),
        )
        legacy_required_paths = required_object_paths(
            ((read.variable_name, read.property_path) for read in member_reads),
            parameter_types,
            schemas,
        )
        required_paths = tuple(dict.fromkeys(
            prefix
            for requirement in dereference_requirements
            for prefix in requirement.non_null_prefixes
        )) or legacy_required_paths
        for schema in schemas:
            key = schema.fqcn or schema.type_name
            global_schemas[key] = schema

        text = str(data.get("text", ""))
        actual_line = int(data.get("base_line", method.line or 1))
        endpoint = (
            _controller_endpoint(source_file, symbol, method, text, actual_line)
            if target_kind == ExecutionTargetKind.CONTROLLER
            else None
        )
        method_diagnostics = list(schema_diagnostics)
        if not bool(data.get("authoritative")):
            method_diagnostics.append(
                "JavaParser bodyFacts unavailable; source-regex fallback used for this method"
            )

        contexts.append(
            MethodExecutionContext(
                method_id=owner_id,
                signature=method.render(),
                source_range=(actual_line, actual_line + max(0, text.count("\n"))) if text else None,
                method_source=text,
                endpoint=endpoint,
                dependency_invocations=tuple(invocations),
                branches=tuple(branches),
                direct_private_helpers=tuple(data.get("helpers", [])),  # type: ignore[arg-type]
                reachable_private_methods=reachable,
                member_reads=tuple(member_reads),
                return_object_reads=tuple(_dedupe_reads(reads)),
                null_guards=tuple(null_guards),
                exits=tuple(exits),
                required_object_paths=required_paths,
                dereference_requirements=tuple(dereference_requirements),
                local_variables=tuple(local_variables),
                assignments=tuple(assignments),
                configuration_requirements=tuple({
                    (requirement.field_name, requirement.line, requirement.branch_id, requirement.branch_arm): requirement
                    for requirement in configuration_requirements
                }.values()),
                line_facts=tuple(line_facts),
                input_payload_types=tuple(
                    type_name for type_name in input_types if extract_application_type_names(type_name)
                ),
                output_payload_types=tuple(
                    type_name for type_name in output_types if extract_application_type_names(type_name)
                ),
                payload_schemas=tuple(schemas),
                diagnostics=tuple(dict.fromkeys(method_diagnostics)),
            )
        )
        timing_event(
            "execution.public_method.end",
            elapsed=perf_counter() - public_started,
            target=symbol.name,
            method=owner_id,
            invocations=len(invocations),
            branches=len(branches),
            schemas=len(schemas),
            required_paths=len(required_paths),
        )

    contracts = [
        invocation.contract
        for method_context in contexts
        for invocation in method_context.dependency_invocations
    ]
    unresolved_contracts = [
        contract for contract in contracts
        if contract.resolution_status not in {ResolutionStatus.RESOLVED, ResolutionStatus.INHERITED}
    ]
    unresolved_schemas = [
        schema
        for method_context in contexts
        for schema in method_context.payload_schemas
        if schema.resolution_status != ResolutionStatus.RESOLVED
    ]
    status = (
        ResolutionStatus.RESOLVED
        if not unresolved_contracts and not unresolved_schemas and used_cli_facts
        else ResolutionStatus.PARTIAL
    )
    if not contexts:
        diagnostics.append("no source-declared public instance methods detected for execution context")

    timing_event(
        "execution.context.end",
        elapsed=perf_counter() - context_started,
        target=symbol.name,
        methods=len(contexts),
        schemas=len(global_schemas),
        status=status.value,
    )
    return ExecutionContext(
        target_kind=target_kind,
        target_fqcn=symbol.fqcn,
        dependencies=tuple(dependencies),
        configuration_fields=tuple(configuration_fields),
        methods=tuple(contexts),
        payload_schemas=tuple(global_schemas.values()),  # type: ignore[arg-type]
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        extraction_status=status,
        metadata={
            "private_helper_depth": str(MAX_PRIVATE_HELPER_DEPTH),
            "source_parser": "javaparser-cli-bodyFacts" if used_cli_facts else "source-fallback",
            "generation_mode": "one-llm-call-per-public-method",
            "payload_depth": str(MAX_PAYLOAD_DEPTH),
            "payload_types_per_method": str(MAX_PAYLOAD_TYPES_PER_METHOD),
            "payload_mode": "accessed-path-projection",
        },
    )

