"""Code backflow service: generate patches from HARNESS workspace to source of truth.

This module handles:
1. Scanning returnable files from the test workspace based on allowlist patterns.
2. Excluding non-returnable artifacts (harness project, inc-gen, build, logs, etc.).
3. Generating a unified diff patch for the returnable files.
4. Detecting conflicts between the patch and the current source of truth.
5. Applying the patch (overwrite or new version) with audit logging.
"""

import difflib
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Default allowlist: source files that can be backflowed from HARNESS workspace
DEFAULT_RETURNABLE_PATTERNS = [
    "**/*.c",
    "**/*.cpp",
    "**/*.cc",
    "**/*.cxx",
    "**/*.h",
    "**/*.hpp",
    "**/*.hxx",
    "**/*.py",
    "**/*.xml",
]

# Directories and patterns that are always excluded from backflow
NON_RETURNABLE_DIR_NAMES = {
    "build",
    "cmake-build",
    "inc-gen",
    "harness_project",
    ".csm",
    "logs",
    "output",
    "__pycache__",
    ".git",
}

NON_RETURNABLE_FILE_PATTERNS = [
    "CMakeLists.txt",
    "Makefile",
    "*.cmake",
    "*.mk",
    "runtime.log",
    "csm.log",
    "*.o",
    "*.so",
    "*.a",
    "*.dylib",
    "*.dll",
    "*.exe",
]


@dataclass
class BackflowFile:
    """Represents a single file in the backflow patch."""
    relative_path: str
    source_path: str  # Absolute path in HARNESS workspace
    target_path: str  # Absolute path in source of truth
    is_returnable: bool
    exclusion_reason: Optional[str] = None
    diff: Optional[str] = None
    is_new: bool = False  # File doesn't exist in target yet
    is_conflict: bool = False
    conflict_reason: Optional[str] = None


@dataclass
class BackflowPatch:
    """Represents a complete backflow patch."""
    task_id: str
    returnable_files: list[BackflowFile] = field(default_factory=list)
    excluded_files: list[BackflowFile] = field(default_factory=list)
    patch_content: str = ""
    patch_hash: str = ""
    has_conflicts: bool = False
    conflict_files: list[BackflowFile] = field(default_factory=list)


@dataclass
class BackflowResult:
    """Result of applying a backflow patch."""
    success: bool
    applied_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)
    error_message: Optional[str] = None
    patch_artifact_path: Optional[str] = None
    source_revision: Optional[str] = None


def _is_excluded_dir(path: Path) -> bool:
    """Check if any component of the path is a non-returnable directory."""
    for part in path.parts:
        if part.lower() in NON_RETURNABLE_DIR_NAMES:
            return True
    return False


def _is_excluded_file(path: Path) -> bool:
    """Check if the filename matches a non-returnable pattern."""
    name = path.name
    for pattern in NON_RETURNABLE_FILE_PATTERNS:
        if pattern.startswith("*"):
            if name.endswith(pattern[1:]):
                return True
        elif name == pattern:
            return True
    return False


def _matches_allowlist(path: Path, patterns: list[str]) -> bool:
    """Check if a relative path matches any of the allowlist glob patterns."""
    rel_str = str(path).replace("\\", "/")
    for pattern in patterns:
        # Simple glob matching: ** matches any path segment
        if pattern.startswith("**/"):
            suffix = pattern[3:]
            if rel_str.endswith(suffix) or "/" + suffix in rel_str:
                return True
        elif rel_str == pattern:
            return True
    return False


def scan_backflow_files(
    harness_workspace: str | Path,
    source_root: str | Path,
    returnable_patterns: list[str] | None = None,
    returnable_components: list[str] | None = None,
) -> tuple[list[BackflowFile], list[BackflowFile]]:
    """Scan HARNESS workspace for returnable and excluded files.

    Args:
        harness_workspace: Path to the HARNESS test workspace root.
        source_root: Path to the source of truth root.
        returnable_patterns: Glob patterns for returnable files (default: DEFAULT_RETURNABLE_PATTERNS).
        returnable_components: Optional list of component names to restrict backflow to.

    Returns:
        Tuple of (returnable_files, excluded_files).
    """
    workspace = Path(harness_workspace)
    source = Path(source_root)
    patterns = returnable_patterns or DEFAULT_RETURNABLE_PATTERNS

    returnable: list[BackflowFile] = []
    excluded: list[BackflowFile] = []

    if not workspace.exists():
        return returnable, excluded

    for abs_path in workspace.rglob("*"):
        if not abs_path.is_file():
            continue

        rel_path = abs_path.relative_to(workspace)

        # Check directory exclusions first
        if _is_excluded_dir(rel_path):
            excluded.append(BackflowFile(
                relative_path=str(rel_path),
                source_path=str(abs_path),
                target_path=str(source / rel_path),
                is_returnable=False,
                exclusion_reason="excluded directory",
            ))
            continue

        # Check file exclusions
        if _is_excluded_file(rel_path):
            excluded.append(BackflowFile(
                relative_path=str(rel_path),
                source_path=str(abs_path),
                target_path=str(source / rel_path),
                is_returnable=False,
                exclusion_reason="excluded file pattern",
            ))
            continue

        # Check allowlist patterns
        if not _matches_allowlist(rel_path, patterns):
            excluded.append(BackflowFile(
                relative_path=str(rel_path),
                source_path=str(abs_path),
                target_path=str(source / rel_path),
                is_returnable=False,
                exclusion_reason="not in returnable allowlist",
            ))
            continue

        # Optional component filter
        if returnable_components:
            # Check if any component name appears in the path
            component_found = any(comp in str(rel_path) for comp in returnable_components)
            if not component_found:
                excluded.append(BackflowFile(
                    relative_path=str(rel_path),
                    source_path=str(abs_path),
                    target_path=str(source / rel_path),
                    is_returnable=False,
                    exclusion_reason="component not selected for backflow",
                ))
                continue

        target_abs = source / rel_path
        returnable.append(BackflowFile(
            relative_path=str(rel_path),
            source_path=str(abs_path),
            target_path=str(target_abs),
            is_returnable=True,
            is_new=not target_abs.exists(),
        ))

    return returnable, excluded


def generate_patch(
    returnable_files: list[BackflowFile],
    task_id: str,
) -> BackflowPatch:
    """Generate a unified diff patch from the returnable files.

    Args:
        returnable_files: List of BackflowFile objects with is_returnable=True.
        task_id: The generation task ID for audit.

    Returns:
        BackflowPatch with diff content and hash.
    """
    patch_lines: list[str] = []
    has_conflicts = False
    conflict_files: list[BackflowFile] = []

    for bf in returnable_files:
        source_path = Path(bf.source_path)
        target_path = Path(bf.target_path)

        try:
            source_content = source_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except Exception:
            source_content = []

        if bf.is_new:
            # New file: entire content is the diff
            diff_lines = list(difflib.unified_diff(
                [], source_content,
                fromfile=f"a/{bf.relative_path}",
                tofile=f"b/{bf.relative_path}",
            ))
            bf.diff = "".join(diff_lines)
            bf.is_conflict = False
        else:
            # Existing file: compute diff against target
            try:
                target_content = target_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except Exception:
                target_content = []

            diff_lines = list(difflib.unified_diff(
                target_content, source_content,
                fromfile=f"a/{bf.relative_path}",
                tofile=f"b/{bf.relative_path}",
            ))

            if diff_lines:
                bf.diff = "".join(diff_lines)

                # Conflict detection: check if source has been modified since the
                # skeleton was generated. We detect conflicts by checking if the
                # target file has modifications that aren't in the source.
                # Simple heuristic: if both files differ from each other and the
                # source has lines that the target doesn't, there may be a conflict.
                target_set = set(line.strip() for line in target_content if line.strip())
                source_set = set(line.strip() for line in source_content if line.strip())

                # Lines in target but not in source = potential conflict
                target_only = target_set - source_set
                # Lines in source but not in target = new additions (not conflict)
                source_only = source_set - target_set

                # If target has lines that source doesn't, those might be concurrent edits
                if target_only and source_only:
                    # Both sides have unique changes — potential conflict
                    bf.is_conflict = True
                    bf.conflict_reason = "Both source and target have unique modifications"
                    has_conflicts = True
                    conflict_files.append(bf)
                else:
                    bf.is_conflict = False
            else:
                # No differences
                bf.diff = None
                bf.is_conflict = False

        if bf.diff:
            patch_lines.append(bf.diff)

    patch_content = "".join(patch_lines)
    patch_hash = hashlib.sha256(patch_content.encode()).hexdigest()[:16] if patch_content else ""

    return BackflowPatch(
        task_id=task_id,
        returnable_files=[bf for bf in returnable_files if bf.diff is not None],
        excluded_files=[],  # Already separated in scan step
        patch_content=patch_content,
        patch_hash=patch_hash,
        has_conflicts=has_conflicts,
        conflict_files=conflict_files,
    )


def apply_patch(
    patch: BackflowPatch,
    source_root: str | Path,
    mode: str = "overwrite",
    patch_artifact_dir: str | Path | None = None,
) -> BackflowResult:
    """Apply a backflow patch to the source of truth.

    Args:
        patch: The BackflowPatch to apply.
        source_root: Path to the source of truth root.
        mode: "overwrite" to apply to current version, "new_version" to create a copy.
        patch_artifact_dir: Directory to save the patch file for audit.

    Returns:
        BackflowResult with applied/skipped/conflict file lists.
    """
    source = Path(source_root)
    applied: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    source_revision = f"backflow-{patch.task_id}-{patch.patch_hash}"

    if patch.has_conflicts:
        # Cannot auto-apply patches with conflicts
        for bf in patch.conflict_files:
            conflicts.append(bf.relative_path)
        return BackflowResult(
            success=False,
            applied_files=applied,
            skipped_files=skipped,
            conflict_files=conflicts,
            error_message="Patch has conflicts — manual resolution required",
            source_revision=source_revision,
        )

    for bf in patch.returnable_files:
        if bf.diff is None:
            skipped.append(bf.relative_path)
            continue

        target_path = Path(bf.target_path)
        source_path = Path(bf.source_path)

        try:
            # Ensure target directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)

            if mode == "new_version":
                # Create a backup of the original before overwriting
                if target_path.exists():
                    backup_path = target_path.with_suffix(target_path.suffix + ".bak")
                    shutil.copy2(target_path, backup_path)

            # Copy the source file to target
            shutil.copy2(source_path, target_path)
            applied.append(bf.relative_path)
        except Exception as exc:
            skipped.append(bf.relative_path)

    # Save patch artifact for audit
    patch_artifact_path = None
    if patch_artifact_dir and patch.patch_content:
        artifact_dir = Path(patch_artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_file = artifact_dir / f"backflow-patch-{patch.task_id}-{patch.patch_hash}.patch"
        try:
            artifact_file.write_text(patch.patch_content, encoding="utf-8")
            patch_artifact_path = str(artifact_file)
        except Exception:
            pass

    return BackflowResult(
        success=len(applied) > 0,
        applied_files=applied,
        skipped_files=skipped,
        conflict_files=conflicts,
        patch_artifact_path=patch_artifact_path,
        source_revision=source_revision,
    )
