from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python.helpers import verra_receipt_chain


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_config() -> dict[str, Any]:
    return {
        "schema_version": "verra_trust_integrity.v1",
        "enabled": True,
        "quarantine_on_failure": True,
        "quarantine_on_chain_missing": False,
        "verify_on_boot": True,
        "monitored_files": [
            {"path": "promotion_receipts.json", "type": "list_doc", "list_key": "receipts", "doc_kind": "verra_promotion_receipts"},
            {"path": "sandbox_execution_receipts.json", "type": "list_doc", "list_key": "receipts", "doc_kind": "verra_sandbox_exec_receipts"},
            {"path": "tool_simulation_receipts.json", "type": "list_doc", "list_key": "receipts", "doc_kind": "verra_tool_sim_receipts"},
            {"path": "economic_receipts.json", "type": "list_doc", "list_key": "receipts", "doc_kind": "verra_economic_receipts"},
            {"path": "economic_ledger.json", "type": "list_doc", "list_key": "entries", "doc_kind": "verra_economic_ledger_entries"},
            {"path": "self_update_state.json", "type": "self_update_state"},
        ],
    }


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                cfg = default_config()
                cfg.update(data)
                return cfg
        except Exception:
            pass
    return default_config()


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "schema_version": "verra_trust_integrity_state.v1",
        "status": "unknown",
        "quarantine_active": False,
        "verified_at": None,
        "summary": {
            "files_checked": 0,
            "verified_ok": 0,
            "warnings": 0,
            "failures": 0,
        },
        "results": [],
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    path.write_text(json.dumps(state, indent=2) + "\n", "utf-8")


def verify_runtime(data_dir: Path, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_config(data_dir / "trust_integrity_config.json")
    out = load_state(data_dir / "trust_integrity_state.json")
    out["verified_at"] = _now_iso()
    out["results"] = []

    if not bool(cfg.get("enabled", True)):
        out["status"] = "disabled"
        out["quarantine_active"] = False
        out["summary"] = {"files_checked": 0, "verified_ok": 0, "warnings": 0, "failures": 0}
        return out

    rcfg = verra_receipt_chain.load_config(data_dir / "receipt_chain_config.json")
    files_checked = verified_ok = warnings = failures = 0
    quarantine_reasons: list[str] = []

    for spec in cfg.get("monitored_files", []) or []:
        result = _verify_spec(data_dir, rcfg, spec)
        out["results"].append(result)
        files_checked += 1
        sev = str(result.get("severity", "info"))
        if sev == "ok":
            verified_ok += 1
        elif sev == "warning":
            warnings += 1
            if bool(cfg.get("quarantine_on_chain_missing", False)) and str(result.get("status")) == "chain_missing":
                quarantine_reasons.append(f"{result.get('path')}: chain_missing")
        elif sev == "failure":
            failures += 1
            quarantine_reasons.append(f"{result.get('path')}: {result.get('status')}")

    quarantine_active = bool(cfg.get("quarantine_on_failure", True)) and len(quarantine_reasons) > 0
    out["quarantine_active"] = quarantine_active
    out["status"] = "quarantine" if quarantine_active else ("warning" if warnings else "ok")
    out["summary"] = {
        "files_checked": files_checked,
        "verified_ok": verified_ok,
        "warnings": warnings,
        "failures": failures,
    }
    if quarantine_reasons:
        out["quarantine_reason"] = "; ".join(quarantine_reasons[:5])
    else:
        out.pop("quarantine_reason", None)
    return out


def build_prompt_fragment(state: dict[str, Any]) -> str:
    summary = state.get("summary") or {}
    lines = [
        "VERRA TRUST INTEGRITY:",
        f"- status: {state.get('status', 'unknown')}",
        f"- quarantine_active: {bool(state.get('quarantine_active', False))}",
        f"- files_checked: {summary.get('files_checked', 0)}",
        f"- verified_ok: {summary.get('verified_ok', 0)} warnings: {summary.get('warnings', 0)} failures: {summary.get('failures', 0)}",
    ]
    if state.get("quarantine_reason"):
        lines.append(f"- quarantine_reason: {state.get('quarantine_reason')}")
        lines.append("- In quarantine, avoid live writes/economic actions; prefer read-only analysis and simulation previews.")
    return "\n".join(lines)


def _verify_spec(data_dir: Path, rcfg: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    rel = str(spec.get("path", "") or "").strip()
    typ = str(spec.get("type", "list_doc"))
    path = (data_dir / rel).resolve()
    result: dict[str, Any] = {"path": rel, "type": typ}
    if not path.exists():
        result.update({"status": "missing", "severity": "warning", "ok": None})
        return result
    try:
        doc = json.loads(path.read_text("utf-8"))
    except Exception as e:
        result.update({"status": "parse_error", "severity": "failure", "ok": False, "error": str(e)[:300]})
        return result

    if typ == "list_doc":
        list_key = str(spec.get("list_key", "receipts"))
        doc_kind = str(spec.get("doc_kind", "receipt_doc"))
        items = doc.get(list_key)
        if not isinstance(items, list):
            result.update({"status": "list_key_missing", "severity": "failure", "ok": False, "list_key": list_key})
            return result
        has_chain = _doc_has_list_chain(doc, list_key, items)
        if not has_chain and len(items) > 0:
            result.update({"status": "chain_missing", "severity": "warning", "ok": None, "count": len(items)})
            return result
        verify = verra_receipt_chain.verify_list_doc(doc, config=rcfg, list_key=list_key, doc_kind=doc_kind)
        if bool(verify.get("ok", False)):
            result.update({"status": "verified", "severity": "ok", "ok": True, "count": len(items)})
        else:
            result.update({"status": str(verify.get("reason", "verify_failed")), "severity": "failure", "ok": False, "detail": verify})
        return result

    if typ == "self_update_state":
        history = doc.get("history")
        has_history_chain = isinstance(history, list) and _doc_has_list_chain(doc, "history", history)
        has_state_chain = isinstance(doc.get("_state_chain"), dict)
        if (not has_history_chain and isinstance(history, list) and len(history) > 0) or not has_state_chain:
            # compatibility mode: warn if legacy unchained state exists
            result.update(
                {
                    "status": "chain_missing",
                    "severity": "warning",
                    "ok": None,
                    "has_state_chain": has_state_chain,
                    "has_history_chain": has_history_chain,
                }
            )
            return result
        hv = verra_receipt_chain.verify_list_doc(
            doc,
            config=rcfg,
            list_key="history",
            doc_kind="verra_self_update_state:history",
        )
        sv = verra_receipt_chain.verify_state_doc(
            doc,
            config_path=data_dir / "receipt_chain_config.json",
            doc_kind="verra_self_update_state",
        )
        if bool(hv.get("ok", False)) and bool(sv.get("ok", False)):
            result.update({"status": "verified", "severity": "ok", "ok": True, "history_count": len(history or [])})
        else:
            result.update({"status": "verify_failed", "severity": "failure", "ok": False, "history_verify": hv, "state_verify": sv})
        return result

    result.update({"status": "unsupported_type", "severity": "warning", "ok": None})
    return result


def _doc_has_list_chain(doc: dict[str, Any], list_key: str, items: list[Any]) -> bool:
    top = doc.get("_receipt_chain") or {}
    if isinstance(top, dict) and isinstance(top.get(list_key), dict):
        return True
    for item in items[:5]:
        if isinstance(item, dict) and isinstance(item.get("_chain"), dict):
            return True
    return False
