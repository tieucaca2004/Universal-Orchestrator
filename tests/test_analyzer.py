import unittest

from universal_orchestrator.analysis.analyzer import TaskAnalyzer
from universal_orchestrator.core.models import Constitution, ProjectProfile, TaskRequest


def _profile(**overrides):
    defaults = {"name": "demo-project", "repository_path": "/workspace/demo-project"}
    defaults.update(overrides)
    return ProjectProfile(**defaults)


class RequestParsingTests(unittest.TestCase):
    def test_objective_comes_from_title_and_scope_from_description(self):
        request = TaskRequest(title="  Add health check  ", description="Expose /health")
        spec = TaskAnalyzer().analyze(request, _profile())

        self.assertEqual(spec.objective, "Add health check")
        self.assertEqual(spec.scope, "Expose /health")

    def test_explicit_scope_metadata_overrides_description(self):
        request = TaskRequest(
            title="Add health check",
            description="ignored",
            metadata={"scope": "Backend service only"},
        )
        spec = TaskAnalyzer().analyze(request, _profile())

        self.assertEqual(spec.scope, "Backend service only")


class RequirementExtractionTests(unittest.TestCase):
    def test_extracts_all_structured_fields_from_metadata(self):
        request = TaskRequest(
            title="Add health check",
            description="Expose /health",
            metadata={
                "constraints": ["must not require auth"],
                "dependencies": ["database migration 0007"],
                "expected_deliverables": ["GET /health endpoint"],
                "acceptance_criteria": ["returns 200 when DB reachable"],
                "risks": ["false positive under DB failover"],
                "required_capabilities": ["python", "sqlite"],
            },
        )
        spec = TaskAnalyzer().analyze(request, _profile())

        self.assertEqual(spec.constraints, ["must not require auth"])
        self.assertEqual(spec.dependencies, ["database migration 0007"])
        self.assertEqual(spec.expected_deliverables, ["GET /health endpoint"])
        self.assertEqual(spec.acceptance_criteria, ["returns 200 when DB reachable"])
        self.assertEqual(spec.risks, ["false positive under DB failover"])
        self.assertEqual(spec.required_capabilities, ["python", "sqlite"])
        self.assertEqual(spec.missing_information, [])

    def test_a_single_string_metadata_value_is_treated_as_one_item(self):
        request = TaskRequest(
            title="Add health check",
            description="x",
            metadata={"constraints": "must not require auth"},
        )
        spec = TaskAnalyzer().analyze(request, _profile())

        self.assertEqual(spec.constraints, ["must not require auth"])


class MissingInformationTests(unittest.TestCase):
    def test_flags_missing_objective(self):
        request = TaskRequest(title="   ", description="x")
        spec = TaskAnalyzer().analyze(request, _profile())
        self.assertTrue(any("objective" in item for item in spec.missing_information))

    def test_flags_missing_scope(self):
        request = TaskRequest(title="Add health check", description="")
        spec = TaskAnalyzer().analyze(request, _profile())
        self.assertTrue(any("scope" in item for item in spec.missing_information))

    def test_flags_missing_acceptance_criteria_and_deliverables(self):
        request = TaskRequest(title="Add health check", description="Expose /health")
        spec = TaskAnalyzer().analyze(request, _profile())
        self.assertTrue(any("acceptance_criteria" in item for item in spec.missing_information))
        self.assertTrue(any("expected_deliverables" in item for item in spec.missing_information))

    def test_flags_project_profile_without_repository_path(self):
        request = TaskRequest(
            title="Add health check",
            description="Expose /health",
            metadata={
                "acceptance_criteria": ["x"],
                "expected_deliverables": ["y"],
            },
        )
        spec = TaskAnalyzer().analyze(request, _profile(repository_path=""))
        self.assertTrue(
            any("repository_path" in item for item in spec.missing_information)
        )

    def test_well_formed_request_has_no_missing_information(self):
        request = TaskRequest(
            title="Add health check",
            description="Expose /health",
            metadata={
                "acceptance_criteria": ["returns 200"],
                "expected_deliverables": ["endpoint"],
            },
        )
        spec = TaskAnalyzer().analyze(request, _profile())
        self.assertEqual(spec.missing_information, [])


class ProjectProfileInputTests(unittest.TestCase):
    def test_project_profile_is_never_looked_up_only_used_as_given(self):
        profile_a = _profile(name="alpha", repository_path="/workspace/alpha")
        profile_b = _profile(name="beta", repository_path="/workspace/beta")
        request = TaskRequest(
            title="t",
            description="d",
            metadata={"acceptance_criteria": ["x"], "expected_deliverables": ["y"]},
        )
        analyzer = TaskAnalyzer()

        spec_a = analyzer.analyze(request, profile_a)
        spec_b = analyzer.analyze(request, profile_b)

        self.assertEqual(spec_a.missing_information, [])
        self.assertEqual(spec_b.missing_information, [])


class ConstitutionConstraintTests(unittest.TestCase):
    def test_constitution_rules_are_merged_into_constraints_with_source_tag(self):
        request = TaskRequest(title="t", description="d")
        constitution = Constitution(rules=["never touch production without approval"], source="FOUNDER_CONSTITUTION")
        spec = TaskAnalyzer().analyze(request, _profile(), constitution)

        self.assertIn(
            "[FOUNDER_CONSTITUTION] never touch production without approval",
            spec.constraints,
        )

    def test_no_constitution_means_no_fabricated_constraints(self):
        request = TaskRequest(title="t", description="d")
        spec = TaskAnalyzer().analyze(request, _profile(), constitution=None)
        self.assertEqual(spec.constraints, [])


if __name__ == "__main__":
    unittest.main()
