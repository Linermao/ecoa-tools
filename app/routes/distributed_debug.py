"""API routes for distributed debug orchestration."""

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, InternalServerError, NotFound

from app.services.distributed_debug_runtime import DistributedDebugRuntime, DistributedDebugRuntimeError
from app.utils.logger import setup_logger

bp = Blueprint("distributed_debug", __name__, url_prefix="/api/distributed-debug")
logger = setup_logger("app.routes.distributed_debug")
runtime_service = DistributedDebugRuntime()


@bp.route("/start", methods=["POST"])
def start_distributed_debug() -> tuple:
    """Start the distributed debug compose stack and attach the IDE container."""
    data = _require_json_body()
    target_dir = data.get("target_dir")
    client_container = data.get("client_container")

    if not target_dir:
        raise BadRequest("'target_dir' is required")

    try:
        result = runtime_service.start(target_dir, client_container=client_container)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    except DistributedDebugRuntimeError as exc:
        logger.error("Distributed debug start failed: %s", exc)
        raise InternalServerError(str(exc)) from exc

    return jsonify(result), 200


@bp.route("/stop", methods=["POST"])
def stop_distributed_debug() -> tuple:
    """Stop the distributed debug compose stack and detach the IDE container."""
    data = _require_json_body()
    target_dir = data.get("target_dir")
    client_container = data.get("client_container")

    if not target_dir:
        raise BadRequest("'target_dir' is required")

    try:
        result = runtime_service.stop(target_dir, client_container=client_container)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    except DistributedDebugRuntimeError as exc:
        logger.error("Distributed debug stop failed: %s", exc)
        raise InternalServerError(str(exc)) from exc

    return jsonify(result), 200


@bp.route("/status", methods=["GET"])
def distributed_debug_status() -> tuple:
    """Return distributed debug status for the given Steps workspace."""
    target_dir = request.args.get("target_dir", type=str)
    client_container = request.args.get("client_container", type=str)

    if not target_dir:
        raise BadRequest("Query parameter 'target_dir' is required")

    try:
        result = runtime_service.status(target_dir, client_container=client_container)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    except DistributedDebugRuntimeError as exc:
        logger.error("Distributed debug status failed: %s", exc)
        raise InternalServerError(str(exc)) from exc

    return jsonify(result), 200


@bp.route("/check-docker", methods=["GET"])
def check_docker() -> tuple:
    """Check Docker daemon connectivity from the ecoa-tools container."""
    try:
        docker_host = runtime_service._ensure_docker_available()
        return jsonify({"available": True, "docker_host": docker_host}), 200
    except DistributedDebugRuntimeError as exc:
        return jsonify({"available": False, "error": str(exc)}), 200


def _require_json_body() -> dict:
    if not request.is_json:
        raise BadRequest("Request must be JSON")
    return request.get_json(silent=True) or {}
