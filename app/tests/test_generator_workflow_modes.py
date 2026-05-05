import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda _content: {
    "verbose": 3,
    "uploads_dir": "uploads",
    "outputs_dir": "outputs",
    "logs_dir": "logs",
    "tools": {},
    "api": {"max_upload_size": 16777216},
    "server": {"debug": False},
}
sys.modules.setdefault("yaml", yaml_stub)

from app.utils import config as config_module


class _FakeConfig:
    logs_dir = "logs"
    max_upload_size = 16777216
    server_debug = False

    def get(self, key, default=None):
        return default


config_module.get_config = lambda _config_path="config.yaml": _FakeConfig()

from app.app import create_app
from app.services.generation_workflow import (
    WorkflowContext,
    default_selected_phases,
    should_await_code,
    resolve_phase_steps,
    validate_phase_selection,
)
import app.routes.generator as generator_module


class GeneratorWorkflowModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_run_pipeline = generator_module._run_pipeline
        generator_module._run_pipeline = lambda *args, **kwargs: None
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    def test_harness_initial_phase_order_is_exvt_asctg_mscigt(self):
        self.assertEqual(
            default_selected_phases("HARNESS", continuing=False),
            ["EXVT", "ASCTG", "MSCIGT"],
        )
        self.assertEqual(
            resolve_phase_steps("HARNESS", selected_phases=None, continuing=False),
            ["EXVT", "ASCTG", "MSCIGT"],
        )
        # HARNESS maps to HARNESS_DEV internally
        self.assertEqual(
            default_selected_phases("HARNESS_DEV", continuing=False),
            ["EXVT", "ASCTG", "MSCIGT"],
        )

    def test_integration_phase_order_omits_asctg_and_mscigt(self):
        self.assertEqual(
            default_selected_phases("INTEGRATION", continuing=False),
            ["EXVT", "LDP"],
        )
        self.assertEqual(
            resolve_phase_steps("INTEGRATION", selected_phases=None, continuing=False),
            ["EXVT", "LDP"],
        )
        self.assertEqual(
            resolve_phase_steps("INTEGRATION", selected_phases=["EXVT", "CSMGVT", "LDP"], continuing=False),
            ["EXVT", "CSMGVT", "LDP"],
        )

    def test_harness_initial_request_rejects_execution_phases(self):
        with self.assertRaisesRegex(
            ValueError,
            "HARNESS_DEV initial runs only allow EXVT, ASCTG and MSCIGT",
        ):
            validate_phase_selection(
                "HARNESS",
                ["EXVT", "CSMGVT"],
                continuing=False,
            )

    def test_harness_continue_request_rejects_modeling_phases(self):
        with self.assertRaisesRegex(
            ValueError,
            "HARNESS_DEV continue runs only allow CSMGVT and LDP",
        ):
            validate_phase_selection(
                "HARNESS",
                ["CSMGVT", "MSCIGT"],
                continuing=True,
            )

    def test_integration_initial_request_allows_optional_csmgvt(self):
        validate_phase_selection(
            "INTEGRATION",
            ["EXVT", "CSMGVT", "LDP"],
            continuing=False,
        )

    def test_harness_initial_request_rejects_execution_phases_at_request_level(self):
        client = self.app.test_client()

        response = client.post(
            "/api/generate",
            json={
                "taskId": "task-1",
                "projectId": "project-1",
                "callbackUrl": "http://localhost/callback",
                "workflowMode": "HARNESS",
                "selectedPhases": ["EXVT", "CSMGVT"],
                "continuing": False,
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["error"], "HARNESS_DEV initial runs only allow EXVT, ASCTG and MSCIGT")
        self.assertEqual(payload["message"], "HARNESS_DEV initial runs only allow EXVT, ASCTG and MSCIGT")

    def test_harness_continue_request_rejects_modeling_phases_at_request_level(self):
        client = self.app.test_client()

        response = client.post(
            "/api/generate",
            json={
                "taskId": "task-2",
                "projectId": "project-1",
                "callbackUrl": "http://localhost/callback",
                "workflowMode": "HARNESS",
                "selectedPhases": ["CSMGVT", "MSCIGT"],
                "continuing": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["error"], "HARNESS_DEV continue runs only allow CSMGVT and LDP")
        self.assertEqual(payload["message"], "HARNESS_DEV continue runs only allow CSMGVT and LDP")

    def test_explicit_empty_selected_phases_is_rejected(self):
        client = self.app.test_client()

        response = client.post(
            "/api/generate",
            json={
                "taskId": "task-3",
                "projectId": "project-1",
                "callbackUrl": "http://localhost/callback",
                "workflowMode": "HARNESS",
                "selectedPhases": [],
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["error"], "selectedPhases must be a non-empty list of strings")
        self.assertEqual(payload["message"], "selectedPhases must be a non-empty list of strings")

    def test_invalid_workflow_mode_request_is_rejected_when_selected_phases_are_omitted(self):
        client = self.app.test_client()

        response = client.post(
            "/api/generate",
            json={
                "taskId": "task-4",
                "projectId": "project-1",
                "callbackUrl": "http://localhost/callback",
                "workflowMode": "SOMETHING_ELSE",
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["error"], "Unknown workflow mode: SOMETHING_ELSE")
        self.assertEqual(payload["message"], "Unknown workflow mode: SOMETHING_ELSE")

    def test_continuing_string_false_is_parsed_as_false(self):
        client = self.app.test_client()

        response = client.post(
            "/api/generate",
            json={
                "taskId": "task-5",
                "projectId": "project-1",
                "callbackUrl": "http://localhost/callback",
                "workflowMode": "HARNESS",
                "selectedPhases": ["EXVT"],
                "continuing": "false",
            },
        )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["message"], "Accepted")

    def test_harness_pipeline_runs_mscigt_after_asctg_with_harness_project(self):
        callbacks: list[dict] = []
        recorded_tool_runs: list[tuple[str, str]] = []

        with TemporaryDirectory() as tmpdir:
            steps_root = Path(tmpdir)
            (steps_root / "base.project.xml").write_text("<project />", encoding="utf-8")
            (steps_root / "base-harness.project.xml").write_text("<project />", encoding="utf-8")

            def fake_callback(_url, payload, _task_id):
                callbacks.append(payload)

            def fake_execute_in_project(*, tool_id, project_file, **_kwargs):
                recorded_tool_runs.append((tool_id, project_file))
                return {
                    "success": True,
                    "return_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "generated_files": [],
                    "project_path": str(steps_root / project_file),
                }

            with (
                patch.object(generator_module, "_run_pipeline", type(self).original_run_pipeline),
                patch.object(generator_module, "_send_callback", side_effect=fake_callback),
                patch.object(
                    generator_module,
                    "_resolve_project_file",
                    return_value=("project-1", "base.project.xml", steps_root),
                ),
                patch.object(
                    generator_module,
                    "execute_asctg_from_steps_dir",
                    return_value={
                        "success": True,
                        "return_code": 0,
                        "workspace_root": str(steps_root),
                        "project_path": str(steps_root / "base.project.xml"),
                        "stdout": "",
                        "stderr": "",
                        "generated_files": [],
                    },
                ),
                patch.object(generator_module.ToolExecutor, "execute_in_project", side_effect=fake_execute_in_project),
            ):
                generator_module._run_pipeline(
                    task_id="task-6",
                    project_id="project-1",
                    output_dir="/workspace",
                    callback_url="http://localhost/callback",
                    selected_phases=["EXVT", "MSCIGT", "ASCTG"],
                    continue_on_error=False,
                    phase_params={"ASCTG": {"selected_components": "comp_a,comp_b"}},
                    skip_export=True,
                    workflow_mode="HARNESS",
                    continuing=False,
                )

        self.assertEqual(
            recorded_tool_runs,
            [
                ("exvt", "base.project.xml"),
                ("mscigt", "base-harness.project.xml"),
            ],
        )
        self.assertEqual(callbacks[-1]["status"], "AWAITING_CODE")
        self.assertEqual(callbacks[-1]["workflowMode"], "HARNESS_DEV")
        self.assertEqual(callbacks[-1]["baseProjectFile"], "base.project.xml")
        self.assertEqual(callbacks[-1]["activeProjectFile"], "base-harness.project.xml")
        self.assertEqual(callbacks[-1]["harnessProjectFile"], "base-harness.project.xml")

    def test_harness_only_enters_awaiting_code_after_mscigt(self):
        self.assertTrue(should_await_code("HARNESS", ["EXVT", "MSCIGT"], had_failure=False, continuing=False))
        self.assertTrue(should_await_code("HARNESS_DEV", ["EXVT", "MSCIGT"], had_failure=False, continuing=False))

    def test_direct_dev_enters_awaiting_code_after_mscigt(self):
        self.assertTrue(should_await_code("DIRECT_DEV", ["EXVT", "MSCIGT"], had_failure=False, continuing=False))

    def test_direct_dev_default_initial_phases(self):
        self.assertEqual(
            default_selected_phases("DIRECT_DEV", continuing=False),
            ["EXVT", "MSCIGT"],
        )
        self.assertEqual(
            default_selected_phases("DIRECT_DEV", continuing=True),
            ["CSMGVT", "LDP"],
        )

    def test_direct_dev_initial_rejects_asctg(self):
        with self.assertRaisesRegex(
            ValueError,
            "DIRECT_DEV initial runs only allow EXVT and MSCIGT",
        ):
            validate_phase_selection(
                "DIRECT_DEV",
                ["EXVT", "ASCTG"],
                continuing=False,
            )

    def test_harness_does_not_enter_awaiting_code_after_only_asctg(self):
        self.assertFalse(should_await_code("HARNESS", ["ASCTG"], had_failure=False, continuing=False))

    def test_integration_never_enters_awaiting_code(self):
        self.assertFalse(should_await_code("INTEGRATION", ["EXVT", "MSCIGT"], had_failure=False, continuing=False))

    def test_direct_dev_does_not_enter_awaiting_code_if_continuing(self):
        self.assertFalse(should_await_code("DIRECT_DEV", ["EXVT", "MSCIGT"], had_failure=False, continuing=True))

    def test_asctg_with_no_harness_project_file_fails_cleanly(self):
        callbacks: list[dict] = []

        with TemporaryDirectory() as tmpdir:
            steps_root = Path(tmpdir)
            (steps_root / "base.project.xml").write_text("<project />", encoding="utf-8")

            def fake_callback(_url, payload, _task_id):
                callbacks.append(payload)

            with (
                patch.object(generator_module, "_run_pipeline", type(self).original_run_pipeline),
                patch.object(generator_module, "_send_callback", side_effect=fake_callback),
                patch.object(
                    generator_module,
                    "_resolve_project_file",
                    return_value=("project-1", "base.project.xml", steps_root),
                ),
                patch.object(
                    generator_module,
                    "execute_asctg_from_steps_dir",
                    return_value={
                        "success": True,
                        "return_code": 0,
                        "workspace_root": str(steps_root),
                        "project_path": str(steps_root / "base.project.xml"),
                        "stdout": "",
                        "stderr": "",
                        "generated_files": [],
                    },
                ),
                patch.object(generator_module.ToolExecutor, "execute_in_project") as execute_mock,
            ):
                generator_module._run_pipeline(
                    task_id="task-7",
                    project_id="project-1",
                    output_dir="/workspace",
                    callback_url="http://localhost/callback",
                    selected_phases=["ASCTG", "CSMGVT"],
                    continue_on_error=False,
                    phase_params={"ASCTG": {"selected_components": "comp_a,comp_b"}},
                    skip_export=True,
                    workflow_mode="HARNESS",
                    continuing=False,
                )

        self.assertFalse(execute_mock.called)
        self.assertEqual(callbacks[-1]["status"], "FAILED")
        self.assertIn("harness project file", " ".join(callbacks[-1]["logs"]).lower())

    def test_asctg_without_components_or_config_fails_in_harness_mode(self):
        callbacks: list[dict] = []

        with TemporaryDirectory() as tmpdir:
            steps_root = Path(tmpdir)
            (steps_root / "base.project.xml").write_text("<project />", encoding="utf-8")

            def fake_callback(_url, payload, _task_id):
                callbacks.append(payload)

            with (
                patch.object(generator_module, "_run_pipeline", type(self).original_run_pipeline),
                patch.object(generator_module, "_send_callback", side_effect=fake_callback),
                patch.object(
                    generator_module,
                    "_resolve_project_file",
                    return_value=("project-1", "base.project.xml", steps_root),
                ),
                patch.object(generator_module.ToolExecutor, "execute_in_project") as execute_mock,
            ):
                generator_module._run_pipeline(
                    task_id="task-8",
                    project_id="project-1",
                    output_dir="/workspace",
                    callback_url="http://localhost/callback",
                    selected_phases=["ASCTG"],
                    continue_on_error=False,
                    phase_params={},
                    skip_export=True,
                    workflow_mode="HARNESS",
                    continuing=False,
                )

        self.assertFalse(execute_mock.called)
        self.assertEqual(callbacks[-1]["status"], "FAILED")
        self.assertIn("missing selected components or config.xml", " ".join(callbacks[-1]["logs"]).lower())

    def test_continue_run_reuses_persisted_active_project_file(self):
        callbacks: list[dict] = []
        recorded_tool_runs: list[tuple[str, str]] = []

        with TemporaryDirectory() as tmpdir:
            steps_root = Path(tmpdir)
            (steps_root / "base.project.xml").write_text("<project />", encoding="utf-8")
            (steps_root / "base-harness.project.xml").write_text("<project />", encoding="utf-8")

            def fake_callback(_url, payload, _task_id):
                callbacks.append(payload)

            def fake_execute_in_project(*, tool_id, project_file, **_kwargs):
                recorded_tool_runs.append((tool_id, project_file))
                return {
                    "success": True,
                    "return_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "generated_files": [],
                    "project_path": str(steps_root / project_file),
                }

            with (
                patch.object(generator_module, "_run_pipeline", type(self).original_run_pipeline),
                patch.object(generator_module, "_send_callback", side_effect=fake_callback),
                patch.object(
                    generator_module,
                    "_resolve_project_file",
                    return_value=("project-1", "base.project.xml", steps_root),
                ),
                patch.object(generator_module.ToolExecutor, "execute_in_project", side_effect=fake_execute_in_project),
            ):
                generator_module._run_pipeline(
                    task_id="task-9",
                    project_id="project-1",
                    output_dir="/workspace",
                    callback_url="http://localhost/callback",
                    selected_phases=["CSMGVT"],
                    continue_on_error=False,
                    phase_params={},
                    skip_export=True,
                    workflow_mode="HARNESS",
                    continuing=True,
                    base_project_file="base.project.xml",
                    active_project_file="base-harness.project.xml",
                    harness_project_file="base-harness.project.xml",
                )

        self.assertEqual(recorded_tool_runs, [("csmgvt", "base-harness.project.xml")])
        self.assertEqual(callbacks[-1]["workflowMode"], "HARNESS_DEV")
        self.assertEqual(callbacks[-1]["activeProjectFile"], "base-harness.project.xml")


if __name__ == "__main__":
    unittest.main()
