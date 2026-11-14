"""Scheduler Alerts CLI — Milestone 7, Component B.

    python3 -m universal_orchestrator.cli.alerts_cli scan
    python3 -m universal_orchestrator.cli.alerts_cli list [--all]
    python3 -m universal_orchestrator.cli.alerts_cli show <alert_id>
    python3 -m universal_orchestrator.cli.alerts_cli acknowledge <alert_id> --reason "..."

Deliberately separate from `approval_cli`: acknowledging an alert here
never records an `ApprovalDecision` and never touches
`integration_proposals` — the State-aware Stall Detector's own
module docstring is explicit that alerts must never silently block or
approve an integration, and this CLI is the only way a human
interacts with them, so it must not blur that line either.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from universal_orchestrator.config.settings import Settings, load_settings
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.stall_detector import StallDetector
from universal_orchestrator.execution.scheduler import SchedulerAlert
from universal_orchestrator.execution.worktree_manager import WorktreeManager


class AlertsCLI:
    def __init__(self, state: StateManager) -> None:
        self.state = state
        self.engine = OrchestrationEngine.bootstrap(state)
        self.worktrees = WorktreeManager(state)
        self.detector = StallDetector(state, self.engine.manager, self.worktrees)

    def scan(self) -> str:
        new_alerts = self.detector.scan()
        if not new_alerts:
            return "Scan complete. No new alerts."
        lines = [f"Scan complete. {len(new_alerts)} new alert(s):", ""]
        lines.extend(_format_alert_line(a) for a in new_alerts)
        return "\n".join(lines)

    def list_alerts(self, *, include_acknowledged: bool) -> str:
        alerts = self.state.list_alerts(acknowledged=None if include_acknowledged else False)
        if not alerts:
            return "No open alerts." if not include_acknowledged else "No alerts."
        lines = [f"{len(alerts)} alert(s):", ""]
        lines.extend(_format_alert_line(a) for a in alerts)
        return "\n".join(lines)

    def show(self, alert_id: str) -> str:
        alert = self.state.get_alert(alert_id)
        if alert is None:
            return f"No alert on record with id '{alert_id}'."
        lines = [
            f"Alert ID       : {alert.alert_id}",
            f"Task ID        : {alert.task_id}",
            f"Kind           : {alert.kind.value}",
            f"Severity       : {alert.severity.value}",
            f"Detected at    : {alert.detected_at.isoformat()}",
            f"Detail         : {alert.detail}",
            f"Acknowledged   : {alert.acknowledged}",
        ]
        if alert.acknowledged:
            lines.append(f"Acknowledged at: {alert.acknowledged_at.isoformat()}")
            lines.append(f"Reason         : {alert.acknowledged_reason!r}")
        return "\n".join(lines)

    def acknowledge(self, alert_id: str, reason: str) -> str:
        alert = self.state.get_alert(alert_id)
        if alert is None:
            raise SystemExit(f"No alert on record with id '{alert_id}'.")
        ok = self.state.acknowledge_alert(alert_id, datetime.now(timezone.utc), reason)
        if not ok:
            return f"Alert '{alert_id}' was already acknowledged."
        return f"Acknowledged alert '{alert_id}' (task {alert.task_id}, kind {alert.kind.value})."


def _format_alert_line(alert: SchedulerAlert) -> str:
    return (
        f"  [{alert.severity.value.upper():8}] {alert.kind.value:22} "
        f"task={alert.task_id}  id={alert.alert_id}  "
        f"detected_at={alert.detected_at.isoformat()}  ack={alert.acknowledged}\n"
        f"      {alert.detail}"
    )


def _open_state(db_path: str | None) -> StateManager:
    if db_path is not None:
        return StateManager(Path(db_path))
    settings: Settings = load_settings()
    return StateManager(settings.state_db_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alerts_cli",
        description="Scan for, list, and acknowledge M7 Scheduler alerts (never an approval mechanism).",
    )
    parser.add_argument("--db", default=None, help="Path to the orchestrator SQLite database.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("scan", help="Run the Stall Detector once and persist any new alerts.")

    list_cmd = subparsers.add_parser("list", help="List open alerts (or all, with --all).")
    list_cmd.add_argument("--all", action="store_true", help="Include already-acknowledged alerts.")

    show = subparsers.add_parser("show", help="Show full detail for one alert.")
    show.add_argument("alert_id")

    ack = subparsers.add_parser("acknowledge", help="Acknowledge an alert.")
    ack.add_argument("alert_id")
    ack.add_argument("--reason", default="", help="Why this is being acknowledged.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state = _open_state(args.db)
    try:
        cli = AlertsCLI(state)
        if args.command == "scan":
            print(cli.scan())
        elif args.command == "list":
            print(cli.list_alerts(include_acknowledged=args.all))
        elif args.command == "show":
            print(cli.show(args.alert_id))
        elif args.command == "acknowledge":
            print(cli.acknowledge(args.alert_id, args.reason))
        else:  # pragma: no cover - argparse's `required=True` prevents this
            parser.print_help()
            return 2
        return 0
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
