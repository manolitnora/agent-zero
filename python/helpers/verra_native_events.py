from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return f"{prefix}_{h.hexdigest()[:16]}"


def _event_id(family: str, *parts: Any) -> str:
    return _stable_id("evt", family, *parts)


def _trace_id(session_id: str) -> str:
    return _stable_id("trace", "session", session_id)


def _span_id(family: str, *parts: Any) -> str:
    return _stable_id("span", family, *parts)


def _correlation_id(family: str, *parts: Any) -> str:
    return _stable_id("corr", family, *parts)


def _safe_timestamp(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else _now_iso()


def _status_from_value(value: Any, default: str = "success") -> str:
    if not isinstance(value, dict):
        return default
    status = str(value.get("status", "")).strip().lower()
    if not status:
        return default
    if any(word in status for word in ("fail", "error", "blocked", "not_found", "too_large", "unset")):
        return "failed" if "fail" in status or "error" in status else "blocked"
    if "preview" in status or "sim" in status:
        return "simulated"
    if "partial" in status:
        return "warning"
    return "success"


def _receipt_status(receipt: dict[str, Any], default: str = "success") -> str:
    if str(receipt.get("status", "")).strip():
        return _status_from_value(receipt, default)
    if "response_ok" in receipt:
        return "success" if bool(receipt.get("response_ok")) else "failed"
    nested = receipt.get("result") if isinstance(receipt.get("result"), dict) else receipt.get("preview")
    return _status_from_value(nested, default)


def _read_doc(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text("utf-8"))
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def _chain(doc: dict[str, Any], source: Path) -> dict[str, Any] | None:
    root = doc.get("_receipt_chain")
    meta = root.get("receipts") if isinstance(root, dict) else None
    if not isinstance(meta, dict):
        return None
    return {
        "chain_id": str(source),
        "verified": bool(meta.get("verified", False)),
        "hash": str(meta.get("tail_hash", "")),
        "hmac_id": str(meta.get("alg", "")),
    }


def _base_event(
    *,
    event_id: str,
    event_type: str,
    receipt: dict[str, Any],
    index: int,
    source: Path,
    status: str,
    component: str,
    payload: dict[str, Any],
    correlation_id: str,
    parent_event_id: str | None,
    chain: dict[str, Any] | None,
    span_family: str,
) -> dict[str, Any]:
    session_id = str(receipt.get("session_id") or "unknown_session")
    event = {
        "schema_version": "verra.event.v1",
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": _safe_timestamp(receipt.get("at")),
        "session_id": session_id,
        "status": status,
        "causality": {
            "trace_id": _trace_id(session_id),
            "span_id": _span_id(span_family, payload.get("tool_name", ""), index),
            "parent_event_id": parent_event_id,
            "correlation_id": correlation_id,
        },
        "actor": {
            "component": component,
            "embodiment": "agent_zero",
            "runtime": "local",
        },
        "payload": payload,
        "extensions": {
            "agent_zero": {
                "legacy_type": receipt.get("type"),
                "source_receipt_key_set": sorted(receipt.keys()),
            }
        },
    }
    if chain is not None:
        event["chain"] = chain
    return event


def _family_events(
    source: Path,
    *,
    event_type_for,
    family: str,
    component: str,
    default_status: str,
    correlation_for,
    payload_for,
) -> list[dict[str, Any]]:
    doc = _read_doc(source)
    receipts = doc.get("receipts")
    if not isinstance(receipts, list):
        return []
    chain = _chain(doc, source)
    last_by_correlation: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            continue
        event_type = event_type_for(receipt)
        if not event_type:
            continue
        session_id = str(receipt.get("session_id") or "unknown_session")
        tool_name = str(receipt.get("tool_name") or "")
        at = _safe_timestamp(receipt.get("at"))
        event_id = _event_id(family, event_type, session_id, tool_name, index, at)
        correlation_id = correlation_for(receipt)
        parent = last_by_correlation.get(correlation_id)
        last_by_correlation[correlation_id] = event_id
        events.append(
            _base_event(
                event_id=event_id,
                event_type=event_type,
                receipt=receipt,
                index=index,
                source=source,
                status=_receipt_status(receipt, default_status),
                component=component,
                payload=payload_for(receipt, index, source),
                correlation_id=correlation_id,
                parent_event_id=parent,
                chain=chain,
                span_family=family,
            )
        )
    return events


def _fingerprint(receipt: dict[str, Any], *fields: str) -> str:
    return "|".join(str(receipt.get(field, "")) for field in fields)


def _tool_payload(wrapper: str):
    def build(receipt: dict[str, Any], index: int, source: Path) -> dict[str, Any]:
        body = dict(receipt)
        for key in ("at", "session_id", "type"):
            body.pop(key, None)
        return {
            "tool_name": str(receipt.get("tool_name") or ""),
            wrapper: body,
            "source_receipt_index": index,
            "source_receipts_file": str(source),
        }

    return build


def _promotion_events(data_dir: Path) -> list[dict[str, Any]]:
    source = data_dir / "promotion_receipts.json"
    doc = _read_doc(source)
    receipts = doc.get("receipts")
    if not isinstance(receipts, list):
        return []
    chain = _chain(doc, source)
    type_map = {
        "promotion_preview": ("promotion.preview", None),
        "promotion_preflight_eval": ("promotion.preflight", None),
        "promotion_apply": ("promotion.apply", None),
        "promotion_canary_check": ("promotion.canary", None),
        "promotion_rollback_preview": ("promotion.rollback", "preview"),
        "promotion_rollback_apply": ("promotion.rollback", "apply"),
    }
    last_by_corr: dict[str, str] = {}
    corr_by_rollback_dir: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            continue
        raw_type = str(receipt.get("type") or "")
        event_type, stage = type_map.get(raw_type, ("promotion.unknown", None))
        request = receipt.get("request") if isinstance(receipt.get("request"), dict) else {}
        rollback_dir = str(request.get("rollback_dir") or "")
        fingerprint = "|".join(
            (
                str(request.get("mode", "")),
                str(request.get("patch_path", "")),
                rollback_dir,
                str(request.get("label", "")),
                str(receipt.get("tool_name", "")),
                str(receipt.get("session_id", "")),
            )
        )
        corr = corr_by_rollback_dir.get(rollback_dir) if rollback_dir and raw_type.startswith("promotion_rollback") else None
        corr = corr or _correlation_id("promotion", fingerprint)
        at = _safe_timestamp(receipt.get("at"))
        event_id = _event_id("promotion", raw_type, receipt.get("session_id", ""), index, at)
        parent = last_by_corr.get(corr)
        last_by_corr[corr] = event_id
        result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
        if raw_type == "promotion_apply":
            applied_rollback_dir = str(result.get("rollback_dir") or "")
            if applied_rollback_dir:
                corr_by_rollback_dir.setdefault(applied_rollback_dir, corr)
        payload = {
            "tool_name": receipt.get("tool_name"),
            "request": request,
            "source_receipt_index": index,
            "source_receipts_file": str(source),
        }
        if "preview" in receipt:
            payload["preview"] = receipt.get("preview")
        if "result" in receipt:
            payload["result"] = receipt.get("result")
        if "source" in receipt:
            payload["source"] = receipt.get("source")
        if stage:
            payload["stage"] = stage
        nested = receipt.get("result") if isinstance(receipt.get("result"), dict) else receipt.get("preview")
        events.append(
            _base_event(
                event_id=event_id,
                event_type=event_type,
                receipt=receipt,
                index=index,
                source=source,
                status=_status_from_value(nested),
                component="verra_promotion_pipeline",
                payload=payload,
                correlation_id=corr,
                parent_event_id=parent,
                chain=chain,
                span_family="promotion",
            )
        )
    return events


def _kernel_events(data_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    sessions = data_dir / "sessions"
    if not sessions.exists():
        return events
    for session_file in sorted(sessions.glob("*.json")):
        doc = _read_doc(session_file)
        session_id = str(doc.get("id") or session_file.stem)
        receipts = doc.get("receipts")
        if not isinstance(receipts, list):
            continue
        for index, receipt in enumerate(receipts):
            if not isinstance(receipt, dict) or "kernel_diagnostics" not in receipt or "policy_hints" not in receipt:
                continue
            at = _safe_timestamp(receipt.get("at") or doc.get("updated_at"))
            event_id = _event_id("kernel_step", session_id, index, at)
            events.append(
                {
                    "schema_version": "verra.event.v1",
                    "event_id": event_id,
                    "event_type": "kernel.step",
                    "timestamp": at,
                    "session_id": session_id,
                    "status": "success",
                    "causality": {
                        "trace_id": _trace_id(session_id),
                        "span_id": _span_id("kernel_step", session_id, index),
                        "parent_event_id": None,
                        "correlation_id": _correlation_id("kernel_step", session_id),
                    },
                    "actor": {"component": "verra_control", "embodiment": "agent_zero", "runtime": "local"},
                    "payload": {
                        "diagnostics": receipt.get("kernel_diagnostics") or {},
                        "policy_hints": receipt.get("policy_hints") or {},
                        "observation": {"metrics_for_session": receipt.get("snapshot_metrics") or {}},
                        "source_receipt_index": index,
                        "source_session_file": str(session_file),
                    },
                    "extensions": {"agent_zero": {"source_receipt_key_set": sorted(receipt.keys())}},
                    "shadow": {"cognition": receipt.get("shadow_cognition") or {}},
                }
            )
    return events


def _backfill_causes(events: list[dict[str, Any]]) -> None:
    last_sim: dict[tuple[str, str], str] = {}
    last_exec: dict[tuple[str, str], str] = {}
    last_policy: dict[tuple[str, str], str] = {}
    for event in sorted(events, key=lambda item: (str(item.get("timestamp", "")), str(item.get("event_id", "")))):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        key = (str(event.get("session_id") or ""), str(payload.get("tool_name") or ""))
        event_type = str(event.get("event_type") or "")
        causality = event.get("causality")
        if not isinstance(causality, dict):
            continue
        if event_type == "tool.simulation.preview" and key[1] and key in last_policy:
            causality["caused_by_action_id"] = last_policy[key]
        elif event_type in {"tool.execution.sandbox", "tool.execution.live"} and key[1]:
            cause = last_sim.get(key) or last_policy.get(key)
            if cause:
                causality["caused_by_action_id"] = cause
        elif event_type.startswith("payment.") and key[1]:
            cause = last_exec.get(key) or last_sim.get(key)
            if cause:
                causality["caused_by_action_id"] = cause
        elif event_type.startswith("promotion.") and key[1] and key in last_sim:
            causality["caused_by_action_id"] = last_sim[key]
        event_id = str(event.get("event_id") or "")
        if event_type == "policy.decision" and key[1]:
            last_policy[key] = event_id
        elif event_type == "tool.simulation.preview" and key[1]:
            last_sim[key] = event_id
        elif event_type in {"tool.execution.sandbox", "tool.execution.live"} and key[1]:
            last_exec[key] = event_id


def rebuild_native_events(data_dir: Path) -> None:
    data_dir = data_dir.resolve()
    tool_sim = _family_events(
        data_dir / "tool_simulation_receipts.json",
        event_type_for=lambda _r: "tool.simulation.preview",
        family="tool_sim",
        component="verra_policy_gate",
        default_status="simulated",
        correlation_for=lambda r: _correlation_id(
            "tool_sim",
            _fingerprint(r, "tool_name", "kind", "command", "cwd", "session_id"),
        ),
        payload_for=_tool_payload("simulation"),
    )
    sandbox = _family_events(
        data_dir / "sandbox_execution_receipts.json",
        event_type_for=lambda _r: "tool.execution.sandbox",
        family="sandbox_exec",
        component="verra_sandbox_exec",
        default_status="success",
        correlation_for=lambda r: _correlation_id(
            "tool_sandbox",
            _fingerprint(r, "tool_name", "runtime", "sandbox_cwd", "command_preview", "session_id"),
        ),
        payload_for=lambda r, i, source: {
            "tool_name": str(r.get("tool_name") or ""),
            "runtime": r.get("runtime"),
            "sandbox_cwd": r.get("sandbox_cwd"),
            "command_preview": r.get("command_preview"),
            "response_message_preview": r.get("response_message_preview"),
            "snapshots": {"before": r.get("before") or {}, "after": r.get("after") or {}},
            "source_receipt_index": i,
            "source_receipts_file": str(source),
        },
    )
    live = _family_events(
        data_dir / "tool_execution_live_receipts.json",
        event_type_for=lambda _r: "tool.execution.live",
        family="tool_exec_live",
        component="verra_capture_tool",
        default_status="success",
        correlation_for=lambda r: _correlation_id("tool_live", _fingerprint(r, "tool_name", "session_id")),
        payload_for=_tool_payload("execution"),
    )
    payments = _family_events(
        data_dir / "economic_receipts.json",
        event_type_for=lambda r: {
            "payment_simulation_preview": "payment.simulation.preview",
            "payment_execution": "payment.execution",
        }.get(str(r.get("type") or ""), "payment.unknown"),
        family="economic",
        component="verra_economic_receipt",
        default_status="success",
        correlation_for=lambda r: _correlation_id(
            "payment",
            _fingerprint(r, "tool_name", "adapter", "merchant", "resource", "session_id"),
        ),
        payload_for=_tool_payload("payment"),
    )
    policy = _family_events(
        data_dir / "policy_decision_receipts.json",
        event_type_for=lambda _r: "policy.decision",
        family="policy_decision",
        component="verra_policy_gate",
        default_status="warning",
        correlation_for=lambda r: _correlation_id(
            "policy_decision",
            "|".join(
                (
                    str(r.get("tool_name", "")),
                    str((r.get("decision") or {}).get("risk_class", "")),
                    str((r.get("decision") or {}).get("risk_level", "")),
                    str(bool((r.get("flags") or {}).get("simulate", False))),
                    str(bool((r.get("flags") or {}).get("live_approved", False))),
                    str(r.get("session_id", "")),
                )
            ),
        ),
        payload_for=_tool_payload("policy"),
    )
    for event in policy:
        decision = (event.get("payload") or {}).get("policy", {}).get("decision", {})
        event["status"] = "blocked" if decision.get("blocked") else "success" if decision.get("allowed") else "warning"
    events = sorted(
        _kernel_events(data_dir) + _promotion_events(data_dir) + tool_sim + sandbox + live + payments + policy,
        key=lambda item: (str(item.get("timestamp", "")), str(item.get("event_id", ""))),
    )
    _backfill_causes(events)
    output = data_dir / "verra_events_v1.json"
    output.write_text(
        json.dumps(
            {
                "schema_version": "verra.event.stream.v1",
                "updated_at": _now_iso(),
                "events": events,
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
