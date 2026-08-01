# Recursive Schema Discovery and Reusable Fixtures

## Purpose

This implementation moves Java payload discovery out of the LLM and into the deterministic `llm-junit-assist` engine. For a selected Controller or `ServiceImpl`, the engine now builds one class-level schema catalog, generates reusable fixture factories, compiler-validates the fixture skeleton, and supplies each method-generation request only the fixture subset it can safely reuse.

## Previous failure mode

The earlier flow projected only selected per-method property paths and then asked the LLM to construct objects inside individual tests. This frequently produced shallow collaborator responses:

```java
GetMotorPremiumResponse response = new GetMotorPremiumResponse();
GetMotorPremiumResponseData data = new GetMotorPremiumResponseData();
response.setMotorPremium(data);
```

The object compiled but deeper production dereferences still failed at runtime. The repository also contained a fixture emitter, but it was disconnected from the production path and expected property fields that the actual execution model did not contain.

## New execution flow

```text
Selected source class
    -> JavaParser/symbol extraction
    -> complete schema-root discovery
    -> context-aware type resolution
    -> class-level recursive schema catalog
    -> dynamic map schema catalog
    -> dereference/cardinality/value constraints
    -> construction planning
    -> deterministic fixture catalog
    -> fixture-enabled test skeleton
    -> compiler preflight
    -> per-method fixture projection
    -> LLM test-method generation
    -> final compile/run/coverage gates
```

## Schema roots

The analyzer now collects roots from the selected class and its reachable helper methods, including:

- method parameters and return types;
- local variable declared types;
- object-creation and cast target types;
- collaborator parameter and return types;
- application payload fields on the selected class;
- generic element/value types and wrapper payloads.

Infrastructure types are filtered out. Repository source types are resolved lazily and cached.

## Recursive expansion

The default configuration is:

```text
JUNITFORGE_SCHEMA_MAX_OBJECT_LEVELS=5
JUNITFORGE_PAYLOAD_MAX_TYPES=80
```

The root is object level 1. Collection and wrapper nodes do not consume an object level. For example:

```text
PolicyDto                 level 1
  PersonDto               level 2
    List<AddressDto>
      AddressDto          level 3
        List<ContactDto>
          ContactDto      level 4
```

The catalog contains all verified fields for each resolved schema, not only the fields observed in one method. Schemas discovered by different methods are merged field by field by canonical schema identity.

## Type resolution

`LazySymbolResolver` now resolves using source context:

1. exact fully qualified name;
2. enclosing/nested type;
3. explicit import;
4. same package;
5. wildcard import;
6. unique repository-wide simple name;
7. unresolved/ambiguous result rather than guessing.

Nested classes are recursively indexed. Duplicate simple names do not use first-file selection. Fixture code uses fully qualified names where two catalog schemas have the same simple name, and the skeleton suppresses illegal duplicate imports.

## Inheritance and Lombok

The schema catalog traverses application superclass chains and includes inherited fields with their declaring type. Construction planning considers explicit and inferred access mechanisms:

1. verified no-argument construction plus setters/public fields;
2. verified builder or `@Builder`/`@SuperBuilder`;
3. one verified complete constructor;
4. record canonical construction;
5. unsupported/reduced-support reporting.

Lombok accessors are inferred only from actual annotations. Large all-arguments constructors are not selected when a safe no-argument/setter or builder path exists.

Non-static inner classes are reported as unsupported by deterministic construction because they require an enclosing instance. Static nested classes remain supported.

## Cycle handling

Traversal uses path-based cycle detection. The global catalog contains each schema once, but recursive expansion stops when the same type reappears in the current path. Fixture emission omits an unsafe back-reference and reports it rather than recursively calling fixture methods forever.

Required two-stage entity back-reference construction is not inferred automatically; such cases remain visible as reduced-support diagnostics unless the source provides a deterministic construction path.

## Collection and wrapper requirements

The analyzer recognizes indexed and wrapper navigation, including:

- `list.get(0)` and `list.get(1)`;
- `stream().findFirst()`;
- no-argument wrapper `get()`;
- arrays, lists, sets, pages, optionals, response wrappers and supported generic wrappers.

Collection requirements record minimum cardinality and accessed indexes. The emitter repeats fresh element-factory calls to satisfy the required size. For example, a `get(1)` path causes at least two fixture elements.

Literal equality checks and common date parsers can refine baseline values. Examples include:

- `"RIDER1".equals(rider.getCode())`;
- `LocalDate.parse(dto.getStartDate())`;
- `new SimpleDateFormat("dd/MM/yyyy").parse(dto.getDob())`.

This source-derived overlay changes values only; the schema structure remains repository-derived.

## Dynamic `Map<String, Object>` schemas

Dynamic maps use a dedicated catalog. Literal key reads are captured without pretending that map keys are Java fields. The analyzer records:

- map variable and semantic schema identity;
- exact key spelling;
- cast/assignment-derived runtime value type;
- nested map relationships;
- list-of-map relationships where deterministically derivable;
- methods using each entry.

Generated helpers use mutable `LinkedHashMap` and `ArrayList` instances. Production spelling is preserved exactly, including misspelled keys.

Current map analysis is deliberately bounded to source-verifiable literal keys. Computed keys and highly indirect aliases are reported as unresolved rather than invented.

## Construction and baseline values

Fixture methods return a fresh object graph on every call. Deterministic values include:

- fixed scalar strings selected by field semantics;
- `BigDecimal` and `BigInteger` values with correct runtime types;
- fixed UUID values;
- fixed Java temporal values;
- resolved enum constants;
- mutable collections and maps;
- present optionals/wrappers when a nested value is constructible.

The emitter never calls `new Type()` unless construction planning verified that path. Unsupported schemas remain in the fixture report with diagnostics.

## Skeleton integration

`build_test_skeleton()` now inserts supported parser-derived fixture methods before the class closing brace. The execution context retains structured `FixtureSpec` records containing:

- fixture ID;
- method name;
- return type;
- dependent fixture IDs;
- required imports;
- Java method source;
- support state and diagnostics.

Before any per-method LLM call, the engine writes the fixture-enabled skeleton to the target test path and invokes the existing compile gate. A failed fixture skeleton stops method generation so one invalid helper cannot contaminate every generated test.

## Method-level reuse

Each method context receives:

- required schema IDs;
- required fixture IDs;
- dynamic map schema IDs;
- dereference paths;
- collection requirements.

The LLM prompt lists only verified fixture signatures and requires the generated test to:

- call those existing helpers;
- avoid reconstructing the same object graph inline;
- mutate only the smallest branch-specific path;
- keep Mockito stubbing local to the applicable test;
- never call an unlisted fixture helper;
- never invent repository symbols.

The former unconditional assertion that fixture builders already existed has been removed. That instruction is emitted only when a verified fixture is actually available.

## Reports

For each selected class, the engine writes:

```text
<repo>/reports/execution-context/<fqcn>.json
<repo>/reports/schema-catalog/<fqcn>.json
<repo>/reports/fixture-catalog/<fqcn>.json
```

The schema report includes the class catalog, dynamic map catalog, method usage, cardinality requirements and diagnostics. The fixture report includes Java helper source, dependencies, method reuse and unsupported fixture records.

## Performance

The implementation preserves lazy source resolution and method-wise LLM generation. It parses referenced source files on demand, caches physical files, builds the class catalog once, and sends each method only its required fixture subset.

Timing events now include schema-catalog construction, fixture-catalog generation and fixture-skeleton compilation in addition to the existing class/method generation events.

## Known limitations

The following remain explicitly bounded:

- computed/non-literal dynamic map keys;
- complex data flow through aliases not represented by existing parser facts;
- automatic discovery of concrete implementations for arbitrary interfaces/abstract payload types;
- non-static inner-class construction;
- mandatory bidirectional back-references requiring domain-specific two-stage linking;
- arbitrary custom parser/formatter constraints beyond the implemented common patterns;
- full predicate-to-element data-flow for every possible stream/lambda form;
- separate LLM-based fixture repair. Fixture preflight currently fails fast and reports compiler diagnostics; the existing final-class repair pipeline remains unchanged.

These cases are reported as unresolved or reduced support. They are not silently treated as covered.

## Commands

### Compile Python

```bash
python -m compileall -q -f junitforge
```

### Run the included focused tests

```bash
python -m unittest discover -s tests -v
```

### Build the JavaParser CLI

From the project root:

```bash
cd tools/javaparser-cli
mvn clean package
cd ../..
```

The expected executable is:

```text
tools/javaparser-cli/target/javaparser-cli.jar
```

### Dry-run one class

```bash
python -m junitforge \
  --repo-path <target-repository> \
  --only MotorServiceImpl.java \
  --limit 1 \
  --dry-run \
  --verbose
```

### Generate one ServiceImpl without coverage

```bash
python -m junitforge \
  --repo-path <target-repository> \
  --only MotorServiceImpl.java \
  --limit 1 \
  --overwrite \
  --no-coverage \
  --verbose
```

### Generate with coverage

```bash
python -m junitforge \
  --repo-path <target-repository> \
  --only MotorServiceImpl.java \
  --limit 1 \
  --overwrite \
  --coverage \
  --verbose
```

Watsonx credentials and model configuration must be present in the environment or target repository `.env`, as required by the existing engine.

## TravelServiceImpl production-run corrections (v3)

A real `TravelServiceImpl` run exposed gaps that synthetic fixture compilation did not cover:

- `getQuotation(String,String)` was parsed but excluded from the public entry's reachable execution closure because the graph followed private methods only. The graph now follows every concrete same-class method invoked by the selected public entry, including package-visible/protected/public siblings, so collaborator calls inside those methods are available to the test prompt.
- Method prompts previously rendered the complete class fixture catalog and complete repeated AST fact sets. They now list only direct test fixture roots (public inputs, collaborator return roots and externally supplied root maps) and use a compact method execution contract. The full catalog remains in JSON reports and the compiled skeleton.
- Dynamic map list entries now link to the lambda element map fixture. Helper parameter map shapes are propagated back to caller aliases, so `giAddresses`, `giContacts`, `children`, `companion` and similar lists can receive populated child maps instead of empty lists.
- Reachable `SimpleDateFormat` patterns refine dynamic-map date strings, including `dd/MM/yyyy` baselines.
- An explicit Java `= null` initializer no longer overrides a verified constructible nested fixture.
- Initial method responses rejected before compilation now receive one bounded structural repair attempt. This specifically repairs illegal `doReturn(...).when(cut)` / `when(cut...)` / `verify(cut...)` usage by using exact collaborators from the reachable same-class execution path.

The validator still rejects Mockito stubbing or verification of the CUT. The correction is to provide and use the legal collaborator path, not to weaken that rule.

## Same-class helper execution closure

A public entry method may delegate to another concrete method declared on the same class regardless of that method's visibility. Calls such as:

```java
Map<?, ?> quotation = getQuotation(quotationNo, transactionType);
```

are treated as real same-class execution edges. The helper itself is never mocked or spied. Its collaborator invocations are merged into the entry method's execution contract, so tests must stub calls such as `aesEncUtils.encode(...)` and `iQuotationClient.getQuotationByRefNoTransType(...)` while allowing `getQuotation(...)` to execute normally.

JavaParser may classify bare calls as `this`, `implicit`, `unscoped`, or with no receiver kind. The analyzer accepts these forms when the name and arity resolve to a concrete method declared on the selected class, and supplements missing CLI edges from the exact method source.

## Writable dynamic-map branch mutations

Generated map fixtures return `Map<String, Object>`. A wildcard map such as `Map<?, ?>` may be read but cannot be mutated because Java captures unknown key and value types.

Incorrect:

```java
Map<?, ?> packageTypeMap = (Map<?, ?>) quotationMap.get("packageType");
packageTypeMap.put("code", "F");
```

Correct:

```java
@SuppressWarnings("unchecked")
Map<String, Object> packageTypeMap =
        (Map<String, Object>) quotationMap.get("packageType");
packageTypeMap.put("code", "F");
```

The method-generation flow deterministically normalizes mutated wildcard-map locals to `Map<String, Object>` before structural validation. The validator still rejects any wildcard mutation that remains, and the bounded structural/compile repair prompts retain the same rule as secondary safeguards. This prevents the known capture error from producing a `.failing` file in the normal path.

## Public helper declaration matching and stub enforcement

Same-class helper eligibility is based on the resolved source declaration's method name and arity, not exact rendered return-type text. This avoids false exclusions caused by harmless generic formatting differences such as `Map<?,?>` versus `Map<?, ?>`.

Before the LLM request, the engine emits `execution.public_method.call_graph` with:

- every reachable same-class method;
- every merged collaborator invocation, including the caller method that owns it.

After generation, the method-suite validator requires source-proven unconditional non-void collaborator calls to be stubbed somewhere in the returned suite. Missing helper-path stubs trigger bounded structural repair rather than allowing null/default Mockito returns into compilation or runtime.

## Narrow primitive setter values and Windows compiler output

Fixture values are method arguments, not assignment initializers. Java therefore requires explicit casts for deterministic `byte` and `short` setter values:

```java
entity.setByteField((byte) 1);
entity.setShortField((short) 1);
```

The compile gate also supports Windows drive-letter javac paths and preserves bounded raw compiler output whenever structured parsing cannot recognize a diagnostic. A failed fixture preflight must never be reported with an empty error message.

## v7: generalized structured-input fixtures

Recursive object-graph generation is no longer enabled by the `ServiceImpl` stereotype alone. The execution-context classifier now activates for:

- controllers;
- ServiceImpl/business services;
- validators;
- utility/helper classes, including public static entry methods;
- mappers/converters;
- any other concrete class with a source-declared public method accepting a structured parameter.

DTOs and entities remain the schema nodes used to build those object graphs. Their own POJO test style remains separate, so this change does not force ServiceImpl-style Mockito generation onto DTO/entity classes.

For class-wide utility, validator, mapper, and structured-unit generation, verified fixture methods are inserted deterministically into the generated Java class before finalization and compilation. This prevents a test from calling `validRequest()` when no such helper exists.

A utility method that only delegates its DTO/entity parameter to another static utility still receives a recursive fixture rooted at that public parameter. The root parameter type is sufficient to seed the schema catalog even when the delegating method itself performs no getter dereferences.

## v7: safe null-field assertions

The postprocessor now recognizes both forms:

```java
value.setPolicyPlanType(null);
assertNull(value.getPolicyPlanType());
```

```java
value.policyPlanType(null);
assertNull(value.getPolicyPlanType());
```

The second form is a fluent mutator commonly generated by OpenAPI-style models. Deterministic repair supports direct assertions, assertions inside `assertAll`, multiline assertions, and assertions with a message argument. A null write is inserted only when the exact one-argument field mutator is verified from the class symbol.
