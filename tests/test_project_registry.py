import json
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.models import ProjectProfile
from universal_orchestrator.errors import DuplicateError, NotFoundError, OrchestratorError
from universal_orchestrator.registry.projects import ProjectRegistry, load_from_config


def _profile(**overrides):
    defaults = {"name": "demo", "repository_path": "/workspace/demo"}
    defaults.update(overrides)
    return ProjectProfile(**defaults)


class RegistrationTests(unittest.TestCase):
    def test_register_then_get(self):
        registry = ProjectRegistry()
        profile = registry.register(_profile())
        self.assertIs(registry.get(profile.project_id), profile)

    def test_duplicate_project_id_raises(self):
        registry = ProjectRegistry()
        profile = _profile()
        registry.register(profile)
        with self.assertRaises(DuplicateError):
            registry.register(profile)

    def test_rejects_empty_name_or_repository_path(self):
        registry = ProjectRegistry()
        with self.assertRaises(ValueError):
            registry.register(_profile(name=""))
        with self.assertRaises(ValueError):
            registry.register(_profile(repository_path=""))

    def test_rejects_operation_in_both_allowed_and_restricted(self):
        registry = ProjectRegistry()
        with self.assertRaises(ValueError):
            registry.register(
                _profile(allowed_operations=["deploy"], restricted_operations=["deploy"])
            )


class RetrievalTests(unittest.TestCase):
    def test_missing_profile_raises_not_found(self):
        registry = ProjectRegistry()
        with self.assertRaises(NotFoundError):
            registry.get("proj_missing")

    def test_list_returns_all_registered_profiles(self):
        registry = ProjectRegistry()
        a = registry.register(_profile(name="a"))
        b = registry.register(_profile(name="b"))
        self.assertEqual({p.project_id for p in registry.list()}, {a.project_id, b.project_id})


class MetadataUpdateTests(unittest.TestCase):
    def test_update_metadata_merges_without_touching_structural_fields(self):
        registry = ProjectRegistry()
        profile = registry.register(_profile(main_branch="develop"))
        registry.update_metadata(profile.project_id, {"owner_team": "platform"})

        updated = registry.get(profile.project_id)
        self.assertEqual(updated.metadata["owner_team"], "platform")
        self.assertEqual(updated.main_branch, "develop")


class ProjectRestrictionTests(unittest.TestCase):
    def test_restricted_operation_raises(self):
        registry = ProjectRegistry()
        profile = registry.register(_profile(restricted_operations=["force_push"]))
        with self.assertRaises(OrchestratorError):
            registry.check_operation_allowed(profile.project_id, "force_push")

    def test_operation_outside_allow_list_raises(self):
        registry = ProjectRegistry()
        profile = registry.register(_profile(allowed_operations=["read", "test"]))
        with self.assertRaises(OrchestratorError):
            registry.check_operation_allowed(profile.project_id, "deploy")

    def test_operation_in_allow_list_is_permitted(self):
        registry = ProjectRegistry()
        profile = registry.register(_profile(allowed_operations=["read", "test"]))
        registry.check_operation_allowed(profile.project_id, "test")  # does not raise

    def test_no_lists_configured_permits_anything(self):
        registry = ProjectRegistry()
        profile = registry.register(_profile())
        registry.check_operation_allowed(profile.project_id, "anything")  # does not raise


class ConfigDrivenLoadingTests(unittest.TestCase):
    def test_load_from_config_builds_profiles_from_a_founder_supplied_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            config_path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "project_id": "proj_demo",
                                "name": "demo",
                                "repository_path": "/workspace/demo",
                                "technology_stack": "python",
                            }
                        ]
                    }
                )
            )
            profiles = load_from_config(config_path)

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].project_id, "proj_demo")
        # A single string value for a list field is normalized to a list.
        self.assertEqual(profiles[0].technology_stack, ["python"])

    def test_loaded_profiles_register_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            config_path.write_text(
                json.dumps({"projects": [{"name": "demo", "repository_path": "/workspace/demo"}]})
            )
            registry = ProjectRegistry()
            for profile in load_from_config(config_path):
                registry.register(profile)

        self.assertEqual(len(registry.list()), 1)


if __name__ == "__main__":
    unittest.main()
