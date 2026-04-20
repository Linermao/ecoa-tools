import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree


DEBUG_START_PORT = 2000
LOCAL_LAUNCH_NAME = "Debug platform"
COMPOUND_NAME = "Attach distributed ECOA"
COMPOSE_PROJECT_NAME = "ecoa-distributed-debug"
COMPOSE_FILENAME = "distributed-debug.compose.yml"
START_SCRIPT_FILENAME = "start-distributed-debug.sh"
STOP_SCRIPT_FILENAME = "stop-distributed-debug.sh"
STATUS_SCRIPT_FILENAME = "status-distributed-debug.sh"
CONTAINER_PROJECT_ROOT = "/workspace/project"


@dataclass(frozen=True)
class DebugProcess:
    name: str
    node_id: str
    host: str
    port: int
    service_name: str


@dataclass(frozen=True)
class DebugTopology:
    integration_dir: str
    docker_subnet: str
    processes: List[DebugProcess]
    is_distributed: bool


def container_binary_dir(build_dir: str) -> str:
    """Return the in-container path to the generated binary directory."""
    build_path = Path(build_dir)
    build_parts = build_path.parts
    output_index = next(
        (index for index, part in enumerate(build_parts) if part.lower().startswith("6-output")),
        None,
    )
    if output_index is None:
        raise ValueError(f"Build directory is not under a 6-output directory: {build_dir}")

    relative_build_dir = Path(*build_parts[output_index:])
    return f"{CONTAINER_PROJECT_ROOT}/{relative_build_dir.as_posix()}/bin"


def gdbserver_command(build_dir: str, process: DebugProcess) -> str:
    """Return the shell command used to start gdbserver for a process."""
    binary_dir = container_binary_dir(build_dir)
    library_dir = f"{Path(binary_dir).parent.as_posix()}/lib"
    return (
        f"mkdir -p {binary_dir}/../logs && "
        f"cd {binary_dir} && "
        f"export LD_LIBRARY_PATH={library_dir}:${{LD_LIBRARY_PATH:-}} && "
        f"nohup gdbserver 0.0.0.0:{process.port} ./{process.name} "
        f"> ../logs/{process.name}.gdbserver.log 2>&1 &"
    )


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _sanitize_service_name(node_id: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in node_id.lower()).strip("-")


def _yaml_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _service_name_by_host(node_hosts: Dict[str, str]) -> Dict[str, str]:
    grouped_nodes: Dict[str, List[str]] = {}
    for node_id, host in node_hosts.items():
        grouped_nodes.setdefault(host, []).append(node_id)

    service_names: Dict[str, str] = {}
    for host, node_ids in grouped_nodes.items():
        preferred_node_id = next((node_id for node_id in node_ids if node_id != "main"), node_ids[0])
        service_names[host] = f"ecoa-{_sanitize_service_name(preferred_node_id)}"
    return service_names


def _find_integration_dir(project_path: str) -> Optional[Path]:
    root = Path(project_path)
    direct_candidates = [candidate for candidate in root.iterdir() if candidate.is_dir() and candidate.name.startswith("5-Integration")]
    if direct_candidates:
        return sorted(direct_candidates)[0]

    recursive_candidates = [candidate for candidate in root.rglob("*") if candidate.is_dir() and candidate.name.startswith("5-Integration")]
    if recursive_candidates:
        return sorted(recursive_candidates)[0]
    return None


def _resolve_project_file_path(project_path: str, project_file: Optional[str]) -> Optional[Path]:
    if not project_file:
        return None

    candidate = Path(project_file)
    if not candidate.is_absolute():
        candidate = Path(project_path) / project_file
    if candidate.exists():
        return candidate

    for existing_project_file in sorted(Path(project_path).rglob("*.project.xml")):
        if existing_project_file.name == project_file:
            return existing_project_file
    return None


def _project_deployment_file(project_file_path: Path) -> Optional[Path]:
    try:
        root = ElementTree.parse(project_file_path).getroot()
    except ElementTree.ParseError:
        return None

    for element in root:
        if _local_name(element.tag) != "deploymentSchema":
            continue
        deployment_schema = (element.text or "").strip()
        if not deployment_schema:
            continue

        deployment_file = project_file_path.parent / deployment_schema
        if deployment_file.exists():
            return deployment_file
    return None


def _deployment_candidates(project_path: str, integration_dir: Path, project_file: Optional[str]) -> List[Path]:
    requested_project_file = _resolve_project_file_path(project_path, project_file)
    if requested_project_file is not None:
        requested_deployment = _project_deployment_file(requested_project_file)
        if requested_deployment is not None:
            return [requested_deployment]

    deployment_candidates: List[Path] = []
    seen_candidates = set()
    for project_xml in sorted(Path(project_path).rglob("*.project.xml")):
        deployment_file = _project_deployment_file(project_xml)
        if deployment_file is None:
            continue
        deployment_key = str(deployment_file)
        if deployment_key in seen_candidates:
            continue
        seen_candidates.add(deployment_key)
        deployment_candidates.append(deployment_file)

    if deployment_candidates:
        return deployment_candidates

    return sorted(integration_dir.glob("*.deployment.xml"))


def _built_debug_binaries(build_dir: str) -> set[str]:
    bin_dir = Path(build_dir) / "bin"
    if not bin_dir.exists():
        return set()

    return {
        file_path.name
        for file_path in bin_dir.iterdir()
        if file_path.is_file() and file_path.name.startswith("PD_")
    }


def _parse_deployment_processes(deployment_file: Path) -> List[tuple[str, str]]:
    processes: List[tuple[str, str]] = []

    try:
        root = ElementTree.parse(deployment_file).getroot()
    except ElementTree.ParseError:
        return processes

    if _local_name(root.tag) != "deployment":
        return processes

    for element in root:
        if _local_name(element.tag) != "protectionDomain":
            continue

        name = element.get("name")
        if not name:
            continue

        execute_on = next((child for child in element if _local_name(child.tag) == "executeOn"), None)
        if execute_on is None:
            continue

        node_id = execute_on.get("computingNode")
        if not node_id:
            continue

        processes.append((f"PD_{name}", node_id))

    return processes


def _find_deployment_file(
    project_path: str,
    integration_dir: Path,
    build_dir: str,
    project_file: Optional[str],
) -> Optional[Path]:
    deployment_candidates = _deployment_candidates(project_path, integration_dir, project_file)
    if not deployment_candidates:
        return None

    if len(deployment_candidates) == 1:
        return deployment_candidates[0]

    built_binaries = _built_debug_binaries(build_dir)
    if not built_binaries:
        return deployment_candidates[0]

    def score(candidate: Path) -> tuple[int, int, int]:
        expected_binaries = {process_name for process_name, _node_id in _parse_deployment_processes(candidate)}
        matches = len(expected_binaries & built_binaries)
        missing = len(expected_binaries - built_binaries)
        extras = len(built_binaries - expected_binaries)
        return (matches, -missing, -extras)

    return max(deployment_candidates, key=score)


def _parse_nodes_deployment(integration_dir: Path) -> Dict[str, str]:
    nodes_path = next(iter(sorted(integration_dir.rglob("nodes_deployment.xml"))), None)
    if nodes_path is None:
        return {}

    root = ElementTree.parse(nodes_path).getroot()
    nodes: Dict[str, str] = {}
    for element in root:
        if _local_name(element.tag) != "logicalComputingNode":
            continue
        node_id = element.get("id")
        ip_address = element.get("ipAddress")
        if node_id and ip_address:
            nodes[node_id] = ip_address
    return nodes


def _derive_docker_subnet(addresses: List[str]) -> str:
    if not addresses:
        return ""

    octets = [address.split(".") for address in addresses]
    if all(parts[:3] == octets[0][:3] for parts in octets):
        return ".".join(octets[0][:3] + ["0"]) + "/24"
    if all(parts[:2] == octets[0][:2] for parts in octets):
        return ".".join(octets[0][:2] + ["0", "0"]) + "/16"
    if all(parts[:1] == octets[0][:1] for parts in octets):
        return ".".join(octets[0][:1] + ["0", "0", "0"]) + "/8"
    raise ValueError("All nodes must share a common network prefix for Docker bridge generation")


def collect_debug_topology(project_path: str, build_dir: str, project_file: Optional[str] = None) -> Optional[DebugTopology]:
    integration_dir = _find_integration_dir(project_path)
    if integration_dir is None:
        return None

    node_hosts = _parse_nodes_deployment(integration_dir)
    if not node_hosts:
        return None

    deployment_file = _find_deployment_file(project_path, integration_dir, build_dir, project_file)
    if deployment_file is None:
        return None

    pd_processes = _parse_deployment_processes(deployment_file)
    if not pd_processes:
        return None

    platform_host = node_hosts.get("main")
    if not platform_host:
        return None

    service_names = _service_name_by_host(node_hosts)
    processes = [
        DebugProcess(
            name="platform",
            node_id="main",
            host=platform_host,
            port=DEBUG_START_PORT,
            service_name=service_names[platform_host],
        )
    ]

    for index, (process_name, node_id) in enumerate(pd_processes, start=1):
        host = node_hosts.get(node_id)
        if not host:
            continue
        processes.append(
            DebugProcess(
                name=process_name,
                node_id=node_id,
                host=host,
                port=DEBUG_START_PORT + index,
                service_name=service_names[host],
            )
        )

    if len(processes) == 1:
        return None

    docker_subnet = _derive_docker_subnet([process.host for process in processes])
    return DebugTopology(
        integration_dir=str(integration_dir),
        docker_subnet=docker_subnet,
        processes=processes,
        is_distributed=True,
    )


def _relative_build_dir(target_dir: str, build_dir: str) -> str:
    try:
        return os.path.relpath(build_dir, target_dir).replace("\\", "/")
    except ValueError:
        return build_dir.replace("\\", "/")


def _local_launch_config(target_dir: str, build_dir: str) -> dict:
    rel_build_dir = _relative_build_dir(target_dir, build_dir)
    return {
        "name": LOCAL_LAUNCH_NAME,
        "type": "cppdbg",
        "request": "launch",
        "program": f"${{workspaceFolder}}/{rel_build_dir}/bin/platform",
        "cwd": f"${{workspaceFolder}}/{rel_build_dir}/bin",
        "args": [],
        "stopAtEntry": True,
        "MIMode": "gdb",
    }


def _distributed_launch_config(target_dir: str, build_dir: str, process: DebugProcess) -> dict:
    rel_build_dir = _relative_build_dir(target_dir, build_dir)
    binary_name = process.name
    if process.name == "platform":
        name = "Attach platform (main)"
    else:
        name = f"Attach {process.name.replace('PD_', 'PD ')} ({process.node_id})"

    return {
        "name": name,
        "type": "cppdbg",
        "request": "launch",
        "program": f"${{workspaceFolder}}/{rel_build_dir}/bin/{binary_name}",
        "cwd": f"${{workspaceFolder}}/{rel_build_dir}/bin",
        "args": [],
        "stopAtEntry": False,
        "MIMode": "gdb",
        "miDebuggerPath": "/usr/bin/gdb",
        "miDebuggerServerAddress": f"{process.host}:{process.port}",
        "externalConsole": False,
    }


def _compose_yaml(
    build_dir: str,
    topology: DebugTopology,
    project_mount_source: str = "..",
    debug_image: Optional[str] = None,
    compose_project_name: str = COMPOSE_PROJECT_NAME,
    network_name: Optional[str] = None,
) -> str:
    rel_bin_dir = Path(build_dir).name
    del rel_bin_dir
    binary_dir = container_binary_dir(build_dir)
    image_reference = debug_image or "${ECOA_DISTRIBUTED_DEBUG_IMAGE:-sirius-web-code-server:latest}"
    compose_network_name = network_name or f"{compose_project_name}_ecoa_debug_net"
    lines = [
        f"name: {compose_project_name}",
        'services:',
    ]

    unique_services = {}
    for process in topology.processes:
        existing = unique_services.get(process.service_name)
        if existing is None or (existing[0] == "main" and process.node_id != "main"):
            unique_services[process.service_name] = (process.node_id, process.host)

    for service_name, (node_id, host) in unique_services.items():
        lines.extend(
            [
                f"  {service_name}:",
                f"    image: {image_reference}",
                '    command: ["bash", "-lc", "sleep infinity"]',
                f'    working_dir: "{binary_dir}"',
                '    volumes:',
                '      - type: bind',
                f"        source: {_yaml_single_quote(project_mount_source)}",
                f'        target: "{CONTAINER_PROJECT_ROOT}"',
                '    environment:',
                f'      ECOA_NODE_ID: "{node_id}"',
                '    networks:',
                '      ecoa_debug_net:',
                f'        ipv4_address: {host}',
            ]
        )

    lines.extend(
        [
            'networks:',
            '  ecoa_debug_net:',
            f'    name: {compose_network_name}',
            '    driver: bridge',
            '    ipam:',
            '      config:',
            f'        - subnet: {topology.docker_subnet}',
        ]
    )
    return "\n".join(lines) + "\n"


def render_distributed_debug_compose(
    build_dir: str,
    topology: DebugTopology,
    project_mount_source: str = "..",
    debug_image: Optional[str] = None,
    compose_project_name: str = COMPOSE_PROJECT_NAME,
    network_name: Optional[str] = None,
) -> str:
    """Render the distributed debug compose definition."""
    return _compose_yaml(
        build_dir,
        topology,
        project_mount_source=project_mount_source,
        debug_image=debug_image,
        compose_project_name=compose_project_name,
        network_name=network_name,
    )


def _api_script(endpoint: str, method: str = "POST") -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        'export ECOA_DISTRIBUTED_DEBUG_API_URL="${ECOA_DISTRIBUTED_DEBUG_API_URL:-http://ecoa-tools:5000}"',
        "",
        "python - <<'PY'",
        "import json",
        "import os",
        "import sys",
        "import urllib.error",
        "import urllib.parse",
        "import urllib.request",
        "",
        'api_base_url = os.environ["ECOA_DISTRIBUTED_DEBUG_API_URL"].rstrip("/")',
        'payload = {',
        '    "target_dir": os.environ.get("ECOA_DISTRIBUTED_DEBUG_TARGET_DIR", os.getcwd()),',
        '    "client_container": os.environ.get("ECOA_DISTRIBUTED_DEBUG_CLIENT_CONTAINER", "code-server"),',
        '}',
        "",
        f'if "{method}" == "GET":',
        '    query = urllib.parse.urlencode(payload)',
        f'    request = urllib.request.Request(f"{{api_base_url}}{endpoint}?{{query}}", method="GET")',
        "else:",
        '    body = json.dumps(payload).encode("utf-8")',
        f'    request = urllib.request.Request(f"{{api_base_url}}{endpoint}", data=body, headers={{"Content-Type": "application/json"}}, method="{method}")',
        "",
        "try:",
        "    with urllib.request.urlopen(request) as response:",
        '        sys.stdout.write(response.read().decode("utf-8"))',
        '        sys.stdout.write("\\n")',
        "except urllib.error.HTTPError as exc:",
        '    error_body = exc.read().decode("utf-8", errors="replace")',
        '    sys.stderr.write(error_body or str(exc))',
        '    sys.stderr.write("\\n")',
        "    raise",
        "PY",
        "",
    ]
    return "\n".join(lines).strip() + "\n"


def _start_script() -> str:
    return _api_script("/api/distributed-debug/start")


def _stop_script() -> str:
    return _api_script("/api/distributed-debug/stop")


def _status_script() -> str:
    return _api_script("/api/distributed-debug/status", method="GET")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def write_distributed_debug_launch_json(target_dir: str, build_dir: str, topology: Optional[DebugTopology]) -> str:
    target_path = Path(target_dir)
    vscode_dir = target_path / ".vscode"
    vscode_dir.mkdir(parents=True, exist_ok=True)

    launch_json_path = vscode_dir / "launch.json"
    launch_configurations = [_local_launch_config(target_dir, build_dir)]
    compounds = []

    if topology and topology.is_distributed:
        distributed_configs = [_distributed_launch_config(target_dir, build_dir, process) for process in topology.processes]
        launch_configurations.extend(distributed_configs)
        compounds = [
            {
                "name": COMPOUND_NAME,
                "configurations": [config["name"] for config in distributed_configs],
            }
        ]

    launch_json_path.write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": launch_configurations,
                "compounds": compounds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(launch_json_path)


def write_distributed_debug_assets(target_dir: str, build_dir: str, topology: Optional[DebugTopology]) -> Dict[str, str]:
    target_path = Path(target_dir)
    vscode_dir = target_path / ".vscode"
    vscode_dir.mkdir(parents=True, exist_ok=True)

    result = {"launch_json": write_distributed_debug_launch_json(target_dir, build_dir, topology)}

    if not topology or not topology.is_distributed:
        return result

    compose_path = vscode_dir / COMPOSE_FILENAME
    start_script_path = vscode_dir / START_SCRIPT_FILENAME
    stop_script_path = vscode_dir / STOP_SCRIPT_FILENAME
    status_script_path = vscode_dir / STATUS_SCRIPT_FILENAME

    compose_path.write_text(render_distributed_debug_compose(build_dir, topology), encoding="utf-8")
    _write_executable(start_script_path, _start_script())
    _write_executable(stop_script_path, _stop_script())
    _write_executable(status_script_path, _status_script())

    result.update(
        {
            "docker_compose": str(compose_path),
            "start_script": str(start_script_path),
            "stop_script": str(stop_script_path),
            "status_script": str(status_script_path),
        }
    )
    return result
