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
    extract_test_method_block,
    insert_method_block,
    method_block_for_id,
    owner_method_id_for_line,
    replace_method_block,
    test_method_names,
    validate_method_block,
)
from junitforge.execution.renderer import write_execution_context_json
from junitforge.discovery import (
    is_test_target,
    module_for_source,
    test_package_for,
    test_path_for,
)
from typing import Callable as _Callable
from junitforge.models import (
    ClassSymbol,
    GenerationContext,
    ModuleInfo,
    StackProfile,
    TargetOutcome,
)
from junitforge.parser.collaborators import resolve_collaborators
from junitforge.parser.java_symbols import parse_file
from junitforge.postprocess import finalize_java
from junitforge.prompts.augmentation import build_augmentation_messages, build_method_augmentation_messages
from junitforge.prompts.generation import build_generation_messages, build_method_generation_messages
from junitforge.prompts.repair import (
    build_compile_repair_messages,
    build_method_compile_repair_messages,
    build_method_runtime_repair_messages,
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
                outcome.notes.append(f"execution context: {execution_context.extraction_status.value}")
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not write execution context for %s: %s", symbol.fqcn, exc)

        _t = perf_counter()
        ctx = build_context(
            source_file=jf, symbol=symbol, collaborators=collaborators,
            test_package=test_pkg, test_class_name=test_class,
            stack=self.stack, classpath=cp, template=template, source_path=source_path,
            execution_context=execution_context,
        )
        _elapsed = perf_counter() - _t
        log.info("[TIMING] prompt context build: %.3fs", _elapsed)
        timing_event("engine.prompt_context.end", elapsed=_elapsed)

        # Back up any pre-existing test so a failed generation never destroys it.
        original = test_path.read_text(encoding="utf-8") if test_path.exists() else None

        # 1) generate
        _t = perf_counter()
        code = self._generate(ctx, outcome)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] initial generation: %.3fs", _elapsed)
        timing_event("engine.initial_generation.end", elapsed=_elapsed)
        if code is None:
            outcome.status = "generation_failed"
            self._restore_or_remove(test_path, original)
            return outcome
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(code, encoding="utf-8")

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
            good = self._compile_with_repair(ctx, test_path, module, outcome)
        _elapsed = perf_counter() - _t
        log.info("[TIMING] compile + repair: %.3fs", _elapsed)
        timing_event("engine.compile_repair.end", elapsed=_elapsed, success=good)
        if good is None:
            self._capture_failed_test_snapshot(test_path, outcome)
            self._restore_or_remove(test_path, original)
            outcome.status = "compile_failed"
            outcome.notes.append("reverted non-compiling test (restored original)"
                                 if original is not None else "removed non-compiling test")
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
            _t = perf_counter()
            self._coverage_loop(ctx, module, test_path, symbol, good, outcome, original)
            _elapsed = perf_counter() - _t
            log.info("[TIMING] runtime + coverage loop: %.3fs", _elapsed)
            timing_event("engine.runtime_coverage.end", elapsed=_elapsed)
        else:
            outcome.status = "compiled"
            outcome.notes.append("coverage skipped during generation; run JaCoCo/EclEmma once at the end")

        _elapsed = perf_counter() - target_started
        log.info("[TIMING] target end: %.3fs | %s | status=%s", _elapsed, symbol.name, outcome.status)
        timing_event("engine.target.end", elapsed=_elapsed, class_name=symbol.name, status=outcome.status)
        return outcome
    

    # -- generation --------------------------------------------------------

    def _generate(self, ctx: GenerationContext, outcome: TargetOutcome) -> str | None:
        execution = ctx.execution_context
        if execution is not None and execution.target_kind.value in {"controller", "service-impl"}:
            return self._generate_methodwise(ctx, outcome)
        return self._generate_classwide(ctx, outcome)

    def _generate_methodwise(self, ctx: GenerationContext, outcome: TargetOutcome) -> str | None:
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
            block = extract_test_method_block(raw or "")
            validation = validate_method_block(ctx, production_method, method_context, block, set())
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
            res = finalize_java(
                raw or "", test_package=ctx.test_package, test_class_name=ctx.test_class_name,
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
            res = finalize_java(
                raw or "", test_package=ctx.test_package, test_class_name=ctx.test_class_name,
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
            outcome.notes.append(
                f"{label} stopped: compiler line could not be mapped to a generated method block"
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
        replacement = extract_test_method_block(raw or "")
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

        # one-shot test-failure repair if the run reported failures
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
            good = self._compile_with_repair(ctx, test_path, module, outcome)
            if good is None:
                # revert to last compiling snapshot - never ship broken
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
        res = finalize_java(
            raw or "", test_package=ctx.test_package, test_class_name=ctx.test_class_name,
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
        new_block = extract_test_method_block(raw or "")
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
        res = finalize_java(
            raw or "", test_package=ctx.test_package, test_class_name=ctx.test_class_name,
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
        replacement = extract_test_method_block(raw or "")
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
                res = finalize_java(
                    raw or "",
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