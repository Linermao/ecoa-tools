# ECOA Tools Backend API

Backend API for executing ECOA (European Component Oriented Architecture) tools.

## Overview

Flask-based REST API for executing ECOA tools on project files. Tools run in project directories, keeping generated files in place.

## Supported Tools

| Tool ID  | Command       | Description                                       | Category        |
| -------- | ------------- | ------------------------------------------------- | --------------- |
| `exvt`   | `ecoa-exvt`   | ECOA XML Validation Tool                          | validation      |
| `csmgvt` | `ecoa-csmgvt` | CSM Generator & Verification Tool                 | testing         |
| `mscigt` | `ecoa-mscigt` | Module Skeletons & Container Interfaces Generator | code_generation |
| `asctg`  | `ecoa-asctg`  | Application Software Components Test Generator    | testing         |
| `ldp`    | `ecoa-ldp`    | Lightweight Development Platform (with auto-compilation support) | code_generation |

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

**Configure `config.yaml` before starting:**

```yaml
projects_base_dir: "/path/to/your/ecoa-projects"
```

> **Important:** Set this to where your frontend exports project files.

**LDP Compilation Configuration:**

The `ldp` tool supports automatic compilation after code generation. Configure compilation settings in `config.yaml`:

```yaml
ldp:
  # ... existing configuration ...
  compile:
    enabled: false                     # Enable compilation by default
    default_log_library: "log4cplus"   # Default logging library
    timeout: 600                       # Compilation timeout in seconds
    cmake_options:                     # Default CMake options
      - "-DLDP_LOG_USE=${log_library}"
    make_options:                      # Default make options
      - "-j"
```

Compilation parameters can be overridden via API request. See [Execute Tool in Project](#execute-tool-in-project) for details.

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
  "tool": "ldp",
  "compile": false,
  "log_library": "log4cplus",
  "cmake_options": ["-DLDP_LOG_USE=log4cplus"],
  "verbose": 3,
  "checker": "ecoa-exvt"
}
```

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `project_name` | string | Yes | - | Project directory name |
| `project_file` | string | Yes | - | Project XML file name |
| `tool` | string | Yes | `exvt` | Tool ID to execute (`ldp`, `exvt`, `csmgvt`, `mscigt`, `asctg`) |
| `compile` | boolean | No | `false` | Whether to compile project after tool execution (for `ldp` tool only) |
| `log_library` | string | No | `log4cplus` | Logging library for compilation (`log4cplus`, `zlog`, `lttng`) |
| `cmake_options` | array | No | `[]` | Additional CMake options for compilation |
| `verbose` | integer | No | `3` | Verbosity level (0-4) |
| `checker` | string | No | `ecoa-exvt` | Checker tool for validation |
| `config_file` | string | No | - | Config file name (required for `asctg` tool) |

> **Note:** The `compile`, `log_library`, and `cmake_options` parameters only apply to the `ldp` tool. If specified for other tools, they will be ignored.

**Response (without compilation):**

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

**Response (with compilation `compile: true`):**

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
  "return_code": 0,
  "compile_success": true,
  "compile_stdout": "=== CMake Output ===\n...\n=== Make Output ===\n...",
  "compile_stderr": "=== CMake Errors ===\n...\n=== Make Errors ===\n...",
  "compile_return_code": 0,
  "executable_files": ["app1", "app2"],
  "cmake_dir": "/path/to/ecoa-projects/marx_brothers/6-output",
  "build_dir": "/path/to/ecoa-projects/marx_brothers/6-output/build"
}
```

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
  -d '{"project_name": "my_project", "project_file": "project.xml", "tool": "mscigt"}'

# Generate LDP platform without compilation (default behavior)
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "my_project", "project_file": "project.xml", "tool": "ldp", "checker": "ecoa-exvt"}'

# Generate LDP platform with compilation using log4cplus
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "my_project", "project_file": "project.xml", "tool": "ldp", "compile": true, "log_library": "log4cplus", "cmake_options": ["-DLDP_LOG_USE=log4cplus"], "checker": "ecoa-exvt"}'

# Generate LDP platform with compilation using zlog library
curl -X POST http://localhost:5000/api/tools/execute-project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "my_project", "project_file": "project.xml", "tool": "ldp", "compile": true, "log_library": "zlog", "cmake_options": ["-DLDP_LOG_USE=zlog", "-DCMAKE_BUILD_TYPE=Release"], "checker": "ecoa-exvt"}'
```
