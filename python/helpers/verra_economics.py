from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from python.helpers import verra_receipt_chain


def load_ledger(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_economic_ledger.v1",
        "summary": {
            "session_spend_limit_usd": 50.0,
            "spent_usd": 0.0,
            "reserved_usd": 0.0,
        },
        "entries": [],
    }


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verra_receipt_chain.chain_receipt_doc(
        ledger,
        config_path=path.parent / "receipt_chain_config.json",
        list_key="entries",
        doc_kind="verra_economic_ledger_entries",
    )
    path.write_text(json.dumps(ledger, indent=2) + "\n", "utf-8")


def set_limit_from_policy(ledger: dict[str, Any], policy: dict[str, Any]) -> None:
    limit = float((policy.get("budgets") or {}).get("session_spend_limit_usd", 50.0))
    ledger.setdefault("summary", {})
    ledger["summary"]["session_spend_limit_usd"] = limit


def budget_summary(ledger: dict[str, Any]) -> dict[str, float]:
    summary = ledger.get("summary") or {}
    limit = float(summary.get("session_spend_limit_usd", 50.0))
    spent = float(summary.get("spent_usd", 0.0))
    reserved = float(summary.get("reserved_usd", 0.0))
    remaining = max(0.0, limit - spent - reserved)
    return {
        "limit_usd": round(limit, 4),
        "spent_usd": round(spent, 4),
        "reserved_usd": round(reserved, 4),
        "remaining_usd": round(remaining, 4),
    }


def append_entry(
    ledger: dict[str, Any],
    *,
    entry: dict[str, Any],
    max_entries: int = 1000,
) -> None:
    ledger.setdefault("entries", []).append(entry)
    del ledger["entries"][:-max_entries]


def record_preview(
    ledger: dict[str, Any],
    *,
    at: str,
    session_id: str,
    tool_name: str,
    estimated_cost_usd: float,
    risk_level: str,
    reason: str,
) -> None:
    append_entry(
        ledger,
        entry={
            "at": at,
            "type": "simulation_preview",
            "session_id": session_id,
            "tool_name": tool_name,
            "risk_level": risk_level,
            "estimated_cost_usd": float(estimated_cost_usd),
            "reason": reason,
        },
    )


def record_live_action(
    ledger: dict[str, Any],
    *,
    at: str,
    session_id: str,
    tool_name: str,
    estimated_cost_usd: float,
    risk_level: str,
    success: bool,
    message_preview: str,
) -> None:
    append_entry(
        ledger,
        entry={
            "at": at,
            "type": "live_action",
            "session_id": session_id,
            "tool_name": tool_name,
            "risk_level": risk_level,
            "estimated_cost_usd": float(estimated_cost_usd),
            "success": bool(success),
            "message_preview": message_preview[:500],
        },
    )
    # Conservative accounting: only book cost for economic actions / nonzero estimates.
    if estimated_cost_usd > 0:
        ledger.setdefault("summary", {})
        ledger["summary"]["spent_usd"] = float(ledger["summary"].get("spent_usd", 0.0)) + float(
            estimated_cost_usd
        )
