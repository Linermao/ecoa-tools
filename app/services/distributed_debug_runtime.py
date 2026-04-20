"""Runtime orchestration for distributed debug containers."""

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

from app.services.distributed_debug import (
    COMPOSE_FILENAME,
    COMPOSE_PROJECT_NAME,
    DebugProcess,
    DebugTopology,
    collect_debug_topology,
    gdbserver_command,
    render_distributed_debug_compose,
    write_distributed_debug_launch_json,
)


DEFAULT_CLIENT_CONTAINER = os.environ.get("ECOA_DISTRIBUTED_DEBUG_CLIENT_CONTAINER", "code-server")
COMPOSE_NETWORK_NAME = f"{COMPOSE_PROJECT_NAME}_ecoa_debug_net"
RUNTIME_COMPOSE_FILENAME = "distributed-debug.runtime.compose.yml"
SESSION_FILENAME = "distributed-debug.session.json"
SESSION_SUBNET_PREFIX = "172.29"


class DistributedDebugRuntimeError(RuntimeError):
    """Raised when distributed debug orchestration fails."""


@dataclass(frozen=True)
class DistributedDebugContext:
    """Resolved filesystem and Docker context for distributed debug."""

    target_dir: Path
    build_dir: Path
    compose_file: Path
    topology: DebugTopology


@dataclass(frozen=True)
class DistributedDebugSession:
    session_id: str
    compose_project_name: str
    network_name: str
    docker_subnet: str
    compose_file: Path
    client_container: Optional[str]
    topology: DebugTopology


class DistributedDebugRuntime:
    """Start, stop, and inspect distributed debug compose stacks."""

    def __init__(self, default_client_container: Optional[str] = None):
        self.default_client_container = default_client_container or DEFAULT_CLIENT_CONTAINER

    def start(self, target_dir: str, client_container: Optional[str] = None) -> Dict[str, Any]:
        context = self._resolve_context(target_dir)
        client_name = client_container or self.default_client_container
        session = self._create_session(context, client_name)
        compose_result = self._run_command(self._compose_command(session, "up", "-d", "--remove-orphans"))
        client_connected = False

        if client_name:
            if not self._container_connected_to_network(client_name, session.network_name):
                self._run_command(["docker", "network", "connect", session.network_name, client_name])
            client_connected = True

        for process in session.topology.processes:
            self._run_command(
                self._compose_command(
                    session,
                    "exec",
                    "-T",
                    process.service_name,
                    "bash",
                    "-lc",
                    gdbserver_command(str(context.build_dir), process),
                )
            )

        running_services = self._compose_services(session, "ps", "--services", "--status", "running")
        return {
            "success": True,
            "target_dir": str(context.target_dir),
            "build_dir": str(context.build_dir),
            "compose_file": str(session.compose_file),
            "network_name": session.network_name,
            "session_id": session.session_id,
            "compose_project_name": session.compose_project_name,
            "docker_subnet": session.docker_subnet,
            "client_container": client_name,
            "client_connected": client_connected,
            "running_services": running_services,
            "stdout": compose_result.stdout.strip(),
            "stderr": compose_result.stderr.strip(),
        }

    def stop(self, target_dir: str, client_container: Optional[str] = None) -> Dict[str, Any]:
        context = self._resolve_context(target_dir)
        session = self._load_or_create_session(context, client_container or self.default_client_container)
        client_name = client_container or session.client_container or self.default_client_container

        if client_name and self._network_exists(session.network_name) and self._container_connected_to_network(client_name, session.network_name):
            self._run_command(["docker", "network", "disconnect", session.network_name, client_name])

        compose_result = self._run_command(self._compose_command(session, "down", "--remove-orphans"))
        return {
            "success": True,
            "target_dir": str(context.target_dir),
            "build_dir": str(context.build_dir),
            "compose_file": str(session.compose_file),
            "network_name": session.network_name,
            "session_id": session.session_id,
            "compose_project_name": session.compose_project_name,
            "docker_subnet": session.docker_subnet,
            "client_container": client_name,
            "stopped": True,
            "stdout": compose_result.stdout.strip(),
            "stderr": compose_result.stderr.strip(),
        }

    def status(self, target_dir: str, client_container: Optional[str] = None) -> Dict[str, Any]:
        context = self._resolve_context(target_dir)
        session = self._load_or_create_session(context, client_container or self.default_client_container)
        client_name = client_container or session.client_container or self.default_client_container
        configured_services = self._compose_services(session, "config", "--services")
        running_services = self._compose_services(session, "ps", "--services", "--status", "running")
        client_connected = False

        if client_name and self._network_exists(session.network_name):
            client_connected = self._container_connected_to_network(client_name, session.network_name)

        return {
            "success": True,
            "target_dir": str(context.target_dir),
            "build_dir": str(context.build_dir),
            "compose_file": str(session.compose_file),
            "network_name": session.network_name,
            "session_id": session.session_id,
            "compose_project_name": session.compose_project_name,
            "docker_subnet": session.docker_subnet,
            "client_container": client_name,
            "client_connected": client_connected,
            "configured_services": configured_services,
            "running_services": running_services,
            "started": bool(running_services),
        }

    def _resolve_context(self, target_dir: str) -> DistributedDebugContext:
        if not target_dir:
            raise ValueError("target_dir is required")

        target_path = Path(target_dir).resolve()
        if not target_path.exists():
            raise FileNotFoundError(f"Target directory not found: {target_dir}")

        compose_file = target_path / ".vscode" / COMPOSE_FILENAME
        if not compose_file.exists():
            raise FileNotFoundError(f"Distributed debug compose file not found: {compose_file}")

        build_dir = self._find_build_dir(target_path)
        topology = collect_debug_topology(str(target_path), str(build_dir))
        if topology is None or not topology.is_distributed:
            raise FileNotFoundError(f"Distributed debug topology metadata not found under: {target_path}")

        return DistributedDebugContext(
            target_dir=target_path,
            build_dir=build_dir,
            compose_file=compose_file,
            topology=topology,
        )

    def _find_build_dir(self, target_dir: Path) -> Path:
        direct_candidates = [
            target_dir / "6-Output" / "build",
            target_dir / "6-output" / "build",
            target_dir / "build",
        ]
        for candidate in direct_candidates:
            if (candidate / "bin" / "platform").exists():
                return candidate

        for candidate in sorted(target_dir.rglob("build")):
            if (candidate / "bin" / "platform").exists():
                return candidate

        raise FileNotFoundError(f"Build directory with platform binary not found under: {target_dir}")

    def _compose_file_for_runtime(
        self,
        context: DistributedDebugContext,
        client_container: Optional[str],
        session: DistributedDebugSession,
    ) -> Path:
        host_project_dir = ".."
        if str(context.target_dir).startswith("/workspace/"):
            resolved_host_project_dir = self._resolve_host_project_dir(context.target_dir, client_container)
            if resolved_host_project_dir:
                host_project_dir = resolved_host_project_dir
        debug_image = self._resolve_debug_image(client_container)
        return self._write_runtime_compose_file(
            target_dir=context.target_dir,
            build_dir=context.build_dir,
            topology=session.topology,
            host_project_dir=host_project_dir,
            debug_image=debug_image,
            compose_project_name=session.compose_project_name,
            network_name=session.network_name,
            runtime_compose_file=session.compose_file,
        )

    def _write_runtime_compose_file(
        self,
        target_dir: Path,
        build_dir: Path,
        topology: DebugTopology,
        host_project_dir: str,
        debug_image: Optional[str] = None,
        compose_project_name: str = COMPOSE_PROJECT_NAME,
        network_name: str = COMPOSE_NETWORK_NAME,
        output_dir: Optional[Path] = None,
        runtime_compose_file: Optional[Path] = None,
    ) -> Path:
        compose_output_dir = output_dir or (target_dir / ".vscode")
        compose_output_dir.mkdir(parents=True, exist_ok=True)
        runtime_compose_file = runtime_compose_file or (compose_output_dir / RUNTIME_COMPOSE_FILENAME)
        runtime_compose_file.write_text(
            render_distributed_debug_compose(
                str(build_dir),
                topology,
                project_mount_source=host_project_dir,
                debug_image=debug_image,
                compose_project_name=compose_project_name,
                network_name=network_name,
            ),
            encoding="utf-8",
        )
        return runtime_compose_file

    def _session_file(self, target_dir: Path) -> Path:
        return target_dir / ".vscode" / SESSION_FILENAME

    def _create_session(self, context: DistributedDebugContext, client_container: Optional[str]) -> DistributedDebugSession:
        session_id = uuid.uuid4().hex[:8]
        compose_project_name = f"{COMPOSE_PROJECT_NAME}-{session_id}"
        network_name = f"{compose_project_name}_ecoa_debug_net"
        docker_subnet = self._allocate_docker_subnet()
        runtime_topology = self._runtime_topology_for_subnet(context.topology, docker_subnet)
        compose_file = context.target_dir / ".vscode" / f"distributed-debug.{session_id}.runtime.compose.yml"
        session = DistributedDebugSession(
            session_id=session_id,
            compose_project_name=compose_project_name,
            network_name=network_name,
            docker_subnet=docker_subnet,
            compose_file=compose_file,
            client_container=client_container,
            topology=runtime_topology,
        )
        runtime_compose = self._compose_file_for_runtime(context, client_container, session)
        session = replace(session, compose_file=runtime_compose)
        self._write_session_metadata(context.target_dir, session)
        write_distributed_debug_launch_json(str(context.target_dir), str(context.build_dir), session.topology)
        return session

    def _load_or_create_session(self, context: DistributedDebugContext, client_container: Optional[str]) -> DistributedDebugSession:
        session = self._read_session_metadata(context.target_dir, context.topology)
        if session is not None:
            return session
        return self._create_session(context, client_container)

    def _write_session_metadata(self, target_dir: Path, session: DistributedDebugSession) -> None:
        session_file = self._session_file(target_dir)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(
            json.dumps(
                {
                    "session_id": session.session_id,
                    "compose_project_name": session.compose_project_name,
                    "network_name": session.network_name,
                    "docker_subnet": session.docker_subnet,
                    "compose_file": str(session.compose_file),
                    "client_container": session.client_container,
                    "topology": {
                        "integration_dir": session.topology.integration_dir,
                        "docker_subnet": session.topology.docker_subnet,
                        "is_distributed": session.topology.is_distributed,
                        "processes": [
                            {
                                "name": process.name,
                                "node_id": process.node_id,
                                "host": process.host,
                                "port": process.port,
                                "service_name": process.service_name,
                            }
                            for process in session.topology.processes
                        ],
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _read_session_metadata(self, target_dir: Path, fallback_topology: DebugTopology) -> Optional[DistributedDebugSession]:
        session_file = self._session_file(target_dir)
        if not session_file.exists():
            return None

        session_data = json.loads(session_file.read_text(encoding="utf-8"))
        topology_data = session_data.get("topology") or {}
        processes = [
            DebugProcess(
                name=process["name"],
                node_id=process["node_id"],
                host=process["host"],
                port=process["port"],
                service_name=process["service_name"],
            )
            for process in topology_data.get("processes", [])
        ]
        runtime_topology = DebugTopology(
            integration_dir=topology_data.get("integration_dir", fallback_topology.integration_dir),
            docker_subnet=topology_data.get("docker_subnet", fallback_topology.docker_subnet),
            processes=processes or fallback_topology.processes,
            is_distributed=topology_data.get("is_distributed", fallback_topology.is_distributed),
        )
        return DistributedDebugSession(
            session_id=session_data["session_id"],
            compose_project_name=session_data["compose_project_name"],
            network_name=session_data["network_name"],
            docker_subnet=session_data["docker_subnet"],
            compose_file=Path(session_data["compose_file"]),
            client_container=session_data.get("client_container"),
            topology=runtime_topology,
        )

    def _allocate_docker_subnet(self) -> str:
        occupied = set(self._existing_debug_subnets())
        for subnet_index in range(1, 255):
            candidate = f"{SESSION_SUBNET_PREFIX}.{subnet_index}.0/24"
            if candidate not in occupied:
                return candidate
        raise DistributedDebugRuntimeError("No free distributed debug subnet available in 172.29.0.0/16")

    def _existing_debug_subnets(self) -> List[str]:
        try:
            result = subprocess.run(
                ["docker", "network", "ls", "--format", "{{.Name}}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DistributedDebugRuntimeError(
                "Docker CLI is unavailable in ecoa-tools. Install Docker and mount /var/run/docker.sock."
            ) from exc
        if result.returncode != 0:
            raise DistributedDebugRuntimeError(self._format_command_error(result, ["docker", "network", "ls", "--format", "{{.Name}}"]))

        subnets: List[str] = []
        for network_name in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            inspect_result = subprocess.run(
                ["docker", "network", "inspect", network_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect_result.returncode != 0:
                continue
            network_info = json.loads(inspect_result.stdout or "[]")
            if not network_info:
                continue
            ipam_config = network_info[0].get("IPAM", {}).get("Config") or []
            for config in ipam_config:
                subnet = config.get("Subnet")
                if subnet and subnet.startswith(f"{SESSION_SUBNET_PREFIX}."):
                    subnets.append(subnet)
        return subnets

    def _runtime_topology_for_subnet(self, topology: DebugTopology, docker_subnet: str) -> DebugTopology:
        subnet_prefix = ".".join(docker_subnet.split("/")[0].split(".")[:3])
        host_mapping: Dict[str, str] = {}
        next_host_octet = 10
        remapped_processes = []
        for process in topology.processes:
            if process.host not in host_mapping:
                host_mapping[process.host] = f"{subnet_prefix}.{next_host_octet}"
                next_host_octet += 1
            remapped_processes.append(replace(process, host=host_mapping[process.host]))
        return replace(topology, docker_subnet=docker_subnet, processes=remapped_processes)

    def _resolve_host_project_dir(self, target_dir: Path, client_container: Optional[str]) -> Optional[str]:
        if not client_container:
            return None

        try:
            container_info = self._inspect_container(client_container)
        except DistributedDebugRuntimeError:
            return None

        return self._host_path_for_target_dir(target_dir, container_info.get("Mounts", []))

    def _resolve_debug_image(self, client_container: Optional[str]) -> Optional[str]:
        if not client_container:
            return None

        try:
            container_info = self._inspect_container(client_container)
        except DistributedDebugRuntimeError:
            return None

        return container_info.get("Config", {}).get("Image")

    def _host_path_for_target_dir(self, target_dir: Path, mounts: List[Dict[str, Any]]) -> Optional[str]:
        target_path = PurePosixPath(target_dir.as_posix())

        for mount in mounts:
            destination = mount.get("Destination")
            source = mount.get("Source")
            if not destination or not source:
                continue

            destination_path = PurePosixPath(destination)
            try:
                relative_target = target_path.relative_to(destination_path)
            except ValueError:
                continue

            if len(source) >= 2 and source[1] == ":":
                return str(PureWindowsPath(source) / PureWindowsPath(*relative_target.parts))

            return str(Path(source) / Path(*relative_target.parts))

        return None

    def _compose_command(self, session: DistributedDebugSession, *args: str) -> List[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            session.compose_project_name,
            "-f",
            str(session.compose_file),
            *args,
        ]

    def _compose_services(self, session: DistributedDebugSession, *args: str) -> List[str]:
        result = self._run_command(self._compose_command(session, *args))
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _network_exists(self, network_name: str) -> bool:
        try:
            result = subprocess.run(
                ["docker", "network", "inspect", network_name],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DistributedDebugRuntimeError(
                "Docker CLI is unavailable in ecoa-tools. Install Docker and mount /var/run/docker.sock."
            ) from exc
        if result.returncode == 0:
            return True
        if "No such network" in (result.stderr or "") or "not found" in (result.stderr or ""):
            return False
        raise DistributedDebugRuntimeError(
            f"Unable to inspect Docker network {network_name}: {self._format_command_error(result)}"
        )

    def _inspect_container(self, container_name: str) -> Dict[str, Any]:
        result = self._run_command(["docker", "inspect", container_name])
        container_info = json.loads(result.stdout or "[]")
        if not container_info:
            raise DistributedDebugRuntimeError(f"Docker container not found: {container_name}")
        return container_info[0]

    def _container_connected_to_network(self, container_name: str, network_name: str) -> bool:
        container_info = self._inspect_container(container_name)
        networks = container_info.get("NetworkSettings", {}).get("Networks", {})
        return network_name in networks

    def _run_command(self, command: List[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DistributedDebugRuntimeError(
                "Docker CLI is unavailable in ecoa-tools. Install Docker and mount /var/run/docker.sock."
            ) from exc

        if result.returncode != 0:
            raise DistributedDebugRuntimeError(self._format_command_error(result, command))

        return result

    @staticmethod
    def _format_command_error(result: subprocess.CompletedProcess[str], command: Optional[List[str]] = None) -> str:
        command_text = " ".join(command or result.args or [])
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        details = stderr or stdout or f"exit code {result.returncode}"
        return f"Command failed: {command_text}. {details}"
