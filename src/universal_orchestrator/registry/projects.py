"""Project Profile Registry.

Supplies `ProjectProfile` objects to the Task Analyzer, the Task Manager
and the Agent Router — nothing else constructs or looks one up. No path
from this machine is hardcoded anywhere in this module; every profile
either comes from an explicit `register()` call or from
`load_from_config`, which reads a Founder-supplied JSON file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from universal_orchestrator.core.models import ProjectProfile
from universal_orchestrator.errors import DuplicateError, NotFoundError, OrchestratorError

_LIST_FIELDS = (
    "technology_stack",
    "instructions",
    "allowed_operations",
    "restricted_operations",
    "acceptance_criteria",
    "required_testing_commands",
    "security_constraints",
)


class ProjectRegistry:
    def __init__(self) -> None:
        self._projects: dict[str, ProjectProfile] = {}

    def register(self, profile: ProjectProfile) -> ProjectProfile:
        _validate_profile(profile)
        if profile.project_id in self._projects:
            raise DuplicateError("Project", profile.project_id)
        self._projects[profile.project_id] = profile
        return profile

    def get(self, project_id: str) -> ProjectProfile:
        profile = self._projects.get(project_id)
        if profile is None:
            raise NotFoundError("Project", project_id)
        return profile

    def list(self) -> list[ProjectProfile]:
        return list(self._projects.values())

    def update_metadata(self, project_id: str, updates: dict[str, Any]) -> ProjectProfile:
        """Merge `updates` into the profile's free-form `metadata` only —
        never the structural fields (repository_path, allowed/restricted
        operations, ...), which are governance decisions, not metadata."""
        profile = self.get(project_id)
        profile.metadata = {**profile.metadata, **updates}
        return profile

    def check_operation_allowed(self, project_id: str, operation: str) -> None:
        """Raise `OrchestratorError` if `operation` is restricted for
        this project, or if an allow-list exists and excludes it. A
        project with neither list set permits any operation (no implicit
        denial from an empty, unconfigured profile)."""
        profile = self.get(project_id)
        if operation in profile.restricted_operations:
            raise OrchestratorError(
                f"operation '{operation}' is restricted for project '{project_id}'"
            )
        if profile.allowed_operations and operation not in profile.allowed_operations:
            raise OrchestratorError(
                f"operation '{operation}' is not in the allowed operations "
                f"for project '{project_id}'"
            )


def _validate_profile(profile: ProjectProfile) -> None:
    if not profile.name.strip():
        raise ValueError("ProjectProfile.name must not be empty")
    if not profile.repository_path.strip():
        raise ValueError("ProjectProfile.repository_path must not be empty")
    overlap = set(profile.allowed_operations) & set(profile.restricted_operations)
    if overlap:
        raise ValueError(
            f"ProjectProfile '{profile.project_id}': operations cannot be both "
            f"allowed and restricted: {sorted(overlap)}"
        )


def load_from_config(config_path: Path) -> list[ProjectProfile]:
    """Read a Founder-supplied JSON file shaped like
    ``{"projects": [{"project_id": ..., "name": ..., "repository_path": ...,
    ...}, ...]}`` and return the resulting `ProjectProfile` list. Never
    called with a hardcoded path in this codebase — the path is always
    the caller's to supply (e.g. from `config.settings.Settings`)."""
    data = json.loads(Path(config_path).read_text())
    profiles = []
    for entry in data.get("projects", []):
        kwargs = dict(entry)
        for field_name in _LIST_FIELDS:
            if field_name in kwargs and not isinstance(kwargs[field_name], list):
                kwargs[field_name] = [kwargs[field_name]]
        profiles.append(ProjectProfile(**kwargs))
    return profiles
