"""Configuration management: environment variables override an optional
JSON config file, which overrides built-in defaults.

Resource-efficiency note: defaults assume the current execution host (a
4-thread, ~7 GB RAM laptop) — nothing here requires a database server,
container runtime, or network service beyond the filesystem.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILE_ENV = "UO_CONFIG_FILE"
DATA_DIR_ENV = "UO_DATA_DIR"
LOG_DIR_ENV = "UO_LOG_DIR"
LOG_LEVEL_ENV = "UO_LOG_LEVEL"
APPROVAL_DEFAULT_ENV = "UO_APPROVAL_REQUIRED_BY_DEFAULT"

DEFAULT_DATA_DIRNAME = "var/state"
DEFAULT_LOG_DIRNAME = "var/logs"
_FALSEY = {"0", "false", "no", "off"}


@dataclass(slots=True)
class Settings:
    repo_root: Path
    data_dir: Path
    log_dir: Path
    log_level: str = "INFO"
    approval_required_by_default: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "orchestrator.sqlite3"


def _repo_root() -> Path:
    # src/universal_orchestrator/config/settings.py -> repo root is 4 up.
    return Path(__file__).resolve().parents[3]


def load_settings(config_path: str | Path | None = None) -> Settings:
    root = _repo_root()

    overrides: dict[str, Any] = {}
    path = config_path or os.environ.get(CONFIG_FILE_ENV)
    if path:
        file_path = Path(path)
        if file_path.is_file():
            overrides = json.loads(file_path.read_text())

    data_dir = Path(
        os.environ.get(
            DATA_DIR_ENV, overrides.get("data_dir", root / DEFAULT_DATA_DIRNAME)
        )
    )
    log_dir = Path(
        os.environ.get(
            LOG_DIR_ENV, overrides.get("log_dir", root / DEFAULT_LOG_DIRNAME)
        )
    )
    log_level = str(
        os.environ.get(LOG_LEVEL_ENV, overrides.get("log_level", "INFO"))
    ).upper()

    approval_default = bool(overrides.get("approval_required_by_default", True))
    if APPROVAL_DEFAULT_ENV in os.environ:
        approval_default = (
            os.environ[APPROVAL_DEFAULT_ENV].strip().lower() not in _FALSEY
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        repo_root=root,
        data_dir=data_dir,
        log_dir=log_dir,
        log_level=log_level,
        approval_required_by_default=approval_default,
        extra=overrides,
    )
