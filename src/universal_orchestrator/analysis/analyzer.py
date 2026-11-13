"""Task Analyzer.

Deterministic, rule-based extraction only — this module never calls an
external AI agent and never guesses at facts it was not given. Everything
it cannot find in the explicit input is reported in
`TaskSpecification.missing_information` rather than invented.

Project Profile and Constitution are both explicit, caller-supplied
inputs (see `core.models.ProjectProfile`, `core.models.Constitution`) —
this milestone does not implement a Project Registry or a Constitution
file loader, so the Analyzer never looks either up on its own.

`TaskRequest.metadata` carries the structured hints a caller already has
about the request. Recognised keys (each optional, each a list[str]
unless noted):
    constraints, dependencies, expected_deliverables,
    acceptance_criteria, risks, required_capabilities, scope (str)
Unrecognised keys are ignored, not fabricated into anything.
"""

from __future__ import annotations

from universal_orchestrator.core.models import (
    Constitution,
    ProjectProfile,
    TaskRequest,
    TaskSpecification,
)

_LIST_METADATA_FIELDS = (
    "constraints",
    "dependencies",
    "expected_deliverables",
    "acceptance_criteria",
    "risks",
    "required_capabilities",
)


class TaskAnalyzer:
    """Stateless; safe to share a single instance across requests."""

    def analyze(
        self,
        request: TaskRequest,
        project_profile: ProjectProfile,
        constitution: Constitution | None = None,
    ) -> TaskSpecification:
        objective = request.title.strip()
        scope = str(request.metadata.get("scope", request.description)).strip()

        fields: dict[str, list[str]] = {
            name: _as_str_list(request.metadata.get(name, []))
            for name in _LIST_METADATA_FIELDS
        }

        constraints = list(fields["constraints"])
        if constitution is not None:
            for rule in constitution.rules:
                constraints.append(f"[{constitution.source}] {rule}")

        missing_information = _find_missing_information(
            objective=objective,
            scope=scope,
            acceptance_criteria=fields["acceptance_criteria"],
            expected_deliverables=fields["expected_deliverables"],
            project_profile=project_profile,
        )

        return TaskSpecification(
            objective=objective,
            scope=scope,
            constraints=constraints,
            dependencies=fields["dependencies"],
            expected_deliverables=fields["expected_deliverables"],
            acceptance_criteria=fields["acceptance_criteria"],
            risks=fields["risks"],
            required_capabilities=fields["required_capabilities"],
            missing_information=missing_information,
        )


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _find_missing_information(
    *,
    objective: str,
    scope: str,
    acceptance_criteria: list[str],
    expected_deliverables: list[str],
    project_profile: ProjectProfile,
) -> list[str]:
    missing: list[str] = []
    if not objective:
        missing.append("objective: request title is empty")
    if not scope:
        missing.append("scope: neither metadata['scope'] nor description was provided")
    if not acceptance_criteria:
        missing.append("acceptance_criteria: none specified")
    if not expected_deliverables:
        missing.append("expected_deliverables: none specified")
    if not project_profile.repository_path:
        missing.append("project_profile.repository_path: not set")
    return missing
