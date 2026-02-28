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
| `ldp`    | `ecoa-ldp`    | Lightweight Development Platform                  | code_generation |

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
  "tool": "exvt",
  "verbose": 3
}
```

**Response:**

```json
{
  "success": true,
  "tool": "exvt",
  "project_name": "marx_brothers",
  "project_path": "/path/to/ecoa-projects/marx_brothers",
  "generated_files": [...],
  "message": "Tool exvt executed successfully",
  "return_code": 0
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
```
