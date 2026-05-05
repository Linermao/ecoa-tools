from dataclasses import dataclass, field, replace
from pathlib import Path

DIRECT_DEV_INITIAL_PHASES = ["EXVT", "MSCIGT"]
DIRECT_DEV_CONTINUE_PHASES = ["CSMGVT", "LDP"]
HARNESS_DEV_INITIAL_PHASES = ["EXVT", "ASCTG", "MSCIGT"]
HARNESS_DEV_CONTINUE_PHASES = ["CSMGVT", "LDP"]
INTEGRATION_INITIAL_PHASES = ["EXVT", "LDP"]
INTEGRATION_INITIAL_ORDER = ["EXVT", "CSMGVT", "LDP"]
INTEGRATION_CONTINUE_PHASES = ["CSMGVT", "LDP"]
KNOWN_WORKFLOW_MODES = {"DIRECT_DEV", "HARNESS_DEV", "HARNESS", "INTEGRATION"}
PHASE_ORDER = {
    ("DIRECT_DEV", False): DIRECT_DEV_INITIAL_PHASES,
    ("DIRECT_DEV", True): DIRECT_DEV_CONTINUE_PHASES,
    ("HARNESS_DEV", False): HARNESS_DEV_INITIAL_PHASES,
    ("HARNESS_DEV", True): HARNESS_DEV_CONTINUE_PHASES,
    ("HARNESS", False): HARNESS_DEV_INITIAL_PHASES,
    ("HARNESS", True): HARNESS_DEV_CONTINUE_PHASES,
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
    normalized = (workflow_mode or "DIRECT_DEV").upper()
    # Legacy compatibility: HARNESS → HARNESS_DEV
    if normalized == "HARNESS":
        return "HARNESS_DEV"
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

    if mode == "DIRECT_DEV":
        return DIRECT_DEV_CONTINUE_PHASES[:] if continuing else DIRECT_DEV_INITIAL_PHASES[:]
    if mode == "HARNESS_DEV":
        return HARNESS_DEV_CONTINUE_PHASES[:] if continuing else HARNESS_DEV_INITIAL_PHASES[:]
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


def _resolve_harness_project_file(base_project_file: str, steps_root: str | Path, asctg_result: dict | None = None) -> str:
    """Resolve the harness project file after ASCTG.

    Priority:
    1. Use the project file returned by ASCTG result (if available).
    2. Match by *-harness.project.xml glob pattern.
    3. Fall back to first non-base project file (legacy behavior).
    """
    steps_path = Path(steps_root)

    # Priority 1: ASCTG result may indicate the harness project file
    if asctg_result and asctg_result.get("harness_project_file"):
        harness_name = asctg_result["harness_project_file"]
        if (steps_path / harness_name).exists() or any(steps_path.rglob(harness_name)):
            return harness_name

    # Priority 2: Match by *-harness.project.xml pattern
    harness_glob_candidates = sorted(steps_path.rglob("*-harness.project.xml"))
    if harness_glob_candidates:
        return harness_glob_candidates[0].name

    # Priority 3: Legacy fallback — first non-base project file
    project_candidates = sorted(steps_path.rglob("*.project.xml"))
    harness_candidates = [candidate for candidate in project_candidates if candidate.name != base_project_file]

    if harness_candidates:
        return harness_candidates[0].name

    raise FileNotFoundError(
        f"No harness project file found under Steps directory: {steps_path}"
    )


def activate_harness_project(context: WorkflowContext, steps_root: str | Path, asctg_result: dict | None = None) -> WorkflowContext:
    harness_project_file = _resolve_harness_project_file(context.base_project_file, steps_root, asctg_result)
    return context.with_harness_project(harness_project_file)


def validate_phase_selection(
    workflow_mode: str | None,
    selected_phases: list[str] | None,
    continuing: bool,
) -> None:
    mode = _normalize_workflow_mode(workflow_mode)
    phases = [] if selected_phases is None else selected_phases
    allowed = PHASE_ORDER[(mode, continuing)]

    if mode == "DIRECT_DEV" and not continuing:
        invalid = [phase for phase in phases if phase not in allowed]
        if invalid:
            raise ValueError("DIRECT_DEV initial runs only allow EXVT and MSCIGT")
        return

    if mode == "DIRECT_DEV" and continuing:
        invalid = [phase for phase in phases if phase not in allowed]
        if invalid:
            raise ValueError("DIRECT_DEV continue runs only allow CSMGVT and LDP")
        return

    if mode == "HARNESS_DEV" and not continuing:
        invalid = [phase for phase in phases if phase not in allowed]
        if invalid:
            raise ValueError("HARNESS_DEV initial runs only allow EXVT, ASCTG and MSCIGT")
        return

    if mode == "HARNESS_DEV" and continuing:
        invalid = [phase for phase in phases if phase not in allowed]
        if invalid:
            raise ValueError("HARNESS_DEV continue runs only allow CSMGVT and LDP")
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
    # Both DIRECT_DEV and HARNESS_DEV enter AWAITING_CODE after MSCIGT
    if mode not in ("DIRECT_DEV", "HARNESS_DEV") or continuing or had_failure:
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
