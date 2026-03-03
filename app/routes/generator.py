"""
Generator pipeline route — Phase-aware ECOA toolchain execution.

Provides POST /api/generate endpoint that:
  1. Calls the Java backend to export ECOA XML to the shared workspace.
  2. For each selected phase, invokes the corresponding tool(s) directly
     via the existing ToolExecutor (no self-HTTP loop).
  3. Reports progress back to the Java callback URL after each step.

Environment variables:
  SIRIUS_WEB_URL   — Java backend URL  (default: http://localhost:8080)
  ECOA_WORKSPACE   — Shared workspace root (default: /workspace)
"""

import os
import threading
from pathlib import Path
from typing import Optional

import requests
from flask import Blueprint, jsonify, request

from app.services.executor import ToolExecutor
from app.utils.logger import setup_logger

bp = Blueprint("generator", __name__)
logger = setup_logger("app.routes.generator")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SIRIUS_WEB_URL = os.environ.get("SIRIUS_WEB_URL", "http://localhost:8080")
WORKSPACE_ROOT = Path(os.environ.get("ECOA_WORKSPACE", "/workspace"))

logger.info(f"[Generator] sirius-web: {SIRIUS_WEB_URL}")
logger.info(f"[Generator] workspace:  {WORKSPACE_ROOT}")

# ---------------------------------------------------------------------------
# Per-step definitions
# ---------------------------------------------------------------------------
#  Each step:  phaseId, toolId, subStatus label, progressStart, progressEnd, requiresConfigFile
PHASE_STEPS = [
    dict(phaseId="EXVT",   toolId="exvt",   subStatus="RUNNING_EXVT",    label="[EXVT] XML Validation",      pStart=0,  pEnd=20,  needsCfg=False),
    dict(phaseId="MSCIGT", toolId="mscigt", subStatus="RUNNING_MSCIGT",  label="[MSCIGT] Skeleton Generator", pStart=20, pEnd=40,  needsCfg=False),
    dict(phaseId="ASCTG",  toolId="asctg",  subStatus="RUNNING_ASCTG",   label="[ASCTG] Test Generator",     pStart=40, pEnd=60,  needsCfg=True),
    dict(phaseId="CSMGVT", toolId="csmgvt", subStatus="RUNNING_CSMGVT",  label="[CSMGVT] Cork/Stub Gen",     pStart=60, pEnd=80,  needsCfg=False),
    dict(phaseId="LDP",    toolId="ldp",    subStatus="RUNNING_LDP",     label="[LDP] Middleware Builder",   pStart=80, pEnd=100, needsCfg=False),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_callback(callback_url: str, payload: dict, task_id: str) -> None:
    """POST a progress/status payload to the Java backend (fire-and-forget)."""
    try:
        resp = requests.post(callback_url, json=payload, timeout=10)
        logger.debug(f"[CB] task={task_id} status={payload.get('status')} "
                     f"progress={payload.get('progress')}% → HTTP {resp.status_code}")
    except Exception as exc:
        logger.error(f"[CB ERROR] task={task_id}: {exc}")


def _export_to_disk(project_id: str) -> tuple[bool, str, str, str]:
    """
    Ask the Java backend to export ECOA XML into the shared workspace.
    Returns (success, projectName, projectFile, errorMsg).
    """
    url = f"{SIRIUS_WEB_URL}/api/edt/ecoa/export-to-disk/{project_id}"
    try:
        resp = requests.post(url, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            return True, data["projectName"], data["projectFile"], ""
        return False, "", "", f"HTTP {resp.status_code}: {resp.text}"
    except Exception as exc:
        return False, "", "", f"Connection error: {exc}"


def _find_config_file(project_id: str, project_name: str) -> Optional[str]:
    """Locate an ASCTG config XML inside the project Steps directory."""
    steps_dir = WORKSPACE_ROOT / project_id / project_name / "Steps"
    if not steps_dir.exists():
        steps_dir = WORKSPACE_ROOT / project_id / "Steps"
    for pattern in ["*.config.xml", "*config*.xml"]:
        matches = list(steps_dir.rglob(pattern))
        if matches:
            try:
                return str(matches[0].relative_to(steps_dir))
            except ValueError:
                pass
    return None


def _run_pipeline(
    task_id: str,
    project_id: str,
    output_dir: str,
    callback_url: str,
    selected_phases: list[str],
    continue_on_error: bool,
    phase_params: dict,
) -> None:
    """Background pipeline execution (runs in a daemon thread)."""
    executor = ToolExecutor()
    had_failure = False
    output_path = output_dir

    # ── 0. Export ECOA XML to disk ──────────────────────────────────────────
    _send_callback(callback_url, {
        "status": "EXPORTING_XML",
        "subStatus": "NONE",
        "progress": 0,
        "logs": ["[ECOA-WEB] 正在将 EDT 模型导出为 ECOA XML 文件..."],
    }, task_id)

    export_ok, project_name, project_file, export_err = _export_to_disk(project_id)

    if not export_ok:
        _send_callback(callback_url, {
            "status": "FAILED",
            "subStatus": "NONE",
            "progress": 0,
            "logs": [
                f"[ERROR] ECOA XML 导出失败: {export_err}",
                "[ERROR] 请确认项目存在且 sirius-web 后端可访问",
            ],
        }, task_id)
        return

    # Java exported structure: /workspace/{project_id}/{project_name}/Steps
    tool_cwd = f"{project_id}/{project_name}/Steps"

    _send_callback(callback_url, {
        "status": "GENERATING",
        "subStatus": "NONE",
        "progress": 5,
        "logs": [
            f"[ECOA-WEB] ✓ 导出成功: {project_name}/{project_file}",
            f"[ECOA-WEB] 工作区路径: {WORKSPACE_ROOT}/{tool_cwd}/{project_file}",
        ],
    }, task_id)

    # ── 1–5. Run each tool step ─────────────────────────────────────────────
    for step in PHASE_STEPS:
        phase_id   = step["phaseId"]
        tool_id    = step["toolId"]
        sub_status = step["subStatus"]
        label      = step["label"]
        p_start    = step["pStart"]
        p_end      = step["pEnd"]
        needs_cfg  = step["needsCfg"]

        if phase_id not in selected_phases:
            _send_callback(callback_url, {
                "status": "GENERATING",
                "subStatus": "NONE",
                "progress": p_end,
                "logs": [f"[SKIP] 跳过未选阶段: {phase_id} ({tool_id})"],
            }, task_id)
            continue

        logger.info(f"[Pipeline] {label} ...")

        _send_callback(callback_url, {
            "status": "GENERATING",
            "subStatus": sub_status,
            "progress": p_start,
            "logs": [f"{label} 开始执行..."],
        }, task_id)

        # Handle asctg config file
        config_file = None
        if needs_cfg and tool_id == "asctg":
            config_file = _find_config_file(project_id, project_name)
            if not config_file:
                mid = p_start + (p_end - p_start) // 2
                _send_callback(callback_url, {
                    "status": "GENERATING",
                    "subStatus": sub_status,
                    "progress": p_end,
                    "logs": [f"[ASCTG] [WARN] config_file 未找到，跳过 ASCTG"],
                }, task_id)
                continue

        # Get phase params
        import shlex
        phase_config = phase_params.get(phase_id, {})
        additional_args_str = phase_config.get("additionalArgs", "")
        additional_args = shlex.split(additional_args_str) if additional_args_str else []

        if additional_args:
            logger.info(f"[Pipeline] {label} extra args: {additional_args}")

        # Execute tool directly (no inter-service HTTP)
        try:
            result = executor.execute_in_project(
                tool_id=tool_id,
                project_name=tool_cwd,  # directory under projects_base_dir
                project_file=project_file,
                verbose=3,
                checker=None,
                config_file=config_file,
                compile=(False if tool_id == "ldp" else None),
                additional_args=additional_args,
            )
        except Exception as exc:
            result = {
                "success": False,
                "return_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "generated_files": [],
                "project_path": "",
                "message": str(exc),
            }

        # Collect log lines from stdout/stderr
        tool_logs: list[str] = []
        for line in (result.get("stdout") or "").splitlines():
            if line.strip():
                tool_logs.append(f"[{tool_id.upper()}] {line}")
        for line in (result.get("stderr") or "").splitlines():
            if line.strip():
                tool_logs.append(f"[{tool_id.upper()}] [STDERR] {line}")

        gen_files = result.get("generated_files", [])
        if gen_files:
            tool_logs.append(f"[{tool_id.upper()}] ✓ 生成文件: {len(gen_files)} 个")

        if result.get("project_path"):
            output_path = result["project_path"]

        success = result.get("success", False) and result.get("return_code", 1) == 0

        mid_progress = p_start + (p_end - p_start) // 2
        if tool_logs:
            _send_callback(callback_url, {
                "status": "GENERATING",
                "subStatus": sub_status,
                "progress": mid_progress,
                "logs": tool_logs,
            }, task_id)

        if success:
            _send_callback(callback_url, {
                "status": "GENERATING",
                "subStatus": sub_status,
                "progress": p_end,
                "logs": [f"{label} ✓ 执行成功"],
            }, task_id)
        else:
            had_failure = True
            rc = result.get("return_code", -1)
            fail_logs = [f"{label} [ERROR] 执行失败 (return_code={rc})"]

            if continue_on_error:
                fail_logs.append(f"[WARN] continueOnError=true，跳过 {tool_id} 继续下一阶段")
                _send_callback(callback_url, {
                    "status": "GENERATING",
                    "subStatus": sub_status,
                    "progress": p_end,
                    "logs": fail_logs,
                }, task_id)
            else:
                _send_callback(callback_url, {
                    "status": "FAILED",
                    "subStatus": sub_status,
                    "progress": p_end,
                    "outputPath": output_path,
                    "logs": fail_logs,
                }, task_id)
                logger.error(f"[Pipeline] FAILED at {tool_id}, aborting task {task_id}")
                return

    # ── Final callback ───────────────────────────────────────────────────────
    final_logs = [
        "✓ ECOA 代码生成流水线全部完成！",
        f"[SUCCESS] 产物路径: {output_path}",
    ]
    if had_failure and continue_on_error:
        final_logs.append("[WARN] 部分阶段失败已跳过（continueOnError=true）")

    logger.info(f"[Pipeline] COMPLETED task={task_id}, outputPath={output_path}")

    _send_callback(callback_url, {
        "status": "COMPLETED",
        "subStatus": "NONE",
        "progress": 100,
        "outputPath": output_path,
        "logs": final_logs,
    }, task_id)


# ---------------------------------------------------------------------------
# API Endpoint
# ---------------------------------------------------------------------------

@bp.route("/api/generate", methods=["POST"])
def trigger_generation():
    """
    Accept a generation request from the Java backend and run the ECOA
    toolchain pipeline in a background thread.

    Request JSON:
        {
            "taskId":         "uuid",
            "projectId":      "uuid",
            "stepsDir":       "/workspace/{id}/Steps",
            "outputDir":      "/workspace/{id}/src",
            "callbackUrl":    "http://sirius-web:8080/api/internal/tasks/{id}/status",
            "selectedPhases": ["EXVT", "MSCIGT_ASCTG", "CSMGVT", "LDP"],
            "continueOnError": false
        }
    """
    data = request.get_json(force=True, silent=True) or {}

    task_id     = data.get("taskId")
    project_id  = data.get("projectId")
    output_dir  = data.get("outputDir", "/workspace")
    callback_url = data.get("callbackUrl")
    selected_phases = data.get("selectedPhases", ["EXVT", "ASCTG", "MSCIGT", "CSMGVT", "LDP"])
    continue_on_error = bool(data.get("continueOnError", False))
    phase_params = data.get("phaseParams", {})

    if not task_id or not project_id or not callback_url:
        return jsonify({"success": False, "error": "taskId, projectId and callbackUrl are required"}), 400

    logger.info(f"[API] Generate accepted: task={task_id}, project={project_id}, "
                f"phases={selected_phases}, continueOnError={continue_on_error}, params={phase_params}")

    t = threading.Thread(
        target=_run_pipeline,
        args=(task_id, project_id, output_dir, callback_url, selected_phases, continue_on_error, phase_params),
        daemon=True,
    )
    t.start()

    return jsonify({"message": "Accepted", "taskId": task_id}), 202
