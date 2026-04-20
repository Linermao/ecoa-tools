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
    activate_harness_project,
    default_selected_phases,
    parse_continuing_flag,
    resolve_phase_steps,
    should_await_code,
    validate_phase_selection,
)
from app.services.executor import ToolExecutor
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
    mode = workflow_mode or "INTEGRATION"

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
            if tool_id == "asctg" and mode == "HARNESS":
                try:
                    context = activate_harness_project(context, steps_root)
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
            continue

        had_failure = True
        if not tool_success:
            rc = result.get("return_code", -1)
            fail_logs = [f"[{phase_id}][ERROR] Tool execution failed (return_code={rc})"]
        else:
            rc = result.get("compile_return_code", -1)
            fail_logs = [f"[{phase_id}][COMPILE][ERROR] Compilation failed (return_code={rc})"]

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

    status = "AWAITING_CODE" if should_await_code(mode, selected_phases, had_failure, continuing) else "COMPLETED"
    if status == "AWAITING_CODE":
        final_logs = [
            "[PIPELINE][INFO] Skeleton generation finished.",
            "[PIPELINE][INFO] Open Code Server, add your business code, then continue with CSMGVT or LDP.",
        ]
    else:
        final_logs = [
            "[PIPELINE][SUCCESS] ECOA generation finished.",
            f"[PIPELINE][INFO] Output path: {output_path}",
        ]
        if had_failure and continue_on_error:
            final_logs.append("[PIPELINE][WARN] Some phases failed, but the pipeline continued because continueOnError=true.")

    logger.info("[Pipeline] %s task=%s, outputPath=%s", status, task_id, output_path)
    _send_callback(
        callback_url,
        _callback_payload(
            {
            "status": status,
            "subStatus": "NONE",
            "progress": 100,
            "outputPath": output_path,
            "logs": final_logs,
            },
            context,
        ),
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
