import json
import logging
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.audit.logger import LOGGER_NAME, configure_logging, get_logger


class LoggingTestCase(unittest.TestCase):
    def setUp(self):
        logging.getLogger(LOGGER_NAME).handlers.clear()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self):
        logger = logging.getLogger(LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()
        self._tmpdir.cleanup()


class ConfigureLoggingTests(LoggingTestCase):
    def test_writes_structured_json(self):
        logger = configure_logging(self.tmp_path, level="INFO")
        logger.info("hello orchestrator")
        for handler in logger.handlers:
            handler.flush()

        log_file = self.tmp_path / "orchestrator.log"
        self.assertTrue(log_file.is_file())

        line = log_file.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        self.assertEqual(record["message"], "hello orchestrator")
        self.assertEqual(record["level"], "INFO")
        self.assertIn("timestamp", record)

    def test_is_idempotent(self):
        logger1 = configure_logging(self.tmp_path, level="INFO")
        handler_count = len(logger1.handlers)
        logger2 = configure_logging(self.tmp_path, level="INFO")

        self.assertIs(logger1, logger2)
        self.assertEqual(len(logger2.handlers), handler_count)


class GetLoggerTests(LoggingTestCase):
    def test_returns_child_logger(self):
        configure_logging(self.tmp_path, level="INFO")
        child = get_logger("registry.projects")
        self.assertEqual(child.name, f"{LOGGER_NAME}.registry.projects")


if __name__ == "__main__":
    unittest.main()
