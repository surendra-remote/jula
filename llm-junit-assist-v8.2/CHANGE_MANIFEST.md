# Changed-File Manifest

| File | Status | Reason | Major changes |
|---|---|---|---|
| `junitforge/models.py` | Modified | Preserve source/type facts needed by recursive schemas | Added field initializer/declaring type and class package/import/source/enclosing/type-parameter metadata. |
| `junitforge/parser/java_symbols.py` | Modified | Retain richer JavaParser facts and owner context | Recursively attaches package/import/source/enclosing context; consumes initializer, declaring type and type parameters. |
| `junitforge/parser/lazy_symbol_resolver.py` | Modified | Prevent wrong class selection and resolve nested types | Added import/package/enclosing-aware lookup, ambiguity handling, recursive nested indexing and caching. |
| `junitforge/execution/models.py` | Modified | Represent deterministic schema/fixture facts | Added property/construction kinds, rich schema properties, collection/map schemas, fixture specs and method/class usage fields. |
| `junitforge/execution/payload_schema.py` | Modified | Replace shallow per-method projection with a full catalog | Added five-level recursive expansion, inheritance, cycles, field-wise union, direct-root fixture projection, dynamic map collection-element linking, date-format overlays, construction planning and cardinality/value constraints. |
| `junitforge/execution/analyzer.py` | Modified | Build one class-level schema and fixture catalog | Expanded root discovery, follows all reachable same-class concrete methods (not only private helpers), joins helper collaborator calls, separates catalog schemas from direct fixture roots, integrates deep map aliases/constraints, and records method-to-fixture mapping. |
| `junitforge/execution/fixture_builder_emitter.py` | Modified | Existing emitter was disconnected/model-incompatible | Rewritten as deterministic deep fixture emitter supporting DTOs, entities, records, collections, wrappers, maps, enums and cycles; now records collection-element dependencies and does not let an explicit `null` initializer suppress a verified nested fixture. |
| `junitforge/execution/assembly.py` | Modified | Put reusable fixtures in the real test skeleton | Inserts supported helper methods/imports and suppresses duplicate simple-name imports. |
| `junitforge/execution/renderer.py` | Modified | Expose catalogs and method usage without oversized LLM requests | Added report writers plus a compact method-generation contract that filters the class catalog to direct fixture/map roots and removes duplicated statement/member/dereference dumps. |
| `junitforge/prompts/generation.py` | Modified | Ensure method tests reuse only real fixtures | Lists only verified direct fixture roots, recursively describes relevant nested map shapes, prohibits inline reconstruction and removes false fixture-availability claims. |
| `junitforge/prompts/repair.py` | Modified | Repair structurally invalid initial method responses | Added a bounded method-generation validation repair prompt that removes CUT spying/stubbing and redirects setup to exact collaborators on reachable same-class paths. |
| `junitforge/loop.py` | Modified | Validate fixtures and preserve valid method output | Uses context-aware resolver, writes reports, compile-gates the fixture skeleton and performs one bounded structural repair when an initial method response is rejected before javac. |
| `tools/javaparser-cli/src/main/java/com/singlife/junitforge/JavaSymbolExtractor.java` | Modified | Supply missing source facts | Emits field initializers/declaring types plus object creations, casts, literal map-key reads, collection access and lambda bindings. |
| `tests/test_schema_fixture_pipeline.py` | New | Validate the new deterministic pipeline | Covers recursion, inheritance, cycles, duplicate names, nested types, dynamic map collection hydration, date formats, fixture-root filtering, same-class reachability, structural repair prompts, null-initializer override, skeleton integration and `javac` fixture compilation. |
| `docs/recursive-schema-fixtures.md` | New | Operational and design documentation | Documents architecture, models, algorithms, reports, limitations and commands. |
| `CHANGE_MANIFEST.md` | New | Delivery traceability | Lists all modified/new files and their purpose. |
| `VALIDATION_REPORT.md` | New | Verification evidence | Records executed checks, results and environment limitations. |
| `RUN_COMMANDS.md` | New | Reproducible setup and execution | Provides Windows/Linux dependency, build, test, CLI, configuration and report commands. |

## Compatibility correction — ClassSymbol source context

- `junitforge/models.py`: `ClassSymbol` now explicitly declares `package_name`, `imports`, `source_path`, `enclosing_fqcn`, and `type_parameters`, and is intentionally unslotted so parser/resolver source-context enrichment remains backward-compatible.
- `tests/test_schema_fixture_pipeline.py`: added model-contract and nested source-context regression tests reproducing the reported `AttributeError`.

## v4 targeted corrections after TravelServiceImpl execution

The original-project totals remain **13 modified existing files + 5 new files**. Compared with v3, these files changed again:

| File | v4 change |
|---|---|
| `junitforge/execution/analyzer.py` | Recognizes implicit/unscoped same-class calls and supplements missing JavaParser helper edges from exact source, allowing `getQuotation(...)` collaborator invocations to propagate into the public entry method. |
| `junitforge/execution/assembly.py` | Adds deterministic normalization of mutated wildcard-map locals to `Map<String, Object>` and rejects any unsafe mutation that remains before javac. |
| `junitforge/execution/renderer.py` | States that collaborators inside public/protected/package-visible helpers must be stubbed and that mutable maps require `Map<String, Object>`. |
| `junitforge/prompts/generation.py` | Adds explicit same-class helper collaborator-stubbing and writable dynamic-map mutation rules. |
| `junitforge/prompts/repair.py` | Repairs omitted helper-path stubs and wildcard-map capture mutations in both structural and compile repair. |
| `junitforge/loop.py` | Applies deterministic writable-map normalization to initial generation and every method-level repair response before validation/insertion. |
| `tests/test_schema_fixture_pipeline.py` | Adds regressions for implicit helper receivers, source fallback, deterministic wildcard-map normalization, wildcard-map rejection, typed-map acceptance, and prompt requirements. |
| `docs/recursive-schema-fixtures.md` | Documents same-class execution closure and writable map mutation rules. |
| `CHANGE_MANIFEST.md` | Records the v4 correction set. |
| `VALIDATION_REPORT.md` | Records the expanded 22-test validation. |

## v5 correction — generic-return public helper propagation and mandatory stub coverage

Compared with v4, these files changed:

| File | v5 change |
|---|---|
| `junitforge/execution/analyzer.py` | Replaced exact rendered-return-type declaration matching with method-name/arity validation over the resolved source declaration. This prevents `Map<?,?>` versus `Map<?, ?>` formatting from excluding `getQuotation(...)`. Added `execution.public_method.call_graph` timing output containing exact reachable same-class methods and merged collaborator calls. |
| `junitforge/execution/assembly.py` | Added suite-level validation for source-proven unconditional non-void collaborator calls. A generated suite that omits every stub for helper-path calls such as `aesEncUtils.encode` or `iQuotationClient.getQuotationByRefNoTransType` is rejected before javac and routed through bounded structural repair. |
| `junitforge/prompts/repair.py` | Explicitly directs structural repair to restore every named missing collaborator stub and preserve transformed value flow between chained calls. |
| `tests/test_schema_fixture_pipeline.py` | Added generic-return declaration matching, complete public-helper execution-context aggregation, missing-stub rejection, and valid-stub acceptance regressions. |
| `docs/recursive-schema-fixtures.md` | Documents generic formatting independence, call-graph audit logging, and mandatory suite-level helper collaborator coverage. |
| `CHANGE_MANIFEST.md` | Records the v5 correction set. |
| `VALIDATION_REPORT.md` | Records the 27-test v5 validation and expected runtime evidence. |

The original-project totals remain **13 modified existing files + 5 new files**. v5 changes four source/test files relative to v4, plus the delivery documentation above.

## v6 correction — narrow primitive fixture arguments and Windows javac diagnostics

Compared with v5, these files changed:

| File | v6 change |
|---|---|
| `junitforge/execution/fixture_builder_emitter.py` | Emits `(byte) 1` and `(short) 1` for primitive and boxed byte/short setter parameters. Java does not apply assignment-style constant narrowing during method invocation, so plain `1` was invalid for `setRnldurn(byte)`, `setNofoutinst(short)`, and similar entity setters. |
| `junitforge/compile_gate.py` | Accepts Windows drive-letter paths in javac diagnostics and adds a bounded raw-output fallback so a failed compile gate can never produce an empty diagnostic list. |
| `junitforge/vendor/javac.py` | Applies the same Windows-safe javac error parsing to compact compiler messages. |
| `tests/test_schema_fixture_pipeline.py` | Adds Java-compilation regression coverage for primitive/boxed `byte` and `short` fixture setters. |
| `tests/test_compile_error_parsing.py` | New regression tests for structured and compact javac errors containing Windows paths such as `C:\\repo\\...\\GeneratedTest.java`. |
| `CHANGE_MANIFEST.md` | Records the v6 correction set. |
| `VALIDATION_REPORT.md` | Records the supplied `GiRenewalAuthServiceImpl` catalog reproduction and 30-test validation. |

### Supplied catalog reproduction

`validGiContractHeaderEntity()` generated 12 invalid narrow-primitive setter calls:

- byte: `setRnldurn(1)`, `setDishnrcnt(1)`, `setGprmnths(1)`, `setTermage(1)`;
- short: `setNofoutinst(1)`, `setNofrisks(1)`, `setPolinc(1)`, `setPolsum(1)`, `setNxtsfx(1)`, `setTfrswused(1)`, `setTfrswleft(1)`, `setZendno(1)`.

They now emit explicit casts. No JavaParser source changed from v5 to v6.

## v7 correction — fluent null assertions and class-type-independent object graphs

Compared with v6, these files changed:

| File | v7 change |
|---|---|
| `junitforge/postprocess.py` | Repairs unsafe default-null assertions using either a verified JavaBean setter or a verified fluent mutator. Supports direct, `assertAll`, multiline, and message-overload formatting before applying the rejection gate. |
| `junitforge/execution/models.py` | Adds validator, utility, mapper, and generic structured-unit execution-target kinds while preserving method-wise generation only for controllers and ServiceImpl classes. |
| `junitforge/stack/classifier.py` | Replaces the Controller/ServiceImpl-only execution-context gate with capability-based classification for validators, utilities, mappers, and any concrete class exposing a structured public parameter. |
| `junitforge/execution/analyzer.py` | Builds recursive schemas and fixtures for the generalized targets, includes public/private static same-class method reachability for utility-style classes, and prevents class-qualified external calls from becoming false self-helper edges. |
| `junitforge/execution/assembly.py` | Adds deterministic fixture/import insertion for class-wide utility, validator, mapper, and structured-unit output. Fixture dependency closure is inserted before final compilation. |
| `junitforge/execution/renderer.py` | Distinguishes precompiled method-wise fixtures from deterministic class-wide fixtures inserted before compile and updates generalized generation rules. |
| `junitforge/loop.py` | Preserves deterministic fixtures across initial class-wide generation, compile repair, augmentation, runtime repair, and Maven repair. |
| `junitforge/prompts/generation.py` | Teaches entity/DTO generation to use verified JavaBean setters or fluent mutators for null-field coverage. |
| `junitforge/prompts/augmentation.py` | Applies the same null-field rule during coverage augmentation. |
| `junitforge/prompts/repair.py` | Applies the same null-field rule during compile/runtime repair. |
| `tests/test_v7_generalized_fixtures.py` | New regressions for fluent/default-null repair, multiline `assertAll`, static utility delegation with recursive DTO graphs, private static helper propagation, validator/mapper/general classification, and deterministic fixture insertion. |
| `docs/recursive-schema-fixtures.md` | Documents generalized structured-input coverage and fluent null-mutator handling. |
| `RUN_COMMANDS.md` | Adds utility/validator execution examples and v7 upgrade notes. |
| `VALIDATION_REPORT.md` | Records the 37-test v7 validation and remaining environment limitation. |

No JavaParser Java source changed in v7. Upgrading from v6 does not require rebuilding `javaparser-cli.jar`.

## Targeted ServiceImpl primary-test generation update

This update is intentionally limited to initial ServiceImpl generation. It does not change compilation, execution, repair, retention, publication, coverage augmentation, or DTO/entity equality generation.

| File | Change |
|---|---|
| `junitforge/models.py` | Preserves every parent-interface type reference without shifting existing positional model fields. |
| `junitforge/parser/java_symbols.py` | Retains generic implements/extends declarations and all inherited interface parents in CLI, javalang, and fallback parsing. |
| `junitforge/stack/classifier.py` | Normalizes generic implemented-interface names for existing Service classification. |
| `junitforge/execution/models.py` | Adds Service contracts, selected primary paths, and per-method fixture projections. |
| `junitforge/execution/service_contracts.py` | Resolves inherited generic Service contracts and matches normalized override signatures; unresolved contracts remain explicit. |
| `junitforge/execution/analyzer.py` | Selects only matched Service entries, chooses one bounded successful path, includes reachable same-class helpers, and builds the class union before method projections. |
| `junitforge/execution/payload_schema.py` | Adds opt-in Service path pruning, collection cardinality facts, branch-compatible String witnesses, and nested getter literal extraction. |
| `junitforge/execution/fixture_builder_emitter.py` | Emits one projection-aware helper per schema with fresh mutable objects and method-specific scenario values. |
| `junitforge/execution/stub_planner.py` | Requires correct successful-path syntax for selected void collaborator calls. |
| `junitforge/execution/renderer.py` | Reports matched contracts, selected paths, limitations, and exact projected fixture calls. |
| `junitforge/prompts/generation.py` | Requests one primary Service test, path-local stubs, and only `assertNotNull(response)`. |
| `junitforge/execution/assembly.py` | Validates one ordinary test, one captured entry invocation, one allowed assertion, and type-correct required stubs. |
| `tests/test_schema_fixture_pipeline.py` | Updates the existing Service helper integration fixture with its declared Service contract. |
| `tests/test_serviceimpl_primary_generation.py` | Adds the 13 focused regressions required for this update, including Java compilation of projected fixtures. |

## v8 Validation, Repair, and Publication phase

This phase starts only after the complete ServiceImpl test class has been assembled. It does not change Service-interface discovery, primary successful-path selection, schema/fixture generation, DTO/entity equality generation, or coverage augmentation.

### Modified files

| File | Function-level change |
|---|---|
| `junitforge/models.py` | Adds stable generated-scope kinds/spans, per-test execution results, complete-class execution results, and the class-validation report payload on `TargetOutcome`. |
| `junitforge/execution/assembly.py` | Maps compiler/runtime locations to import, field, mock, setup, fixture, stub, test, and other declarations; extracts and applies complete scope replacements in one batch; rejects multi-scope output, test deletion, disabling, assertion weakening, CUT-invocation bypass, and Mockito leniency. |
| `junitforge/compile_gate.py` | Adds `check_complete_class()`: exact one-file javac compilation into `target/test-classes`, generated-file-specific Maven compiler evidence, Windows/space-safe Maven diagnostics, javac caret-column retention, and isolation from unrelated sibling compiler failures. |
| `junitforge/coverage_run.py` | Adds one-class direct Surefire execution, Surefire/Console XML parsing for every expected test, canonical method-name handling, assertion/runtime/Mockito evidence extraction with exact stub source, framework-frame filtering, missing/skipped-test detection, and no-coverage execution support. |
| `junitforge/backends/ant.py` | Adds the matching complete-class JUnit ConsoleLauncher execution surface and XML result parsing without changing Ant coverage measurement. |
| `junitforge/prompts/repair.py` | Adds scope-focused compilation and execution repair requests containing exact diagnostics, minimal generated dependencies, selected-path production evidence, and only matching verified collaborator/schema/configuration contracts; removes ServiceImpl test-deletion repair instructions. |
| `junitforge/loop.py` | Replaces ServiceImpl skeleton preflight and per-owner repair with the required complete-class compile/execute lifecycle, rejects incomplete primary-class assembly, groups repairs, enforces three-round ceilings and semantic no-progress detection, keeps execution-introduced compile correction in the same round, rejects unsafe Mockito repairs, hard-gates publication, and routes augmented ServiceImpl classes back through the same validator. Non-ServiceImpl paths retain their existing flow. |
| `junitforge/config.py` | Sets the execution-repair ceiling to three and exposes `--max-execution-repairs`; runtime clamps both repair phases to a maximum of three. |
| `junitforge/cli.py` | Creates the class execution backend even when coverage is disabled, reports `test_failed`, and preserves optional JaCoCo warm-up behavior. |
| `junitforge/report.py` | Emits one concise JSON and Markdown lifecycle report per processed generated class, records primary-test completeness and applied/rejected repairs, and counts class-level execution independently of JaCoCo measurement. |
| `tests/test_compile_error_parsing.py` | Adds Maven compiler parsing coverage for Windows paths containing spaces, exact line/column, and continuation details. |
| `CHANGE_MANIFEST.md` | Records this phase's exact changes. |
| `VALIDATION_REPORT.md` | Records the final commands, complete results, validation mapping, and known limitations. |

### Added files

| File | Purpose |
|---|---|
| `tests/test_class_validation_pipeline.py` | Contains 23 focused lifecycle regressions mapping one-to-one to the required validation behaviors, plus real Surefire XML evidence parsing checks. |

### Deleted files

None.

## v8.1 DTO/Entity Equals and HashCode phase

This phase replaces the generic DTO/entity equality template with one bounded,
source-backed strategy resolution pass. ServiceImpl generation, class-level
validation/repair/publication, and coverage augmentation remain unchanged.

### Modified files

| File | Function-level change |
|---|---|
| `junitforge/models.py` | Adds the compact equality strategy, assertion mode, participating-member, and descriptor models plus optional context/symbol attachment points. |
| `junitforge/parser/java_symbols.py` | Retains exact class/method annotation expressions and record components; resolves Object identity, Lombok, record, simple manual, and inherited equality; identifies participating fields, parent state, witnesses, JPA identifiers/relationships, and conservative limitations. |
| `junitforge/context.py` | Resolves equality only for existing `dto-pojo` and `entity-pojo` contexts, using the existing lazy symbol lookup and failing closed on analysis errors. |
| `junitforge/stack/classifier.py` | Routes Java records through the existing DTO POJO template so language-defined record equality is tested. |
| `junitforge/prompts/generation.py` | Replaces the unconditional three-object guidance with strategy-specific identity/value/conservative assertions, minimal equality-only fixture rules, exact participating members/witnesses, shared nested references, parent-state rules, and JPA identifier/business-key safeguards. |
| `junitforge/postprocess.py` | Applies descriptor-aware DTO/entity assertion guards: preserves identity inequality, rejects/removes unsupported independent pairs, requires value contracts for cross-instance hash equality, and removes unequal-object hash inequality assertions. Non-DTO/entity behavior retains the prior path. |
| `junitforge/loop.py` | Passes the existing contextual symbol resolver into DTO/entity equality analysis and records the resolved strategy/mode plus concise limitations in existing outcome notes. |
| `CHANGE_MANIFEST.md` | Records this phase's exact changes. |
| `VALIDATION_REPORT.md` | Records commands, results, regression mapping, and limitations for this phase. |

### Added files

| File | Purpose |
|---|---|
| `tests/test_equality_generation.py` | Contains 22 focused regressions covering the 17 required behaviors plus simple/incomplete manual equality and safe/unsafe included-method handling. |

### Deleted files

None.
