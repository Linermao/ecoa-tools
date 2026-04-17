import json
import os
import sys
import types
import unittest
import uuid
from pathlib import Path
import shutil
import stat

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda _content: {
    "verbose": 3,
    "uploads_dir": "uploads",
    "outputs_dir": "outputs",
    "logs_dir": "logs",
    "tools": {},
}
sys.modules.setdefault("yaml", yaml_stub)

from app.services.executor import ToolExecutor
from app.services.distributed_debug import collect_debug_topology, write_distributed_debug_assets

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
  <logicalComputingNode id="machine0" ipAddress="192.168.10.11"/>
  <logicalComputingNode id="machine1" ipAddress="192.168.10.12"/>
</nodesDeployment>
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
        self.assertIn("ecoa-machine0", compose_content)
        self.assertIn("ecoa-machine1", compose_content)
        self.assertIn("ipv4_address: 192.168.10.11", compose_content)
        self.assertIn("ipv4_address: 192.168.10.12", compose_content)
        self.assertEqual(compose_content.count("ipv4_address:"), 2)

        start_script = Path(assets["start_script"]).read_text(encoding="utf-8")
        self.assertIn("docker compose -f .vscode/distributed-debug.compose.yml up -d", start_script)
        self.assertIn("gdbserver 0.0.0.0:2000 ./platform", start_script)
        self.assertIn("gdbserver 0.0.0.0:2001 ./PD_Writer_Reader_PD", start_script)
        self.assertIn("gdbserver 0.0.0.0:2002 ./PD_Reader_PD", start_script)
        self.assertEqual(start_script.count("exec -T ecoa-machine0"), 2)
        self.assertEqual(start_script.count("exec -T ecoa-machine1"), 1)

        stop_script = Path(assets["stop_script"]).read_text(encoding="utf-8")
        self.assertIn("docker compose -f .vscode/distributed-debug.compose.yml down", stop_script)

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


if __name__ == "__main__":
    unittest.main()
