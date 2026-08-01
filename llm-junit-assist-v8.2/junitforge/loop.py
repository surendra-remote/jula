"""Per-target generation engine + coverage-gap-driven regeneration loop.

Flow per class:
  generate -> finalize (re-ask once) -> compile gate (repair, bounded) ->
  coverage (JaCoCo) -> augment uncovered lines/branches (bounded, until target /
  no-progress / budget) -> honest outcome.

Never ships a non-compiling test: on repeated compile failure it reverts to the
last compiling snapshot.
"""

from __future__ import annotations

import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from junitforge.timing import event as timing_event, heartbeat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from junitforge.compile_gate import CompileGate, parse_maven_compiler_errors
from junitforge.config import GenConfig
from junitforge.kb import KbIndex
from junitforge.context import build_context, collapse_ranges, slice_uncovered
from junitforge.coverage_parse import parse_jacoco_xml
from junitforge.coverage_run import CoverageRunner
from junitforge.execution.analyzer import build_execution_context
from junitforge.execution.assembly import (
    build_test_skeleton,
    extract_corrected_scope,
    extract_test_method_block,
    generated_scopes,
    insert_method_block,
    inject_reusable_fixtures,
    method_block_for_id,
    normalize_writable_map_mutations,
    offending_statement,
    owner_method_id_for_line,
    replace_generated_scopes,
    replace_method_block,
    scope_for_line,
    test_method_names,
    validate_scoped_class_repair,
    validate_method_block,
)
from junitforge.execution.renderer import (
    write_execution_context_json,
    write_fixture_catalog_json,
    write_schema_catalog_json,
)
from junitforge.discovery import (
    is_test_target,
    module_for_source,
    test_package_for,
    test_path_for,
)
from typing import Callable as _Callable
from junitforge.models import (
    ClassExecutionResult,
    ClassSymbol,
    CompileError,
    GeneratedScope,
    GeneratedScopeKind,
    GenerationContext,
    ModuleInfo,
    StackProfile,
    TargetOutcome,
    TestMethodResult,
    TestMethodStatus,
)
from junitforge.parser.collaborators import resolve_collaborators
from junitforge.parser.java_symbols import parse_file
from junitforge.postprocess import finalize_java
from junitforge.prompts.augmentation import build_augmentation_messages, build_method_augmentation_messages
from junitforge.prompts.generation import build_generation_messages, build_method_generation_messages
from junitforge.prompts.repair import (
    build_compile_repair_messages,
    build_method_compile_repair_messages,
    build_method_generation_validation_repair_messages,
    build_method_runtime_repair_messages,
    build_scoped_compile_repair_messages,
    build_scoped_execution_repair_messages,
    build_test_failure_repair_messages,
)
from junitforge.stack.classifier import classify, classify_execution_target
from junitforge.stack.classpath_profile import from_classpath_string, from_pom_artifacts
from junitforge.vendor.llm.base import LLMClient
from junitforge.vendor.logging_utils import get

log = get(__name__)

_TESTS_RUN = re.compile(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)")


@dataclass
class Engine:
    llm: LLMClient
    compile_gate: CompileGate
    coverage_runner: CoverageRunner | None
    stack: StackProfile
    modules: list[ModuleInfo]
    repo_root: Path
    lookup: Callable[[str], ClassSymbol | None]
    cfg: GenConfig
    kb: KbIndex | None = None
    test_path_fn: _Callable = test_path_for
    symbol_resolver: object | None = None
    configuration_values: dict[str, str] | None = None

    # -- public ------------------------------------------------------------

    def run_target(self, source_path: Path) -> TargetOutcome:
        target_started = perf_counter()
        log.info("[TIMING] target start | %s", source_path)
        timing_event("engine.target.start", file=source_path)
        _t = perf_counter()
        if self.symbol_resolver is not None and hasattr(self.symbol_resolver, "parse_target"):
            jf = self.symbol_resolver.parse_target(source_path)
        else:
            jf = parse_file(source_path)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] target parse: %.3fs | %s", _elapsed, source_path.name)
        timing_event("engine.target.parse.end", elapsed=_elapsed, file=source_path.name)
        symbol = jf.primary_type
        module = module_for_source(self.modules, source_path)
        outcome = TargetOutcome(
            fqcn=(symbol.fqcn if symbol else source_path.stem),
            module=(module.name if module else ""),
            source_path=str(source_path), test_path="", classification="", testable=True,
        )

        ok, why = is_test_target(jf)
        if not ok or symbol is None or module is None:
            outcome.status, outcome.testable, outcome.classification = "skipped", False, "n/a"
            outcome.notes.append(why if not ok else "no module/type")
            return outcome

        cp = self._classpath_profile(module)
        template = classify(symbol, self.stack, cp)
        outcome.classification = template.kind.value
        if not template.testable:
            outcome.status, outcome.testable = "integration_only", False
            outcome.notes.extend(template.reasons)
            return outcome
        if not cp.has_junit_jupiter:
            outcome.status, outcome.testable = "skipped", False
            outcome.notes.append(
                f"module '{module.name}' has no JUnit on its test classpath "
                "(add spring-boot-starter-test to generate tests here)")
            return outcome

        test_pkg = test_package_for(symbol)
        test_class = f"{symbol.name}Test"
        test_path = self.test_path_fn(source_path, module, symbol)
        if test_path.exists() and not self.cfg.overwrite:
            outcome.status = "skipped"
            outcome.notes.append("test exists (use --overwrite)")
            outcome.test_path = str(test_path)
            return outcome
        if test_path.exists() and self.cfg.overwrite:
            # Never clobber an existing test: it may define helper/nested classes
            # other tests depend on (deleting it breaks the module's test-compile).
            # Generate side-by-side with a distinct name instead.
            test_class = f"{symbol.name}JfTest"
            test_path = test_path.parent / f"{test_class}.java"
            outcome.notes.append(f"existing test preserved; generated side-by-side as {test_class}")
        outcome.test_path = str(test_path)

        _t = perf_counter()
        collaborators = resolve_collaborators(symbol, self.lookup)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] collaborator resolution: %.3fs | count=%d", _elapsed, len(collaborators))
        timing_event("engine.collaborators.end", elapsed=_elapsed, count=len(collaborators))
        execution_target = classify_execution_target(symbol)
        _t = perf_counter()
        with heartbeat(
            "engine.execution_context",
            interval_seconds=15.0,
            target=symbol.name,
        ):
            execution_context = build_execution_context(
                source_file=jf,
                symbol=symbol,
                lookup=self.lookup,
                target_kind=execution_target,
                configuration_values=self.configuration_values,
                schema_lookup=(
                    self.symbol_resolver.lookup_context
                    if self.symbol_resolver is not None and hasattr(self.symbol_resolver, "lookup_context")
                    else self.lookup
                ),
            )
        _elapsed = perf_counter() - _t
        log.info(
            "[TIMING] execution context: %.3fs | methods=%d | schemas=%d",
            _elapsed,
            len(execution_context.methods) if execution_context else 0,
            len(execution_context.payload_schemas) if execution_context else 0,
        )
        timing_event(
            "engine.execution_context.complete",
            elapsed=_elapsed,
            target=symbol.name,
            methods=len(execution_context.methods) if execution_context else 0,
            schemas=len(execution_context.payload_schemas) if execution_context else 0,
        )
        if execution_context is not None:
            diagnostic_name = symbol.fqcn.replace(".", "_") + ".json"
            diagnostic_path = self.repo_root / "reports" / "execution-context" / diagnostic_name
            try:
                write_execution_context_json(execution_context, diagnostic_path)
                schema_path = self.repo_root / "reports" / "schema-catalog" / diagnostic_name
                fixture_path = self.repo_root / "reports" / "fixture-catalog" / diagnostic_name
                write_schema_catalog_json(execution_context, schema_path)
                write_fixture_catalog_json(execution_context, fixture_path)
                outcome.notes.append(f"execution context: {execution_context.extraction_status.value}")
                outcome.notes.append(f"schema catalog: {schema_path}")
                outcome.notes.append(f"fixture catalog: {fixture_path}")
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not write execution context for %s: %s", symbol.fqcn, exc)

        _t = perf_counter()
        ctx = build_context(
            source_file=jf, symbol=symbol, collaborators=collaborators,
            test_package=test_pkg, test_class_name=test_class,
            stack=self.stack, classpath=cp, template=template, source_path=source_path,
            execution_context=execution_context,
            symbol_lookup=(
                self.symbol_resolver.lookup_context
                if self.symbol_resolver is not None
                and hasattr(self.symbol_resolver, "lookup_context")
                else self.lookup
            ),
        )
        if ctx.equality_descriptor is not None:
            outcome.notes.append(
                "equality strategy: "
                f"{ctx.equality_descriptor.strategy.value}; "
                f"assertions={ctx.equality_descriptor.assertion_mode.value}"
            )
            for warning in ctx.equality_descriptor.warnings:
                outcome.notes.append(f"equality limitation: {warning}")
        _elapsed = perf_counter() - _t
        log.info("[TIMING] prompt context build: %.3fs", _elapsed)
        timing_event("engine.prompt_context.end", elapsed=_elapsed)

        # Back up any pre-existing test so a failed generation never destroys it.
        original = test_path.read_text(encoding="utf-8") if test_path.exists() else None

        # 1) generate
        _t = perf_counter()
        code = self._generate(ctx, outcome, module=module, test_path=test_path)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] initial generation: %.3fs", _elapsed)
        timing_event("engine.initial_generation.end", elapsed=_elapsed)
        if code is None:
            outcome.status = "generation_failed"
            self._restore_or_remove(test_path, original)
            return outcome
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(code, encoding="utf-8")

        strict_serviceimpl = (
            ctx.execution_context is not None
            and ctx.execution_context.target_kind.value == "service-impl"
        )

        # 2) compile (+ bounded repair)
        # good = self._compile_with_repair(ctx, test_path, module, outcome)
        # if good is None:
        #     # Restore the original (or remove if we created it) so a non-compiling
        #     # test can't break sibling test-compile / coverage.
        #     self._restore_or_remove(test_path, original)
        #     outcome.status = "compile_failed"
        #     outcome.notes.append("reverted non-compiling test (restored original)"
        #                          if original is not None else "removed non-compiling test")
        #     return outcome

        # # 3) coverage loop
        # if self.cfg.run_coverage and self.coverage_runner and self.coverage_runner.available():
        #     self._coverage_loop(ctx, module, test_path, symbol, good, outcome, original)
        # else:
        #     outcome.status = "compiled (coverage not run)"
        # return outcome
                # 2) compile (+ bounded repair)
        _t = perf_counter()
        with heartbeat(
            "engine.compile_repair",
            interval_seconds=15.0,
            target=symbol.name,
            test_file=test_path.name,
        ):
            good = (
                self._validate_serviceimpl_class(ctx, test_path, module, outcome)
                if strict_serviceimpl
                else self._compile_with_repair(ctx, test_path, module, outcome)
            )
        _elapsed = perf_counter() - _t
        log.info("[TIMING] compile + repair: %.3fs", _elapsed)
        timing_event("engine.compile_repair.end", elapsed=_elapsed, success=good)
        if good is None:
            self._capture_failed_test_snapshot(test_path, outcome)
            self._restore_or_remove(test_path, original)
            if outcome.status == "pending":
                outcome.status = "compile_failed"
            outcome.notes.append(
                "reverted unresolved generated test (restored original)"
                if original is not None else "removed unresolved generated test"
            )
            if outcome.status == "compile_failed":
                self._log_compile_failure(outcome, symbol.name, test_path)
            return outcome

        # # 3) coverage loop
        # if self.cfg.run_coverage and self.coverage_runner and self.coverage_runner.available():
        #     self._coverage_loop(ctx, module, test_path, symbol, good, outcome, original)
            
        #     # ===========================================================================
        #     # 🚨 INTERCEPTOR 2: MAVEN COVERAGE COMPILATION STAGE FAILURES
        #     # ===========================================================================
        #     if outcome.status == "compile_failed":
        #         print("\n" + "🛑 " + "!" * 78)
        #         print(f"🚨 MAVEN EXECUTION COMPILATION CRASH FOR: {symbol.name}")
        #         print("!" * 80)
        #         if getattr(outcome, "compile_errors", None):
        #             print("AUTHORITATIVE MAVEN COMPILER ERRORS:")
        #             print("-" * 80)
        #             for err_str in outcome.compile_errors:
        #                 print(f"[ERROR] ➔ {err_str}")
        #         print("!" * 80 + "\n")
        #     # ===========================================================================
        # else:
        #     outcome.status = "compiled (coverage not run)"
        # return outcome
        # 3) coverage loop
        # IMPORTANT:
        # For fast batch generation, keep cfg.run_coverage=False.
        # This prevents one full Maven + JaCoCo execution per generated class.
        if self.cfg.run_coverage and self.coverage_runner and self.coverage_runner.available():
            pre_coverage_validation = outcome.class_validation if strict_serviceimpl else {}
            _t = perf_counter()
            self._coverage_loop(ctx, module, test_path, symbol, good, outcome, original)
            _elapsed = perf_counter() - _t
            log.info("[TIMING] runtime + coverage loop: %.3fs", _elapsed)
            timing_event("engine.runtime_coverage.end", elapsed=_elapsed)
            # Coverage augmentation is unchanged, but it may append tests after
            # the primary class lifecycle. Revalidate only when it changed the
            # ServiceImpl source so final normal publication still means every
            # test in the actual published class compiles and passes.
            if strict_serviceimpl and outcome.status in {
                "compile_failed",
                "generation_failed",
                "test_failed",
            }:
                if outcome.class_validation.get("publication_result") == "normal_java":
                    reason = outcome.notes[-1] if outcome.notes else "coverage-stage validation failed"
                    outcome.class_validation["publication_result"] = "failing_artifact"
                    outcome.class_validation["publication_path"] = str(
                        test_path.with_name(test_path.name + ".failing")
                    )
                    outcome.class_validation["publication_reason"] = reason
                    if outcome.status == "compile_failed":
                        outcome.class_validation["final_compilation_result"] = "failed"
                    outcome.class_validation["unresolved_issues"] = [
                        {
                            "scope": "post-validation coverage boundary",
                            "diagnostic": diagnostic,
                            "repair_attempts": 0,
                            "phase": "publication boundary",
                            "final_reason": reason,
                        }
                        for diagnostic in (outcome.compile_errors or [reason])
                    ]
                return outcome
            if strict_serviceimpl and test_path.exists():
                augmented_source = test_path.read_text(encoding="utf-8")
                coverage_status = outcome.status
                final_good = self._validate_serviceimpl_class(
                    ctx,
                    test_path,
                    module,
                    outcome,
                    required_primary_test_names=tuple(
                        pre_coverage_validation.get("initial_test_methods", [])
                    ),
                )
                outcome.class_validation["validation_trigger"] = (
                    "post_coverage_augmentation"
                    if augmented_source != good
                    else "post_coverage_final_publication"
                )
                outcome.class_validation["pre_augmentation_validation"] = pre_coverage_validation
                if final_good is None:
                    self._capture_failed_test_snapshot(test_path, outcome)
                    self._restore_or_remove(test_path, original)
                    outcome.notes.append("post-coverage complete-class validation failed")
                    return outcome
                outcome.status = coverage_status
        else:
            if not strict_serviceimpl:
                outcome.status = "compiled"
            outcome.notes.append("coverage skipped during generation; run JaCoCo/EclEmma once at the end")

        _elapsed = perf_counter() - target_started
        log.info("[TIMING] target end: %.3fs | %s | status=%s", _elapsed, symbol.name, outcome.status)
        timing_event("engine.target.end", elapsed=_elapsed, class_name=symbol.name, status=outcome.status)
        return outcome
    

    # -- generation --------------------------------------------------------

    def _generate(
        self,
        ctx: GenerationContext,
        outcome: TargetOutcome,
        *,
        module: ModuleInfo | None = None,
        test_path: Path | None = None,
    ) -> str | None:
        execution = ctx.execution_context
        if execution is not None and execution.target_kind.value in {"controller", "service-impl"}:
            return self._generate_methodwise(ctx, outcome, module=module, test_path=test_path)
        return self._generate_classwide(ctx, outcome)

    def _generate_methodwise(
        self,
        ctx: GenerationContext,
        outcome: TargetOutcome,
        *,
        module: ModuleInfo | None = None,
        test_path: Path | None = None,
    ) -> str | None:
        """Generate method blocks concurrently, assemble deterministically, finalize once."""
        execution = ctx.execution_context
        if execution is None:
            return None
        _t = perf_counter()
        try:
            skeleton = build_test_skeleton(ctx)
        except Exception as exc:  # noqa: BLE001
            outcome.notes.append(f"method-wise skeleton failed: {type(exc).__name__}: {exc}")
            return None
        log.info("[TIMING] deterministic skeleton: %.3fs", perf_counter() - _t)

        # ServiceImpl validation starts only after every primary method block has
        # been generated and assembled. Keep the legacy controller-only skeleton
        # preflight unchanged; it is outside the ServiceImpl lifecycle phase.
        if (
            module is not None
            and test_path is not None
            and execution.target_kind.value != "service-impl"
        ):
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(skeleton, encoding="utf-8")
            preflight_started = perf_counter()
            result, errors = self.compile_gate.check(test_path, module)
            timing_event(
                "fixture_skeleton.compile.end",
                elapsed=perf_counter() - preflight_started,
                success=result.ok,
                errors=len(errors),
            )
            if not result.ok:
                outcome.compile_errors = [error.render() for error in errors][:12]
                outcome.notes.append(
                    "fixture skeleton compile failed before method generation: "
                    + " | ".join(outcome.compile_errors[:4])
                )
                return None
            outcome.notes.append("fixture skeleton compiled before method generation")

        method_by_id = {
            f"{method.name}({','.join(type_name for type_name, _ in method.params)})": method
            for method in (ctx.symbol.methods or [])
        }
        jobs = []
        for order, method_context in enumerate(execution.methods):
            production_method = method_by_id.get(method_context.method_id)
            if production_method is None:
                outcome.notes.append(f"method context skipped; signature not found: {method_context.method_id}")
                continue
            jobs.append((order, production_method, method_context))

        workers = max(1, min(int(os.getenv("JUNITFORGE_METHOD_WORKERS", "3")), len(jobs) or 1))
        log.info("[TIMING] method generation batch start | methods=%d | workers=%d", len(jobs), workers)
        timing_event("llm.batch.start", methods=len(jobs), workers=workers)

        def generate_one(job):
            order, production_method, method_context = job
            started = perf_counter()
            timing_event("llm.method.start", index=order + 1, total=len(jobs), method=method_context.method_id)
            messages = build_method_generation_messages(ctx, production_method, "")
            try:
                with heartbeat(
                    "llm.method",
                    interval_seconds=15.0,
                    target=ctx.symbol.name,
                    method=method_context.method_id,
                    worker_index=order + 1,
                ):
                    resp = self.llm.chat(
                        messages,
                        temperature=self.cfg.temperature_gen,
                        max_tokens=self.cfg.max_tokens_gen,
                    )
                raw = resp.message
                tokens = resp.usage.total_tokens if getattr(resp, "usage", None) else 0
            except Exception as exc:  # noqa: BLE001
                log.error("[TIMING] LLM error | %s | %.3fs | %s", method_context.method_id, perf_counter() - started, exc)
                return order, method_context, None, (), 0, f"llm error: {str(exc)[:180]}"
            block = normalize_writable_map_mutations(extract_test_method_block(raw or ""))
            validation = validate_method_block(ctx, production_method, method_context, block, set())
            if not validation.ok and block.strip():
                # One bounded structural repair before discarding the entire
                # method suite. This is especially important when the model
                # illegally spies/stubs a same-class helper instead of using the
                # helper's now-listed collaborator path.
                repair_messages = build_method_generation_validation_repair_messages(
                    ctx,
                    production_method,
                    block,
                    validation.reason,
                )
                try:
                    with heartbeat(
                        "llm.method.validation_repair",
                        interval_seconds=15.0,
                        target=ctx.symbol.name,
                        method=method_context.method_id,
                        worker_index=order + 1,
                    ):
                        repaired = self.llm.chat(
                            repair_messages,
                            temperature=self.cfg.temperature_repair,
                            max_tokens=self.cfg.max_tokens_repair,
                        )
                    if getattr(repaired, "usage", None):
                        tokens += repaired.usage.total_tokens
                    candidate = normalize_writable_map_mutations(extract_test_method_block(repaired.message or ""))
                    repaired_validation = validate_method_block(
                        ctx,
                        production_method,
                        method_context,
                        candidate,
                        set(),
                    )
                    if repaired_validation.ok:
                        block = candidate
                        validation = repaired_validation
                    else:
                        validation = repaired_validation
                except Exception as exc:  # noqa: BLE001
                    return (
                        order,
                        method_context,
                        None,
                        (),
                        tokens,
                        f"{validation.reason}; validation repair error: {str(exc)[:160]}",
                    )
            elapsed = perf_counter() - started
            if not validation.ok:
                return order, method_context, None, (), tokens, validation.reason
            if validation.warnings:
                # Advisory only: keep the block and let the compile gate arbitrate.
                # Recorded on the outcome so summary.py can group and count them:
                # these are the MEASURED analyzer extraction gaps.
                for warning in validation.warnings:
                    outcome.notes.append(f"advisory [{method_context.method_id}]: {warning}")
            return order, method_context, block, validation.test_names, tokens, None

        batch_started = perf_counter()
        results = []
        with heartbeat(
            "llm.batch",
            interval_seconds=15.0,
            target=ctx.symbol.name,
            methods=len(jobs),
            workers=workers,
        ):
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="junitforge-method") as pool:
                futures = [pool.submit(generate_one, job) for job in jobs]
                for future in as_completed(futures):
                    results.append(future.result())
        _elapsed = perf_counter() - batch_started
        log.info("[TIMING] method generation batch end: %.3fs", _elapsed)
        timing_event("llm.batch.end", elapsed=_elapsed, methods=len(jobs))

        current = skeleton
        accepted = 0
        existing_names: set[str] = set()
        for order, method_context, block, names, tokens, reason in sorted(results, key=lambda item: item[0]):
            outcome.tokens_used += tokens
            if block is None:
                outcome.notes.append(f"method generation rejected [{method_context.method_id}]: {reason}")
                continue
            duplicate = existing_names.intersection(names)
            if duplicate:
                outcome.notes.append(
                    f"method generation rejected [{method_context.method_id}]: duplicate test names {sorted(duplicate)}"
                )
                continue
            current = insert_method_block(current, method_context.method_id, block)
            existing_names.update(names)
            accepted += 1
            outcome.notes.append(f"method generated [{method_context.method_id}]: {len(names)} test(s)")

        if accepted == 0 or not test_method_names(current):
            outcome.notes.append("method-wise generation produced no accepted test methods")
            return None

        _t = perf_counter()
        final = finalize_java(
            current,
            test_package=ctx.test_package,
            test_class_name=ctx.test_class_name,
            template=ctx.template,
            cut_symbol=ctx.symbol,
            collaborators=ctx.collaborators,
            source_imports=ctx.source_imports,
        )
        _elapsed = perf_counter() - _t
        log.info("[TIMING] full-class finalize once: %.3fs", _elapsed)
        timing_event("assembly.finalize.end", elapsed=_elapsed)
        if not final.ok or not final.code:
            outcome.notes.append(f"assembled method-wise test rejected: {final.reason}")
            return None
        outcome.notes.extend(final.issues)
        outcome.notes.append(f"method-wise generation complete: {accepted}/{len(execution.methods)} production methods")
        return final.code

    def _generate_classwide(self, ctx: GenerationContext, outcome: TargetOutcome) -> str | None:
        """Existing whole-class path retained for DTO/entity and other stable types."""
        messages = build_generation_messages(ctx)
        for attempt in range(self.cfg.max_gen_reasks + 1):
            raw = self._chat(messages, self.cfg.temperature_gen, self.cfg.max_tokens_gen, outcome)
            candidate = inject_reusable_fixtures(raw or "", ctx)
            res = finalize_java(
                candidate, test_package=ctx.test_package, test_class_name=ctx.test_class_name,
                template=ctx.template, cut_symbol=ctx.symbol, collaborators=ctx.collaborators,
                source_imports=ctx.source_imports)
            if res.ok and res.code:
                outcome.notes.extend(res.issues)
                return res.code
            if attempt < self.cfg.max_gen_reasks:
                correction = (
                    f"Your previous output was rejected: {res.reason}. "
                    "Return one valid Java test file per the output contract."
                )
                messages = messages + [{"role": "user", "content": correction}]
        outcome.notes.append(f"generation rejected: {res.reason}")
        return None

    # -- ServiceImpl complete-class validation / repair ------------------

    @staticmethod
    def _service_fixture_names(ctx: GenerationContext) -> set[str]:
        execution = ctx.execution_context
        if execution is None:
            return set()
        return {fixture.method_name for fixture in execution.fixtures if fixture.method_name}

    def _check_complete_class(self, test_path: Path, module: ModuleInfo):
        checker = getattr(self.compile_gate, "check_complete_class", None)
        if callable(checker):
            return checker(test_path, module)
        return self.compile_gate.check(test_path, module)

    def _group_compile_diagnostics(
        self,
        ctx: GenerationContext,
        source: str,
        errors: list[CompileError],
    ) -> list[tuple[GeneratedScope | None, list[CompileError], list[str]]]:
        fixture_names = self._service_fixture_names(ctx)
        grouped: dict[str, tuple[GeneratedScope | None, list[CompileError], list[str]]] = {}
        for index, error in enumerate(errors):
            scope = scope_for_line(
                source,
                error.line,
                fixture_names=fixture_names,
            )
            key = scope.scope_id if scope is not None else f"unmapped:{index}"
            if key not in grouped:
                grouped[key] = (scope, [], [])
            grouped[key][1].append(error)
            grouped[key][2].append(offending_statement(source, error.line))
        return sorted(
            grouped.values(),
            key=lambda item: item[0].start_offset if item[0] is not None else len(source) + 1,
        )

    @staticmethod
    def _compile_fingerprints(
        groups: list[tuple[GeneratedScope | None, list[CompileError], list[str]]],
    ) -> set[str]:
        fingerprints: set[str] = set()
        for scope, errors, statements in groups:
            scope_name = scope.scope_id if scope is not None else "unmapped"
            for index, error in enumerate(errors):
                statement = statements[index] if index < len(statements) else ""
                blob = "|".join([
                    scope_name,
                    error.message or "",
                    " ".join(error.detail),
                    statement,
                ])
                fingerprints.add(re.sub(r"\s+", " ", blob).strip())
        return fingerprints

    @staticmethod
    def _compile_diagnostic_records(
        groups: list[tuple[GeneratedScope | None, list[CompileError], list[str]]],
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for scope, errors, statements in groups:
            for index, error in enumerate(errors):
                records.append({
                    "file": str(error.file) if error.file is not None else None,
                    "line": error.line,
                    "column": error.col,
                    "message": error.message,
                    "details": list(error.detail),
                    "offending_statement": statements[index] if index < len(statements) else None,
                    "scope_id": scope.scope_id if scope is not None else None,
                    "scope_kind": scope.kind.value if scope is not None else None,
                    "scope_name": scope.name if scope is not None else None,
                })
        return records

    @staticmethod
    def _execution_result_record(result: ClassExecutionResult) -> dict[str, object]:
        return {
            "compiled": result.compiled,
            "executed": result.executed,
            "passed": result.ok,
            "note": result.note,
            "tests": [
                {
                    "method": item.method_name,
                    "status": item.status.value,
                    "exception_type": item.exception_type,
                    "message": item.message,
                    "expected": item.expected,
                    "actual": item.actual,
                    "source_line": item.source_line,
                    "root_cause": item.root_cause,
                    "cause_chain": list(item.cause_chain),
                    "filtered_stack_trace": list(item.filtered_stack_trace),
                    "generated_test_frame": item.generated_test_frame,
                    "cut_frame": item.cut_frame,
                    "null_path": item.null_path,
                    "mockito_subtype": item.mockito_subtype,
                    "stub_declaration": item.stub_declaration,
                    "stub_source_location": item.stub_source_location,
                    "actual_invocation": item.actual_invocation,
                    "actual_invocation_source_location": item.actual_invocation_source_location,
                    "expected_arguments": list(item.expected_arguments),
                    "actual_arguments": list(item.actual_arguments),
                    "unused_stub_locations": list(item.unused_stub_locations),
                }
                for item in result.test_methods
            ],
        }

    def _new_class_validation_report(
        self,
        ctx: GenerationContext,
        test_path: Path,
        initial_test_names: tuple[str, ...],
        *,
        expected_primary_method_ids: tuple[str, ...] = (),
        missing_primary_method_ids: tuple[str, ...] = (),
    ) -> dict[str, object]:
        return {
            "target_test_class": self._test_fqcn(ctx),
            "test_path": str(test_path),
            "initial_test_count": len(initial_test_names),
            "initial_test_methods": list(initial_test_names),
            "expected_primary_method_count": len(expected_primary_method_ids),
            "expected_primary_method_ids": list(expected_primary_method_ids),
            "missing_primary_method_ids": list(missing_primary_method_ids),
            "all_primary_tests_generated_before_compilation": not missing_primary_method_ids,
            "initial_compilation_result": "pending",
            "compilation_diagnostics_found": [],
            "affected_compilation_scopes": [],
            "compilation_repair_rounds_attempted": 0,
            "compilation_repair_history": [],
            "diagnostics_still_unresolved": [],
            "final_compilation_result": "pending",
            "initial_complete_class_execution_result": "not_run",
            "initial_execution_details": None,
            "passing_test_methods": [],
            "failed_or_erroneous_test_methods": [],
            "execution_repair_rounds_attempted": 0,
            "execution_repair_history": [],
            "latest_unresolved_execution_diagnostics": [],
            "final_complete_class_execution_result": "not_run",
            "publication_result": "pending",
            "publication_path": None,
            "publication_reason": "pending",
            "unresolved_issues": [],
        }

    @staticmethod
    def _affected_scope_records(
        groups: list[tuple[GeneratedScope | None, list[CompileError], list[str]]],
    ) -> list[dict[str, object]]:
        seen: set[str] = set()
        records: list[dict[str, object]] = []
        for scope, errors, _ in groups:
            key = scope.scope_id if scope is not None else "unmapped"
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "scope_id": scope.scope_id if scope is not None else None,
                "scope_kind": scope.kind.value if scope is not None else None,
                "scope_name": scope.name if scope is not None else None,
                "diagnostic_count": len(errors),
            })
        return records

    def _apply_compilation_scope_requests(
        self,
        ctx: GenerationContext,
        source: str,
        groups: list[tuple[GeneratedScope | None, list[CompileError], list[str]]],
        outcome: TargetOutcome,
        *,
        phase: str,
        affected_test_names: tuple[str, ...] = (),
    ) -> tuple[str | None, list[dict[str, object]], str | None]:
        replacements: dict[str, tuple[GeneratedScope, str]] = {}
        records: list[dict[str, object]] = []
        stop_reason: str | None = None
        for scope, errors, statements in groups:
            if scope is None:
                records.append({
                    "scope_id": None,
                    "applied": False,
                    "diagnostics": [error.render() for error in errors],
                    "reason": "compiler diagnostic has no generated source location",
                })
                stop_reason = "unmapped or toolchain compiler diagnostic cannot be safely repaired"
                continue
            messages = build_scoped_compile_repair_messages(
                ctx,
                source,
                scope,
                errors,
                statements,
                affected_test_names=affected_test_names,
                phase=phase,
            )
            raw = self._chat(
                messages,
                self.cfg.temperature_repair,
                self.cfg.max_tokens_repair,
                outcome,
            )
            replacement = extract_corrected_scope(raw or "", scope)
            record = {
                "scope_id": scope.scope_id,
                "scope_kind": scope.kind.value,
                "scope_name": scope.name,
                "diagnostics": [error.render() for error in errors],
                "applied": False,
                "reason": None,
            }
            if replacement is None:
                record["reason"] = "repair output could not be parsed as the complete affected scope"
                stop_reason = str(record["reason"])
            elif re.sub(r"\s+", " ", replacement).strip() == re.sub(r"\s+", " ", scope.source).strip():
                record["reason"] = "repair did not change the affected scope"
                stop_reason = str(record["reason"])
            else:
                replacements[scope.scope_id] = (scope, replacement)
                record["applied"] = True
            records.append(record)

        if not replacements:
            return None, records, stop_reason or "no accepted scope repair"
        candidate = replace_generated_scopes(source, replacements)
        rejection = validate_scoped_class_repair(
            source,
            candidate,
            modified_scope_ids=set(replacements),
            strict_serviceimpl=True,
            forbid_mockito_weakening=phase.startswith("execution repair"),
        )
        if rejection:
            for record in records:
                if record.get("applied"):
                    record["applied"] = False
                    record["reason"] = rejection
            return None, records, f"repair could not be safely applied: {rejection}"
        return candidate, records, stop_reason

    def _compile_serviceimpl_class(
        self,
        ctx: GenerationContext,
        test_path: Path,
        module: ModuleInfo,
        outcome: TargetOutcome,
        report: dict[str, object],
    ) -> tuple[str | None, list[CompileError], str | None]:
        source = test_path.read_text(encoding="utf-8")
        result, errors = self._check_complete_class(test_path, module)
        groups = self._group_compile_diagnostics(ctx, source, list(errors))
        report["initial_compilation_result"] = "passed" if result.ok else "failed"
        report["compilation_diagnostics_found"] = self._compile_diagnostic_records(groups)
        report["affected_compilation_scopes"] = self._affected_scope_records(groups)
        if result.ok:
            report["final_compilation_result"] = "passed"
            return source, [], None

        max_rounds = max(0, min(3, int(self.cfg.max_compile_repairs)))
        previous_fingerprints = self._compile_fingerprints(groups)
        no_progress_rounds = 0
        final_reason = "compilation repair limit reached"
        history: list[dict[str, object]] = report["compilation_repair_history"]  # type: ignore[assignment]

        for round_number in range(1, max_rounds + 1):
            report["compilation_repair_rounds_attempted"] = round_number
            candidate, repairs, request_stop = self._apply_compilation_scope_requests(
                ctx,
                source,
                groups,
                outcome,
                phase=f"initial compilation repair round {round_number}",
            )
            round_record: dict[str, object] = {
                "round": round_number,
                "diagnostics_before": self._compile_diagnostic_records(groups),
                "repairs": repairs,
                "resolved_diagnostics": [],
                "unresolved_diagnostics": [],
                "progress": False,
                "stop_reason": request_stop,
            }
            if candidate is None:
                round_record["unresolved_diagnostics"] = self._compile_diagnostic_records(groups)
                history.append(round_record)
                final_reason = request_stop or "no compilation repair was safely applicable"
                break

            source = candidate
            test_path.write_text(source, encoding="utf-8")
            result, errors = self._check_complete_class(test_path, module)
            new_groups = self._group_compile_diagnostics(ctx, source, list(errors))
            new_fingerprints = self._compile_fingerprints(new_groups)
            resolved = sorted(previous_fingerprints - new_fingerprints)
            round_record["resolved_diagnostics"] = resolved
            round_record["unresolved_diagnostics"] = self._compile_diagnostic_records(new_groups)
            round_record["progress"] = bool(resolved) or result.ok
            history.append(round_record)
            if result.ok:
                report["final_compilation_result"] = "passed"
                report["diagnostics_still_unresolved"] = []
                return source, [], None

            groups = new_groups
            if new_fingerprints == previous_fingerprints:
                final_reason = "same unresolved compiler diagnostics repeated after repair"
                history[-1]["stop_reason"] = final_reason
                break
            if not resolved:
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0
            if request_stop:
                final_reason = request_stop
                break
            if no_progress_rounds >= 2:
                final_reason = "two consecutive compilation repair rounds made no meaningful progress"
                history[-1]["stop_reason"] = final_reason
                break
            previous_fingerprints = new_fingerprints
        else:
            final_reason = "maximum of three compilation-repair rounds reached"

        report["final_compilation_result"] = "failed"
        report["diagnostics_still_unresolved"] = self._compile_diagnostic_records(groups)
        outcome.compile_errors = [error.render() for _, scope_errors, _ in groups for error in scope_errors]
        return None, [error for _, scope_errors, _ in groups for error in scope_errors], final_reason

    def _execute_serviceimpl_class(
        self,
        ctx: GenerationContext,
        module: ModuleInfo,
        test_path: Path,
        expected_test_methods: tuple[str, ...],
    ) -> ClassExecutionResult:
        runner = self.coverage_runner
        execute = getattr(runner, "execute_class", None) if runner is not None else None
        if not callable(execute):
            return ClassExecutionResult(
                compiled=True,
                executed=False,
                ok=False,
                test_methods=[
                    TestMethodResult(
                        method_name=name,
                        status=TestMethodStatus.MISSING,
                        message="complete-class JUnit/Surefire runner is unavailable",
                    )
                    for name in expected_test_methods
                ],
                note="complete-class JUnit/Surefire runner is unavailable",
            )
        return execute(
            module,
            test_class_name=self._test_fqcn(ctx),
            test_file=test_path,
            cut_class_name=ctx.symbol.name,
            expected_test_methods=expected_test_methods,
        )

    def _group_execution_failures(
        self,
        ctx: GenerationContext,
        source: str,
        failures: list[TestMethodResult],
    ) -> list[tuple[GeneratedScope | None, list[TestMethodResult]]]:
        fixture_names = self._service_fixture_names(ctx)
        scopes = generated_scopes(source, fixture_names=fixture_names)
        test_scopes = {
            scope.name: scope for scope in scopes
            if scope.kind is GeneratedScopeKind.TEST_METHOD
        }
        grouped: dict[str, tuple[GeneratedScope | None, list[TestMethodResult]]] = {}
        for index, failure in enumerate(failures):
            line = failure.source_line
            if failure.status is TestMethodStatus.MOCKITO_FAILURE and failure.stub_source_location:
                location_line = re.search(rf"{re.escape(ctx.test_class_name)}\.java:(\d+)", failure.stub_source_location)
                if location_line:
                    line = int(location_line.group(1))
            scope = scope_for_line(source, line, fixture_names=fixture_names)
            if scope is None or scope.kind in {
                GeneratedScopeKind.IMPORT_BLOCK,
                GeneratedScopeKind.CLASS_FIELD,
                GeneratedScopeKind.MOCK_DECLARATION,
            } or (
                scope.kind is GeneratedScopeKind.OTHER_CLASS_DECLARATION
                and "(" not in scope.source
            ):
                scope = test_scopes.get(failure.method_name.split("(", 1)[0], scope)
            key = scope.scope_id if scope is not None else f"unmapped:{index}"
            if key not in grouped:
                grouped[key] = (scope, [])
            grouped[key][1].append(failure)
        return sorted(
            grouped.values(),
            key=lambda item: item[0].start_offset if item[0] is not None else len(source) + 1,
        )

    @staticmethod
    def _execution_fingerprints(result: ClassExecutionResult) -> set[str]:
        return {failure.fingerprint() for failure in result.unresolved_methods}

    @staticmethod
    def _matching_parenthesis(source: str, open_index: int) -> int | None:
        depth = 0
        quote: str | None = None
        escaped = False
        for index in range(open_index, len(source)):
            char = source[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
        return None

    @classmethod
    def _split_java_arguments(cls, source: str) -> list[str]:
        arguments: list[str] = []
        start = 0
        parens = brackets = braces = angles = 0
        quote: str | None = None
        escaped = False
        for index, char in enumerate(source):
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "(":
                parens += 1
            elif char == ")" and parens:
                parens -= 1
            elif char == "[":
                brackets += 1
            elif char == "]" and brackets:
                brackets -= 1
            elif char == "{":
                braces += 1
            elif char == "}" and braces:
                braces -= 1
            elif char == "<":
                angles += 1
            elif char == ">" and angles:
                angles -= 1
            elif char == "," and not (parens or brackets or braces or angles):
                arguments.append(source[start:index].strip())
                start = index + 1
        tail = source[start:].strip()
        if tail:
            arguments.append(tail)
        return arguments

    def _validate_execution_repair_contract(
        self,
        ctx: GenerationContext,
        replacement: str,
    ) -> str | None:
        """Reject unsafe Mockito shortcuts before a repaired scope is applied."""
        execution = ctx.execution_context
        if execution is None:
            return None
        contracts: dict[tuple[str, str], set[str]] = {}
        for method in execution.methods:
            for invocation in method.dependency_invocations:
                contract = invocation.contract
                if contract.resolution_status.value not in {"resolved", "inherited", "external"}:
                    continue
                contracts.setdefault(
                    (invocation.dependency_field, contract.method_name),
                    set(),
                ).add((contract.return_type or "").strip())

        for (field, method_name), return_types in contracts.items():
            field_re = re.escape(field)
            method_re = re.escape(method_name)
            do_nothing = re.search(
                rf"\b(?:doNothing|willDoNothing)\s*\(\s*\)\s*\.\s*"
                rf"(?:when|given)\s*\(\s*{field_re}\s*\)\s*\.\s*{method_re}\s*\(",
                replacement,
                flags=re.DOTALL,
            )
            if do_nothing and any(return_type != "void" for return_type in return_types):
                return f"Mockito doNothing/willDoNothing targets verified non-void method {field}.{method_name}"
            non_void_stubbing = re.search(
                rf"\b(?:when|given)\s*\(\s*{field_re}\s*\.\s*{method_re}\s*\(",
                replacement,
                flags=re.DOTALL,
            )
            if non_void_stubbing and return_types == {"void"}:
                return f"Mockito when/given return stubbing targets verified void method {field}.{method_name}"

            call_pattern = re.compile(rf"\b{field_re}\s*\.\s*{method_re}\s*\(")
            for call in call_pattern.finditer(replacement):
                open_index = replacement.find("(", call.start())
                close_index = self._matching_parenthesis(replacement, open_index)
                if close_index is None:
                    continue
                arguments = self._split_java_arguments(
                    replacement[open_index + 1:close_index]
                )
                if arguments and all(
                    re.fullmatch(r"(?:Mockito\s*\.\s*)?any\s*\(\s*\)", argument)
                    for argument in arguments
                ):
                    return f"Mockito repair replaced every {field}.{method_name} argument with raw any()"

        fixture_names = self._service_fixture_names(ctx)
        real_fixture_vars: set[str] = set()
        for fixture_name in fixture_names:
            real_fixture_vars.update(re.findall(
                rf"\b([A-Za-z_$][\w$]*)\s*=\s*{re.escape(fixture_name)}\s*\(",
                replacement,
            ))
        real_fixture_vars.update(re.findall(
            r"\b([A-Za-z_$][\w$]*)\s*=\s*new\s+[A-Za-z_$][\w$<>.,? ]*\s*\(",
            replacement,
        ))
        for variable in real_fixture_vars:
            if re.search(
                rf"\b(?:when|given)\s*\(\s*{re.escape(variable)}\s*\.\s*"
                r"(?:get|is)[A-Z_$][\w$]*\s*\(",
                replacement,
            ):
                return f"Mockito repair stubs a getter on real fixture object {variable}"
        return None

    def _apply_execution_scope_requests(
        self,
        ctx: GenerationContext,
        source: str,
        groups: list[tuple[GeneratedScope | None, list[TestMethodResult]]],
        outcome: TargetOutcome,
    ) -> tuple[str | None, list[dict[str, object]], str | None]:
        replacements: dict[str, tuple[GeneratedScope, str]] = {}
        records: list[dict[str, object]] = []
        stop_reason: str | None = None
        for scope, failures in groups:
            if scope is None:
                records.append({
                    "scope_id": None,
                    "affected_tests": [failure.method_name for failure in failures],
                    "applied": False,
                    "reason": "failure did not map to a generated test or shared scope",
                })
                stop_reason = "execution failure could not be mapped to a safe generated scope"
                continue
            messages = build_scoped_execution_repair_messages(
                ctx,
                source,
                scope,
                failures,
            )
            raw = self._chat(
                messages,
                self.cfg.temperature_repair,
                self.cfg.max_tokens_repair,
                outcome,
            )
            replacement = extract_corrected_scope(raw or "", scope)
            record = {
                "scope_id": scope.scope_id,
                "scope_kind": scope.kind.value,
                "scope_name": scope.name,
                "affected_tests": [failure.method_name for failure in failures],
                "diagnostics": [failure.fingerprint() for failure in failures],
                "applied": False,
                "reason": None,
            }
            if replacement is None:
                record["reason"] = "repair output could not be parsed as the complete affected scope"
                stop_reason = str(record["reason"])
            elif re.sub(r"\s+", " ", replacement).strip() == re.sub(r"\s+", " ", scope.source).strip():
                record["reason"] = "repair did not change the affected scope"
                stop_reason = str(record["reason"])
            else:
                contract_rejection = self._validate_execution_repair_contract(ctx, replacement)
                if contract_rejection:
                    record["reason"] = contract_rejection
                    stop_reason = contract_rejection
                else:
                    replacements[scope.scope_id] = (scope, replacement)
                    record["applied"] = True
            records.append(record)

        if not replacements:
            return None, records, stop_reason or "no accepted execution repair"
        candidate = replace_generated_scopes(source, replacements)
        rejection = validate_scoped_class_repair(
            source,
            candidate,
            modified_scope_ids=set(replacements),
            strict_serviceimpl=True,
            forbid_mockito_weakening=True,
        )
        if rejection:
            for record in records:
                if record.get("applied"):
                    record["applied"] = False
                    record["reason"] = rejection
            return None, records, f"execution repair could not be safely applied: {rejection}"
        return candidate, records, stop_reason

    def _repair_execution_introduced_compile_errors(
        self,
        ctx: GenerationContext,
        test_path: Path,
        module: ModuleInfo,
        outcome: TargetOutcome,
        errors: list[CompileError],
        *,
        round_number: int,
        affected_test_names: tuple[str, ...],
    ) -> tuple[str | None, list[dict[str, object]], list[CompileError], str | None]:
        source = test_path.read_text(encoding="utf-8")
        groups = self._group_compile_diagnostics(ctx, source, errors)
        candidate, repairs, stop_reason = self._apply_compilation_scope_requests(
            ctx,
            source,
            groups,
            outcome,
            phase=f"execution repair round {round_number} compilation correction",
            affected_test_names=affected_test_names,
        )
        if candidate is None:
            return None, repairs, errors, stop_reason
        test_path.write_text(candidate, encoding="utf-8")
        result, latest_errors = self._check_complete_class(test_path, module)
        if result.ok:
            return candidate, repairs, [], stop_reason
        return None, repairs, list(latest_errors), (
            stop_reason or "compilation still failed after the scoped correction within this execution-repair round"
        )

    def _finalize_validation_failure(
        self,
        report: dict[str, object],
        test_path: Path,
        *,
        phase: str,
        reason: str,
        unresolved: list[dict[str, object]],
    ) -> None:
        report["publication_result"] = "failing_artifact"
        report["publication_path"] = str(test_path.with_name(test_path.name + ".failing"))
        report["publication_reason"] = reason
        report["unresolved_issues"] = [
            {**issue, "phase": phase, "final_reason": reason}
            for issue in unresolved
        ]

    def _validate_serviceimpl_class(
        self,
        ctx: GenerationContext,
        test_path: Path,
        module: ModuleInfo,
        outcome: TargetOutcome,
        *,
        required_primary_test_names: tuple[str, ...] = (),
    ) -> str | None:
        """Compile, execute, repair, and select publication for one complete class."""
        initial_source = test_path.read_text(encoding="utf-8")
        initial_test_names = test_method_names(initial_source)
        execution_context = ctx.execution_context
        expected_primary_ids = tuple(
            method.method_id for method in execution_context.methods
        ) if execution_context is not None else ()
        missing_primary_ids = tuple(
            method_id
            for method_id in expected_primary_ids
            if not (
                (block := method_block_for_id(initial_source, method_id))
                and test_method_names(block)
            )
        )
        report = self._new_class_validation_report(
            ctx,
            test_path,
            initial_test_names,
            expected_primary_method_ids=expected_primary_ids,
            missing_primary_method_ids=missing_primary_ids,
        )
        report["required_primary_test_names"] = list(required_primary_test_names)
        outcome.class_validation = report
        if missing_primary_ids:
            reason = (
                "complete generated class is missing primary test blocks for: "
                + ", ".join(missing_primary_ids)
            )
            report["initial_compilation_result"] = "not_run"
            report["final_compilation_result"] = "not_run"
            self._finalize_validation_failure(
                report,
                test_path,
                phase="generation boundary",
                reason=reason,
                unresolved=[
                    {
                        "scope": method_id,
                        "diagnostic": "primary ServiceImpl test block is absent or contains no @Test method",
                        "repair_attempts": 0,
                    }
                    for method_id in missing_primary_ids
                ],
            )
            outcome.status = "generation_failed"
            outcome.notes.append(reason)
            return None
        if required_primary_test_names:
            scopes_by_test = {
                scope.name: scope
                for scope in generated_scopes(
                    initial_source,
                    fixture_names=self._service_fixture_names(ctx),
                )
                if scope.kind is GeneratedScopeKind.TEST_METHOD
            }
            primary_violations: list[dict[str, object]] = []
            for test_name in required_primary_test_names:
                scope = scopes_by_test.get(test_name)
                if scope is None:
                    diagnostic = "previously validated primary test method is missing"
                else:
                    assertions = re.findall(
                        r"(?<![A-Za-z0-9_$])(?:Assertions\s*\.\s*)?"
                        r"(assert[A-Za-z0-9_$]*)\s*\((.*?)\)\s*;",
                        scope.source,
                        flags=re.DOTALL,
                    )
                    diagnostic = "" if (
                        len(assertions) == 1
                        and assertions[0][0] == "assertNotNull"
                        and assertions[0][1].strip() == "response"
                    ) else "primary ServiceImpl assertion is no longer exactly assertNotNull(response)"
                if diagnostic:
                    primary_violations.append({
                        "scope": test_name,
                        "diagnostic": diagnostic,
                        "repair_attempts": 0,
                    })
            if primary_violations:
                reason = "post-augmentation source did not preserve every validated primary test"
                report["initial_compilation_result"] = "not_run"
                report["final_compilation_result"] = "not_run"
                self._finalize_validation_failure(
                    report,
                    test_path,
                    phase="publication boundary",
                    reason=reason,
                    unresolved=primary_violations,
                )
                outcome.status = "generation_failed"
                outcome.notes.append(reason)
                return None
        if not initial_test_names:
            reason = "complete generated class contains no primary test methods"
            report["initial_compilation_result"] = "not_run"
            report["final_compilation_result"] = "failed"
            self._finalize_validation_failure(
                report,
                test_path,
                phase="generation boundary",
                reason=reason,
                unresolved=[{"scope": "test class", "diagnostic": reason, "repair_attempts": 0}],
            )
            outcome.status = "generation_failed"
            return None

        compiled, compile_errors, compile_reason = self._compile_serviceimpl_class(
            ctx,
            test_path,
            module,
            outcome,
            report,
        )
        if compiled is None:
            reason = compile_reason or "complete generated class did not compile"
            report["initial_complete_class_execution_result"] = "not_run"
            report["final_complete_class_execution_result"] = "not_run"
            unresolved = [
                {
                    "scope": record.get("scope_id") or "unmapped",
                    "diagnostic": record.get("message"),
                    "repair_attempts": sum(
                        1
                        for round_record in report.get("compilation_repair_history", [])  # type: ignore[union-attr]
                        for repair in round_record.get("repairs", [])
                        if repair.get("scope_id") == record.get("scope_id")
                    ),
                }
                for record in report.get("diagnostics_still_unresolved", [])  # type: ignore[union-attr]
            ]
            self._finalize_validation_failure(
                report,
                test_path,
                phase="compilation",
                reason=reason,
                unresolved=unresolved,
            )
            outcome.status = "compile_failed"
            outcome.notes.append(reason)
            return None

        current = compiled
        expected_test_names = test_method_names(current)
        execution = self._execute_serviceimpl_class(
            ctx,
            module,
            test_path,
            expected_test_names,
        )
        report["initial_complete_class_execution_result"] = "passed" if execution.ok else (
            "failed" if execution.executed else "not_run"
        )
        report["initial_execution_details"] = self._execution_result_record(execution)
        report["passing_test_methods"] = execution.passing_methods
        report["failed_or_erroneous_test_methods"] = [
            item.method_name for item in execution.unresolved_methods
        ]
        skipped_execution = [
            item for item in execution.unresolved_methods
            if item.status is TestMethodStatus.SKIPPED
        ]
        missing_execution = [
            item for item in execution.unresolved_methods
            if item.status is TestMethodStatus.MISSING
        ]
        repairable_execution = [
            item for item in execution.unresolved_methods
            if item.status in {
                TestMethodStatus.ASSERTION_FAILURE,
                TestMethodStatus.RUNTIME_ERROR,
                TestMethodStatus.MOCKITO_FAILURE,
            }
        ]
        if (
            not execution.executed
            or skipped_execution
            or (missing_execution and not repairable_execution)
        ):
            reason = execution.note or "complete-class execution did not run every generated test"
            if skipped_execution or missing_execution:
                reason = (
                    "complete-class execution did not run every generated test: "
                    + ", ".join(
                        f"{item.method_name}={item.status.value}"
                        for item in (*skipped_execution, *missing_execution)
                    )
                )
            if not execution.compiled:
                report["final_compilation_result"] = "failed"
                compile_groups = self._group_compile_diagnostics(
                    ctx,
                    current,
                    list(execution.compiler_errors),
                )
                report["diagnostics_still_unresolved"] = self._compile_diagnostic_records(
                    compile_groups
                )
                outcome.compile_errors = [
                    error.render() for error in execution.compiler_errors
                ]
            report["latest_unresolved_execution_diagnostics"] = [
                self._execution_result_record(ClassExecutionResult(
                    compiled=execution.compiled,
                    executed=execution.executed,
                    ok=False,
                    test_methods=[failure],
                ))["tests"][0]
                for failure in execution.unresolved_methods
            ]
            report["final_complete_class_execution_result"] = (
                "failed" if execution.executed else "not_run"
            )
            unresolved = [
                {
                    "scope": failure.method_name,
                    "diagnostic": failure.message or failure.status.value,
                    "repair_attempts": 0,
                }
                for failure in execution.unresolved_methods
            ] or [{
                "scope": "complete-class execution",
                "diagnostic": reason,
                "repair_attempts": 0,
            }]
            self._finalize_validation_failure(
                report,
                test_path,
                phase="execution boundary",
                reason=reason,
                unresolved=unresolved,
            )
            outcome.status = "compile_failed" if not execution.compiled else "test_failed"
            outcome.notes.append(reason)
            return None
        if execution.ok:
            report["final_complete_class_execution_result"] = "passed"
            report["publication_result"] = "normal_java"
            report["publication_path"] = str(test_path)
            report["publication_reason"] = "complete class compiled and every generated test passed"
            outcome.status = "passed"
            self._clear_failed_test_snapshot(test_path)
            return current

        max_rounds = max(0, min(3, int(self.cfg.max_failure_repairs)))
        previous_fingerprints = self._execution_fingerprints(execution)
        no_progress_rounds = 0
        final_reason = "execution repair limit reached"
        history: list[dict[str, object]] = report["execution_repair_history"]  # type: ignore[assignment]

        for round_number in range(1, max_rounds + 1):
            report["execution_repair_rounds_attempted"] = round_number
            # Missing report entries are never sent to the LLM. A real class-
            # level setup/extension error may coexist with them; repair only
            # that concrete error, then require the next full run to report all
            # expected methods. Skipped tests are terminated above.
            failures = [
                failure for failure in execution.unresolved_methods
                if failure.status in {
                    TestMethodStatus.ASSERTION_FAILURE,
                    TestMethodStatus.RUNTIME_ERROR,
                    TestMethodStatus.MOCKITO_FAILURE,
                }
            ]
            groups = self._group_execution_failures(ctx, current, failures)
            candidate, repairs, request_stop = self._apply_execution_scope_requests(
                ctx,
                current,
                groups,
                outcome,
            )
            round_record: dict[str, object] = {
                "round": round_number,
                "failures_before": [self._execution_result_record(ClassExecutionResult(
                    compiled=True,
                    executed=True,
                    ok=False,
                    test_methods=[failure],
                ))["tests"][0] for failure in failures],
                "repairs": repairs,
                "compilation_corrections": [],
                "resolved_diagnostics": [],
                "latest_unresolved": [],
                "progress": False,
                "stop_reason": request_stop,
            }
            if candidate is None:
                round_record["latest_unresolved"] = [failure.fingerprint() for failure in failures]
                history.append(round_record)
                final_reason = request_stop or "no execution repair was safely applicable"
                break

            current = candidate
            test_path.write_text(current, encoding="utf-8")

            compile_result, new_compile_errors = self._check_complete_class(test_path, module)
            if not compile_result.ok:
                affected_names = tuple(failure.method_name for failure in failures)
                corrected, compile_repairs, remaining_errors, compile_stop = (
                    self._repair_execution_introduced_compile_errors(
                        ctx,
                        test_path,
                        module,
                        outcome,
                        list(new_compile_errors),
                        round_number=round_number,
                        affected_test_names=affected_names,
                    )
                )
                round_record["compilation_corrections"] = compile_repairs
                if corrected is None:
                    round_record["latest_unresolved"] = [error.render() for error in remaining_errors]
                    round_record["stop_reason"] = compile_stop
                    history.append(round_record)
                    report["final_compilation_result"] = "failed"
                    latest_source = test_path.read_text(encoding="utf-8")
                    latest_compile_groups = self._group_compile_diagnostics(
                        ctx,
                        latest_source,
                        remaining_errors,
                    )
                    report["diagnostics_still_unresolved"] = self._compile_diagnostic_records(
                        latest_compile_groups
                    )
                    outcome.compile_errors = [error.render() for error in remaining_errors]
                    final_reason = compile_stop or "execution repair introduced an unresolved compilation error"
                    break
                current = corrected

            execution = self._execute_serviceimpl_class(
                ctx,
                module,
                test_path,
                test_method_names(current),
            )
            latest_fingerprints = self._execution_fingerprints(execution)
            resolved = sorted(previous_fingerprints - latest_fingerprints)
            round_record["resolved_diagnostics"] = resolved
            round_record["latest_unresolved"] = [
                failure.fingerprint() for failure in execution.unresolved_methods
            ]
            round_record["progress"] = bool(resolved) or execution.ok
            history.append(round_record)
            report["passing_test_methods"] = execution.passing_methods
            report["failed_or_erroneous_test_methods"] = [
                failure.method_name for failure in execution.unresolved_methods
            ]
            if execution.ok:
                report["final_compilation_result"] = "passed"
                report["final_complete_class_execution_result"] = "passed"
                report["latest_unresolved_execution_diagnostics"] = []
                report["publication_result"] = "normal_java"
                report["publication_path"] = str(test_path)
                report["publication_reason"] = (
                    f"complete class compiled and every test passed after execution repair round {round_number}"
                )
                outcome.status = "passed"
                self._clear_failed_test_snapshot(test_path)
                return current

            if latest_fingerprints == previous_fingerprints:
                final_reason = "same execution failures repeated without meaningful change"
                history[-1]["stop_reason"] = final_reason
                break
            if not resolved:
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0
            if request_stop:
                final_reason = request_stop
                break
            if no_progress_rounds >= 2:
                final_reason = "two consecutive execution repair rounds made no meaningful progress"
                history[-1]["stop_reason"] = final_reason
                break
            previous_fingerprints = latest_fingerprints
        else:
            final_reason = "maximum of three execution-repair rounds reached"

        unresolved_results = execution.unresolved_methods
        report["latest_unresolved_execution_diagnostics"] = [
            self._execution_result_record(ClassExecutionResult(
                compiled=True,
                executed=True,
                ok=False,
                test_methods=[failure],
            ))["tests"][0]
            for failure in unresolved_results
        ]
        report["final_complete_class_execution_result"] = (
            "not_run_due_to_compilation_failure"
            if report["final_compilation_result"] == "failed"
            else "failed"
        )
        unresolved = [
            {
                "scope": failure.method_name,
                "diagnostic": failure.message or failure.root_cause or failure.status.value,
                "repair_attempts": sum(
                    1
                    for round_record in report.get("execution_repair_history", [])  # type: ignore[union-attr]
                    for repair in round_record.get("repairs", [])
                    if failure.method_name in repair.get("affected_tests", [])
                ),
            }
            for failure in unresolved_results
        ]
        if report["final_compilation_result"] == "failed" and outcome.compile_errors:
            unresolved.extend({
                "scope": "execution-repaired scope",
                "diagnostic": error,
                "repair_attempts": report["execution_repair_rounds_attempted"],
            } for error in outcome.compile_errors)
        self._finalize_validation_failure(
            report,
            test_path,
            phase="execution",
            reason=final_reason,
            unresolved=unresolved,
        )
        outcome.status = (
            "compile_failed"
            if report["final_compilation_result"] == "failed"
            else "test_failed"
        )
        outcome.notes.append(final_reason)
        return None

    # -- compile + repair --------------------------------------------------

    def _compile_with_repair(self, ctx: GenerationContext, test_path: Path,
                             module: ModuleInfo, outcome: TargetOutcome) -> str | None:
        """Compile once after assembly; repair only the owning method block when possible."""
        result, errors = self.compile_gate.check(test_path, module)
        if result.ok:
            return test_path.read_text(encoding="utf-8")

        if errors and all(error.file is None and error.line is None for error in errors):
            outcome.compile_errors = [error.render() for error in errors][:4]
            outcome.notes.append("non-repairable compile error (toolchain)")
            return None

        execution = ctx.execution_context
        methodwise = execution is not None and execution.target_kind.value in {"controller", "service-impl"}
        if methodwise:
            return self._compile_methodwise_repair(ctx, test_path, module, outcome, errors)

        current = test_path.read_text(encoding="utf-8")
        for attempt in range(self.cfg.max_compile_repairs):
            outcome.notes.append(f"compile repair {attempt + 1}: {len(errors)} error(s)")
            kb_hints = self.kb.hints_for([error.message for error in errors]) if self.kb else None
            messages = build_compile_repair_messages(ctx, current, errors, kb_hints)
            raw = self._chat(messages, self.cfg.temperature_repair, self.cfg.max_tokens_repair, outcome)
            candidate = inject_reusable_fixtures(raw or "", ctx)
            res = finalize_java(
                candidate, test_package=ctx.test_package, test_class_name=ctx.test_class_name,
                template=ctx.template, cut_symbol=ctx.symbol, collaborators=ctx.collaborators,
                source_imports=ctx.source_imports)
            if not res.ok or not res.code:
                continue
            current = res.code
            test_path.write_text(current, encoding="utf-8")
            result, errors = self.compile_gate.check(test_path, module)
            if result.ok:
                return current
        outcome.compile_errors = [error.render() for error in errors][:12]
        return None

    def _compile_methodwise_repair(
        self,
        ctx: GenerationContext,
        test_path: Path,
        module: ModuleInfo,
        outcome: TargetOutcome,
        errors,
    ) -> str | None:
        current = test_path.read_text(encoding="utf-8")
        for attempt in range(self.cfg.max_compile_repairs):
            repaired = self._repair_methodwise_error_batch(
                ctx,
                current,
                errors,
                outcome,
                label=f"method compile repair {attempt + 1}",
            )
            if repaired is None:
                break
            current = repaired
            test_path.write_text(current, encoding="utf-8")
            result, errors = self.compile_gate.check(test_path, module)
            if result.ok:
                return current

        outcome.compile_errors = [error.render() for error in errors][:12]
        return None

    def _repair_methodwise_error_batch(
        self,
        ctx: GenerationContext,
        current: str,
        errors,
        outcome: TargetOutcome,
        *,
        label: str,
    ) -> str | None:
        """Repair only the generated block owning the authoritative compiler errors."""
        owners = [owner_method_id_for_line(current, error.line) for error in errors]
        owners = [owner for owner in owners if owner]
        if not owners:
            # The failing line is OUTSIDE any generated method block -- almost always
            # a header issue (missing import, or a type invented in a body that has no
            # import). Name it loudly instead of dying silently: list the exact
            # compiler errors so the cause is visible in the report, and flag the
            # likely invented-type case for the schema/import work.
            rendered = [e.render() for e in errors][:8]
            outcome.notes.append(
                f"{label} stopped: compile error outside any generated method block "
                f"(header/import). Errors: " + " | ".join(rendered)
            )
            for e in errors:
                msg = (e.render() or "").lower()
                if "cannot find symbol" in msg or "cannot be resolved" in msg:
                    outcome.notes.append(
                        f"{label} likely unimported or invented type at "
                        f"line {e.line}: {e.render()}"
                    )
            return None

        owner = max(set(owners), key=owners.count)
        method_by_id = {
            f"{method.name}({','.join(type_name for type_name, _ in method.params)})": method
            for method in (ctx.symbol.methods or [])
        }
        context_by_id = {
            method.method_id: method
            for method in (ctx.execution_context.methods if ctx.execution_context else ())
        }
        production_method = method_by_id.get(owner)
        method_context = context_by_id.get(owner)
        block = method_block_for_id(current, owner)
        if production_method is None or method_context is None or block is None:
            outcome.notes.append(f"{label} stopped: context missing for {owner}")
            return None

        owner_errors = [
            error for error in errors
            if owner_method_id_for_line(current, error.line) == owner
        ] or list(errors)
        outcome.notes.append(f"{label} [{owner}]: {len(owner_errors)} error(s)")
        messages = build_method_compile_repair_messages(
            ctx,
            production_method,
            block,
            owner_errors,
        )
        raw = self._chat(
            messages,
            self.cfg.temperature_repair,
            self.cfg.max_tokens_repair,
            outcome,
        )
        replacement = normalize_writable_map_mutations(extract_test_method_block(raw or ""))
        existing_names = set(test_method_names(current)) - set(test_method_names(block))
        validation = validate_method_block(
            ctx,
            production_method,
            method_context,
            replacement,
            existing_names,
        )
        if not validation.ok:
            outcome.notes.append(f"{label} rejected [{owner}]: {validation.reason}")
            return None

        candidate = replace_method_block(current, owner, replacement)
        finalized = finalize_java(
            candidate,
            test_package=ctx.test_package,
            test_class_name=ctx.test_class_name,
            template=ctx.template,
            cut_symbol=ctx.symbol,
            collaborators=ctx.collaborators,
            source_imports=ctx.source_imports,
        )
        if not finalized.ok or not finalized.code:
            outcome.notes.append(f"{label} rejected [{owner}]: {finalized.reason}")
            return None
        return finalized.code

    # -- coverage loop -----------------------------------------------------

    def _coverage_loop(self, ctx: GenerationContext, module: ModuleInfo, test_path: Path,
                       symbol: ClassSymbol, last_good: str, outcome: TargetOutcome,
                       original: str | None = None) -> None:
        runner = self.coverage_runner
        assert runner is not None
        tline, tbranch = ctx.template.line_target, ctx.template.branch_target
        strict_serviceimpl = (
            ctx.execution_context is not None
            and ctx.execution_context.target_kind.value == "service-impl"
        )
        required_primary_test_names = test_method_names(last_good)

        run = runner.measure(module, test_classes=[self._test_fqcn(ctx)])
        # The coverage run's Maven test-compile is the AUTHORITATIVE compile check
        # (the fast javac gate can over-accept). If it fails on our test file, repair
        # from those errors and re-measure.
        # run = self._recover_maven_compile(ctx, module, test_path, run, outcome)
        # if not run.ok or not run.measured or run.xml_path is None:
        #     own = self._own_maven_errors(run.log_tail, test_path)
        #     if own:
        #         self._restore_or_remove(test_path, original)
        #         outcome.compile_errors = [e.render() for e in own][:12]
        #         outcome.status = "compile_failed"
        #         outcome.notes.append("failed Maven test-compile during coverage; reverted")
        if not strict_serviceimpl:
            run = self._recover_maven_compile(ctx, module, test_path, run, outcome)
        if not run.ok or not run.measured or run.xml_path is None:
            own = self._own_maven_errors(run.log_tail, test_path)
            if own:
                outcome.compile_errors = [e.render() for e in own][:12]
                self._capture_failed_test_snapshot(test_path, outcome)
                self._restore_or_remove(test_path, original)
                outcome.status = "compile_failed"
                outcome.notes.append("failed Maven test-compile during coverage; reverted")
                self._log_compile_failure(outcome, symbol.name, test_path)
            else:
                reason = self._build_failure_reason(run.log_tail)
                outcome.status = f"compiled (coverage unmeasured: {run.note})"
                outcome.notes.append(run.note)
                if reason:
                    outcome.notes.append("build failure: " + reason)
            return

        # ServiceImpl failures use the focused complete-class lifecycle. Keep
        # the legacy raw-log repair only for class kinds outside this phase.
        if not strict_serviceimpl:
            self._maybe_repair_failures(ctx, test_path, module, run.log_tail, outcome)

        cc = self._class_cov(run.xml_path, module, symbol)
        prev_covered = -1
        no_progress = 0
        for rnd in range(self.cfg.max_coverage_rounds):
            if cc is None:
                outcome.status = "compiled (class absent from coverage)"
                return
            outcome.rounds = rnd
            outcome.line_pct = round(cc.line_pct, 1)
            outcome.branch_pct = round(cc.branch_pct, 1)
            outcome.uncovered_lines = cc.uncovered_lines()
            outcome.uncovered_branches = cc.uncovered_branches()

            if cc.line_pct >= tline and cc.branch_pct >= tbranch:
                outcome.status = "covered"
                return
            if cc.covered_instr <= prev_covered:
                no_progress += 1
                if no_progress >= 2:
                    outcome.status = "stalled"
                    outcome.notes.append("no coverage progress for 2 rounds")
                    return
            else:
                no_progress = 0
            prev_covered = cc.covered_instr

            # augment
            augmented = self._augment(ctx, test_path, symbol, cc, outcome)
            if not augmented:
                break
            good = (
                self._validate_serviceimpl_class(
                    ctx,
                    test_path,
                    module,
                    outcome,
                    required_primary_test_names=required_primary_test_names,
                )
                if strict_serviceimpl
                else self._compile_with_repair(ctx, test_path, module, outcome)
            )
            if good is None:
                if strict_serviceimpl:
                    self._capture_failed_test_snapshot(test_path, outcome)
                    self._restore_or_remove(test_path, original)
                    outcome.notes.append("augmented complete class failed validation")
                    return
                # Existing non-ServiceImpl behavior remains unchanged.
                test_path.write_text(last_good, encoding="utf-8")
                outcome.notes.append("augmentation broke compile; reverted to last good")
                break
            last_good = good
            run = runner.measure(module, test_classes=[self._test_fqcn(ctx)])
            if not run.ok:
                outcome.notes.append(f"re-measure failed: {run.note}")
                break
            cc = self._class_cov(run.xml_path, module, symbol)

        # Loop ended without meeting the target (exhausted rounds / broke out).
        if outcome.status == "pending":
            outcome.status = "below_threshold"

    def _augment(self, ctx, test_path, symbol, cc, outcome) -> bool:
        execution = ctx.execution_context
        if execution is not None and execution.target_kind.value in {"controller", "service-impl"}:
            return self._augment_methodwise(ctx, test_path, cc, outcome)

        existing = test_path.read_text(encoding="utf-8")
        ranges = collapse_ranges(cc.uncovered_lines())
        if not ranges and not cc.uncovered_branches():
            return False
        excerpt = slice_uncovered(ctx.cut_source, ranges)
        branch_hints = self._branch_hints(ctx.cut_source, cc.uncovered_branches())
        messages = build_augmentation_messages(ctx, existing, excerpt, branch_hints)
        raw = self._chat(messages, self.cfg.temperature_gen, self.cfg.max_tokens_aug, outcome)
        candidate = inject_reusable_fixtures(raw or "", ctx)
        res = finalize_java(
            candidate, test_package=ctx.test_package, test_class_name=ctx.test_class_name,
            template=ctx.template, cut_symbol=ctx.symbol, collaborators=ctx.collaborators,
            source_imports=ctx.source_imports)
        if not res.ok or not res.code:
            outcome.notes.append(f"augmentation rejected: {res.reason}")
            return False
        test_path.write_text(res.code, encoding="utf-8")
        return True

    def _augment_methodwise(self, ctx, test_path, cc, outcome) -> bool:
        current = test_path.read_text(encoding="utf-8")
        uncovered = set(cc.uncovered_lines())
        candidates = []
        for method_context in ctx.execution_context.methods:
            if not method_context.source_range:
                continue
            start, end = method_context.source_range
            covered_lines = sorted(line for line in uncovered if start <= line <= end)
            if covered_lines:
                candidates.append((len(covered_lines), method_context, covered_lines))
        if not candidates:
            outcome.notes.append("method-wise augmentation skipped: uncovered lines not mapped to a production method")
            return False

        _, method_context, method_lines = max(candidates, key=lambda item: item[0])
        method = next(
            (
                candidate for candidate in (ctx.symbol.methods or [])
                if f"{candidate.name}({','.join(type_name for type_name, _ in candidate.params)})"
                == method_context.method_id
            ),
            None,
        )
        existing_block = method_block_for_id(current, method_context.method_id)
        if method is None or existing_block is None:
            outcome.notes.append(
                f"method-wise augmentation skipped: generated block missing for {method_context.method_id}"
            )
            return False

        ranges = collapse_ranges(method_lines)
        excerpt = slice_uncovered(ctx.cut_source, ranges)
        if not excerpt.strip():
            excerpt = method_context.method_source
        branch_hints = self._branch_hints(ctx.cut_source, cc.uncovered_branches())
        messages = build_method_augmentation_messages(
            ctx,
            method,
            existing_block,
            excerpt,
            branch_hints,
        )
        raw = self._chat(messages, self.cfg.temperature_gen, self.cfg.max_tokens_aug, outcome)
        new_block = normalize_writable_map_mutations(extract_test_method_block(raw or ""))
        validation = validate_method_block(
            ctx,
            method,
            method_context,
            new_block,
            set(test_method_names(current)),
        )
        if not validation.ok:
            outcome.notes.append(
                f"method augmentation rejected [{method_context.method_id}]: {validation.reason}"
            )
            return False

        combined = existing_block.rstrip() + "\n\n" + new_block.strip()
        candidate = replace_method_block(current, method_context.method_id, combined)
        res = finalize_java(
            candidate,
            test_package=ctx.test_package,
            test_class_name=ctx.test_class_name,
            template=ctx.template,
            cut_symbol=ctx.symbol,
            collaborators=ctx.collaborators,
            source_imports=ctx.source_imports,
        )
        if not res.ok or not res.code:
            outcome.notes.append(
                f"method augmentation rejected [{method_context.method_id}]: {res.reason}"
            )
            return False
        test_path.write_text(res.code, encoding="utf-8")
        outcome.notes.append(
            f"method augmented [{method_context.method_id}]: {len(validation.test_names)} new test(s)"
        )
        return True

    def _maybe_repair_failures(self, ctx, test_path, module, log_tail, outcome) -> None:
        if self.cfg.max_failure_repairs <= 0 or not log_tail:
            return
        match = _TESTS_RUN.search(log_tail)
        if not match:
            return
        failures, errors = int(match.group(2)), int(match.group(3))
        if failures + errors == 0:
            return

        execution = ctx.execution_context
        if execution is not None and execution.target_kind.value in {"controller", "service-impl"}:
            self._maybe_repair_method_failure(ctx, test_path, module, log_tail, outcome)
            return

        outcome.notes.append(f"test failures detected ({failures}F/{errors}E); repairing")
        current = test_path.read_text(encoding="utf-8")
        messages = build_test_failure_repair_messages(ctx, current, log_tail[-2500:])
        raw = self._chat(messages, self.cfg.temperature_repair, self.cfg.max_tokens_repair, outcome)
        candidate = inject_reusable_fixtures(raw or "", ctx)
        res = finalize_java(
            candidate, test_package=ctx.test_package, test_class_name=ctx.test_class_name,
            template=ctx.template, cut_symbol=ctx.symbol, collaborators=ctx.collaborators,
            source_imports=ctx.source_imports)
        if res.ok and res.code:
            test_path.write_text(res.code, encoding="utf-8")
            good = self._compile_with_repair(ctx, test_path, module, outcome)
            if good is None:
                test_path.write_text(current, encoding="utf-8")

    def _maybe_repair_method_failure(self, ctx, test_path, module, log_tail, outcome) -> None:
        current = test_path.read_text(encoding="utf-8")
        line_matches = re.findall(
            rf"{re.escape(ctx.test_class_name)}\.java:(\d+)",
            log_tail,
        )
        owners = [
            owner_method_id_for_line(current, int(line))
            for line in line_matches
        ]
        owners = [owner for owner in owners if owner]
        if not owners:
            outcome.notes.append(
                "method runtime repair skipped: stack trace line did not map to a generated method block"
            )
            return

        owner = max(set(owners), key=owners.count)
        method = next(
            (
                candidate for candidate in (ctx.symbol.methods or [])
                if f"{candidate.name}({','.join(type_name for type_name, _ in candidate.params)})" == owner
            ),
            None,
        )
        method_context = next(
            (candidate for candidate in ctx.execution_context.methods if candidate.method_id == owner),
            None,
        )
        current_block = method_block_for_id(current, owner)
        if method is None or method_context is None or current_block is None:
            outcome.notes.append(f"method runtime repair skipped: context missing for {owner}")
            return

        outcome.notes.append(f"runtime repair isolated to production method [{owner}]")
        messages = build_method_runtime_repair_messages(
            ctx,
            method,
            current_block,
            log_tail[-3000:],
        )
        raw = self._chat(messages, self.cfg.temperature_repair, self.cfg.max_tokens_repair, outcome)
        replacement = normalize_writable_map_mutations(extract_test_method_block(raw or ""))
        existing_names = set(test_method_names(current)) - set(test_method_names(current_block))
        validation = validate_method_block(
            ctx,
            method,
            method_context,
            replacement,
            existing_names,
        )
        if not validation.ok:
            outcome.notes.append(f"method runtime repair rejected [{owner}]: {validation.reason}")
            return

        candidate = replace_method_block(current, owner, replacement)
        finalized = finalize_java(
            candidate,
            test_package=ctx.test_package,
            test_class_name=ctx.test_class_name,
            template=ctx.template,
            cut_symbol=ctx.symbol,
            collaborators=ctx.collaborators,
            source_imports=ctx.source_imports,
        )
        if not finalized.ok or not finalized.code:
            outcome.notes.append(f"method runtime repair rejected [{owner}]: {finalized.reason}")
            return
        test_path.write_text(finalized.code, encoding="utf-8")
        good = self._compile_with_repair(ctx, test_path, module, outcome)
        if good is None:
            test_path.write_text(current, encoding="utf-8")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _build_failure_reason(log_tail: str) -> str | None:
        """Pull a concise reason out of a Maven build-failure log tail."""
        if not log_tail:
            return None
        m = re.search(r"Failed to execute goal\s+([\w.\-]+:[\w.\-]+:[\w.\-]+:[\w.\-]+)", log_tail)
        plugin = m.group(1) if m else None
        if "COMPILATION ERROR" in log_tail:
            em = re.search(r"\[ERROR\]\s+(/[^\n]+\.java:\[\d+,\d+\][^\n]+)", log_tail)
            detail = em.group(1).strip() if em else "a sibling test failed to compile"
            return f"sibling test-compile failed ({detail[:120]})"
        return f"plugin {plugin}" if plugin else None

    @staticmethod
    def _restore_or_remove(test_path: Path, original: str | None) -> None:
        try:
            if original is not None:
                test_path.write_text(original, encoding="utf-8")
            elif test_path.exists():
                test_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _capture_failed_test_snapshot(test_path: Path, outcome: TargetOutcome) -> None:
        """Persist the rejected/non-compiling generated test for inspection.

        The main generated test is restored or removed immediately after a
        compile failure so the Java module is not left broken. This snapshot is
        deliberately written beside the generated test with a .failing suffix so
        it is easy to inspect but is not compiled by Maven/Gradle as a test
        source.
        """
        try:
            if not test_path.exists():
                return
            snapshot_path = test_path.with_name(test_path.name + ".failing")
            snapshot_path.write_text(test_path.read_text(encoding="utf-8"), encoding="utf-8")
            outcome.notes.append(f"saved failing generated test snapshot: {snapshot_path}")
        except OSError as exc:
            outcome.notes.append(f"could not save failing test snapshot: {exc}")

    @staticmethod
    def _clear_failed_test_snapshot(test_path: Path) -> None:
        """Remove only the stale snapshot for this exact successful test file."""
        snapshot_path = test_path.with_name(test_path.name + ".failing")
        try:
            if snapshot_path.exists():
                snapshot_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _log_compile_failure(outcome: TargetOutcome, class_name: str, test_path: Path) -> None:
        """Emit concise compile-failure diagnostics without crashing the loop.

        The CLI also prints compile_failed details after run_target() returns.
        This helper exists for Engine users that call run_target() directly and
        to keep the compile-failure path safe.
        """
        if outcome.compile_errors:
            log.error("compile_failed: %s", class_name)
            log.error("  test: %s", test_path)
            for err in outcome.compile_errors[:12]:
                log.error("  %s", err.replace("\n", "\n  "))
        else:
            log.warning("compile_failed: %s; no compiler errors captured", class_name)

    @staticmethod
    def _test_fqcn(ctx) -> str:
        return f"{ctx.test_package}.{ctx.test_class_name}" if ctx.test_package else ctx.test_class_name

    @staticmethod
    def _own_maven_errors(log_tail: str, test_path: Path):
        return [e for e in parse_maven_compiler_errors(log_tail or "")
                if e.file and e.file.name == test_path.name]

    def _recover_maven_compile(self, ctx, module, test_path, run, outcome):
        """Repair authoritative Maven compile failures without regenerating other methods."""
        methodwise = (
            ctx.execution_context is not None
            and ctx.execution_context.target_kind.value in {"controller", "service-impl"}
        )
        for attempt in range(self.cfg.max_compile_repairs):
            if run.ok:
                return run
            own = self._own_maven_errors(run.log_tail, test_path)
            if not own:
                return run  # not our compile error (sibling/env) -> genuinely unmeasured

            if methodwise:
                current = test_path.read_text(encoding="utf-8")
                repaired = self._repair_methodwise_error_batch(
                    ctx,
                    current,
                    own,
                    outcome,
                    label=f"maven method compile repair {attempt + 1}",
                )
                if repaired is None:
                    return run
                test_path.write_text(repaired, encoding="utf-8")
            else:
                outcome.notes.append(f"maven compile repair: {len(own)} error(s)")
                kb_hints = self.kb.hints_for([e.message for e in own]) if self.kb else None
                messages = build_compile_repair_messages(
                    ctx,
                    test_path.read_text(encoding="utf-8"),
                    own,
                    kb_hints,
                )
                raw = self._chat(
                    messages,
                    self.cfg.temperature_repair,
                    self.cfg.max_tokens_repair,
                    outcome,
                )
                candidate = inject_reusable_fixtures(raw or "", ctx)
                res = finalize_java(
                    candidate,
                    test_package=ctx.test_package,
                    test_class_name=ctx.test_class_name,
                    template=ctx.template,
                    cut_symbol=ctx.symbol,
                    collaborators=ctx.collaborators,
                    source_imports=ctx.source_imports,
                )
                if not res.ok or not res.code:
                    return run
                test_path.write_text(res.code, encoding="utf-8")

            run = self.coverage_runner.measure(
                module,
                test_classes=[self._test_fqcn(ctx)],
            )
        return run

    def _classpath_profile(self, module: ModuleInfo):
        """Prefer the resolved test classpath (accurate slice/junit detection);
        fall back to scanning pom artifacts when the compile gate is unavailable."""
        bm = self.stack.boot_major
        if self.compile_gate.ready:
            prof = from_classpath_string(self.compile_gate.classpath_for(module.name), boot_major=bm)
            if any(".jar" in e for e in prof.entries):
                return prof
        return from_pom_artifacts(self.repo_root, module.root, boot_major=bm)

    def _class_cov(self, xml_path: Path, module: ModuleInfo, symbol: ClassSymbol):
        for cc in parse_jacoco_xml(xml_path, module.name, module.root):
            if cc.fqcn == symbol.fqcn:
                return cc
        return None

    @staticmethod
    def _branch_hints(source: str, branch_lines: list[int]) -> list[str]:
        lines = source.splitlines()
        hints: list[str] = []
        for nr in branch_lines[:12]:
            if 1 <= nr <= len(lines):
                hints.append(f"line {nr}: {lines[nr - 1].strip()}  (some branches untaken)")
        return hints

    def _chat(self, messages, temperature, max_tokens, outcome: TargetOutcome) -> str | None:
        started = perf_counter()
        log.info("[TIMING] LLM repair/augmentation start | messages=%d | maxTokens=%d", len(messages), max_tokens)
        try:
            resp = self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            outcome.notes.append(f"llm error: {str(exc)[:140]}")
            return None
        if getattr(resp, "usage", None):
            outcome.tokens_used += resp.usage.total_tokens
        log.info(
            "[TIMING] LLM repair/augmentation end: %.3fs | tokens=%d",
            perf_counter() - started,
            resp.usage.total_tokens if getattr(resp, "usage", None) else 0,
        )
        return resp.message
