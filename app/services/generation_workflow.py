from dataclasses import dataclass, field, replace
from pathlib import Path

HARNESS_INITIAL_PHASES = ["EXVT", "ASCTG", "MSCIGT"]
HARNESS_CONTINUE_PHASES = ["CSMGVT", "LDP"]
INTEGRATION_INITIAL_PHASES = ["EXVT", "LDP"]
INTEGRATION_INITIAL_ORDER = ["EXVT", "CSMGVT", "LDP"]
INTEGRATION_CONTINUE_PHASES = ["CSMGVT", "LDP"]
KNOWN_WORKFLOW_MODES = {"HARNESS", "INTEGRATION"}
PHASE_ORDER = {
    ("HARNESS", False): HARNESS_INITIAL_PHASES,
    ("HARNESS", True): HARNESS_CONTINUE_PHASES,
    ("INTEGRATION", False): INTEGRATION_INITIAL_ORDER,
    ("INTEGRATION", True): INTEGRATION_CONTINUE_PHASES,
}


@dataclass(frozen=True)
class WorkflowContext:
    workflow_mode: str
    base_project_file: str
    active_project_file: str
    harness_project_file: str | None = None
    selected_phases: list[str] = field(default_factory=list)
    continuing: bool = False

    def with_active_project(self, project_file: str) -> "WorkflowContext":
        return replace(self, active_project_file=project_file)

    def with_harness_project(self, project_file: str) -> "WorkflowContext":
        return replace(self, active_project_file=project_file, harness_project_file=project_file)


def _normalize_workflow_mode(workflow_mode: str | None) -> str:
    normalized = (workflow_mode or "INTEGRATION").upper()
    if normalized not in KNOWN_WORKFLOW_MODES:
        raise ValueError(f"Unknown workflow mode: {workflow_mode}")
    return normalized


def _format_allowed_phases(phases: list[str]) -> str:
    if len(phases) <= 1:
        return phases[0] if phases else ""
    if len(phases) == 2:
        return " and ".join(phases)
    return ", ".join(phases[:-1]) + f" and {phases[-1]}"


def default_selected_phases(workflow_mode: str | None, continuing: bool) -> list[str]:
    mode = _normalize_workflow_mode(workflow_mode)

    if mode == "HARNESS":
        return HARNESS_CONTINUE_PHASES[:] if continuing else HARNESS_INITIAL_PHASES[:]
    if continuing:
        return INTEGRATION_CONTINUE_PHASES[:]
    return INTEGRATION_INITIAL_PHASES[:]


def resolve_phase_steps(
    workflow_mode: str | None,
    selected_phases: list[str] | None,
    continuing: bool,
) -> list[str]:
    mode = _normalize_workflow_mode(workflow_mode)
    phases = default_selected_phases(mode, continuing) if selected_phases is None else selected_phases
    canonical_order = PHASE_ORDER[(mode, continuing)]
    selected_set = set(phases)
    return [phase for phase in canonical_order if phase in selected_set]


def _resolve_harness_project_file(base_project_file: str, steps_root: str | Path) -> str:
    steps_path = Path(steps_root)
    project_candidates = sorted(steps_path.rglob("*.project.xml"))
    harness_candidates = [candidate for candidate in project_candidates if candidate.name != base_project_file]

    if harness_candidates:
        return harness_candidates[0].name

    raise FileNotFoundError(
        f"No harness project file found under Steps directory: {steps_path}"
    )


def activate_harness_project(context: WorkflowContext, steps_root: str | Path) -> WorkflowContext:
    harness_project_file = _resolve_harness_project_file(context.base_project_file, steps_root)
    return context.with_harness_project(harness_project_file)


def validate_phase_selection(
    workflow_mode: str | None,
    selected_phases: list[str] | None,
    continuing: bool,
) -> None:
    mode = _normalize_workflow_mode(workflow_mode)
    phases = [] if selected_phases is None else selected_phases
    allowed = PHASE_ORDER[(mode, continuing)]

    if mode == "HARNESS" and not continuing:
        invalid = [phase for phase in phases if phase not in allowed]
        if invalid:
            raise ValueError("HARNESS initial runs only allow EXVT, ASCTG and MSCIGT")
        return

    if mode == "HARNESS" and continuing:
        invalid = [phase for phase in phases if phase not in allowed]
        if invalid:
            raise ValueError("HARNESS continue runs only allow CSMGVT and LDP")
        return

    invalid = [phase for phase in phases if phase not in allowed]
    if invalid:
        raise ValueError(
            f"{mode} {'continue' if continuing else 'initial'} runs only allow "
            f"{_format_allowed_phases(allowed)}"
        )


def should_await_code(
    workflow_mode: str | None,
    selected_phases: list[str] | None,
    had_failure: bool,
    continuing: bool,
) -> bool:
    mode = _normalize_workflow_mode(workflow_mode)
    if mode != "HARNESS" or continuing or had_failure:
        return False

    phases = set(selected_phases or [])
    execution_phases = {"CSMGVT", "LDP"}
    return "MSCIGT" in phases and not phases.intersection(execution_phases)


def parse_continuing_flag(raw_value: object) -> bool:
    if raw_value is None:
        return False
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        raise ValueError(f"Invalid continuing value: {raw_value}")
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    raise ValueError(f"Invalid continuing value: {raw_value}")
