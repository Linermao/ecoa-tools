import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from app.utils.logger import setup_logger
from app.utils.xml_parser import parse_component_names

logger = setup_logger("app.services.asctg_service")
DEFAULT_ASCTG_RUNS_DIR = "/workspace/asctg_runs"


def _local_name(tag_name: str) -> str:
    """Get local xml tag name without namespace."""
    return tag_name.split("}", 1)[-1] if "}" in tag_name else tag_name


def generate_config_xml(components: list[str], output_path: str) -> str:
    """Generate ASCTG config.xml from selected component names."""
    if not components:
        raise ValueError("'components' must not be empty")

    root = ET.Element("asctg")
    components_node = ET.SubElement(root, "components")

    for name in components:
        component_node = ET.SubElement(components_node, "componentInstance")
        component_node.text = name

    output_file = Path(output_path)
    if output_file.parent and not output_file.parent.exists():
        raise FileNotFoundError(
            f"Output directory does not exist: {output_file.parent}"
        )

    tree = ET.ElementTree(root)
    tree.write(output_file, encoding="utf-8", xml_declaration=True)

    return str(output_file)


def create_asctg_config(
    composite_path: str,
    selected_components: list[str],
    output_path: str,
) -> str:
    """Validate selected components against composite and generate ASCTG config."""
    all_components = parse_component_names(composite_path)
    valid_components = set(all_components)

    invalid_components = [
        name for name in selected_components if name not in valid_components
    ]
    if invalid_components:
        invalid_list = ", ".join(sorted(set(invalid_components)))
        raise ValueError(
            f"Invalid component(s) not found in composite: {invalid_list}"
        )

    return generate_config_xml(selected_components, output_path)


def _generate_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"


def prepare_project_workspace(
    project_path: str,
    workspace_base_dir: Optional[str] = None,
    source_project_root: Optional[str] = None,
) -> dict:
    """
    Copy source ECOA project directory into a unique per-run workspace.

    Returns:
        {
            "workspace_root": ".../asctg_runs/<run_id>",
            "source_project_root": ".../examples/project_name",
            "project_dir": ".../asctg_runs/<run_id>/project_name",
            "project_path": ".../asctg_runs/<run_id>/project_name/<project_file>"
        }
    """
    source_project_file = Path(project_path).resolve()
    if not source_project_file.is_file():
        raise FileNotFoundError(f"Project file not found: {project_path}")

    if source_project_root:
        source_project_root_path = Path(source_project_root).resolve()
        if not source_project_root_path.is_dir():
            raise FileNotFoundError(
                f"Source project root directory not found: {source_project_root}"
            )
        if source_project_root_path not in source_project_file.parents:
            raise ValueError(
                f"Project file '{project_path}' is not under source project root "
                f"'{source_project_root}'"
            )
    else:
        source_project_root_path = source_project_file.parent

    workspace_base = Path(
        workspace_base_dir
        or os.environ.get("ASCTG_WORKSPACE_BASE", DEFAULT_ASCTG_RUNS_DIR)
    )
    run_id = _generate_run_id()
    workspace_root = workspace_base / run_id
    workspace_project_dir = workspace_root / source_project_root_path.name

    logger.info(
        "Preparing ASCTG workspace (source=%s, run_id=%s)",
        source_project_root_path,
        run_id,
    )

    workspace_root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source_project_root_path, workspace_project_dir)

    copied_project_path = workspace_project_dir / source_project_file.relative_to(
        source_project_root_path
    )
    if not copied_project_path.is_file():
        raise FileNotFoundError(
            f"Copied project file not found in workspace: {copied_project_path}"
        )

    return {
        "workspace_root": str(workspace_root),
        "source_project_root": str(source_project_root_path),
        "project_dir": str(workspace_project_dir),
        "project_path": str(copied_project_path),
    }


def infer_project_paths_from_steps_dir(steps_dir: str, project_id: Optional[str] = None) -> dict:
    """
    Infer project/composite/source-root paths from workspace Steps directory.
    """
    steps_path = Path(steps_dir).resolve()
    if not steps_path.is_dir():
        raise FileNotFoundError(f"Steps directory not found: {steps_dir}")

    project_workspace_root = steps_path.parent
    inferred_project_id = project_workspace_root.name
    if project_id and project_id != inferred_project_id:
        logger.warning(
            "project_id mismatch: request=%s, inferred_from_steps=%s",
            project_id,
            inferred_project_id,
        )

    project_candidates = sorted(steps_path.rglob("*.project.xml"))
    if not project_candidates:
        raise FileNotFoundError(
            f"No '*.project.xml' file found under Steps directory: {steps_path}"
        )
    project_path = project_candidates[0]

    composite_path: Optional[Path] = None
    try:
        tree = ET.parse(project_path)
        root = tree.getroot()
        for element in root.iter():
            if _local_name(element.tag) == "initialAssembly" and element.text:
                candidate = (project_path.parent / element.text.strip()).resolve()
                if candidate.is_file():
                    composite_path = candidate
                    break
    except ET.ParseError as exc:
        raise ValueError(
            f"Failed to parse project XML while inferring composite: {project_path}: {exc}"
        ) from exc

    if composite_path is None:
        composite_candidates = sorted((steps_path / "3-InitialAssembly").rglob("*.composite"))
        if composite_candidates:
            composite_path = composite_candidates[0]

    if composite_path is None:
        raise FileNotFoundError(
            f"No composite file found in Steps directory: {steps_path}"
        )

    return {
        "project_id": project_id or inferred_project_id,
        "project_workspace_root": str(project_workspace_root),
        "steps_dir": str(steps_path),
        "source_project_root": str(steps_path),
        "project_path": str(project_path),
        "composite_path": str(composite_path),
    }


def map_path_to_workspace(
    source_path: str,
    source_project_root: str,
    workspace_project_dir: str,
) -> str:
    """Map a source path under source project root into copied workspace project."""
    source = Path(source_path).resolve()
    source_root = Path(source_project_root).resolve()
    workspace_root = Path(workspace_project_dir).resolve()

    try:
        relative_path = source.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            f"Path '{source_path}' is not under source project root '{source_project_root}'"
        ) from exc

    mapped = workspace_root / relative_path
    if not mapped.exists():
        raise FileNotFoundError(f"Mapped path not found in workspace: {mapped}")

    return str(mapped)


def run_asctg(
    project_path: str,
    config_path: str,
    output_dir: Optional[str] = None,
    force: bool = True,
) -> dict:
    """Run ecoa-asctg with project and config paths."""
    project_file = Path(project_path)
    config_file = Path(config_path)

    if not project_file.is_file():
        error_message = f"Project file not found: {project_path}"
        logger.error(error_message)
        return {
            "success": False,
            "error": error_message,
            "stderr": "",
            "stdout": "",
        }

    if not config_file.is_file():
        error_message = f"Config file not found: {config_path}"
        logger.error(error_message)
        return {
            "success": False,
            "error": error_message,
            "stderr": "",
            "stdout": "",
        }

    cmd = [
        "ecoa-asctg",
        "-p",
        str(project_file),
        "-c",
        str(config_file),
        "-k",
        "ecoa-exvt",
    ]
    if output_dir:
        output_dir_path = Path(output_dir).resolve()
        # Avoid deleting project source by passing an output directory that is
        # the same as (or a parent of) project directory when -f is enabled.
        if project_file.resolve().parent == output_dir_path or output_dir_path in project_file.resolve().parents:
            return {
                "success": False,
                "error": (
                    "Invalid output_dir: must not be project directory or its parent "
                    "when force mode is enabled"
                ),
                "stderr": "",
                "stdout": "",
            }
        cmd.extend(["-o", str(output_dir)])
    if force:
        cmd.append("-f")
    logger.info("Running ASCTG command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.exception("Failed to execute ecoa-asctg: %s", exc)
        return {
            "success": False,
            "error": f"Failed to execute ecoa-asctg: {exc}",
            "stderr": str(exc),
            "stdout": "",
        }

    response = {
        "success": result.returncode == 0,
        "return_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }

    if response["success"]:
        logger.info("ASCTG finished successfully (return_code=%s)", result.returncode)
        return response

    logger.error(
        "ASCTG failed (return_code=%s): %s",
        result.returncode,
        (result.stderr or "").strip(),
    )
    return {
        "success": False,
        "error": f"ecoa-asctg failed with return code {result.returncode}",
        "stderr": result.stderr,
        "return_code": result.returncode,
        "stdout": result.stdout,
    }


def execute_asctg(
    composite_path: str,
    selected_components: list[str],
    project_path: str,
    output_path: Optional[str] = None,
    workspace_base_dir: Optional[str] = None,
    source_project_root: Optional[str] = None,
    copy_to_workspace: bool = True,
) -> dict:
    """
    End-to-end ASCTG execution on project.

    Flow:
      1) (Optional) Copy source project to unique workspace
      2) Map source composite path into workspace
      3) Generate config under workspace root
      4) Run ecoa-asctg on project
    """
    workspace_info: dict = {}
    config_path = ""

    try:
        if copy_to_workspace:
            workspace_info = prepare_project_workspace(
                project_path,
                workspace_base_dir=workspace_base_dir,
                source_project_root=source_project_root,
            )
            workspace_root = Path(workspace_info["workspace_root"])
            workspace_project_dir = workspace_info["project_dir"]
            mapped_project_path = workspace_info["project_path"]
            mapped_source_root = workspace_info["source_project_root"]
        else:
            # Use original paths directly
            workspace_project_root = Path(project_path).resolve().parent
            workspace_info = {
                "workspace_root": str(workspace_project_root),
                "project_dir": str(workspace_project_root),
                "project_path": project_path,
                "source_project_root": source_project_root or str(workspace_project_root),
            }
            workspace_root = workspace_project_root
            workspace_project_dir = workspace_info["project_dir"]
            mapped_project_path = project_path
            mapped_source_root = workspace_info["source_project_root"]

        mapped_composite_path = map_path_to_workspace(
            source_path=composite_path,
            source_project_root=mapped_source_root,
            workspace_project_dir=workspace_project_dir,
        )

        # Keep config isolated to workspace to avoid polluting source project.
        config_filename = "config.xml"
        if output_path:
            config_filename = Path(output_path).name or "config.xml"
        config_path = str(workspace_root / config_filename)

        generated_config_path = create_asctg_config(
            composite_path=mapped_composite_path,
            selected_components=selected_components,
            output_path=config_path,
        )

        run_result = run_asctg(
            project_path=mapped_project_path,
            config_path=generated_config_path,
            force=True,
        )

        run_result.update(
            {
                "workspace_root": workspace_info["workspace_root"],
                "project_path": mapped_project_path,
                "config_path": generated_config_path,
                "composite_path": mapped_composite_path,
            }
        )
        return run_result

    except Exception as exc:
        logger.exception("ASCTG execute flow failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "stderr": str(exc),
            "stdout": "",
            "workspace_root": workspace_info.get("workspace_root", ""),
            "project_path": workspace_info.get("project_path", ""),
            "config_path": config_path,
        }


def execute_asctg_from_steps_dir(
    project_id: str,
    steps_dir: str,
    selected_components: list[str],
) -> dict:
    """Execute ASCTG from workspace project context (project_id + steps_dir)."""
    context = infer_project_paths_from_steps_dir(steps_dir=steps_dir, project_id=project_id)
    workspace_base_dir = str(Path(context["project_workspace_root"]) / "asctg_runs")
    result = execute_asctg(
        composite_path=context["composite_path"],
        selected_components=selected_components,
        project_path=context["project_path"],
        workspace_base_dir=workspace_base_dir,
        source_project_root=context["source_project_root"],
        copy_to_workspace=False,
    )
    result.update(
        {
            "project_id": context["project_id"],
            "steps_dir": context["steps_dir"],
        }
    )
    return result


def build_asctg_logs(
    project_id: str,
    steps_dir: str,
    selected_components: list[str],
    result: dict,
) -> list[str]:
    """Build detailed frontend logs for an ASCTG execution."""
    logs: list[str] = [
        f"[ASCTG] Project: {project_id}",
        f"[ASCTG] Steps dir: {steps_dir}",
        f"[ASCTG] Selected components: {', '.join(selected_components) if selected_components else '(auto config mode)'}",
    ]

    if result.get("composite_path"):
        logs.append(f"[ASCTG] Composite: {result['composite_path']}")
    if result.get("config_path"):
        logs.append(f"[ASCTG] Config: {result['config_path']}")
    if result.get("workspace_root"):
        logs.append(f"[ASCTG] Workspace: {result['workspace_root']}")

    for line in (result.get("stdout") or "").splitlines():
        if line.strip():
            logs.append(f"[ASCTG] {line}")
    for line in (result.get("stderr") or "").splitlines():
        if line.strip():
            logs.append(f"[ASCTG] [STDERR] {line}")

    if result.get("return_code") is not None:
        logs.append(f"[ASCTG] Return code: {result['return_code']}")

    if result.get("error"):
        logs.append(f"[ASCTG] [ERROR] {result['error']}")

    return logs
