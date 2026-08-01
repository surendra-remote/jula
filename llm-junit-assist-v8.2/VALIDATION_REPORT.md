# Validation Report — llm-junit-assist v8.1 DTO/Entity Equals and HashCode

## v8.1 phase scope

This phase changes only DTO/entity equality analysis, generation guidance, and
the corresponding DTO/entity post-processing guard. It replaces the generic
three-object template with an effective strategy and verified-member plan:

```text
existing Java symbols/source/annotations/inheritance
  -> identity, Lombok, record, manual, inherited, or unresolved strategy
  -> verified participating members and minimal values
  -> identity, resolved-value, or conservative assertions
  -> existing compile/execute/repair/publication lifecycle
```

ServiceImpl entry discovery, successful-path selection, ServiceImpl schema and
fixture generation, class-level validation/repair/publication, coverage
augmentation, and unrelated DTO/entity accessor/constructor/builder guidance
were not redesigned.

## v8.1 commands executed

Syntax validation:

```bash
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python \
  -m compileall -q junitforge tests
```

Focused equality regressions:

```bash
PATH=/tmp/junitforge-jdk-tools:$PATH \
PYTHONPATH=/tmp/llm_junit_pytest \
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python \
  -m pytest -q tests/test_equality_generation.py
```

Result:

```text
......................                                                   [100%]
22 passed in 0.13s
```

Complete project suite:

```bash
PATH=/tmp/junitforge-jdk-tools:$PATH \
PYTHONPATH=/tmp/llm_junit_pytest \
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python -m pytest -q
```

Final result:

```text
........................................................................ [ 74%]
.........................                                                [100%]
97 passed in 1.42s
```

## v8.1 required regression mapping

`tests/test_equality_generation.py` proves:

1. Object-identity classes do not receive independent same-value equality.
2. `@EqualsAndHashCode` uses only participating non-static/non-transient fields.
3. Lombok `@Data` equality is recognized.
4. Lombok `@Value` equality and equality-only construction are recognized.
5. Java records use their record components and canonical constructor.
6. `@EqualsAndHashCode.Exclude` fields cannot drive inequality.
7. `onlyExplicitlyIncluded = true` uses only resolved includes.
8. `callSuper = false` excludes parent equality state.
9. `callSuper = true` includes only source-resolved parent equality state.
10. Inherited equality excludes child-only fields.
11. Equal pairs require equal hash codes.
12. Unequal pairs never require different hash codes; invalid generated assertions are removed.
13. Equality guidance forbids complete recursive fixtures and unrelated object graphs.
14. Participating nested references reuse one exact instance in equal pairs.
15. Generated-ID entities require non-null identifier witnesses and no null-ID transient pair.
16. Relationship-heavy entity equality becomes conservative and does not traverse relationships.
17. ServiceImpl generation and validation remain outside equality resolution.

Five additional regressions cover simple manual equality, one-sided
equals/hashCode overrides, safely resolvable included replacement methods,
complex included methods, and empty explicit-include contracts.

## v8.1 known limitations

- Manual equality is interpreted only when field comparisons/hashing are directly
  visible and use common Java/Object/array comparison calls. Normalization,
  external state, `super.equals`, or unsupported helper logic becomes conservative.
- Included methods are resolved only when a zero-argument method directly returns
  one verified field or its verified getter. Other method bodies are reported and
  tested conservatively.
- Parent equality requires the existing contextual source resolver. Missing or
  ambiguous parent source produces conservative assertions and an outcome warning.
- A participating reference needs verified minimal construction for resolved-value
  testing. Otherwise the class falls back to conservative assertions instead of
  inventing a constructor or factory.
- Entity relationships, mutable collection state, and proxy behavior are not
  interpreted. Relationship-participating entity equality fails closed; Hibernate
  proxies are intentionally not simulated.
- Enum inequality requires two verified constants. With fewer verified constants,
  another participating member must provide the difference or the plan is conservative.
- No enterprise Java repository/classpath was supplied with this phase, so
  validation uses source-symbol regressions and the project's existing real Java
  fixture compilation tests rather than claiming execution against a particular
  enterprise DTO/entity class.

---

## Preserved v8 lifecycle validation record

## Scope

This delivery implements only the class-level lifecycle that begins after the complete ServiceImpl primary test class has been assembled:

```text
complete generated class
  -> complete-class compilation
  -> grouped evidence-based compilation repair when required
  -> complete-class compilation
  -> one complete-class test execution
  -> focused assertion/runtime/Mockito repair when required
  -> one complete-class compilation and execution
  -> normal or failing-artifact publication
```

ServiceImpl entry discovery, successful-path selection, schema/fixture generation, DTO/entity equality generation, and coverage-test content generation were not changed. Coverage-enabled ServiceImpl output is routed through the same final publication validator.

## Commands executed

Syntax validation:

```bash
python -m compileall -q junitforge tests
```

Focused lifecycle regressions:

```bash
PATH=/tmp/junitforge-jdk-tools:$PATH \
PYTHONPATH=/tmp/llm_junit_pytest \
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python \
  -m pytest -q tests/test_class_validation_pipeline.py tests/test_compile_error_parsing.py
```

Result:

```text
..........................                                               [100%]
26 passed in 0.20s
```

Complete project suite:

```bash
PATH=/tmp/junitforge-jdk-tools:$PATH \
PYTHONPATH=/tmp/llm_junit_pytest \
/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python -m pytest -q
```

Final result:

```text
........................................................................ [ 96%]
...                                                                      [100%]
75 passed in 1.29s
```

The temporary `javac` launcher delegates to the runtime's real Java 17 `jdk.compiler/com.sun.tools.javac.Main` module. It is validation infrastructure only and is not included in the project ZIP.

## Required regression mapping

`tests/test_class_validation_pipeline.py` contains 23 ordered tests proving:

1. every expected primary method block exists before the first class compilation, with incomplete classes stopped before compiler or runner invocation;
2. the complete class is compiled as one source unit;
3. same-test compiler diagnostics are grouped into one request;
4. shared-helper compiler diagnostics are repaired once at helper scope;
5. compiler repair receives exact diagnostics, offending source, complete scope, minimal generated context, and verified production evidence;
6. no test execution occurs while compilation fails;
7. compilation repair stops immediately on success;
8. compilation repair never exceeds three rounds;
9. repeated compiler or execution diagnostics and no-progress responses terminate early;
10. Surefire executes the complete class once;
11. passing tests are excluded from repair prompts;
12. assertion evidence is supplied and `assertNotNull(response)` plus the selected CUT invocation cannot be weakened or bypassed;
13. runtime repair receives exception/root cause, filtered CUT/test frames, and determinable null path;
14. Mockito repair receives diagnostic subtype, exact stub source, invocation, arguments, locations, and verified signature; raw-all-`any()` and non-void `doNothing()` repairs are rejected;
15. unrelated tests, fixtures, schemas, and production types are excluded;
16. all current failure repairs are applied before one recompile/rerun;
17. execution repair never exceeds three rounds;
18. execution-repair compilation errors stay within that execution round;
19. unresolved tests cannot be removed, commented out, disabled, or renamed;
20. normal Java publication requires complete compilation and all-pass execution;
21. unresolved classes use the existing `.java.failing` convention;
22. the per-class report explains every phase, round, unresolved issue, and publication result;
23. the existing non-ServiceImpl compile path remains unchanged.

`tests/test_compile_error_parsing.py` additionally validates Maven compiler diagnostics containing Windows drive letters, spaces, exact columns, and continuation details.

## Publication behavior validated

- A normal `<ServiceClass>Test.java` remains only after full compilation and an all-pass class execution.
- A stale `.failing` snapshot is removed after successful publication.
- Any remaining compile error, assertion failure, runtime/test error, Mockito failure, skipped test, missing test result, unsafe repair response, or no-progress stop selects `<ServiceClass>Test.java.failing`.
- The failing snapshot preserves the complete latest class and every generated primary test; the normal unresolved `.java` file is restored or removed using the existing behavior.
- Each processed ServiceImpl class receives concise JSON and Markdown reports under `class-validation/` inside the configured report directory.

## Known limitations

- The source-scope mapper intentionally targets conventional top-level declarations emitted by the project's deterministic skeleton. Exotic class-level lambda/anonymous-class field initializers may be unmapped; the class then fails safely instead of receiving a broad rewrite.
- Detailed execution repair depends on standard Surefire/JUnit XML and standard Mockito diagnostic layouts. Unknown provider formats remain unresolved and produce a failing artifact rather than guessed evidence.
- Direct Surefire execution requires Maven/Surefire (or the existing Ant JUnit ConsoleLauncher toolchain) to be available. An unavailable runner cannot produce a normal publication.
- The supplied enterprise target repository and its real Maven classpath were not mounted here. Validation therefore uses deterministic fake compiler/runner boundaries plus real Java compilation of the existing fixture-emission regressions; no claim is made that a particular enterprise ServiceImpl class was executed in this environment.
- Mockito diagnostic extraction covers the standard strict-stubbing, verification, matcher, and unused-stub layouts. Provider-specific prose outside those layouts is retained as an exact diagnostic but may not yield separate argument/location fields.
- Raw Maven/Surefire logs remain available for technical reference, but only extracted class-level evidence is used in repair prompts and concise reports.
