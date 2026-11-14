import json
import os
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.config.settings import load_settings

_ENV_KEYS = (
    "UO_DATA_DIR",
    "UO_LOG_DIR",
    "UO_LOG_LEVEL",
    "UO_CONFIG_FILE",
    "UO_APPROVAL_REQUIRED_BY_DEFAULT",
)


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DefaultsTests(SettingsTestCase):
    def test_defaults_use_env_overrides(self):
        data_dir = self.tmp_path / "state"
        log_dir = self.tmp_path / "logs"
        os.environ["UO_DATA_DIR"] = str(data_dir)
        os.environ["UO_LOG_DIR"] = str(log_dir)

        settings = load_settings()

        self.assertEqual(settings.data_dir, data_dir)
        self.assertEqual(settings.log_dir, log_dir)
        self.assertTrue(data_dir.is_dir())
        self.assertTrue(log_dir.is_dir())
        self.assertEqual(settings.log_level, "INFO")
        self.assertTrue(settings.approval_required_by_default)
        self.assertEqual(settings.state_db_path, data_dir / "orchestrator.sqlite3")


class ApprovalFlagTests(SettingsTestCase):
    def test_can_be_disabled_via_env(self):
        os.environ["UO_DATA_DIR"] = str(self.tmp_path / "state")
        os.environ["UO_LOG_DIR"] = str(self.tmp_path / "logs")
        os.environ["UO_APPROVAL_REQUIRED_BY_DEFAULT"] = "false"

        settings = load_settings()

        self.assertFalse(settings.approval_required_by_default)


class ConfigFileTests(SettingsTestCase):
    def test_config_file_overrides_defaults(self):
        config_file = self.tmp_path / "uo.config.json"
        config_file.write_text(json.dumps({"log_level": "debug"}))
        os.environ["UO_DATA_DIR"] = str(self.tmp_path / "state")
        os.environ["UO_LOG_DIR"] = str(self.tmp_path / "logs")

        settings = load_settings(config_path=config_file)

        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.extra["log_level"], "debug")

    def test_env_log_level_wins_over_config_file(self):
        config_file = self.tmp_path / "uo.config.json"
        config_file.write_text(json.dumps({"log_level": "debug"}))
        os.environ["UO_DATA_DIR"] = str(self.tmp_path / "state")
        os.environ["UO_LOG_DIR"] = str(self.tmp_path / "logs")
        os.environ["UO_LOG_LEVEL"] = "warning"

        settings = load_settings(config_path=config_file)

        self.assertEqual(settings.log_level, "WARNING")


class DataDirPermissionsTests(SettingsTestCase):
    """Audit Finding C-9 regression test: data_dir/log_dir previously
    inherited whatever the process umask produced -- commonly
    world/group-readable -- even though they hold task content,
    Agent stdout/stderr, and execution evidence."""

    def test_data_dir_and_log_dir_are_created_owner_only(self):
        import stat

        os.environ["UO_DATA_DIR"] = str(self.tmp_path / "state")
        os.environ["UO_LOG_DIR"] = str(self.tmp_path / "logs")

        settings = load_settings()

        self.assertEqual(stat.S_IMODE(settings.data_dir.stat().st_mode), stat.S_IRWXU)
        self.assertEqual(stat.S_IMODE(settings.log_dir.stat().st_mode), stat.S_IRWXU)


if __name__ == "__main__":
    unittest.main()
