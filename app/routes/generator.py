"""
Generator pipeline route for phase-aware ECOA toolchain execution.

POST /api/generate:
1. Optionally asks the Java backend to export ECOA XML into the shared workspace.
2. Runs the selected ECOA phases.
3. Reports progress back to the Java callback URL.
"""

import os
import threading
from pathlib import Path
from typing import Optional

import requests
from flask import Blueprint, jsonify, request

from app.services.asctg_service import build_asctg_logs, execute_asctg_from_steps_dir
from app.services.generation_workflow import (
    WorkflowContext,
    _normalize_workflow_mode,
    activate_harness_project,
    default_selected_phases,
    parse_continuing_flag,
    resolve_phase_steps,
    should_await_code,
    validate_phase_selection,
)
from app.services.executor import ToolExecutor
from app.services.code_backflow import (
    scan_backflow_files,
    generate_patch,
    apply_patch,
)
from app.utils.logger import setup_logger

bp = Blueprint("generator", __name__)
logger = setup_logger("app.routes.generator")

SIRIUS_WEB_URL = os.environ.get("SIRIUS_WEB_URL", "http://localhost:8080")
WORKSPACE_ROOT = Path(os.environ.get("ECOA_WORKSPACE", "/workspace"))

logger.info("[Generator] sirius-web: %s", SIRIUS_WEB_URL)
logger.info("[Generator] workspace: %s", WORKSPACE_ROOT)

PHASE_STEPS = [
    dict(phaseId="EXVT", toolId="exvt", subStatus="RUNNING_EXVT", label="[EXVT] XML Validation", pStart=0, pEnd=20, needsCfg=False),
    dict(phaseId="MSCIGT", toolId="mscigt", subStatus="RUNNING_MSCIGT", label="[MSCIGT] Skeleton Generator", pStart=20, pEnd=40, needsCfg=False),
    dict(phaseId="ASCTG", toolId="asctg", subStatus="RUNNING_ASCTG", label="[ASCTG] Test Generator", pStart=40, pEnd=60, needsCfg=True),
    dict(phaseId="CSMGVT", toolId="csmgvt", subStatus="RUNNING_CSMGVT", label="[CSMGVT] Cork/Stub Gen", pStart=60, pEnd=80, needsCfg=False),
    dict(phaseId="LDP", toolId="ldp", subStatus="RUNNING_LDP", label="[LDP] Middleware Builder", pStart=80, pEnd=100, needsCfg=False),
]
PHASE_STEP_LOOKUP = {step["phaseId"]: step for step in PHASE_STEPS}


def _resolve_pipeline_steps(selected_phases: list[str], workflow_mode: str | None, continuing: bool) -> list[dict]:
    """Return the concrete phase plan for this request with monotonically increasing progress."""
    ordered_phase_ids = selected_phases
    if workflow_mode is not None:
        ordered_phase_ids = resolve_phase_steps(workflow_mode, selected_phases, continuing)

    if not ordered_phase_ids:
        return []

    total_steps = len(ordered_phase_ids)
    base_progress = 5
    available_progress = 95
    previous_end = base_progress
    resolved_steps: list[dict] = []

    for index, phase_id in enumerate(ordered_phase_ids):
        template = PHASE_STEP_LOOKUP[phase_id]
        if index == total_steps - 1:
            progress_end = 100
        else:
            progress_end = base_progress + (available_progress * (index + 1)) // total_steps

        step = dict(template)
        step["pStart"] = previous_end
        step["pEnd"] = progress_end
        resolved_steps.append(step)
        previous_end = progress_end

    return resolved_steps


def _workflow_callback_fields(context: WorkflowContext) -> dict:
    return {
        "workflowMode": context.workflow_mode,
        "baseProjectFile": context.base_project_file,
        "activeProjectFile": context.active_project_file,
        "harnessProjectFile": context.harness_project_file,
    }


def _callback_payload(payload: dict, context: WorkflowContext) -> dict:
    return {**payload, **_workflow_callback_fields(context)}


def _send_callback(callback_url: str, payload: dict, task_id: str) -> None:
    """POST a progress/status payload to the Java backend."""
    try:
        resp = requests.post(callback_url, json=payload, timeout=10)
        logger.debug(
            "[CB] task=%s status=%s progress=%s%% -> HTTP %s",
            task_id,
            payload.get("status"),
            payload.get("progress"),
            resp.status_code,
        )
    except Exception as exc:  # pragma: no cover - best effort logging
        logger.error("[CB ERROR] task=%s: %s", task_id, exc)


def _send_callback_if_present(callback_url: Optional[str], payload: dict, task_id: str) -> None:
    """Send callback only when a callback URL is provided."""
    if callback_url:
        _send_callback(callback_url, payload, task_id)


def _export_to_disk(project_id: str, workspace_id: str) -> tuple[bool, str, str, str]:
    """
    Ask the Java backend to export ECOA XML into the shared workspace.

    Returns (success, project_name, project_file, error_message).
    """
    url = f"{SIRIUS_WEB_URL}/api/edt/ecoa/export-to-disk/{project_id}?workspaceId={workspace_id}"
    try:
        resp = requests.post(url, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            return True, data["projectName"], data["projectFile"], ""
        return False, "", "", f"HTTP {resp.status_code}: {resp.text}"
    except Exception as exc:  # pragma: no cover - network failure path
        return False, "", "", f"Connection error: {exc}"


def _find_config_file(project_id: str, workspace_id: str) -> Optional[str]:
    """Locate an ASCTG config XML inside the project Steps directory."""
    steps_dir = WORKSPACE_ROOT / project_id / workspace_id / "Steps"
    if not steps_dir.exists():
        steps_dir = WORKSPACE_ROOT / project_id / "Steps"

    for pattern in ["*.config.xml", "*config*.xml"]:
        matches = list(steps_dir.rglob(pattern))
        if matches:
            try:
                return str(matches[0].relative_to(steps_dir))
            except ValueError:
                return None
    return None


def _resolve_project_file(project_id: str, workspace_id: str, skip_export: bool, callback_url: str, task_id: str) -> tuple[Optional[str], Optional[str], Optional[Path]]:
    """
    Prepare the Steps workspace and return (project_name, project_file, steps_root).
    """
    steps_root = WORKSPACE_ROOT / project_id / workspace_id / "Steps"

    if skip_export:
        project_candidates = sorted(steps_root.glob("*.project.xml"))
        if not project_candidates:
            _send_callback(
                callback_url,
                {
                    "status": "FAILED",
                    "subStatus": "NONE",
                    "progress": 0,
                    "logs": [
                        "[EXPORT][ERROR] Existing workspace does not contain an exported ECOA project XML file.",
                        f"[EXPORT][ERROR] Expected under: {steps_root}",
                    ],
                },
                task_id,
            )
            return None, None, None

        project_file = project_candidates[0].name
        _send_callback(
            callback_url,
            {
                "status": "GENERATING",
                "subStatus": "NONE",
                "progress": 5,
                "logs": [
                    "[EXPORT][WARN] Reusing the existing exported workspace to preserve business code edits.",
                    f"[EXPORT][INFO] Reused project file: {steps_root / project_file}",
                ],
            },
            task_id,
        )
        return project_id, project_file, steps_root

    _send_callback(
        callback_url,
        {
            "status": "EXPORTING_XML",
            "subStatus": "NONE",
            "progress": 0,
            "logs": ["[EXPORT][INFO] Exporting ECOA XML from EDT into the shared workspace..."],
        },
        task_id,
    )

    export_ok, project_name, project_file, export_err = _export_to_disk(project_id, workspace_id)
    if not export_ok:
        _send_callback(
            callback_url,
            {
                "status": "FAILED",
                "subStatus": "NONE",
                "progress": 0,
                "logs": [
                    f"[EXPORT][ERROR] Failed to export ECOA XML: {export_err}",
                    "[EXPORT][ERROR] Please check whether the sirius-web backend is reachable.",
                ],
            },
            task_id,
        )
        return None, None, None

    _send_callback(
        callback_url,
        {
            "status": "GENERATING",
            "subStatus": "NONE",
            "progress": 5,
            "logs": [
                f"[EXPORT][SUCCESS] Exported project descriptor: {project_name}/{project_file}",
                f"[EXPORT][INFO] Workspace path: {steps_root / project_file}",
            ],
        },
        task_id,
    )
    return project_name, project_file, steps_root


def _classify_line(line: str) -> str:
    """Classify a log line into INFO, WARN, or ERROR based on content."""
    lower = line.lower()
    if any(kw in lower for kw in ("error", "fatal", "failed", "failure", "undefined reference", "no such file")):
        return "ERROR"
    if any(kw in lower for kw in ("warning", "warn", "deprecated")):
        return "WARN"
    return "INFO"


def _summarize_compile_logs(tool_id: str, compile_stdout: str, compile_stderr: str) -> list[str]:
    """Extract key summary lines from cmake/make output instead of forwarding raw output.

    Returns log lines in [PHASE][COMPILE][LEVEL] format with:
    - CMake configuration summary
    - Make build summary (built targets)
    - Error lines from both cmake and make stderr
    - Truncated if too many error lines (>20)
    """
    phase = tool_id.upper()
    summary: list[str] = []
    max_errors = 20

    # --- CMake section ---
    cmake_key_lines = []
    cmake_done = False
    for line in compile_stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in (
            "-- Configuring done",
            "-- Generating done",
            "-- Build files have been written",
            "-- The CXX compiler identification",
            "-- Check for working CXX compiler",
        )):
            cmake_key_lines.append(stripped)
        if stripped.startswith("-- Configuring done"):
            cmake_done = True

    if cmake_key_lines or compile_stderr:
        summary.append(f"[{phase}][COMPILE][INFO] === CMake Configuration ===")
        for kl in cmake_key_lines:
            summary.append(f"[{phase}][COMPILE][INFO] {kl}")
        if not cmake_done and compile_stderr:
            summary.append(f"[{phase}][COMPILE][ERROR] CMake configuration failed")

    # --- Make section ---
    built_targets = []
    for line in compile_stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Built target") or stripped.startswith("Linking"):
            built_targets.append(stripped)
        # Skip the CMake section already handled above
        if stripped.startswith("--"):
            continue

    if built_targets:
        summary.append(f"[{phase}][COMPILE][INFO] === Make Build ===")
        for bt in built_targets:
            summary.append(f"[{phase}][COMPILE][INFO] {bt}")

    # --- Error lines from stderr ---
    error_lines = []
    for line in compile_stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        level = _classify_line(stripped)
        if level == "ERROR":
            error_lines.append(stripped)

    if error_lines:
        summary.append(f"[{phase}][COMPILE][INFO] === Build Errors ===")
        for el in error_lines[:max_errors]:
            summary.append(f"[{phase}][COMPILE][ERROR] {el}")
        remaining = len(error_lines) - max_errors
        if remaining > 0:
            summary.append(f"[{phase}][COMPILE][WARN] ... and {remaining} more errors (truncated)")

    # If nothing was extracted but there was output, add a minimal note
    if not summary and (compile_stdout.strip() or compile_stderr.strip()):
        summary.append(f"[{phase}][COMPILE][INFO] Build output produced (see backend logs for details)")

    return summary


def _build_tool_logs(tool_id: str, result: dict) -> list[str]:
    """Normalize stdout/stderr/compile logs into callback log lines with [PHASE][LEVEL] format."""
    phase = tool_id.upper()
    tool_logs: list[str] = []

    # Tool stdout
    for line in (result.get("stdout") or "").splitlines():
        if line.strip():
            level = _classify_line(line)
            tool_logs.append(f"[{phase}][{level}] {line}")

    # Tool stderr
    for line in (result.get("stderr") or "").splitlines():
        if line.strip():
            level = _classify_line(line)
            tool_logs.append(f"[{phase}][{level}] {line}")

    # Compile logs — use summary instead of raw output
    compile_stdout = result.get("compile_stdout") or ""
    compile_stderr = result.get("compile_stderr") or ""
    if compile_stdout.strip() or compile_stderr.strip():
        tool_logs.extend(_summarize_compile_logs(tool_id, compile_stdout, compile_stderr))

    # Generated files count
    gen_files = result.get("generated_files", [])
    if gen_files:
        tool_logs.append(f"[{phase}][INFO] Generated files: {len(gen_files)}")

    # Final status line
    success = result.get("success", False) and result.get("return_code", 1) == 0
    compile_success = result.get("compile_success")
    if compile_success is not None:
        if success and compile_success:
            tool_logs.append(f"[{phase}][SUCCESS] Tool execution and compilation completed")
        elif success and not compile_success:
            tool_logs.append(f"[{phase}][ERROR] Tool executed successfully but compilation failed")
        elif not success:
            rc = result.get("return_code", -1)
            tool_logs.append(f"[{phase}][ERROR] Tool execution failed (return_code={rc})")
    else:
        if success:
            tool_logs.append(f"[{phase}][SUCCESS] Tool execution completed")
        else:
            rc = result.get("return_code", -1)
            tool_logs.append(f"[{phase}][ERROR] Tool execution failed (return_code={rc})")

    return tool_logs


def _check_csmgvt_runtime_log(steps_root: Path, context: WorkflowContext) -> dict:
    """Check CSMGVT runtime.log for key traces and return structured result.

    Returns a dict with:
    - runtimeLogFound: bool
    - runtimeLogPath: str | None
    - keyTraces: dict of trace name -> bool (found or not)
    - failureKeywords: list of found failure keywords
    - isEmpty: bool (runtime.log exists but is empty)
    """
    active_project = steps_root / context.active_project_file
    project_dir = active_project.parent if active_project.suffix == ".xml" else active_project

    # Search for runtime.log in common locations
    runtime_log_candidates = list(Path(project_dir).rglob("runtime.log"))
    if not runtime_log_candidates:
        return {
            "runtimeLogFound": False,
            "runtimeLogPath": None,
            "keyTraces": {},
            "failureKeywords": [],
            "isEmpty": False,
        }

    runtime_log = runtime_log_candidates[0]
    content = ""
    try:
        content = runtime_log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass

    is_empty = not content.strip()

    # Key traces to check
    key_traces = {
        "harness_publish": any(kw in content for kw in ("HARNESS publish", "publish data")),
        "reader_data_updated": "DATA updated" in content,
        "reader_finish": "Reader finish" in content,
        "harness_received_finish": any(kw in content for kw in ("HARNESS received finish", "received finish")),
    }

    # Failure keywords
    failure_keywords = []
    for kw in ("assert failed", "Assertion failed", "Segmentation fault", "Aborted", "SIGABRT", "SIGSEGV"):
        if kw in content:
            failure_keywords.append(kw)

    return {
        "runtimeLogFound": True,
        "runtimeLogPath": str(runtime_log),
        "keyTraces": key_traces,
        "failureKeywords": failure_keywords,
        "isEmpty": is_empty,
    }


def _check_csmgvt_source_integrity(steps_root: Path, context: WorkflowContext, callback_url: str, task_id: str, output_path: str) -> bool:
    """Pre-check CSMGVT source integrity: verify HARNESS src and inc-gen exist.

    Returns True if integrity check passes, False if a FAILED callback was sent.
    """
    active_project = steps_root / context.active_project_file
    if not active_project.exists():
        _send_callback(
            callback_url,
            _callback_payload(
                {
                    "status": "FAILED",
                    "subStatus": "RUNNING_CSMGVT",
                    "progress": 60,
                    "outputPath": output_path,
                    "logs": [
                        "[CSMGVT][ERROR] Active project file not found — cannot verify source integrity.",
                        f"[CSMGVT][ERROR] Expected: {active_project}",
                        "[CSMGVT][INFO] Return to CODE_EDIT_REQUIRED state and ensure the harness project is generated.",
                    ],
                },
                context,
            ),
            task_id,
        )
        return False

    # Check that the active project directory has src/ subdirectories
    project_dir = active_project.parent
    src_dirs = list(project_dir.rglob("src"))
    if not src_dirs:
        _send_callback(
            callback_url,
            _callback_payload(
                {
                    "status": "FAILED",
                    "subStatus": "RUNNING_CSMGVT",
                    "progress": 60,
                    "outputPath": output_path,
                    "logs": [
                        "[CSMGVT][ERROR] No src/ directory found under the active project — HARNESS source code is missing.",
                        f"[CSMGVT][ERROR] Project directory: {project_dir}",
                        "[CSMGVT][INFO] Open Code Server and add business logic before running CSMGVT.",
                    ],
                },
                context,
            ),
            task_id,
        )
        return False

    # Check inc-gen directories exist for the tested components
    inc_gen_dirs = list(project_dir.rglob("inc-gen"))
    if not inc_gen_dirs:
        _send_callback(
            callback_url,
            _callback_payload(
                {
                    "status": "FAILED",
                    "subStatus": "RUNNING_CSMGVT",
                    "progress": 60,
                    "outputPath": output_path,
                    "logs": [
                        "[CSMGVT][ERROR] No inc-gen/ directory found — component interface headers are missing.",
                        f"[CSMGVT][ERROR] Project directory: {project_dir}",
                        "[CSMGVT][INFO] Ensure MSCIGT ran successfully to generate interface headers before CSMGVT.",
                    ],
                },
                context,
            ),
            task_id,
        )
        return False

    return True


def _check_csmgvt_output_products(steps_root: Path, context: WorkflowContext) -> dict:
    """Check CSMGVT output directory for expected products.

    Returns a dict with:
    - outputDirFound: bool
    - outputDirPath: str | None
    - missingProducts: list of missing expected files/dirs
    - foundProducts: list of found expected files/dirs
    """
    active_project = steps_root / context.active_project_file
    project_dir = active_project.parent if active_project.suffix == ".xml" else active_project

    expected_products = [
        "CMakeLists.txt",
        "src/main.cpp",
    ]

    harness_dirs = list(Path(project_dir).rglob("*HARNESS*")) + list(Path(project_dir).rglob("*harness*"))
    harness_found = any(d.is_dir() for d in harness_dirs)

    missing: list[str] = []
    found: list[str] = []

    for product in expected_products:
        candidates = list(Path(project_dir).rglob(product))
        if candidates:
            found.append(product)
        else:
            missing.append(product)

    if harness_found:
        found.append("HARNESS component directory")
    else:
        missing.append("HARNESS component directory")

    return {
        "outputDirFound": project_dir.exists(),
        "outputDirPath": str(project_dir) if project_dir.exists() else None,
        "missingProducts": missing,
        "foundProducts": found,
    }


def _classify_csmgvt_compile_failure(result: dict) -> list[str]:
    """Classify CSMGVT compilation failure into readable error categories."""
    errors: list[str] = []
    compile_stderr = result.get("compile_stderr", "") or ""
    cmake_rc = result.get("cmake_return_code")
    make_rc = result.get("make_return_code") or result.get("compile_return_code", -1)

    if cmake_rc is not None and cmake_rc != 0:
        if "Could not find" in compile_stderr or "not found" in compile_stderr.lower():
            if "HARNESS" in compile_stderr or "harness" in compile_stderr.lower():
                errors.append("CMAKE_HARNESS_MISSING: CMake cannot find HARNESS source files — open Code Server to add business logic")
            elif "inc-gen" in compile_stderr or "include" in compile_stderr.lower():
                errors.append("CMAKE_INC_GEN_MISSING: CMake cannot find component interface headers (inc-gen) — ensure MSCIGT ran successfully")
            else:
                errors.append(f"CMAKE_CONFIG_ERROR: CMake configuration failed (return_code={cmake_rc})")
        else:
            errors.append(f"CMAKE_FAILED: CMake failed with return_code={cmake_rc}")
    elif make_rc != 0 and make_rc != -1:
        if "undefined reference" in compile_stderr or "undefined" in compile_stderr.lower():
            if "HARNESS" in compile_stderr or "harness" in compile_stderr.lower():
                errors.append("MAKE_HARNESS_MISSING: Linker cannot find HARNESS function implementations — open Code Server to add test logic")
            else:
                errors.append(f"MAKE_LINK_ERROR: Linker errors detected (return_code={make_rc})")
        elif "fatal error" in compile_stderr or "No such file" in compile_stderr:
            if "inc-gen" in compile_stderr:
                errors.append("MAKE_INC_GEN_MISSING: Compiler cannot find component interface headers — ensure MSCIGT generated inc-gen correctly")
            elif "HARNESS" in compile_stderr or "harness" in compile_stderr.lower():
                errors.append("MAKE_HARNESS_MISSING: Compiler cannot find HARNESS headers — open Code Server to add business logic")
            else:
                errors.append(f"MAKE_COMPILE_ERROR: Compilation errors detected (return_code={make_rc})")
        else:
            errors.append(f"MAKE_FAILED: Make failed with return_code={make_rc}")

    if not errors and not result.get("compile_success", True):
        errors.append(f"COMPILE_UNKNOWN: Compilation failed (return_code={make_rc})")

    return errors


def _run_csm_executable(build_dir: str, timeout_seconds: int = 60) -> dict:
    """Run the csm executable and capture output.

    Returns a dict with:
    - csmRan: bool
    - csmReturnCode: int
    - csmStdout: str
    - csmStderr: str
    - csmTimedOut: bool
    - csmTimeoutNormal: bool  (return code 124 from timeout = normal stop)
    """
    import subprocess

    build_path = Path(build_dir)
    csm_candidates = list(build_path.rglob("csm")) + list(build_path.rglob("csm.*"))
    bin_dir = build_path / "bin"
    if bin_dir.exists():
        csm_candidates = list(bin_dir.glob("csm*")) + csm_candidates

    if not csm_candidates:
        platform_candidates = list(build_path.rglob("platform"))
        if bin_dir.exists():
            platform_candidates = list(bin_dir.glob("platform*")) + platform_candidates
        if platform_candidates:
            csm_candidates = platform_candidates[:1]
        else:
            return {
                "csmRan": False, "csmReturnCode": -1, "csmStdout": "",
                "csmStderr": "No csm or platform executable found in build directory",
                "csmTimedOut": False, "csmTimeoutNormal": False,
            }

    csm_exe = str(csm_candidates[0])
    if not os.access(csm_exe, os.X_OK):
        return {
            "csmRan": False, "csmReturnCode": -1, "csmStdout": "",
            "csmStderr": f"Executable not executable: {csm_exe}",
            "csmTimedOut": False, "csmTimeoutNormal": False,
        }

    try:
        result = subprocess.run(
            ["timeout", str(timeout_seconds), csm_exe],
            cwd=str(build_path),
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 10,
        )
        rc = result.returncode
        timed_out = rc == 124
        return {
            "csmRan": True, "csmReturnCode": rc,
            "csmStdout": result.stdout, "csmStderr": result.stderr,
            "csmTimedOut": timed_out, "csmTimeoutNormal": timed_out,
        }
    except subprocess.TimeoutExpired:
        return {
            "csmRan": True, "csmReturnCode": 124, "csmStdout": "",
            "csmStderr": "CSM execution timed out (treated as normal stop)",
            "csmTimedOut": True, "csmTimeoutNormal": True,
        }
    except Exception as exc:
        return {
            "csmRan": False, "csmReturnCode": -1, "csmStdout": "",
            "csmStderr": str(exc), "csmTimedOut": False, "csmTimeoutNormal": False,
        }


def _run_pipeline(
    task_id: str,
    project_id: str,
    output_dir: str,
    callback_url: str,
    selected_phases: list[str],
    continue_on_error: bool,
    phase_params: dict,
    skip_export: bool,
    workflow_mode: str | None = None,
    continuing: bool = False,
    base_project_file: str | None = None,
    active_project_file: str | None = None,
    harness_project_file: str | None = None,
) -> None:
    """Background pipeline execution (runs in a daemon thread)."""
    executor = ToolExecutor()
    had_failure = False
    output_path = output_dir
    tool_cwd = f"{project_id}/{task_id}/Steps"
    mode = _normalize_workflow_mode(workflow_mode)

    project_name, resolved_project_file, steps_root = _resolve_project_file(project_id, task_id, skip_export, callback_url, task_id)
    if not resolved_project_file or not steps_root:
        return

    context = WorkflowContext(
        workflow_mode=mode,
        base_project_file=base_project_file or resolved_project_file,
        active_project_file=active_project_file or base_project_file or resolved_project_file,
        harness_project_file=harness_project_file,
        selected_phases=selected_phases[:],
        continuing=continuing,
    )

    pipeline_steps = _resolve_pipeline_steps(selected_phases, workflow_mode, continuing)

    for step in pipeline_steps:
        phase_id = step["phaseId"]
        tool_id = step["toolId"]
        sub_status = step["subStatus"]
        label = step["label"]
        p_start = step["pStart"]
        p_end = step["pEnd"]
        needs_cfg = step["needsCfg"]

        logger.info("[Pipeline] %s ...", label)
        _send_callback(
            callback_url,
            _callback_payload(
                {
                "status": "GENERATING",
                "subStatus": sub_status,
                "progress": p_start,
                "logs": [f"[{phase_id}][INFO] {label} started."],
                },
                context,
            ),
            task_id,
        )

        import shlex

        phase_config = phase_params.get(phase_id, {})
        additional_args_str = phase_config.get("additionalArgs", "")
        additional_args = shlex.split(additional_args_str) if additional_args_str else []

        config_file = None
        selected_components: list[str] = []
        if needs_cfg and tool_id == "asctg":
            selected_comps_str = phase_config.get("selected_components", "")
            if selected_comps_str:
                selected_components = [c.strip() for c in selected_comps_str.split(",") if c.strip()]

            if not selected_components:
                config_file = _find_config_file(project_id, task_id)
                if not config_file:
                    had_failure = True
                    _send_callback(
                        callback_url,
                        _callback_payload(
                            {
                            "status": "FAILED",
                            "subStatus": sub_status,
                            "progress": p_end,
                            "outputPath": output_path,
                            "logs": ["[ASCTG][ERROR] Missing selected components or config.xml"],
                            },
                            context,
                        ),
                        task_id,
                    )
                    logger.error("[Pipeline] ASCTG task=%s missing selected components and config.xml", task_id)
                    return

        if additional_args:
            logger.info("[Pipeline] %s extra args: %s", label, additional_args)

        try:
            if tool_id == "asctg":
                logger.info("[Pipeline] Running ASCTG through the unified pipeline.")
                result = execute_asctg_from_steps_dir(project_id, str(steps_root), selected_components)
                result.setdefault("project_path", result.get("workspace_root", ""))
                result["frontend_logs"] = build_asctg_logs(project_id, str(steps_root), selected_components, result)
            else:
                result = executor.execute_in_project(
                    tool_id=tool_id,
                    project_name=tool_cwd,
                    project_file=context.active_project_file,
                    verbose=3,
                    checker=None,
                    config_file=config_file,
                    compile=True if tool_id == "ldp" else None,
                    additional_args=additional_args,
                    workspace_dir=str(steps_root),
                )
        except Exception as exc:  # pragma: no cover - defensive wrapper
            result = {
                "success": False,
                "return_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "generated_files": [],
                "project_path": "",
                "message": str(exc),
            }

        tool_logs = result.get("frontend_logs") or _build_tool_logs(tool_id, result)
        if result.get("project_path"):
            output_path = result["project_path"]

        tool_success = result.get("success", False) and result.get("return_code", 1) == 0
        compile_success = result.get("compile_success", True) if tool_id in ["ldp", "csmgvt"] else True
        success = tool_success and compile_success

        mid_progress = p_start + (p_end - p_start) // 2
        if tool_logs:
            _send_callback(
                callback_url,
                _callback_payload(
                    {
                    "status": "GENERATING",
                    "subStatus": sub_status,
                    "progress": mid_progress,
                    "logs": tool_logs,
                    },
                    context,
                ),
                task_id,
            )

        if success:
            if tool_id == "asctg" and mode in ("HARNESS", "HARNESS_DEV"):
                try:
                    context = activate_harness_project(context, steps_root, result)
                except FileNotFoundError as exc:
                    had_failure = True
                    _send_callback(
                        callback_url,
                        _callback_payload(
                            {
                            "status": "FAILED",
                            "subStatus": sub_status,
                            "progress": p_end,
                            "outputPath": output_path,
                            "logs": [f"[{phase_id}][ERROR] {exc}"],
                            },
                            context,
                        ),
                        task_id,
                    )
                    logger.error("[Pipeline] %s task=%s, error=%s", tool_id, task_id, exc)
                    return
            _send_callback(
                callback_url,
                _callback_payload(
                    {
                    "status": "GENERATING",
                    "subStatus": sub_status,
                    "progress": p_end,
                    "logs": [f"[{phase_id}][SUCCESS] {label} completed successfully."],
                    },
                    context,
                ),
                task_id,
            )
            # CSMGVT: four sub-steps with structured callbacks
            if tool_id == "csmgvt":
                # ── Sub-step 1: Output product check ──────────────────────
                product_result = _check_csmgvt_output_products(steps_root, context)
                product_logs = []
                if product_result["missingProducts"]:
                    product_logs.append("[CSMGVT][SUB1][WARN] === Output Product Check ===")
                    for mp in product_result["missingProducts"]:
                        product_logs.append(f"[CSMGVT][SUB1][WARN] Missing: {mp}")
                    for fp in product_result["foundProducts"]:
                        product_logs.append(f"[CSMGVT][SUB1][INFO] Found: {fp}")
                else:
                    product_logs.append("[CSMGVT][SUB1][SUCCESS] All expected output products found")

                _send_callback(
                    callback_url,
                    _callback_payload(
                        {
                        "status": "GENERATING",
                        "subStatus": sub_status,
                        "progress": p_start + (p_end - p_start) * 1 // 4,
                        "logs": product_logs,
                        "csmgvtSubStep": "output_check",
                        "csmgvtProductCheck": product_result,
                        },
                        context,
                    ),
                    task_id,
                )

                # ── Sub-step 2: Compile result classification ─────────────
                compile_logs = []
                if not compile_success:
                    classified_errors = _classify_csmgvt_compile_failure(result)
                    compile_logs.append("[CSMGVT][SUB2][ERROR] === Compile Failure Classification ===")
                    for err in classified_errors:
                        compile_logs.append(f"[CSMGVT][SUB2][ERROR] {err}")
                    compile_logs.append("[CSMGVT][SUB2][INFO] Open Code Server to fix compilation errors and retry")
                else:
                    compile_logs.append("[CSMGVT][SUB2][SUCCESS] Compilation succeeded")
                    build_dir = result.get("build_dir", "")
                    if build_dir:
                        compile_logs.append(f"[CSMGVT][SUB2][INFO] Build directory: {build_dir}")

                _send_callback(
                    callback_url,
                    _callback_payload(
                        {
                        "status": "GENERATING",
                        "subStatus": sub_status,
                        "progress": p_start + (p_end - p_start) * 2 // 4,
                        "logs": compile_logs,
                        "csmgvtSubStep": "compile",
                        "csmgvtCompileErrors": _classify_csmgvt_compile_failure(result) if not compile_success else [],
                        },
                        context,
                    ),
                    task_id,
                )

                # ── Sub-step 3: Run csm ──────────────────────────────────
                csm_logs = []
                csm_result = {"csmRan": False, "csmReturnCode": -1, "csmTimedOut": False, "csmTimeoutNormal": False}
                build_dir = result.get("build_dir", "")
                if compile_success and build_dir:
                    csm_result = _run_csm_executable(build_dir)
                    if csm_result["csmRan"]:
                        rc = csm_result["csmReturnCode"]
                        if csm_result["csmTimeoutNormal"]:
                            csm_logs.append(f"[CSMGVT][SUB3][INFO] CSM execution timed out (return_code=124) — treated as normal stop")
                        elif rc == 0:
                            csm_logs.append("[CSMGVT][SUB3][SUCCESS] CSM execution completed successfully")
                        else:
                            csm_logs.append(f"[CSMGVT][SUB3][ERROR] CSM execution failed (return_code={rc})")
                        for line in (csm_result.get("csmStdout") or "").splitlines()[:20]:
                            if line.strip():
                                csm_logs.append(f"[CSMGVT][SUB3][INFO] {line}")
                    else:
                        csm_logs.append(f"[CSMGVT][SUB3][WARN] CSM executable not found: {csm_result.get('csmStderr', '')}")
                elif not compile_success:
                    csm_logs.append("[CSMGVT][SUB3][SKIP] CSM not run — compilation failed")
                else:
                    csm_logs.append("[CSMGVT][SUB3][SKIP] CSM not run — no build directory found")

                _send_callback(
                    callback_url,
                    _callback_payload(
                        {
                        "status": "GENERATING",
                        "subStatus": sub_status,
                        "progress": p_start + (p_end - p_start) * 3 // 4,
                        "logs": csm_logs,
                        "csmgvtSubStep": "run_csm",
                        "csmgvtCsmResult": csm_result,
                        },
                        context,
                    ),
                    task_id,
                )

                # ── Sub-step 4: Check runtime.log ─────────────────────────
                runtime_result = _check_csmgvt_runtime_log(steps_root, context)
                runtime_logs = []
                if runtime_result["runtimeLogFound"]:
                    if runtime_result["isEmpty"]:
                        runtime_logs.append("[CSMGVT][SUB4][WARN] runtime.log is empty — test framework ran but HARNESS did not write test cases")
                        runtime_logs.append("[CSMGVT][SUB4][INFO] Open Code Server to add test logic in HARNESS functions")
                    else:
                        traces = runtime_result["keyTraces"]
                        runtime_logs.append("[CSMGVT][SUB4][INFO] === Runtime Log Trace Check ===")
                        for trace_name, found in traces.items():
                            status_icon = "✓" if found else "✗"
                            runtime_logs.append(f"[CSMGVT][SUB4][INFO] {status_icon} {trace_name}: {'found' if found else 'not found'}")
                        if runtime_result["failureKeywords"]:
                            for kw in runtime_result["failureKeywords"]:
                                runtime_logs.append(f"[CSMGVT][SUB4][ERROR] Failure keyword detected: {kw}")
                        else:
                            runtime_logs.append("[CSMGVT][SUB4][INFO] No failure keywords detected in runtime.log")
                else:
                    runtime_logs.append("[CSMGVT][SUB4][INFO] No runtime.log found (csm may not have been executed)")

                _send_callback(
                    callback_url,
                    _callback_payload(
                        {
                        "status": "GENERATING",
                        "subStatus": sub_status,
                        "progress": p_end,
                        "logs": runtime_logs,
                        "csmgvtSubStep": "check_log",
                        "csmgvtResult": runtime_result,
                        },
                        context,
                    ),
                    task_id,
                )
            continue

        had_failure = True
        if not tool_success:
            rc = result.get("return_code", -1)
            fail_logs = [f"[{phase_id}][ERROR] Tool execution failed (return_code={rc})"]
        else:
            rc = result.get("compile_return_code", -1)
            fail_logs = [f"[{phase_id}][COMPILE][ERROR] Compilation failed (return_code={rc})"]
            # CSMGVT: add classified error hints
            if tool_id == "csmgvt":
                classified = _classify_csmgvt_compile_failure(result)
                for err in classified:
                    fail_logs.append(f"[{phase_id}][COMPILE][ERROR] {err}")

        if continue_on_error:
            fail_logs.append(f"[{phase_id}][WARN] continueOnError=true, continuing after {tool_id} failure.")
            _send_callback(
                callback_url,
                _callback_payload(
                    {
                    "status": "GENERATING",
                    "subStatus": sub_status,
                    "progress": p_end,
                    "logs": fail_logs,
                    },
                    context,
                ),
                task_id,
            )
        else:
            _send_callback(
                callback_url,
                _callback_payload(
                    {
                    "status": "FAILED",
                    "subStatus": sub_status,
                    "progress": p_end,
                    "outputPath": output_path,
                    "logs": fail_logs,
                    },
                    context,
                ),
                task_id,
            )
            logger.error("[Pipeline] FAILED at %s, aborting task %s", tool_id, task_id)
            return

    # ── Source readiness gate before CSMGVT/LDP ────────────────────────────
    if not had_failure and not continuing:
        execution_phases = {"CSMGVT", "LDP"}
        selected_set = set(selected_phases)
        if selected_set.intersection(execution_phases) and mode == "INTEGRATION":
            # INTEGRATION mode: source must be ready before CSMGVT/LDP
            source_ready_evidence = phase_params.get("_meta", {}).get("sourceReadinessEvidence")
            if not source_ready_evidence:
                _send_callback(
                    callback_url,
                    _callback_payload(
                        {
                            "status": "SOURCE_PREP_REQUIRED",
                            "subStatus": "NONE",
                            "progress": 100,
                            "outputPath": output_path,
                            "logs": [
                                "[PIPELINE][ERROR] INTEGRATION mode requires source readiness confirmation before CSMGVT/LDP.",
                                "[PIPELINE][INFO] Provide sourceReadinessEvidence or prepare source via Code Server first.",
                            ],
                        },
                        context,
                    ),
                    task_id,
                )
                logger.error("[Pipeline] SOURCE_PREP_REQUIRED for INTEGRATION task=%s", task_id)
                return

    # ── CSMGVT source integrity pre-check ───────────────────────────────────
    if not had_failure and not continuing:
        for step in pipeline_steps:
            if step["phaseId"] == "CSMGVT" and mode in ("HARNESS", "HARNESS_DEV"):
                _check_csmgvt_source_integrity(steps_root, context, callback_url, task_id, output_path)
                # If integrity check failed, a FAILED callback was already sent
                if not (steps_root / context.active_project_file).exists():
                    return
                break

    status = "AWAITING_CODE" if should_await_code(mode, selected_phases, had_failure, continuing) else "COMPLETED"
    if status == "AWAITING_CODE":
        code_workspace = str(steps_root.parent)  # /workspace/{projectId}/{workspaceId}/src
        final_logs = [
            "[PIPELINE][INFO] Skeleton generation finished.",
            "[PIPELINE][INFO] Open Code Server, add your business code, then continue with CSMGVT or LDP.",
            f"[PIPELINE][INFO] Code workspace path: {code_workspace}",
        ]
    else:
        final_logs = [
            "[PIPELINE][SUCCESS] ECOA generation finished.",
            f"[PIPELINE][INFO] Output path: {output_path}",
        ]
        if had_failure and continue_on_error:
            final_logs.append("[PIPELINE][WARN] Some phases failed, but the pipeline continued because continueOnError=true.")

    logger.info("[Pipeline] %s task=%s, outputPath=%s", status, task_id, output_path)
    final_payload = {
        "status": status,
        "subStatus": "NONE",
        "progress": 100,
        "outputPath": output_path,
        "logs": final_logs,
    }
    if status == "AWAITING_CODE":
        code_workspace = str(steps_root.parent) if steps_root else output_path
        final_payload["codeWorkspacePath"] = code_workspace
        final_payload["sourceState"] = "GENERATED_SKELETON"
    _send_callback(
        callback_url,
        _callback_payload(final_payload, context),
        task_id,
    )


def _run_generate_harness_task(
    task_id: str,
    project_id: str,
    steps_dir: str,
    selected_components: list[str],
    callback_url: Optional[str] = None,
) -> None:
    """Background ASCTG generate_harness task."""
    _send_callback_if_present(
        callback_url,
        {
            "status": "RUNNING",
            "progress": 10,
            "logs": ["[ASCTG][INFO] generate_harness task started"],
        },
        task_id,
    )

    result = execute_asctg_from_steps_dir(
        project_id=project_id,
        steps_dir=steps_dir,
        selected_components=selected_components,
    )

    if result.get("success"):
        _send_callback_if_present(
            callback_url,
            {
                "status": "SUCCESS",
                "progress": 100,
                "logs": ["[ASCTG][SUCCESS] generate_harness task completed"],
            },
            task_id,
        )
        logger.info(
            "[ASCTG TASK] success task=%s project=%s workspace=%s",
            task_id,
            project_id,
            result.get("workspace_root", ""),
        )
        return

    _send_callback_if_present(
        callback_url,
        {
            "status": "FAILED",
            "progress": 100,
            "logs": [f"[ASCTG][ERROR] generate_harness task failed: {result.get('error', 'unknown error')}"],
        },
        task_id,
    )
    logger.error(
        "[ASCTG TASK] failed task=%s project=%s error=%s",
        task_id,
        project_id,
        result.get("error", "unknown error"),
    )


@bp.route("/api/generate", methods=["POST"])
def trigger_generation():
    """
    Accept a generation request from the Java backend and run the ECOA
    toolchain pipeline in a background thread.
    """
    data = request.get_json(force=True, silent=True) or {}

    task_id = data.get("task_id") or data.get("taskId")
    project_id = data.get("project_id") or data.get("projectId")
    step_name = data.get("step_name") or data.get("stepName")
    callback_url = data.get("callback_url") or data.get("callbackUrl")
    workflow_mode = data.get("workflowMode") or data.get("workflow_mode")
    base_project_file = data.get("baseProjectFile") or data.get("base_project_file")
    active_project_file = data.get("activeProjectFile") or data.get("active_project_file")
    harness_project_file = data.get("harnessProjectFile") or data.get("harness_project_file")
    source_readiness_evidence = data.get("sourceReadinessEvidence") or data.get("source_readiness_evidence")

    if step_name == "generate_harness":
        steps_dir = data.get("steps_dir") or data.get("stepsDir")
        selected_components = data.get("selected_components") or data.get("selectedComponents") or []

        if not task_id or not project_id or not steps_dir:
            return jsonify({"success": False, "error": "task_id, project_id and steps_dir are required for generate_harness"}), 400

        if not isinstance(selected_components, list) or not all(isinstance(component, str) for component in selected_components):
            return jsonify({"success": False, "error": "selected_components must be a list of strings"}), 400

        logger.info(
            "[API] Generate harness accepted: task=%s project=%s steps=%s comps=%s",
            task_id,
            project_id,
            steps_dir,
            len(selected_components),
        )

        thread = threading.Thread(
            target=_run_generate_harness_task,
            args=(task_id, project_id, steps_dir, selected_components, callback_url),
            daemon=True,
        )
        thread.start()
        return jsonify({"success": True, "message": "Accepted", "task_id": task_id, "project_id": project_id, "step_name": step_name}), 202

    output_dir = data.get("outputDir", "/workspace")
    selected_phases_present = "selectedPhases" in data
    selected_phases = data.get("selectedPhases")
    continue_on_error = bool(data.get("continueOnError", False))
    phase_params = data.get("phaseParams", {})
    skip_export = bool(data.get("skipExport", False))
    # Continuing runs must always skip export to preserve user code
    continuing = False

    if not task_id or not project_id or not callback_url:
        return jsonify({"success": False, "error": "taskId, projectId and callbackUrl are required"}), 400

    if selected_phases_present and selected_phases == []:
        error_message = "selectedPhases must be a non-empty list of strings"
        return jsonify({"success": False, "error": error_message, "message": error_message}), 400

    if workflow_mode is not None:
        try:
            continuing = parse_continuing_flag(data.get("continuing", False))
        except ValueError as exc:
            error_message = str(exc)
            return jsonify({"success": False, "error": error_message, "message": error_message}), 400

        if selected_phases is not None and (
            not isinstance(selected_phases, list) or not all(isinstance(phase, str) for phase in selected_phases)
        ):
            return jsonify({"success": False, "error": "selectedPhases must be a list of strings"}), 400

        try:
            validate_phase_selection(workflow_mode, selected_phases, continuing)
        except ValueError as exc:
            error_message = str(exc)
            return jsonify({"success": False, "error": error_message, "message": error_message}), 400

        if selected_phases is None:
            selected_phases = default_selected_phases(workflow_mode, continuing)

        selected_phases = resolve_phase_steps(workflow_mode, selected_phases, continuing)
    else:
        selected_phases = selected_phases or ["EXVT", "ASCTG", "MSCIGT", "CSMGVT", "LDP"]

    logger.info(
        "[API] Generate accepted: task=%s project=%s phases=%s continueOnError=%s skipExport=%s params=%s",
        task_id,
        project_id,
        selected_phases,
        continue_on_error,
        skip_export,
        phase_params,
    )

    # Inject sourceReadinessEvidence into phase_params._meta for pipeline access
    if source_readiness_evidence:
        if "_meta" not in phase_params:
            phase_params["_meta"] = {}
        phase_params["_meta"]["sourceReadinessEvidence"] = source_readiness_evidence

    thread = threading.Thread(
        target=_run_pipeline,
        args=(
            task_id,
            project_id,
            output_dir,
            callback_url,
            selected_phases,
            continue_on_error,
            phase_params,
            skip_export,
            workflow_mode,
            continuing,
            base_project_file,
            active_project_file,
            harness_project_file,
        ),
        daemon=True,
    )
    thread.start()

    return jsonify({"message": "Accepted", "taskId": task_id}), 202


# ── Code Backflow Endpoints ─────────────────────────────────────────────────


@bp.post("/api/backflow/scan")
def backflow_scan():
    """Scan HARNESS workspace for returnable and excluded files.

    Request body:
        taskId: str
        projectId: str
        workspaceId: str
        returnableComponents: list[str] | null  (optional component filter)

    Returns:
        returnableFiles: list of {relativePath, isNew}
        excludedFiles: list of {relativePath, exclusionReason}
    """
    body = request.get_json(force=True)
    task_id = body.get("taskId", "")
    project_id = body.get("projectId", "")
    workspace_id = body.get("workspaceId", "")
    returnable_components = body.get("returnableComponents")

    steps_root = WORKSPACE_ROOT / project_id / workspace_id / "src"
    if not steps_root.exists():
        return jsonify({"error": f"Workspace not found: {steps_root}"}), 404

    # Determine source root: typically the base project directory
    # For HARNESS mode, source of truth is the original project before HARNESS overlay
    source_root = steps_root  # Default: same workspace

    returnable, excluded = scan_backflow_files(
        harness_workspace=steps_root,
        source_root=source_root,
        returnable_components=returnable_components,
    )

    return jsonify({
        "taskId": task_id,
        "returnableFiles": [
            {"relativePath": bf.relative_path, "isNew": bf.is_new}
            for bf in returnable
        ],
        "excludedFiles": [
            {"relativePath": bf.relative_path, "exclusionReason": bf.exclusion_reason}
            for bf in excluded
        ],
    })


@bp.post("/api/backflow/patch")
def backflow_generate_patch():
    """Generate a backflow patch from HARNESS workspace.

    Request body:
        taskId: str
        projectId: str
        workspaceId: str
        returnableComponents: list[str] | null

    Returns:
        patchContent: str (unified diff)
        patchHash: str
        returnableFiles: list of {relativePath, isNew, isConflict, conflictReason}
        hasConflicts: bool
        conflictFiles: list
    """
    body = request.get_json(force=True)
    task_id = body.get("taskId", "")
    project_id = body.get("projectId", "")
    workspace_id = body.get("workspaceId", "")
    returnable_components = body.get("returnableComponents")

    steps_root = WORKSPACE_ROOT / project_id / workspace_id / "src"
    if not steps_root.exists():
        return jsonify({"error": f"Workspace not found: {steps_root}"}), 404

    source_root = steps_root

    returnable, excluded = scan_backflow_files(
        harness_workspace=steps_root,
        source_root=source_root,
        returnable_components=returnable_components,
    )

    patch = generate_patch(returnable, task_id)

    return jsonify({
        "taskId": task_id,
        "patchContent": patch.patch_content,
        "patchHash": patch.patch_hash,
        "returnableFiles": [
            {
                "relativePath": bf.relative_path,
                "isNew": bf.is_new,
                "isConflict": bf.is_conflict,
                "conflictReason": bf.conflict_reason,
            }
            for bf in patch.returnable_files
        ],
        "hasConflicts": patch.has_conflicts,
        "conflictFiles": [
            {
                "relativePath": bf.relative_path,
                "conflictReason": bf.conflict_reason,
            }
            for bf in patch.conflict_files
        ],
    })


@bp.post("/api/backflow/apply")
def backflow_apply():
    """Apply a backflow patch to the source of truth.

    Request body:
        taskId: str
        projectId: str
        workspaceId: str
        returnableComponents: list[str] | null
        mode: "overwrite" | "new_version"
        callbackUrl: str  (for status update)

    Returns:
        success: bool
        appliedFiles: list[str]
        skippedFiles: list[str]
        conflictFiles: list[str]
        patchArtifactPath: str | null
        sourceRevision: str
    """
    body = request.get_json(force=True)
    task_id = body.get("taskId", "")
    project_id = body.get("projectId", "")
    workspace_id = body.get("workspaceId", "")
    returnable_components = body.get("returnableComponents")
    mode = body.get("mode", "overwrite")
    callback_url = body.get("callbackUrl", "")

    steps_root = WORKSPACE_ROOT / project_id / workspace_id / "src"
    if not steps_root.exists():
        return jsonify({"error": f"Workspace not found: {steps_root}"}), 404

    source_root = steps_root
    patch_artifact_dir = steps_root / "backflow-artifacts"

    returnable, excluded = scan_backflow_files(
        harness_workspace=steps_root,
        source_root=source_root,
        returnable_components=returnable_components,
    )

    patch = generate_patch(returnable, task_id)
    result = apply_patch(patch, source_root, mode=mode, patch_artifact_dir=patch_artifact_dir)

    # Send callback to Java backend with backflow result
    if callback_url:
        try:
            callback_payload = {
                "status": "COMPLETED" if result.success else "FAILED",
                "subStatus": "CODE_BACKFLOW_APPLIED" if result.success and not result.conflict_files else "CONFLICT" if result.conflict_files else "NONE",
                "progress": 100,
                "logs": [
                    f"[BACKFLOW][INFO] Patch applied: {len(result.applied_files)} files",
                    f"[BACKFLOW][INFO] Skipped: {len(result.skipped_files)} files",
                    f"[BACKFLOW][INFO] Conflicts: {len(result.conflict_files)} files",
                    f"[BACKFLOW][INFO] Source revision: {result.source_revision}",
                ],
                "sourceRevision": result.source_revision,
                "patchArtifactPath": result.patch_artifact_path,
            }
            if result.error_message:
                callback_payload["logs"].append(f"[BACKFLOW][ERROR] {result.error_message}")
            requests.post(callback_url, json=callback_payload, timeout=10)
        except Exception as exc:
            logger.error("[Backflow] Callback failed: %s", exc)

    return jsonify({
        "success": result.success,
        "appliedFiles": result.applied_files,
        "skippedFiles": result.skipped_files,
        "conflictFiles": result.conflict_files,
        "errorMessage": result.error_message,
        "patchArtifactPath": result.patch_artifact_path,
        "sourceRevision": result.source_revision,
    })
