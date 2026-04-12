# ECOA Tools Backend API

Backend API for executing ECOA (European Component Oriented Architecture) tools.

## Overview

Flask-based REST API for executing ECOA tools on project files. Tools run in project directories, keeping generated files in place.

## Supported Tools

| Tool ID  | Command       | Description                                                      | Category        |
| -------- | ------------- | ---------------------------------------------------------------- | --------------- |
| `exvt`   | `ecoa-exvt`   | ECOA XML Validation Tool                                         | validation      |
| `csmgvt` | `ecoa-csmgvt` | CSM Generator & Verification Tool                                | testing         |
| `mscigt` | `ecoa-mscigt` | Module Skeletons & Container Interfaces Generator                | code_generation |
| `asctg`  | `ecoa-asctg`  | Application Software Components Test Generator                   | testing         |
| `ldp`    | `ecoa-ldp`    | Lightweight Development Platform code generation                 | code_generation |
| `make`   | `make`        | Configure and build an LDP-generated project with CMake + make  | build           |

## Installation

### Prerequisites

- Python 3.12+
- ECOA tools installed (see `as6-tools/`)

### Setup

```bash
# Enter nix development environment (if you have nix environment)
nix develop

# Or install dependencies manually
pip install -r requirements.txt

# Install ECOA tools from as6-tools
cd as6-tools
pip install -e ./ecoa-toolset
pip install -e ./ecoa-exvt
pip install -e ./ecoa-csmgvt
pip install -e ./ecoa-mscigt
pip install -e ./ecoa-asctg
pip install -e ./ecoa-ldp
cd ..
```

### Docker (Alternative)

Build and run using Docker CLI standalone:

```bash
# 1. Build Docker image
docker build -t ecoa-tools .

# 2. Create the shared workspace directory (on host)
mkdir -p ../workspace

# 3. Run container in background, mounting the shared workspace
# (On Linux/Mac)
docker run -d \
  -p 5000:5000 \
  -v "$(pwd)/../workspace:/workspace" \
  -e ECOA_PROJECTS_BASE_DIR=/workspace \
  --name ecoa-tools-service \
  ecoa-tools:latest

# (On Windows PowerShell)
docker run -d `
  -p 5000:5000 `
  -v "$((Resolve-Path "..\workspace").Path):/workspace" `
  -e ECOA_PROJECTS_BASE_DIR=/workspace `
  --name ecoa-tools-service `
  ecoa-tools:latest

# debug
docker run -it -p 5000:5000 -v "$(pwd)/../workspace:/workspace" -e ECOA_PROJECTS_BASE_DIR=/workspace ecoa-tools /bin/bash
```

**Configure `config.yaml` before starting:**

```yaml
projects_base_dir: "/path/to/your/ecoa-projects"
```

> **Important:** Set this to where your frontend exports project files.

**LDP Build Configuration:**

The `ldp` tool now only generates code. Use the separate `make` tool to run the LDP build (`cmake` + `make`). Configure default build settings in `config.yaml`:

```yaml
make:
  # ... existing configuration ...
  build:
    default_log_library: "log4cplus" # Default logging library
    timeout: 600 # Build timeout in seconds
    cmake_options: # Default CMake options
      - "-DLDP_LOG_USE=${log_library}"
    make_options: # Default make options
      - "-j"
```

Build parameters can be overridden via API request. See [Execute Tool in Project](#execute-tool-in-project) for details.

```bash
# Start server
python main.py
```

API available at `http://localhost:5000`

## API Endpoints

### List All Tools

```bash
GET /api/tools
```

### Get Tool Details

```bash
GET /api/tools/<tool_id>
```

### Execute Tool in Project

```bash
POST /api/tools/execute-project
Content-Type: application/json

{
  "project_name": "marx_brothers",
  "project_file": "marx_brothers.project.xml",
  "tool": "ldp"
}
```

**Parameters:**

| Parameter       | Type    | Required | Default             | Description                                                                                                                                                 |
| --------------- | ------- | -------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `project_name`  | string  | Yes      | -                   | Project directory name                                                                                                                                      |
| `project_file`  | string  | Yes      | -                   | Project XML file name                                                                                                                                       |
| `tool`          | string  | Yes      | `exvt`              | Tool ID to execute (`ldp`, `make`, `exvt`, `csmgvt`, `mscigt`, `asctg`)                                                                                     |
| `compile`       | boolean | No       | `null` (use config) | Whether to compile project after tool execution (for `csmgvt` only). `null`: use config default, `true`: always compile, `false`: never compile           |
| `log_library`   | string  | No       | `null` (use config) | Logging library for `make` build (`log4cplus`, `zlog`, `lttng`). Uses config default if not specified.                                                     |
| `cmake_options` | array   | No       | `null` (use config) | Additional CMake options for `make` build. Uses config default if not specified.                                                                            |
| `make_options`  | array   | No       | `null` (use config) | Additional make options for `make` build. Uses config default if not specified.                                                                             |
| `verbose`       | integer | No       | `3`                 | Verbosity level (0-4)                                                                                                                                       |
| `checker`       | string  | No       | `ecoa-exvt`         | Checker tool for validation                                                                                                                                 |
| `config_file`   | string  | No       | -                   | Config file name (required for `asctg` tool)                                                                                                                |
| `force`         | boolean | No       | `false`             | Force overwrite existing files (applies to `ldp`, `csmgvt`, `mscigt` tools). Adds `-f` flag to the tool command.                                            |

> **Note:** The `compile` parameter only applies to `csmgvt`. The `log_library`, `cmake_options`, and `make_options` parameters apply to `make`.
>
> **Default Behavior:** `ldp` only generates code. Run `tool: "make"` as the separate build step.

**Response (`tool: "make"`):**

```json
{
  "success": true,
  "tool": "make",
  "project_name": "marx_brothers",
  "project_path": "/path/to/ecoa-projects/marx_brothers",
  "project_file": "marx_brothers.project.xml",
  "generated_files": [],
  "message": "Tool make executed successfully",
  "stdout": "=== CMake Output ===\n...\n=== Make Output ===\n...",
  "stderr": "=== CMake Errors ===\n...\n=== Make Errors ===\n...",
  "return_code": 0,
  "compile_success": true,
  "compile_stdout": "=== CMake Output ===\n...\n=== Make Output ===\n...",
  "compile_stderr": "=== CMake Errors ===\n...\n=== Make Errors ===\n...",
  "compile_return_code": 0,
  "executable_files": ["app1", "app2"],
  "cmake_dir": "/path/to/ecoa-projects/marx_brothers/6-output",
  "build_dir": "/path/to/ecoa-projects/marx_brothers/6-output/build",
  "cmake_command": "cmake ...",
  "make_command": "make ..."
}
```

**Response (`tool: "ldp"` default behavior):**

```json
{
  "success": true,
  "tool": "ldp",
  "project_name": "marx_brothers",
  "project_path": "/path/to/ecoa-projects/marx_brothers",
  "project_file": "marx_brothers.project.xml",
  "generated_files": ["main.c", "platform.c", "CMakeLists.txt", ...],
  "message": "Tool ldp executed successfully",
  "stdout": "...",
  "stderr": "...",
  "return_code": 0
}
```

### Execute Full Pipeline

```bash
POST /api/generate
Content-Type: application/json

{
    "taskId": "task-uuid",
    "projectId": "project-uuid",
    "stepsDir": "/workspace/project-uuid/Steps",
    "outputDir": "/workspace/project-uuid/src",
    "callbackUrl": "http://sirius-web:8080/api/internal/tasks/task-uuid/status",
    "selectedPhases": ["EXVT", "MSCIGT_ASCTG", "CSMGVT", "LDP"],
    "continueOnError": false
}
```

This endpoint executes the full ECOA toolchain pipeline asynchronously:
1. Calls the Java backend (`SIRIUS_WEB_URL/api/edt/ecoa/export-to-disk/{projectId}`) to export the ECOA XML into the shared workspace.
2. In a background thread, sequentially executes the selected phases using local `ToolExecutor` (no HTTP overhead).
3. Posts progress updates and logs to `callbackUrl`.

**Required Environment Variables:**
- `SIRIUS_WEB_URL`: URL of the Java backend (e.g. `http://sirius-web-full:8080`)
- `ECOA_WORKSPACE`: Path to the shared workspace volume (e.g. `/workspace`)
- `ECOA_PROJECTS_BASE_DIR`: Should match `ECOA_WORKSPACE` for project resolution

### Health Check

```bash
GET /health
```

## Usage Examples

```bash
# Validate project
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "marx_brothers", "project_file": "marx_brothers.project.xml", "tool": "exvt"}'

# Generate module skeletons
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "marx_brothers", "project_file": "marx_brothers.project.xml", "tool": "mscigt"}'

# Generate module skeletons (force)
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "marx_brothers", "project_file": "marx_brothers.project.xml", "tool": "mscigt", "force": "true"}'

# csmgvt
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "marx_brothers", "project_file": "marx_brothers.project.xml", "tool": "csmgvt"}'

# Generate LDP platform only
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "marx_brothers", "project_file": "marx_brothers.project.xml", "tool": "ldp"}'

# Build generated LDP platform
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "marx_brothers", "project_file": "marx_brothers.project.xml", "tool": "make"}'
```
