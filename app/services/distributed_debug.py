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
COMPOSE_FILENAME = "distributed-debug.compose.yml"
START_SCRIPT_FILENAME = "start-distributed-debug.sh"
STOP_SCRIPT_FILENAME = "stop-distributed-debug.sh"
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


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _sanitize_service_name(node_id: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in node_id.lower()).strip("-")


def _find_integration_dir(project_path: str) -> Optional[Path]:
    root = Path(project_path)
    direct_candidates = [candidate for candidate in root.iterdir() if candidate.is_dir() and candidate.name.startswith("5-Integration")]
    if direct_candidates:
        return sorted(direct_candidates)[0]

    recursive_candidates = [candidate for candidate in root.rglob("*") if candidate.is_dir() and candidate.name.startswith("5-Integration")]
    if recursive_candidates:
        return sorted(recursive_candidates)[0]
    return None


def _parse_deployment_processes(integration_dir: Path) -> List[tuple[str, str]]:
    processes: List[tuple[str, str]] = []

    for xml_path in sorted(integration_dir.rglob("*.xml")):
        try:
            root = ElementTree.parse(xml_path).getroot()
        except ElementTree.ParseError:
            continue

        if _local_name(root.tag) != "deployment":
            continue

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


def collect_debug_topology(project_path: str, build_dir: str) -> Optional[DebugTopology]:
    del build_dir
    integration_dir = _find_integration_dir(project_path)
    if integration_dir is None:
        return None

    node_hosts = _parse_nodes_deployment(integration_dir)
    if not node_hosts:
        return None

    pd_processes = _parse_deployment_processes(integration_dir)
    if not pd_processes:
        return None

    platform_host = node_hosts.get("main")
    if not platform_host:
        return None

    processes = [
        DebugProcess(
            name="platform",
            node_id="main",
            host=platform_host,
            port=DEBUG_START_PORT,
            service_name="ecoa-main",
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
                service_name=f"ecoa-{_sanitize_service_name(node_id)}",
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


def _compose_yaml(build_dir: str, topology: DebugTopology) -> str:
    rel_bin_dir = Path(build_dir).name
    del rel_bin_dir
    binary_dir = f"{CONTAINER_PROJECT_ROOT}/{Path(build_dir).relative_to(Path(build_dir).parents[1]).as_posix()}/bin"
    lines = [
        'name: ecoa-distributed-debug',
        'services:',
    ]

    unique_services = {}
    for process in topology.processes:
        unique_services[process.service_name] = (process.node_id, process.host)

    for service_name, (node_id, host) in unique_services.items():
        lines.extend(
            [
                f"  {service_name}:",
                '    image: ${ECOA_DISTRIBUTED_DEBUG_IMAGE:-sirius-web-code-server:latest}',
                '    command: ["bash", "-lc", "sleep infinity"]',
                f'    working_dir: "{binary_dir}"',
                '    volumes:',
                f'      - "..:{CONTAINER_PROJECT_ROOT}"',
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
            '    driver: bridge',
            '    ipam:',
            '      config:',
            f'        - subnet: {topology.docker_subnet}',
        ]
    )
    return "\n".join(lines) + "\n"


def _start_script(build_dir: str, topology: DebugTopology) -> str:
    binary_dir = f"{CONTAINER_PROJECT_ROOT}/{Path(build_dir).relative_to(Path(build_dir).parents[1]).as_posix()}/bin"
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "docker compose -f .vscode/distributed-debug.compose.yml up -d",
        "",
    ]

    for process in topology.processes:
        lines.extend(
            [
                f"docker compose -f .vscode/distributed-debug.compose.yml exec -T {process.service_name} bash -lc \\",
                f"  \"mkdir -p {binary_dir}/../logs && cd {binary_dir} && nohup gdbserver 0.0.0.0:{process.port} ./{process.name} > ../logs/{process.name}.gdbserver.log 2>&1 &\"",
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def _stop_script() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "docker compose -f .vscode/distributed-debug.compose.yml down",
            "",
        ]
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def write_distributed_debug_assets(target_dir: str, build_dir: str, topology: Optional[DebugTopology]) -> Dict[str, str]:
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

    result = {"launch_json": str(launch_json_path)}

    if not topology or not topology.is_distributed:
        return result

    compose_path = vscode_dir / COMPOSE_FILENAME
    start_script_path = vscode_dir / START_SCRIPT_FILENAME
    stop_script_path = vscode_dir / STOP_SCRIPT_FILENAME

    compose_path.write_text(_compose_yaml(build_dir, topology), encoding="utf-8")
    _write_executable(start_script_path, _start_script(build_dir, topology))
    _write_executable(stop_script_path, _stop_script())

    result.update(
        {
            "docker_compose": str(compose_path),
            "start_script": str(start_script_path),
            "stop_script": str(stop_script_path),
        }
    )
    return result
