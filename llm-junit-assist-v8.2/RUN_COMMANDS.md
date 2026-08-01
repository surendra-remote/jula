# Run Commands

Run these commands from the extracted `llm-junit-assist` project directory unless stated otherwise.

## 1. Create and activate a virtual environment

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Windows Command Prompt

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 2. Install Python dependencies

The supplied project does not contain a dependency lock file. Install the third-party packages imported by the existing engine:

```bash
python -m pip install --upgrade pip
python -m pip install python-dotenv javalang lxml requests urllib3 PyYAML
```

Use the organization's approved package versions or internal mirror where dependency pinning is mandatory.

## 3. Compile Python

```bash
python -m compileall -q -f junitforge
```

## 4. Run the focused schema/fixture tests

```bash
python -m unittest discover -s tests -v
```

## 5. Build JavaParser CLI

Maven and JDK 17 or later are required.

### Windows / STS terminal

```bat
cd tools\javaparser-cli
mvn clean package
cd ..\..
```

### Linux/macOS

```bash
cd tools/javaparser-cli
mvn clean package
cd ../..
```

Expected output:

```text
tools/javaparser-cli/target/javaparser-cli.jar
```

## 6. Configure Watsonx

Provide the environment variables already required by the existing Watsonx client. The engine loads the process environment and `<target-repository>/.env`.

Do not commit secrets to source control.

## 7. Dry-run one ServiceImpl

```bash
python -m junitforge \
  --repo-path <target-repository> \
  --only MotorServiceImpl.java \
  --limit 1 \
  --dry-run \
  --verbose
```

Windows Command Prompt equivalent:

```bat
python -m junitforge --repo-path <target-repository> --only MotorServiceImpl.java --limit 1 --dry-run --verbose
```

## 8. Generate one ServiceImpl without coverage

```bash
python -m junitforge \
  --repo-path <target-repository> \
  --only MotorServiceImpl.java \
  --limit 1 \
  --overwrite \
  --no-coverage \
  --verbose
```

Windows Command Prompt:

```bat
python -m junitforge --repo-path <target-repository> --only MotorServiceImpl.java --limit 1 --overwrite --no-coverage --verbose
```

## 9. Generate one ServiceImpl with coverage

```bash
python -m junitforge \
  --repo-path <target-repository> \
  --only MotorServiceImpl.java \
  --limit 1 \
  --overwrite \
  --coverage \
  --verbose
```

Windows Command Prompt:

```bat
python -m junitforge --repo-path <target-repository> --only MotorServiceImpl.java --limit 1 --overwrite --coverage --verbose
```

## 10. Optional schema configuration

### Windows Command Prompt

```bat
set JUNITFORGE_SCHEMA_MAX_OBJECT_LEVELS=5
set JUNITFORGE_PAYLOAD_MAX_TYPES=80
set JUNITFORGE_METHOD_WORKERS=3
```

### PowerShell

```powershell
$env:JUNITFORGE_SCHEMA_MAX_OBJECT_LEVELS = "5"
$env:JUNITFORGE_PAYLOAD_MAX_TYPES = "80"
$env:JUNITFORGE_METHOD_WORKERS = "3"
```

### Linux/macOS

```bash
export JUNITFORGE_SCHEMA_MAX_OBJECT_LEVELS=5
export JUNITFORGE_PAYLOAD_MAX_TYPES=80
export JUNITFORGE_METHOD_WORKERS=3
```

## 11. Inspect generated catalogs

After the engine analyzes a class, inspect:

```text
<target-repository>/reports/execution-context/<fqcn>.json
<target-repository>/reports/schema-catalog/<fqcn>.json
<target-repository>/reports/fixture-catalog/<fqcn>.json
```

These reports show recursive fields, dynamic map keys, construction strategy, fixture Java source, method reuse, cardinality and unresolved/reduced-support diagnostics.

## 12. Generate a utility or validator with structured parameters

```bat
python -m junitforge --repo-path <target-repository> --only PolicyValidationUtils.java --limit 1 --overwrite --no-coverage --verbose
```

```bat
python -m junitforge --repo-path <target-repository> --only RequestValidator.java --limit 1 --overwrite --no-coverage --verbose
```

Expected v7 reports:

```text
reports/execution-context/<fqcn>.json
reports/schema-catalog/<fqcn>.json
reports/fixture-catalog/<fqcn>.json
```

For utility classes, public static methods with DTO/entity parameters are now included. Their recursive fixture helpers are inserted into the class-wide generated test before compilation.

## v7 upgrade note

No JavaParser Java source changed relative to v6. Copying the v7 Python files over v6 does not require rebuilding `tools/javaparser-cli/target/javaparser-cli.jar`.
