from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python.helpers import verra_promotion, verra_sandbox, verra_policy, verra_receipt_chain


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_self_update_state.v1",
        "updated_at": _now_iso(),
        "cursors": {
            "promotion_receipts": 0,
            "sandbox_execution_receipts": 0,
            "tool_simulation_receipts": 0,
        },
        "metrics": {
            "reward_total": 0.0,
            "episodes": 0,
            "promotion_apply_success": 0,
            "promotion_apply_fail": 0,
            "promotion_preview_ready": 0,
            "promotion_preflight_pass": 0,
            "promotion_preflight_fail": 0,
            "promotion_canary_pass": 0,
            "promotion_canary_fail": 0,
            "rollback_apply_success": 0,
            "rollback_apply_partial": 0,
            "sandbox_exec_runs": 0,
            "sandbox_patch_artifact_runs": 0,
            "simulation_previews": 0,
        },
        "adaptive": {
            "strategy_mode": "stabilize",
            "verification_bias": 0.65,
            "world_model_refresh_interval_sec": 300,
            "prefer_sandbox": True,
            "promotion_require_simulation_first": True,
            "last_reward": 0.0,
        },
        "history": [],
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    verra_receipt_chain.stamp_state_doc(
        state,
        config_path=path.parent / "receipt_chain_config.json",
        doc_kind="verra_self_update_state",
        chain_list_key="history",
    )
    path.write_text(json.dumps(state, indent=2) + "\n", "utf-8")


def run_learning_cycle(data_dir: Path) -> dict[str, Any]:
    state_path = data_dir / "self_update_state.json"
    state = load_state(state_path)

    promo_doc = verra_promotion.load_receipts(data_dir / "promotion_receipts.json")
    sandbox_exec_doc = _load_receipts_generic(data_dir / "sandbox_execution_receipts.json")
    tool_sim_doc = _load_receipts_generic(data_dir / "tool_simulation_receipts.json")

    curs = state.setdefault("cursors", {})
    promo_recs = (promo_doc.get("receipts") or [])[int(curs.get("promotion_receipts", 0) or 0):]
    sandbox_exec_recs = (sandbox_exec_doc.get("receipts") or [])[int(curs.get("sandbox_execution_receipts", 0) or 0):]
    tool_sim_recs = (tool_sim_doc.get("receipts") or [])[int(curs.get("tool_simulation_receipts", 0) or 0):]

    cycle = _score_cycle(promo_recs, sandbox_exec_recs, tool_sim_recs)
    _accumulate_metrics(state, cycle)
    _update_adaptive(state)
    state.setdefault("adaptive", {})["last_reward"] = float(cycle.get("reward", 0.0) or 0.0)
    _apply_bounded_updates(data_dir, state)

    curs["promotion_receipts"] = len(promo_doc.get("receipts") or [])
    curs["sandbox_execution_receipts"] = len(sandbox_exec_doc.get("receipts") or [])
    curs["tool_simulation_receipts"] = len(tool_sim_doc.get("receipts") or [])

    hist = state.setdefault("history", [])
    hist.append({"at": _now_iso(), **cycle, "adaptive": dict((state.get("adaptive") or {}))})
    del hist[:-100]

    save_state(state_path, state)
    return {"state": state, "cycle": cycle}


def self_update_prompt_fragment(state: dict[str, Any]) -> str:
    a = (state.get("adaptive") or {})
    m = (state.get("metrics") or {})
    return "\n".join(
        [
            "VERRA SELF-UPDATE LOOP:",
            f"- strategy_mode: {a.get('strategy_mode', 'stabilize')} (tunnelling vs rabbithole control)",
            f"- verification_bias: {a.get('verification_bias', 0.65)}",
            f"- world_model_refresh_interval_sec: {a.get('world_model_refresh_interval_sec', 300)}",
            f"- prefer_sandbox: {bool(a.get('prefer_sandbox', True))}",
            f"- promotion_require_simulation_first: {bool(a.get('promotion_require_simulation_first', True))}",
            f"- reward_total: {round(float(m.get('reward_total', 0.0) or 0.0), 3)} over {int(m.get('episodes', 0) or 0)} cycles",
            "- Learn only from verifiable receipts (simulation previews, sandbox exec deltas, promotion apply/rollback outcomes).",
        ]
    )


def _score_cycle(promo_recs: list[dict[str, Any]], sandbox_exec_recs: list[dict[str, Any]], tool_sim_recs: list[dict[str, Any]]) -> dict[str, Any]:
    reward = 0.0
    counts = {
        "promotion_apply_success": 0,
        "promotion_apply_fail": 0,
        "promotion_preview_ready": 0,
        "promotion_preflight_pass": 0,
        "promotion_preflight_fail": 0,
        "promotion_canary_pass": 0,
        "promotion_canary_fail": 0,
        "rollback_apply_success": 0,
        "rollback_apply_partial": 0,
        "sandbox_exec_runs": len(sandbox_exec_recs),
        "sandbox_patch_artifact_runs": 0,
        "simulation_previews": len(tool_sim_recs),
    }

    for r in promo_recs:
        typ = str(r.get("type", ""))
        if typ == "promotion_preview":
            status = str(((r.get("preview") or {}).get("status", "")))
            if status == "preview_ready":
                counts["promotion_preview_ready"] += 1
                reward += 0.6
            else:
                reward -= 0.3
        elif typ == "promotion_apply":
            status = str(((r.get("result") or {}).get("status", "")))
            if status == "applied":
                counts["promotion_apply_success"] += 1
                reward += 2.5
            else:
                counts["promotion_apply_fail"] += 1
                reward -= 2.0
        elif typ == "promotion_preflight_eval":
            status = str(((r.get("result") or {}).get("status", "")))
            if status == "preflight_passed":
                counts["promotion_preflight_pass"] += 1
                reward += 0.8
            elif status in {"preflight_failed", "blocked"}:
                counts["promotion_preflight_fail"] += 1
                reward -= 1.2
        elif typ == "promotion_canary_check":
            status = str(((r.get("result") or {}).get("status", "")))
            if status == "canary_passed":
                counts["promotion_canary_pass"] += 1
                reward += 2.0
            elif status in {"canary_failed", "blocked"}:
                counts["promotion_canary_fail"] += 1
                reward -= 3.0
        elif typ == "promotion_rollback_apply":
            status = str(((r.get("result") or {}).get("status", "")))
            if status == "rollback_applied":
                counts["rollback_apply_success"] += 1
                reward += 0.8 if str(r.get("source", "")) == "canary_auto" else 1.0
            elif status == "rollback_partial":
                counts["rollback_apply_partial"] += 1
                reward -= 0.5

    for r in sandbox_exec_recs:
        after = r.get("after") or {}
        delta = after.get("delta") or {}
        patches_added = len(((delta.get("patches") or {}).get("added") or []))
        scratch_added = len(((delta.get("scratch") or {}).get("added") or []))
        if patches_added or scratch_added:
            counts["sandbox_patch_artifact_runs"] += 1
            reward += 0.4 + min(0.6, 0.1 * (patches_added + scratch_added))
        msg = str(r.get("response_message_preview", "")).lower()
        if any(x in msg for x in ["traceback", "exception", "error:", "failed"]):
            reward -= 0.5

    reward += min(1.0, 0.05 * len(tool_sim_recs))
    return {"reward": round(reward, 4), "counts": counts}


def _accumulate_metrics(state: dict[str, Any], cycle: dict[str, Any]) -> None:
    metrics = state.setdefault("metrics", {})
    metrics["reward_total"] = round(float(metrics.get("reward_total", 0.0) or 0.0) + float(cycle.get("reward", 0.0) or 0.0), 4)
    metrics["episodes"] = int(metrics.get("episodes", 0) or 0) + 1
    for k, v in (cycle.get("counts") or {}).items():
        metrics[k] = int(metrics.get(k, 0) or 0) + int(v or 0)


def _update_adaptive(state: dict[str, Any]) -> None:
    metrics = state.get("metrics") or {}
    adaptive = state.setdefault("adaptive", {})
    reward_total = float(metrics.get("reward_total", 0.0) or 0.0)
    episodes = max(1, int(metrics.get("episodes", 1) or 1))
    avg_reward = reward_total / episodes
    apply_ok = int(metrics.get("promotion_apply_success", 0) or 0)
    apply_fail = int(metrics.get("promotion_apply_fail", 0) or 0)
    preflight_fail = int(metrics.get("promotion_preflight_fail", 0) or 0)
    canary_fail = int(metrics.get("promotion_canary_fail", 0) or 0)
    rollbacks = int(metrics.get("rollback_apply_success", 0) or 0) + int(metrics.get("rollback_apply_partial", 0) or 0)
    previews = int(metrics.get("promotion_preview_ready", 0) or 0) + int(metrics.get("simulation_previews", 0) or 0)

    failure_pressure = (apply_fail + canary_fail + 0.7 * preflight_fail + 0.5 * rollbacks) / max(
        1.0, (apply_ok + apply_fail + canary_fail + preflight_fail + rollbacks)
    )
    verification_bias = _clamp(0.55 + 0.35 * failure_pressure, 0.4, 0.95)
    adaptive["verification_bias"] = round(verification_bias, 3)

    if avg_reward > 1.0 and failure_pressure < 0.2:
        adaptive["strategy_mode"] = "tunnelling"
    elif failure_pressure > 0.35:
        adaptive["strategy_mode"] = "rabbithole"
    else:
        adaptive["strategy_mode"] = "stabilize"

    if apply_ok >= 3 and apply_fail == 0 and canary_fail == 0 and rollbacks == 0:
        adaptive["world_model_refresh_interval_sec"] = 180
    elif failure_pressure > 0.35:
        adaptive["world_model_refresh_interval_sec"] = 120
    elif previews > 10:
        adaptive["world_model_refresh_interval_sec"] = 240
    else:
        adaptive["world_model_refresh_interval_sec"] = 300

    adaptive["prefer_sandbox"] = True
    adaptive["promotion_require_simulation_first"] = True if failure_pressure > 0.1 else bool(adaptive.get("promotion_require_simulation_first", True))


def _apply_bounded_updates(data_dir: Path, state: dict[str, Any]) -> None:
    adaptive = state.get("adaptive") or {}
    # Promotion policy stays simulation-first unless operator explicitly changes it; learner can re-enable it.
    promo_cfg_path = data_dir / "promotion_config.json"
    promo_cfg = verra_promotion.load_config(promo_cfg_path)
    promo_cfg["require_simulation_first"] = bool(adaptive.get("promotion_require_simulation_first", True))
    verra_promotion.save_config(promo_cfg_path, promo_cfg)

    # Keep sandbox enabled + safe defaults, but sync max_sessions within sane bounds.
    sandbox_cfg_path = data_dir / "sandbox_config.json"
    sandbox_cfg = verra_sandbox.load_config(sandbox_cfg_path)
    sandbox_cfg["enabled"] = True
    sandbox_cfg.setdefault("sandbox_rules", {})["network_enabled"] = False
    sandbox_cfg["sandbox_rules"]["default_shell_exec_enabled"] = False
    sandbox_cfg["max_sessions"] = int(_clamp(sandbox_cfg.get("max_sessions", 20), 5, 50))
    verra_sandbox.save_config(sandbox_cfg_path, sandbox_cfg)

    # Optionally tighten policy verification defaults based on repeated failures.
    policy_path = data_dir / "policy.json"
    policy = verra_policy.load_policy(policy_path)
    defaults = policy.setdefault("defaults", {})
    verify_for = set(defaults.get("verification_required_for", []))
    if float(adaptive.get("verification_bias", 0.65)) >= 0.8:
        verify_for.add("medium")
    else:
        verify_for.discard("medium")
    defaults["verification_required_for"] = [x for x in ["high", "critical", "economic", "medium"] if x in verify_for]
    verra_policy.save_policy(policy_path, policy)


def _load_receipts_generic(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {"receipts": []}


def _clamp(x: Any, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except Exception:
        v = float(lo)
    return max(lo, min(hi, v))
