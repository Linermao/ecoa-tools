"""ASCTG helper routes."""

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, NotFound

from app.services.asctg_service import (
    create_asctg_config,
    execute_asctg,
    infer_project_paths_from_steps_dir,
    run_asctg,
)
from app.utils.logger import setup_logger
from app.utils.xml_parser import parse_component_names

bp = Blueprint("asctg", __name__, url_prefix="/asctg")
logger = setup_logger("app.routes.asctg")


@bp.route("/components", methods=["GET"])
def get_components() -> tuple:
    """Return available component names from composite/workspace context."""
    composite_path = request.args.get("composite_path", type=str)
    steps_dir = request.args.get("steps_dir", type=str)
    project_id = request.args.get("project_id", type=str)

    if not composite_path and not steps_dir:
        raise BadRequest(
            "Either query parameter 'composite_path' or 'steps_dir' is required"
        )

    try:
        if steps_dir:
            context = infer_project_paths_from_steps_dir(
                steps_dir=steps_dir,
                project_id=project_id,
            )
            composite_path = context["composite_path"]
        else:
            context = None

        components = parse_component_names(composite_path)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc

    response = {"success": True, "components": components}
    if steps_dir and context:
        response.update(
            {
                "project_id": context["project_id"],
                "steps_dir": context["steps_dir"],
                "composite_path": context["composite_path"],
            }
        )
    return jsonify(response), 200


@bp.route("/config", methods=["POST"])
def generate_asctg_config() -> tuple:
    """Validate selected components and generate ASCTG config.xml."""
    if not request.is_json:
        raise BadRequest("Request must be JSON")

    data = request.get_json(silent=True) or {}
    composite_path = data.get("composite_path")
    selected_components = data.get("selected_components")
    output_path = data.get("output_path")

    if not composite_path:
        raise BadRequest("'composite_path' is required")
    if selected_components is None:
        raise BadRequest("'selected_components' is required")
    if not isinstance(selected_components, list):
        raise BadRequest("'selected_components' must be a list of strings")
    if not all(isinstance(component, str) for component in selected_components):
        raise BadRequest("'selected_components' must contain only strings")
    if not output_path:
        raise BadRequest("'output_path' is required")

    try:
        config_path = create_asctg_config(
            composite_path=composite_path,
            selected_components=selected_components,
            output_path=output_path,
        )
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc

    return jsonify(
        {
            "success": True,
            "config_path": config_path,
            "message": "config.xml generated successfully",
        }
    ), 200


@bp.route("/run", methods=["POST"])
def run_asctg_endpoint() -> tuple:
    """Run ecoa-asctg using provided project and config files."""
    if not request.is_json:
        raise BadRequest("Request must be JSON")

    data = request.get_json(silent=True) or {}
    project_path = data.get("project_path")
    config_path = data.get("config_path")

    if not project_path:
        raise BadRequest("'project_path' is required")
    if not config_path:
        raise BadRequest("'config_path' is required")

    logger.info("API: ASCTG run requested (project=%s, config=%s)", project_path, config_path)
    result = run_asctg(project_path=project_path, config_path=config_path)

    if not result.get("success", False):
        return jsonify(result), 400

    return jsonify(result), 200


@bp.route("/execute", methods=["POST"])
def execute_asctg_endpoint() -> tuple:
    """Generate config and run ecoa-asctg in one request."""
    if not request.is_json:
        raise BadRequest("Request must be JSON")

    data = request.get_json(silent=True) or {}
    composite_path = data.get("composite_path")
    selected_components = data.get("selected_components")
    project_path = data.get("project_path")
    output_path = data.get("output_path")

    if not composite_path:
        raise BadRequest("'composite_path' is required")
    if selected_components is None:
        raise BadRequest("'selected_components' is required")
    if not isinstance(selected_components, list):
        raise BadRequest("'selected_components' must be a list of strings")
    if not all(isinstance(component, str) for component in selected_components):
        raise BadRequest("'selected_components' must contain only strings")
    if not project_path:
        raise BadRequest("'project_path' is required")

    logger.info(
        "API: ASCTG execute requested (composite=%s, project=%s)",
        composite_path,
        project_path,
    )
    result = execute_asctg(
        composite_path=composite_path,
        selected_components=selected_components,
        project_path=project_path,
        output_path=output_path,
    )

    if not result.get("success", False):
        return jsonify(result), 400

    return jsonify(result), 200
