import json
import os
import tempfile
import sys
import types
import unittest
import uuid
from pathlib import Path
import shutil
import stat
from unittest.mock import patch
from typing import Optional

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda _content: {
    "verbose": 3,
    "uploads_dir": "uploads",
    "outputs_dir": "outputs",
    "logs_dir": "logs",
    "tools": {},
}
sys.modules.setdefault("yaml", yaml_stub)

from app.app import create_app
from app.routes import generator as generator_routes
from app.services.executor import ToolExecutor
from app.services.distributed_debug import collect_debug_topology, container_binary_dir, gdbserver_command, write_compile_script, write_distributed_debug_assets
from app.services.distributed_debug_runtime import DistributedDebugRuntime

TEST_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp-tests"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


DEPLOYMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<deployment xmlns="http://www.ecoa.technology/deployment-2.0" finalAssembly="demo" logicalSystem="cs1">
  <protectionDomain name="Writer_Reader_PD">
    <executeOn computingNode="machine0" computingPlatform="Dassault"/>
  </protectionDomain>
  <protectionDomain name="Reader_PD">
    <executeOn computingNode="machine1" computingPlatform="Dassault"/>
  </protectionDomain>
  <platformConfiguration computingPlatform="Dassault" faultHandlerNotificationMaxNumber="8">
    <computingNodeConfiguration computingNode="machine0"/>
    <computingNodeConfiguration computingNode="machine1"/>
  </platformConfiguration>
</deployment>
"""

NODES_DEPLOYMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<nodesDeployment>
  <logicalComputingNode id="main" ipAddress="192.168.10.11"/>
  <logicalComputingNode id="machine0" ipAddress="192.168.10.11"/>
  <logicalComputingNode id="machine1" ipAddress="192.168.10.12"/>
</nodesDeployment>
"""

HARNESS_PROJECT_XML = """<?xml version='1.0' encoding='utf-8'?>
<ECOAProject xmlns="http://www.ecoa.technology/project-2.0" name="2test">
  <outputDirectory>6-output</outputDirectory>
  <deploymentSchema>5-Integration/demo-harness.deployment.xml</deploymentSchema>
</ECOAProject>
"""

WRITER_PROJECT_XML = """<?xml version='1.0' encoding='utf-8'?>
<ECOAProject xmlns="http://www.ecoa.technology/project-2.0" name="2test">
  <outputDirectory>6-output</outputDirectory>
  <deploymentSchema>5-Integration/demo.deployment.xml</deploymentSchema>
</ECOAProject>
"""

HARNESS_DEPLOYMENT_XML = """<?xml version='1.0' encoding='utf-8'?>
<deployment xmlns="http://www.ecoa.technology/deployment-2.0" finalAssembly="demo" logicalSystem="cs1">
  <protectionDomain name="HARNESS_PD">
    <executeOn computingNode="machine0" computingPlatform="Dassault"/>
  </protectionDomain>
  <protectionDomain name="Reader_PD">
    <executeOn computingNode="machine1" computingPlatform="Dassault"/>
  </protectionDomain>
</deployment>
"""


def _create_sample_project(tmp_path: Path) -> tuple[Path, Path]:
    project_path = tmp_path / "Steps"
    integration_dir = project_path / "5-Integration"
    build_bin_dir = project_path / "6-Output" / "build" / "bin"

    integration_dir.mkdir(parents=True)
    build_bin_dir.mkdir(parents=True)

    (integration_dir / "demo.deployment.xml").write_text(DEPLOYMENT_XML, encoding="utf-8")
    (integration_dir / "nodes_deployment.xml").write_text(NODES_DEPLOYMENT_XML, encoding="utf-8")
    (project_path / "6-Output" / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n", encoding="utf-8")

    for binary_name in ["platform", "PD_Writer_Reader_PD", "PD_Reader_PD"]:
        (build_bin_dir / binary_name).write_text("#!/bin/sh\n", encoding="utf-8")

    return project_path, build_bin_dir.parent


def _create_multi_deployment_project(tmp_path: Path) -> tuple[Path, Path]:
    project_path, build_dir = _create_sample_project(tmp_path)
    integration_dir = project_path / "5-Integration"

    (integration_dir / "demo-harness.deployment.xml").write_text(HARNESS_DEPLOYMENT_XML, encoding="utf-8")
    (integration_dir / "demo.deployment.xml").write_text(DEPLOYMENT_XML, encoding="utf-8")
    (project_path / "2test-harness.project.xml").write_text(HARNESS_PROJECT_XML, encoding="utf-8")
    (project_path / "2test.project.xml").write_text(WRITER_PROJECT_XML, encoding="utf-8")

    build_bin_dir = build_dir / "bin"
    for binary_name in ["platform", "PD_HARNESS_PD", "PD_Reader_PD"]:
        (build_bin_dir / binary_name).write_text("#!/bin/sh\n", encoding="utf-8")
    stale_binary = build_bin_dir / "PD_Writer_Reader_PD"
    if stale_binary.exists():
        stale_binary.unlink()

    return project_path, build_dir


class DistributedDebugTests(unittest.TestCase):
    def _new_test_root(self) -> Path:
        root = TEST_TMP_ROOT / str(uuid.uuid4())
        root.mkdir(parents=True, exist_ok=False)
        self.addCleanup(self._cleanup_test_root, root)
        return root

    def _cleanup_test_root(self, root: Path) -> None:
        def onerror(func, path, _exc_info):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(path)

        if root.exists():
            shutil.rmtree(root, onerror=onerror)

    def _docker_result(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr, args=[])

    def _runtime_subprocess_side_effect(
        self,
        session_network: Optional[str] = None,
        session_exists: bool = False,
        running_services: str = "ecoa-machine0\necoa-machine1\n",
    ):
        def side_effect(command, *args, **kwargs):
            if command[:4] == ["docker", "network", "ls", "--format"]:
                return self._docker_result(stdout="")
            if command[:3] == ["docker", "compose", "--project-name"] and "up" in command:
                return self._docker_result(stdout="started\n")
            if command[:3] == ["docker", "compose", "--project-name"] and "config" in command:
                return self._docker_result(stdout=running_services)
            if command[:3] == ["docker", "compose", "--project-name"] and "ps" in command:
                return self._docker_result(stdout=running_services)
            if command[:3] == ["docker", "compose", "--project-name"] and "down" in command:
                return self._docker_result(stdout="removed\n")
            if command[:3] == ["docker", "compose", "--project-name"] and "exec" in command:
                return self._docker_result(stdout="")
            if command[:2] == ["docker", "inspect"]:
                networks = {}
                if session_exists and session_network:
                    networks[session_network] = {}
                return self._docker_result(stdout=json.dumps([{"NetworkSettings": {"Networks": networks}, "Mounts": []}]))
            if command[:3] == ["docker", "network", "inspect"]:
                if session_exists:
                    return self._docker_result(stdout=json.dumps([{"IPAM": {"Config": [{"Subnet": session_network or "172.29.10.0/24"}]}}]))
                return self._docker_result(stderr="Error: No such network", returncode=1)
            if command[:3] == ["docker", "network", "connect"]:
                return self._docker_result(stdout="")
            if command[:3] == ["docker", "network", "disconnect"]:
                return self._docker_result(stdout="")
            raise AssertionError(f"Unexpected command: {command}")

        return side_effect

    def test_collects_processes_from_deployment_and_nodes_deployment(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())

        topology = collect_debug_topology(str(project_path), str(build_dir))

        self.assertIsNotNone(topology)
        self.assertTrue(topology.is_distributed)
        self.assertEqual(topology.docker_subnet, "192.168.10.0/24")
        self.assertEqual(
            [process.name for process in topology.processes],
            ["platform", "PD_Writer_Reader_PD", "PD_Reader_PD"],
        )
        self.assertEqual(
            [process.node_id for process in topology.processes],
            ["main", "machine0", "machine1"],
        )
        self.assertEqual(
            [process.host for process in topology.processes],
            ["192.168.10.11", "192.168.10.11", "192.168.10.12"],
        )
        self.assertEqual([process.port for process in topology.processes], [2000, 2001, 2002])
        self.assertEqual(
            [process.service_name for process in topology.processes],
            ["ecoa-machine0", "ecoa-machine0", "ecoa-machine1"],
        )

    def test_gdbserver_command_includes_build_lib_in_ld_library_path(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))

        command = gdbserver_command(str(build_dir), topology.processes[0])

        self.assertIn("LD_LIBRARY_PATH=", command)
        self.assertIn("/workspace/project/6-Output/build/lib", command)
        self.assertIn("gdbserver 0.0.0.0:2000 ./platform", command)

    def test_container_binary_dir_keeps_harness_platform_subpath(self):
        build_dir = "/workspace/demo/task/Steps/6-output/platform/build"

        binary_dir = container_binary_dir(build_dir)

        self.assertEqual(binary_dir, "/workspace/project/6-output/platform/build/bin")

    def test_writes_launch_compose_and_shell_artifacts_for_multi_node_debug(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )

        launch_json = json.loads(Path(assets["launch_json"]).read_text(encoding="utf-8"))
        config_names = [config["name"] for config in launch_json["configurations"]]

        self.assertIn("Debug platform", config_names)
        self.assertIn("Attach platform (main)", config_names)
        self.assertIn("Attach PD Writer_Reader_PD (machine0)", config_names)
        self.assertIn("Attach PD Reader_PD (machine1)", config_names)
        self.assertEqual(
            launch_json["compounds"],
            [
                {
                    "name": "Attach distributed ECOA",
                    "configurations": [
                        "Attach platform (main)",
                        "Attach PD Writer_Reader_PD (machine0)",
                        "Attach PD Reader_PD (machine1)",
                    ],
                }
            ],
        )

        compose_content = Path(assets["docker_compose"]).read_text(encoding="utf-8")
        self.assertIn("name: ecoa-distributed-debug", compose_content)
        self.assertNotIn("ecoa-main:", compose_content)
        self.assertIn("ecoa-machine0", compose_content)
        self.assertIn("ecoa-machine1", compose_content)
        self.assertIn("ipv4_address: 192.168.10.11", compose_content)
        self.assertIn("ipv4_address: 192.168.10.12", compose_content)
        self.assertEqual(compose_content.count("ipv4_address:"), 2)

        start_script = Path(assets["start_script"]).read_text(encoding="utf-8")
        self.assertIn('ECOA_DISTRIBUTED_DEBUG_API_URL:-http://ecoa-tools:5000', start_script)
        self.assertIn("/api/distributed-debug/start", start_script)
        self.assertIn('"client_container": os.environ.get("ECOA_DISTRIBUTED_DEBUG_CLIENT_CONTAINER", "code-server")', start_script)
        self.assertNotIn("docker compose -f .vscode/distributed-debug.compose.yml up -d", start_script)

        stop_script = Path(assets["stop_script"]).read_text(encoding="utf-8")
        self.assertIn("/api/distributed-debug/stop", stop_script)
        self.assertNotIn("docker compose -f .vscode/distributed-debug.compose.yml down", stop_script)
        self.assertTrue((project_path / ".vscode" / "status-distributed-debug.sh").exists())
        status_script = Path(assets["status_script"]).read_text(encoding="utf-8")
        self.assertIn("/api/distributed-debug/status?", status_script)

    def test_executor_creates_distributed_launch_assets_for_ldp_workspace(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        executor = ToolExecutor()

        executor._create_vscode_launch_config(
            project_path=str(project_path),
            project_name="demo",
            build_dir=str(build_dir),
            cmake_dir=str(project_path / "6-Output"),
            workspace_dir=str(project_path),
        )

        launch_json = json.loads((project_path / ".vscode" / "launch.json").read_text(encoding="utf-8"))
        config_names = [config["name"] for config in launch_json["configurations"]]

        self.assertIn("Attach platform (main)", config_names)
        self.assertTrue((project_path / ".vscode" / "distributed-debug.compose.yml").exists())
        self.assertTrue((project_path / ".vscode" / "start-distributed-debug.sh").exists())
        self.assertTrue((project_path / ".vscode" / "stop-distributed-debug.sh").exists())
        self.assertTrue((project_path / ".vscode" / "status-distributed-debug.sh").exists())
        # compile.sh is also generated when cmake_dir is provided
        self.assertTrue((project_path / ".vscode" / "compile.sh").exists())

    def test_compile_script_integration_mode(self):
        project_path = self._new_test_root() / "Steps"
        output_dir = project_path / "6-output"
        output_dir.mkdir(parents=True)
        (output_dir / "CMakeLists.txt").write_text("project(integration)\n", encoding="utf-8")

        script_path = write_compile_script(
            target_dir=str(project_path),
            build_dir=str(output_dir / "build"),
            cmake_dir=str(output_dir),
            project_file="demo.project.xml",
        )

        script_content = Path(script_path).read_text(encoding="utf-8")
        self.assertIn("# ECOA LDP Compile Script", script_content)
        self.assertIn("integration", script_content)
        # Integration mode should NOT contain harness-specific logic
        self.assertNotIn(".distributed-debug-wrapper", script_content)
        self.assertNotIn("CMakeCache.txt", script_content)
        self.assertIn('_pkg_config_path "apr-1"', script_content)
        self.assertIn('_pkg_config_path "log4cplus"', script_content)
        self.assertIn('_pkg_config_path "cunit"', script_content)
        self.assertIn("-DCMAKE_POLICY_VERSION_MINIMUM=3.5", script_content)
        self.assertIn('-DLDP_LOG_USE="${LOG_LIBRARY}"', script_content)
        self.assertIn('LOG_LIBRARY="${1:-log4cplus}"', script_content)
        self.assertIn('make --no-print-directory -C "${BUILD_DIR}" all', script_content)
        # Integration mode: CMakeLists.txt under 6-output/
        self.assertIn("6-output", script_content)
        self.assertIn("6-Output", script_content)

    def test_compile_script_harness_mode(self):
        project_path = self._new_test_root() / "Steps"
        platform_dir = project_path / "6-output" / "platform"
        platform_dir.mkdir(parents=True)
        (platform_dir / "CMakeLists.txt").write_text("project(platform)\n", encoding="utf-8")

        script_path = write_compile_script(
            target_dir=str(project_path),
            build_dir=str(platform_dir / "build"),
            cmake_dir=str(platform_dir),
            project_file="demo-harness.project.xml",
        )

        script_content = Path(script_path).read_text(encoding="utf-8")
        self.assertIn("# ECOA LDP Compile Script", script_content)
        self.assertIn("harness", script_content)
        # Harness mode: CMakeLists.txt under 6-output/platform/
        self.assertIn("platform/CMakeLists.txt", script_content)
        # Harness uses wrapper as cmake source
        self.assertIn(".distributed-debug-wrapper", script_content)
        # Harness clears CMakeCache if exists
        self.assertIn("CMakeCache.txt", script_content)
        self.assertIn("rm -rf", script_content)
        # Also has pkg-config dynamic resolution
        self.assertIn('_pkg_config_path "apr-1"', script_content)
        self.assertIn('_pkg_config_path "log4cplus"', script_content)
        self.assertIn('_pkg_config_path "cunit"', script_content)

    def test_compile_script_is_executable(self):
        project_path = self._new_test_root() / "Steps"
        output_dir = project_path / "6-output"
        output_dir.mkdir(parents=True)
        (output_dir / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")

        script_path = write_compile_script(
            target_dir=str(project_path),
            build_dir=str(output_dir / "build"),
            cmake_dir=str(output_dir),
            project_file="demo.project.xml",
        )

        self.assertTrue(os.access(script_path, os.X_OK))

    def test_write_distributed_debug_assets_includes_compile_script_when_cmake_dir_provided(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
            cmake_dir=str(project_path / "6-Output"),
            project_file="2test.project.xml",
        )

        self.assertIn("compile_script", assets)
        self.assertTrue(Path(assets["compile_script"]).exists())
        self.assertTrue((project_path / ".vscode" / "compile.sh").exists())

    def test_write_distributed_debug_assets_omits_compile_script_when_no_cmake_dir(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )

        self.assertNotIn("compile_script", assets)
        self.assertFalse((project_path / ".vscode" / "compile.sh").exists())

    def test_readme_generated_with_compile_and_distributed_debug(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
            cmake_dir=str(project_path / "6-Output"),
            project_file="2test.project.xml",
        )

        self.assertIn("readme", assets)
        readme_path = Path(assets["readme"])
        self.assertTrue(readme_path.exists())
        self.assertEqual(readme_path.name, "readme.md")
        readme_content = readme_path.read_text(encoding="utf-8")
        # Contains compile script section
        self.assertIn("compile.sh", readme_content)
        self.assertIn("Integration", readme_content)
        # Contains distributed debug section
        self.assertIn("start-distributed-debug.sh", readme_content)
        self.assertIn("stop-distributed-debug.sh", readme_content)
        self.assertIn("status-distributed-debug.sh", readme_content)
        self.assertIn("launch.json", readme_content)

    def test_readme_harness_mode_content(self):
        project_path, build_dir = _create_multi_deployment_project(self._new_test_root())
        topology = collect_debug_topology(
            str(project_path),
            str(build_dir),
            project_file="2test-harness.project.xml",
        )

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
            cmake_dir=str(project_path / "6-Output"),
            project_file="2test-harness.project.xml",
        )

        readme_content = Path(assets["readme"]).read_text(encoding="utf-8")
        self.assertIn("Harness", readme_content)
        self.assertIn(".distributed-debug-wrapper", readme_content)

    def test_readme_generated_without_cmake_dir(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )

        self.assertIn("readme", assets)
        readme_content = Path(assets["readme"]).read_text(encoding="utf-8")
        # No compile script section
        self.assertNotIn("compile.sh", readme_content)
        # Still has distributed debug section
        self.assertIn("start-distributed-debug.sh", readme_content)

    def test_find_cmakelists_dir_prefers_platform_subdir_for_harness_ldp(self):
        project_path = self._new_test_root() / "Steps"
        platform_dir = project_path / "6-output" / "platform"
        top_level_dir = project_path / "6-output"
        platform_dir.mkdir(parents=True)
        top_level_dir.mkdir(parents=True, exist_ok=True)
        (top_level_dir / "CMakeLists.txt").write_text("project(csm)\n", encoding="utf-8")
        (platform_dir / "CMakeLists.txt").write_text("project(platform)\n", encoding="utf-8")

        executor = ToolExecutor()

        selected_dir = executor._find_cmakelists_dir(
            str(project_path),
            project_file="2test-harness.project.xml",
            tool_id="ldp",
        )

        self.assertEqual(selected_dir, str(platform_dir))

    def test_find_cmakelists_dir_keeps_top_level_for_integration_ldp(self):
        project_path = self._new_test_root() / "Steps"
        platform_dir = project_path / "6-output" / "platform"
        top_level_dir = project_path / "6-output"
        platform_dir.mkdir(parents=True)
        top_level_dir.mkdir(parents=True, exist_ok=True)
        (top_level_dir / "CMakeLists.txt").write_text("project(csm)\n", encoding="utf-8")
        (platform_dir / "CMakeLists.txt").write_text("project(platform)\n", encoding="utf-8")

        executor = ToolExecutor()

        selected_dir = executor._find_cmakelists_dir(
            str(project_path),
            project_file="2test.project.xml",
            tool_id="ldp",
        )

        self.assertEqual(selected_dir, str(top_level_dir))

    def test_prepare_harness_platform_wrapper_includes_platform_lib_and_component_dirs(self):
        project_path = self._new_test_root() / "Steps"
        platform_dir = project_path / "6-output" / "platform"
        harness_dir = project_path / "6-output" / "HARNESS"
        reader_dir = project_path / "6-output" / "mycompReader"
        platform_lib_dir = platform_dir / "lib"
        output_types_dir = project_path / "6-output" / "0-Types" / "inc"
        project_types_dir = project_path / "0-Types" / "inc"

        platform_lib_dir.mkdir(parents=True)
        harness_dir.mkdir(parents=True)
        reader_dir.mkdir(parents=True)
        output_types_dir.mkdir(parents=True)
        project_types_dir.mkdir(parents=True)

        (platform_dir / "CMakeLists.txt").write_text(
            "project(platform)\nadd_executable(PD_HARNESS_PD PD_HARNESS_PD.c)\nadd_executable(platform main.c)\n",
            encoding="utf-8",
        )
        (platform_lib_dir / "CMakeLists.txt").write_text("project(ecoa)\n", encoding="utf-8")
        (harness_dir / "CMakeLists.txt").write_text("add_library(lib_HARNESS component_HARNESS.c)\n", encoding="utf-8")
        (reader_dir / "CMakeLists.txt").write_text("add_library(lib_mycompReader component_mycompReader.c)\n", encoding="utf-8")
        (output_types_dir / "VD_lib.h").write_text("/* vd */\n", encoding="utf-8")
        (output_types_dir / "ECOA.h").write_text("/* ecoa */\n", encoding="utf-8")
        (project_types_dir / "DemoType.h").write_text("/* demo */\n", encoding="utf-8")

        executor = ToolExecutor()

        wrapper_dir = executor._prepare_harness_platform_wrapper(str(platform_dir))
        wrapper_cmake = Path(wrapper_dir) / "CMakeLists.txt"
        types_shim_dir = Path(wrapper_dir) / "types-shim"
        wrapper_content = wrapper_cmake.read_text(encoding="utf-8")

        self.assertIn('PATHS "${APR_DIR}/include/apr-1.0"', wrapper_content)
        self.assertIn('"/usr/include/apr-1.0"', wrapper_content)
        self.assertIn('if(TARGET lib_HARNESS)', wrapper_content)
        self.assertIn('target_include_directories(lib_HARNESS PRIVATE "${CMAKE_CURRENT_LIST_DIR}/types-shim")', wrapper_content)
        self.assertIn('if(TARGET lib_mycompReader)', wrapper_content)
        self.assertIn("find_library(LOG4CPLUS_LIBRARY NAMES log4cplus REQUIRED)", wrapper_content)
        self.assertIn('add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../lib" platform_lib)', wrapper_content)
        self.assertIn('add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../../HARNESS" HARNESS)', wrapper_content)
        self.assertIn('add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../../mycompReader" mycompReader)', wrapper_content)
        self.assertIn('add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/.." generated_platform)', wrapper_content)
        self.assertIn('target_include_directories(PD_HARNESS_PD PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../svc_deserial" "${CMAKE_CURRENT_LIST_DIR}/types-shim")', wrapper_content)
        self.assertIn('set_target_properties(platform PROPERTIES', wrapper_content)
        self.assertIn('set_target_properties(PD_HARNESS_PD PROPERTIES', wrapper_content)
        self.assertTrue((types_shim_dir / "VD_lib.h").exists())
        self.assertTrue((types_shim_dir / "DemoType.h").exists())
        self.assertFalse((types_shim_dir / "ECOA.h").exists())

    def test_runtime_start_connects_code_server_and_reports_running_services(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))
        write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )
        runtime = DistributedDebugRuntime()

        with patch(
            "app.services.distributed_debug_runtime.subprocess.run",
            side_effect=self._runtime_subprocess_side_effect(),
        ) as run_mock:
            result = runtime.start(str(project_path), client_container="code-server")

        self.assertTrue(result["success"])
        self.assertEqual(result["running_services"], ["ecoa-machine0", "ecoa-machine1"])
        self.assertTrue(result["client_connected"])
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertTrue(any(command[:4] == ["docker", "network", "ls", "--format"] for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "compose", "--project-name"] and "up" in command for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "network", "connect"] and command[3] == result["network_name"] for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "compose", "--project-name"] and command[3] == result["compose_project_name"] and "ps" in command for command in commands))

    def test_runtime_maps_host_workspace_path_from_container_mount(self):
        runtime = DistributedDebugRuntime()

        host_path = runtime._host_path_for_target_dir(
            Path("/workspace/project-a/task-1/Steps"),
            [
                {
                    "Source": r"C:\repo\workspace",
                    "Destination": "/workspace",
                }
            ],
        )

        self.assertEqual(host_path, r"C:\repo\workspace\project-a\task-1\Steps")

    def test_runtime_writes_effective_compose_with_host_workspace_mount(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))
        write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )
        runtime = DistributedDebugRuntime()

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_compose = runtime._write_runtime_compose_file(
                target_dir=project_path,
                build_dir=build_dir,
                topology=topology,
                host_project_dir=r"C:\repo\workspace\project-a\task-1\Steps",
                debug_image="sirius-web-dev-code-server:v1.0",
                output_dir=Path(temp_dir),
            )

            compose_content = Path(runtime_compose).read_text(encoding="utf-8")

        self.assertIn("source: 'C:\\repo\\workspace\\project-a\\task-1\\Steps'", compose_content)
        self.assertIn('target: "/workspace/project"', compose_content)
        self.assertIn('image: sirius-web-dev-code-server:v1.0', compose_content)

    def test_runtime_creates_isolated_session_metadata(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))
        write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )
        runtime = DistributedDebugRuntime()

        with patch(
            "app.services.distributed_debug_runtime.subprocess.run",
            side_effect=self._runtime_subprocess_side_effect(),
        ):
            result = runtime.start(str(project_path), client_container="code-server")

        session_file = project_path / ".vscode" / "distributed-debug.session.json"
        self.assertTrue(session_file.exists())
        session = json.loads(session_file.read_text(encoding="utf-8"))

        self.assertIn("session_id", result)
        self.assertEqual(result["session_id"], session["session_id"])
        self.assertEqual(result["compose_project_name"], session["compose_project_name"])
        self.assertEqual(result["network_name"], session["network_name"])
        self.assertEqual(result["docker_subnet"], session["docker_subnet"])
        self.assertNotEqual(result["compose_project_name"], "ecoa-distributed-debug")
        self.assertNotEqual(result["network_name"], "ecoa-distributed-debug_ecoa_debug_net")
        self.assertTrue(result["docker_subnet"].startswith("172.29."))

    def test_runtime_uses_session_specific_project_network_and_compose(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))
        write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )
        runtime = DistributedDebugRuntime()

        with patch(
            "app.services.distributed_debug_runtime.subprocess.run",
            side_effect=self._runtime_subprocess_side_effect(),
        ) as run_mock:
            result = runtime.start(str(project_path), client_container="code-server")

        runtime_compose = Path(result["compose_file"])
        compose_content = runtime_compose.read_text(encoding="utf-8")
        commands = [call.args[0] for call in run_mock.call_args_list]

        self.assertIn(f'name: {result["compose_project_name"]}', compose_content)
        self.assertIn(f'name: {result["network_name"]}', compose_content)
        self.assertIn(result["docker_subnet"].split("/")[0].rsplit(".", 1)[0], compose_content)
        self.assertTrue(any(command[:3] == ["docker", "compose", "--project-name"] and command[3] == result["compose_project_name"] for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "network", "connect"] and command[3] == result["network_name"] for command in commands))

    def test_status_and_stop_reuse_current_session_metadata(self):
        project_path, build_dir = _create_sample_project(self._new_test_root())
        topology = collect_debug_topology(str(project_path), str(build_dir))
        write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )
        session_file = project_path / ".vscode" / "distributed-debug.session.json"
        session_file.write_text(
            json.dumps(
                {
                    "session_id": "sess-1234",
                    "compose_project_name": "ecoa-distributed-debug-sess-1234",
                    "network_name": "ecoa-distributed-debug-sess-1234_ecoa_debug_net",
                    "docker_subnet": "172.29.10.0/24",
                    "compose_file": str(project_path / ".vscode" / "distributed-debug.sess-1234.runtime.compose.yml"),
                    "client_container": "code-server",
                }
            ),
            encoding="utf-8",
        )

        runtime = DistributedDebugRuntime()
        with patch(
            "app.services.distributed_debug_runtime.subprocess.run",
            side_effect=self._runtime_subprocess_side_effect(
                session_network="ecoa-distributed-debug-sess-1234_ecoa_debug_net",
                session_exists=True,
            ),
        ) as run_mock:
            status = runtime.status(str(project_path), client_container="code-server")
            stop = runtime.stop(str(project_path), client_container="code-server")

        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(status["network_name"], "ecoa-distributed-debug-sess-1234_ecoa_debug_net")
        self.assertEqual(stop["network_name"], "ecoa-distributed-debug-sess-1234_ecoa_debug_net")
        self.assertTrue(any(command[:3] == ["docker", "compose", "--project-name"] and command[3] == "ecoa-distributed-debug-sess-1234" and "config" in command for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "network", "inspect"] and command[3] == "ecoa-distributed-debug-sess-1234_ecoa_debug_net" for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "network", "disconnect"] and command[3] == "ecoa-distributed-debug-sess-1234_ecoa_debug_net" for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "compose", "--project-name"] and command[3] == "ecoa-distributed-debug-sess-1234" and "down" in command for command in commands))

    def test_prefers_project_deployment_schema_over_scanning_all_deployments(self):
        project_path, build_dir = _create_multi_deployment_project(self._new_test_root())

        topology = collect_debug_topology(
            str(project_path),
            str(build_dir),
            project_file="2test-harness.project.xml",
        )

        self.assertIsNotNone(topology)
        self.assertEqual(
            [process.name for process in topology.processes],
            ["platform", "PD_HARNESS_PD", "PD_Reader_PD"],
        )

        assets = write_distributed_debug_assets(
            target_dir=str(project_path),
            build_dir=str(build_dir),
            topology=topology,
        )
        launch_json = json.loads(Path(assets["launch_json"]).read_text(encoding="utf-8"))
        config_names = [config["name"] for config in launch_json["configurations"]]

        self.assertIn("Attach PD HARNESS_PD (machine0)", config_names)
        self.assertIn("Attach PD Reader_PD (machine1)", config_names)
        self.assertNotIn("Attach PD Writer_Reader_PD (machine0)", config_names)

    def test_distributed_debug_routes_delegate_to_runtime_service(self):
        app = create_app()
        app.testing = True
        client = app.test_client()

        with patch("app.routes.distributed_debug.runtime_service.start", return_value={"success": True, "running_services": ["ecoa-main"]}) as start_mock, patch(
            "app.routes.distributed_debug.runtime_service.stop",
            return_value={"success": True, "stopped": True},
        ) as stop_mock, patch(
            "app.routes.distributed_debug.runtime_service.status",
            return_value={"success": True, "running_services": ["ecoa-main"], "client_connected": True},
        ) as status_mock:
            start_response = client.post(
                "/api/distributed-debug/start",
                json={"target_dir": "/workspace/demo/Steps", "client_container": "code-server"},
            )
            stop_response = client.post(
                "/api/distributed-debug/stop",
                json={"target_dir": "/workspace/demo/Steps", "client_container": "code-server"},
            )
            status_response = client.get(
                "/api/distributed-debug/status?target_dir=/workspace/demo/Steps&client_container=code-server"
            )

        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(start_response.get_json()["running_services"], ["ecoa-main"])
        start_mock.assert_called_once_with("/workspace/demo/Steps", client_container="code-server")

        self.assertEqual(stop_response.status_code, 200)
        self.assertTrue(stop_response.get_json()["stopped"])
        stop_mock.assert_called_once_with("/workspace/demo/Steps", client_container="code-server")

        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.get_json()["client_connected"])
        status_mock.assert_called_once_with("/workspace/demo/Steps", client_container="code-server")

    def test_pipeline_requests_ldp_compilation_for_distributed_debug_workspaces(self):
        result_payload = {
            "success": True,
            "return_code": 0,
            "stdout": "ok",
            "stderr": "",
            "generated_files": [],
            "project_path": "/workspace/demo/task-1/Steps",
            "compile_success": True,
            "compile_return_code": 0,
            "compile_stdout": "compiled",
            "compile_stderr": "",
            "build_dir": "/workspace/demo/task-1/Steps/6-output/build",
        }

        with patch("app.routes.generator.ToolExecutor") as executor_cls, patch(
            "app.routes.generator._resolve_project_file",
            return_value=("demo", "demo.project.xml", Path("/workspace/demo/task-1/Steps")),
        ), patch(
            "app.routes.generator._send_callback"
        ):
            executor_instance = executor_cls.return_value
            executor_instance.execute_in_project.return_value = result_payload

            generator_routes._run_pipeline(
                task_id="task-1",
                project_id="demo",
                output_dir="/workspace",
                callback_url="http://callback",
                selected_phases=["LDP"],
                continue_on_error=False,
                phase_params={},
                skip_export=True,
            )

        executor_instance.execute_in_project.assert_called_once()
        self.assertTrue(executor_instance.execute_in_project.call_args.kwargs["compile"])


if __name__ == "__main__":
    unittest.main()
