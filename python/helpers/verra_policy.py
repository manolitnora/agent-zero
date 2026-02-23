from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


AUTONOMY_MODES = ("assist", "supervised", "bounded_auto", "full_auto")


def load_policy(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return default_policy()


def save_policy(path: Path, policy: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2) + "\n", "utf-8")


def default_policy() -> dict[str, Any]:
    return {
        "schema_version": "verra_policy.v1",
        "autonomy_mode": "supervised",
        "defaults": {
            "require_simulation_for": ["high", "critical"],
            "verification_required_for": ["high", "critical", "economic"],
            "block_destructive_in_modes": ["assist", "supervised"],
        },
        "budgets": {
            "session_spend_limit_usd": 50.0,
            "per_action_limit_usd": 10.0,
        },
        "risk_classes": {
            "low_read": {
                "risk_level": "low",
                "destructive": False,
                "economic": False,
                "requires_simulation": False,
            },
            "medium_write": {
                "risk_level": "medium",
                "destructive": False,
                "economic": False,
                "requires_simulation": False,
            },
            "high_exec": {
                "risk_level": "high",
                "destructive": True,
                "economic": False,
                "requires_simulation": True,
            },
            "economic": {
                "risk_level": "critical",
                "destructive": False,
                "economic": True,
                "requires_simulation": True,
            },
        },
        "tool_rules": [
            {"pattern": "response", "class": "low_read"},
            {"pattern": "search", "class": "low_read"},
            {"pattern": "browser", "class": "low_read"},
            {"pattern": "web", "class": "low_read"},
            {"pattern": "read", "class": "low_read"},
            {"pattern": "memory", "class": "medium_write"},
            {"pattern": "write", "class": "medium_write"},
            {"pattern": "edit", "class": "medium_write"},
            {"pattern": "file", "class": "medium_write"},
            {"pattern": "terminal", "class": "high_exec"},
            {"pattern": "shell", "class": "high_exec"},
            {"pattern": "bash", "class": "high_exec"},
            {"pattern": "exec", "class": "high_exec"},
            {"pattern": "run", "class": "high_exec"},
            {"pattern": "git", "class": "high_exec"},
            {"pattern": "wallet", "class": "economic"},
            {"pattern": "pay", "class": "economic"},
            {"pattern": "payment", "class": "economic"},
            {"pattern": "stripe", "class": "economic"},
            {"pattern": "coinbase", "class": "economic"},
            {"pattern": "commerce", "class": "economic"},
            {"pattern": "purchase", "class": "economic"},
            {"pattern": "checkout", "class": "economic"},
            {"pattern": "trade", "class": "economic"},
        ],
    }


def parse_verra_tool_flags(tool_args: dict[str, Any]) -> dict[str, bool]:
    # These flags are consumed by Verra and removed before tool execution.
    keys = ["verra_simulate", "simulate_only", "verra_live_approved", "verra_risk_ack"]
    out = {}
    for k in keys:
        v = tool_args.pop(k, None) if isinstance(tool_args, dict) else None
        out[k] = _to_bool(v)
    # normalize aliases
    out["simulate"] = out["verra_simulate"] or out["simulate_only"]
    out["live_approved"] = out["verra_live_approved"] or out["verra_risk_ack"]
    return out


def classify_tool(policy: dict[str, Any], tool_name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
    tname = (tool_name or "").lower()
    matched_class = "medium_write"
    for rule in policy.get("tool_rules", []):
        pat = str(rule.get("pattern", "")).lower()
        if pat and pat in tname:
            matched_class = str(rule.get("class", matched_class))
            break

    klass = (policy.get("risk_classes") or {}).get(matched_class, {})
    result = {
        "tool_name": tool_name,
        "risk_class": matched_class,
        "risk_level": klass.get("risk_level", "medium"),
        "destructive": bool(klass.get("destructive", False)),
        "economic": bool(klass.get("economic", False)),
        "requires_simulation": bool(klass.get("requires_simulation", False)),
    }

    args = tool_args or {}
    if contains_destructive_signals(args):
        result["destructive"] = True
        if result["risk_level"] in ("low", "medium"):
            result["risk_level"] = "high"
            result["requires_simulation"] = True
    if contains_economic_signals(tool_name, args):
        result["economic"] = True
        result["risk_level"] = "critical"
        result["requires_simulation"] = True
        result["risk_class"] = "economic"
    return result


def evaluate_tool_call(
    policy: dict[str, Any],
    classification: dict[str, Any],
    *,
    autonomy_mode: str,
    verra_policy_hints: dict[str, Any] | None,
    flags: dict[str, bool],
    estimated_cost_usd: float = 0.0,
    budget_remaining_usd: float | None = None,
) -> dict[str, Any]:
    mode = autonomy_mode if autonomy_mode in AUTONOMY_MODES else "supervised"
    risk_level = classification["risk_level"]
    destructive = classification["destructive"]
    economic = classification["economic"]

    require_sim_set = set(policy.get("defaults", {}).get("require_simulation_for", []))
    verify_set = set(policy.get("defaults", {}).get("verification_required_for", []))
    block_destructive_modes = set(policy.get("defaults", {}).get("block_destructive_in_modes", []))
    per_action_limit = float((policy.get("budgets") or {}).get("per_action_limit_usd", 10.0))

    kernel_verify = bool((verra_policy_hints or {}).get("verification_required", False))
    kernel_mode = str((verra_policy_hints or {}).get("mode", "stabilize"))

    requires_sim = bool(classification.get("requires_simulation")) or (risk_level in require_sim_set)
    requires_verify = kernel_verify or risk_level in verify_set or economic or kernel_mode == "verify"

    allowed = True
    reason = "allowed"
    require_live_approval = False
    blocked = False

    if destructive and mode in block_destructive_modes:
        allowed = False
        blocked = True
        reason = f"destructive tools blocked in autonomy mode '{mode}'"

    if economic and mode in ("assist", "supervised"):
        require_live_approval = True
        requires_sim = True

    if estimated_cost_usd > per_action_limit and mode != "full_auto":
        allowed = False
        blocked = True
        reason = f"estimated cost ${estimated_cost_usd:.2f} exceeds per-action limit ${per_action_limit:.2f}"

    if budget_remaining_usd is not None and estimated_cost_usd > budget_remaining_usd:
        allowed = False
        blocked = True
        reason = f"estimated cost ${estimated_cost_usd:.2f} exceeds remaining budget ${budget_remaining_usd:.2f}"

    if allowed and requires_sim and not flags.get("simulate") and not flags.get("live_approved"):
        allowed = False
        blocked = True
        reason = "simulation required before live execution"

    if allowed and require_live_approval and not flags.get("live_approved"):
        allowed = False
        blocked = True
        reason = "live approval required for economic/high-risk action"

    if allowed and flags.get("simulate"):
        # synthetic preview mode (skip execution) unless policy explicitly permits read-only live simulation
        allowed = False
        blocked = True
        reason = "simulation preview only (execution intentionally skipped)"

    return {
        "allowed": allowed,
        "blocked": blocked,
        "reason": reason,
        "risk_level": risk_level,
        "risk_class": classification["risk_class"],
        "requires_simulation": requires_sim,
        "requires_verification": requires_verify,
        "require_live_approval": require_live_approval,
        "economic": economic,
        "destructive": destructive,
        "estimated_cost_usd": estimated_cost_usd,
        "autonomy_mode": mode,
        "kernel_mode": kernel_mode,
    }


def contains_destructive_signals(args: dict[str, Any]) -> bool:
    hay = json.dumps(args or {}).lower()
    needles = ["delete", "remove", "rm ", "drop ", "truncate", "overwrite", "reset", "format"]
    return any(n in hay for n in needles)


def contains_economic_signals(tool_name: str, args: dict[str, Any]) -> bool:
    hay = f"{tool_name} {json.dumps(args or {})}".lower()
    needles = ["wallet", "payment", "checkout", "purchase", "trade", "swap", "send money", "spend"]
    return any(n in hay for n in needles)


def estimate_action_cost_usd(tool_name: str, args: dict[str, Any]) -> float:
    # Heuristic estimate from common amount/price fields.
    if not args:
        return 0.0
    candidates = []
    for key, val in args.items():
        key_l = str(key).lower()
        if any(k in key_l for k in ("amount", "price", "cost", "budget", "spend", "value", "usd")):
            num = _extract_number(val)
            if num is not None and num >= 0:
                candidates.append(num)
    if candidates:
        return max(candidates)
    # Non-economic tool default nominal cost.
    if contains_economic_signals(tool_name, args):
        return 1.0
    return 0.0


def build_trust_status_fragment(
    *,
    autonomy_mode: str,
    last_verra_hints: dict[str, Any] | None,
    budget_summary: dict[str, Any] | None,
    policy: dict[str, Any],
) -> str:
    hints = last_verra_hints or {}
    budget = budget_summary or {}
    lines = [
        "VERRA TRUST STATUS:",
        f"- autonomy_mode: {autonomy_mode}",
        f"- verra_mode: {hints.get('mode', 'stabilize')}",
        f"- verification_required: {bool(hints.get('verification_required', False))}",
        f"- llm_temperature_hint: {hints.get('llm_temperature', 'n/a')}",
        f"- top_p_hint: {hints.get('top_p', 'n/a')}",
    ]
    if budget:
        lines.append(
            f"- budget_remaining_usd: {budget.get('remaining_usd', 'n/a')} / {budget.get('limit_usd', 'n/a')}"
        )
    lines.extend(
        [
            "- high-risk/economic tools require simulation-first and then live approval.",
            "- Use tool args `verra_simulate: true` for preview. Verra will skip execution and return a policy preview.",
            "- Use tool args `verra_live_approved: true` for live execution after preview/review.",
            "- These Verra flags are consumed by the runtime and removed before tool execution.",
        ]
    )
    return "\n".join(lines)


def _extract_number(val: Any) -> float | None:
    try:
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val)
        m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
        if m:
            return float(m.group(0))
    except Exception:
        return None
    return None


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")

