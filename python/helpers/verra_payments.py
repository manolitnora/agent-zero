from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from python.helpers import verra_receipt_chain


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_payments_config(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_payments.v1",
        "default_adapter": "mock_wallet",
        "adapters": {
            "mock_wallet": {
                "type": "mock",
                "enabled": True,
                "currency": "USD",
                "max_quote_usd": 1000.0,
                "merchant_allowlist": ["*"],
            },
            "coinbase_agent_wallet": {
                "type": "coinbase_agent_wallet",
                "enabled": False,
                "network": "base",
                "currency": "USD",
                "max_quote_usd": 500.0,
                "merchant_allowlist": ["api.*", "*.coinbase.com", "*.base.org", "*"],
                "wallet_id": "",
                "x402_endpoint": "",
                "api_key_env": "COINBASE_AGENT_WALLET_API_KEY",
            },
            "stripe_agent_commerce": {
                "type": "stripe_agent_commerce",
                "enabled": False,
                "currency": "USD",
                "max_quote_usd": 500.0,
                "merchant_allowlist": ["api.*", "*.stripe.com", "*"],
                "shared_payment_token_env": "STRIPE_SHARED_PAYMENT_TOKEN",
                "acp_endpoint": "",
            },
        },
    }


def save_payments_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", "utf-8")


def load_receipts(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_economic_receipts.v1",
        "receipts": [],
    }


def save_receipts(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verra_receipt_chain.chain_receipt_doc(
        data,
        config_path=path.parent / "receipt_chain_config.json",
        list_key="receipts",
        doc_kind="verra_economic_receipts",
    )
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")


def quote(
    config: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    fallback_estimate_usd: float = 0.0,
) -> dict[str, Any]:
    adapter_name, adapter = _resolve_adapter(config)
    merchant = _infer_merchant(tool_name, tool_args)
    resource = _infer_resource(tool_name, tool_args)
    if not adapter or not adapter.get("enabled", False):
        return {
            "adapter": adapter_name,
            "status": "disabled",
            "currency": "USD",
            "estimated_cost_usd": float(fallback_estimate_usd or 0.0),
            "merchant": merchant,
            "resource": resource,
        }

    allow_status = _enforce_merchant_allowlist(adapter, merchant, resource)
    if allow_status is not None:
        return {
            "adapter": adapter_name,
            "currency": str(adapter.get("currency", "USD")),
            "estimated_cost_usd": float(fallback_estimate_usd or 0.0),
            **allow_status,
        }

    adapter_type = str(adapter.get("type", "mock"))
    if adapter_type == "mock":
        return _mock_quote(adapter_name, adapter, tool_name, tool_args, fallback_estimate_usd)
    if adapter_type == "coinbase_agent_wallet":
        return _coinbase_quote(adapter_name, adapter, tool_name, tool_args, fallback_estimate_usd)
    if adapter_type == "stripe_agent_commerce":
        return _stripe_quote(adapter_name, adapter, tool_name, tool_args, fallback_estimate_usd)
    return {
        "adapter": adapter_name,
        "status": "unknown_adapter",
        "currency": "USD",
        "estimated_cost_usd": float(fallback_estimate_usd or 0.0),
        "merchant": merchant,
        "resource": resource,
    }


def simulate(
    config: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    quote_result: dict[str, Any],
) -> dict[str, Any]:
    adapter_name, adapter = _resolve_adapter(config)
    adapter_type = str(adapter.get("type", "mock"))
    if adapter_type == "mock":
        return {
            "adapter": adapter_name,
            "status": "simulated",
            "simulation_id": _id(
                "sim",
                tool_name,
                quote_result.get("merchant", ""),
                str(quote_result.get("estimated_cost_usd", 0.0)),
            ),
            "merchant": quote_result.get("merchant"),
            "resource": quote_result.get("resource"),
            "currency": quote_result.get("currency", "USD"),
            "amount": float(quote_result.get("estimated_cost_usd", 0.0)),
            "constraints": {
                "spend_limit": "enforced_by_verra_policy",
                "live_approval_required": True,
                "keys_exposed_to_agent": False,
            },
            "note": "Mock simulation only. No real network/payment call executed.",
        }
    if adapter_type == "coinbase_agent_wallet":
        return {
            "adapter": adapter_name,
            "status": "simulated",
            "protocol": "x402",
            "network": str(adapter.get("network", "base")),
            "merchant": quote_result.get("merchant"),
            "resource": quote_result.get("resource"),
            "currency": quote_result.get("currency", "USD"),
            "amount": float(quote_result.get("estimated_cost_usd", 0.0)),
            "wallet_id": str(adapter.get("wallet_id", "")),
            "constraints": {
                "api_key_env": str(adapter.get("api_key_env", "COINBASE_AGENT_WALLET_API_KEY")),
                "x402_endpoint_configured": bool(str(adapter.get("x402_endpoint", "")).strip()),
                "live_approval_required": True,
            },
            "note": "Coinbase Agent Wallet adapter simulation only. External x402 call not executed.",
        }
    if adapter_type == "stripe_agent_commerce":
        return {
            "adapter": adapter_name,
            "status": "simulated",
            "protocol": "stripe_agent_commerce",
            "merchant": quote_result.get("merchant"),
            "resource": quote_result.get("resource"),
            "currency": quote_result.get("currency", "USD"),
            "amount": float(quote_result.get("estimated_cost_usd", 0.0)),
            "constraints": {
                "shared_payment_token_env": str(
                    adapter.get("shared_payment_token_env", "STRIPE_SHARED_PAYMENT_TOKEN")
                ),
                "acp_endpoint_configured": bool(str(adapter.get("acp_endpoint", "")).strip()),
                "live_approval_required": True,
            },
            "note": "Stripe Agent Commerce adapter simulation only. External network call not executed.",
        }
    return {
        "adapter": adapter_name,
        "status": "simulation_not_supported",
    }


def execute(
    config: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    quote_result: dict[str, Any],
    live_approved: bool,
) -> dict[str, Any]:
    adapter_name, adapter = _resolve_adapter(config)
    if not live_approved:
        return {
            "adapter": adapter_name,
            "status": "blocked",
            "reason": "live approval not provided",
        }
    adapter_type = str(adapter.get("type", "mock"))
    if adapter_type == "mock":
        return {
            "adapter": adapter_name,
            "status": "executed_mock",
            "receipt_id": _id("pay", tool_name, quote_result.get("merchant", ""), _now_iso()),
            "merchant": quote_result.get("merchant"),
            "resource": quote_result.get("resource"),
            "currency": quote_result.get("currency", "USD"),
            "amount": float(quote_result.get("estimated_cost_usd", 0.0)),
            "wallet_id": "mock_wallet_default",
            "network": "mocknet",
            "settlement": "simulated_local",
            "created_at": _now_iso(),
        }
    if adapter_type == "coinbase_agent_wallet":
        endpoint = str(adapter.get("x402_endpoint", "")).strip()
        wallet_id = str(adapter.get("wallet_id", "")).strip()
        if not endpoint or not wallet_id:
            return {
                "adapter": adapter_name,
                "status": "unconfigured_adapter",
                "reason": "coinbase_agent_wallet requires x402_endpoint and wallet_id",
            }
        return {
            "adapter": adapter_name,
            "status": "executed_stub",
            "receipt_id": _id("cb", tool_name, quote_result.get("merchant", ""), _now_iso()),
            "merchant": quote_result.get("merchant"),
            "resource": quote_result.get("resource"),
            "currency": quote_result.get("currency", "USD"),
            "amount": float(quote_result.get("estimated_cost_usd", 0.0)),
            "wallet_id": wallet_id,
            "network": str(adapter.get("network", "base")),
            "protocol": "x402",
            "endpoint": endpoint,
            "settlement": "stub_no_network",
            "created_at": _now_iso(),
        }
    if adapter_type == "stripe_agent_commerce":
        endpoint = str(adapter.get("acp_endpoint", "")).strip()
        token_env = str(adapter.get("shared_payment_token_env", "STRIPE_SHARED_PAYMENT_TOKEN")).strip()
        if not endpoint:
            return {
                "adapter": adapter_name,
                "status": "unconfigured_adapter",
                "reason": "stripe_agent_commerce requires acp_endpoint",
            }
        return {
            "adapter": adapter_name,
            "status": "executed_stub",
            "receipt_id": _id("st", tool_name, quote_result.get("merchant", ""), _now_iso()),
            "merchant": quote_result.get("merchant"),
            "resource": quote_result.get("resource"),
            "currency": quote_result.get("currency", "USD"),
            "amount": float(quote_result.get("estimated_cost_usd", 0.0)),
            "protocol": "stripe_agent_commerce",
            "endpoint": endpoint,
            "shared_payment_token_env": token_env,
            "settlement": "stub_no_network",
            "created_at": _now_iso(),
        }
    return {
        "adapter": adapter_name,
        "status": "execution_not_supported",
    }


def append_receipt(receipts_doc: dict[str, Any], receipt: dict[str, Any], max_items: int = 1000) -> None:
    receipts_doc.setdefault("receipts", []).append(receipt)
    del receipts_doc["receipts"][:-max_items]


def summary(payments_cfg: dict[str, Any], receipts_doc: dict[str, Any]) -> dict[str, Any]:
    adapter_name, adapter = _resolve_adapter(payments_cfg)
    recs = receipts_doc.get("receipts", [])
    executed = [r for r in recs if str(r.get("status", "")).startswith("executed")]
    amount = sum(float(r.get("amount", 0.0) or 0.0) for r in executed)
    return {
        "default_adapter": adapter_name,
        "adapter_enabled": bool(adapter.get("enabled", False)),
        "receipt_count": len(recs),
        "executed_count": len(executed),
        "executed_total_usd": round(amount, 4),
    }


def _resolve_adapter(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    adapters = config.get("adapters", {}) or {}
    name = str(config.get("default_adapter", "mock_wallet"))
    adapter = adapters.get(name, {})
    return name, adapter


def _enforce_merchant_allowlist(adapter: dict[str, Any], merchant: str, resource: str) -> dict[str, Any] | None:
    allowlist = adapter.get("merchant_allowlist", ["*"])
    if not isinstance(allowlist, list):
        allowlist = ["*"]
    merchant_l = str(merchant).strip().lower()
    patterns = [str(x).strip().lower() for x in allowlist if str(x).strip()]
    if any(fnmatch(merchant_l, pat) for pat in patterns):
        return None
    return {
        "status": "merchant_blocked",
        "reason": "merchant/domain not in adapter allowlist",
        "merchant": merchant,
        "resource": resource,
        "constraints": {"merchant_allowlist": allowlist},
    }


def _mock_quote(adapter_name: str, adapter: dict[str, Any], tool_name: str, tool_args: dict[str, Any], fallback_estimate_usd: float) -> dict[str, Any]:
    merchant = _infer_merchant(tool_name, tool_args)
    resource = _infer_resource(tool_name, tool_args)
    amount = _extract_amount(tool_args)
    if amount is None:
        amount = float(fallback_estimate_usd or 0.0)
    amount = max(0.0, float(amount))
    max_quote = float(adapter.get("max_quote_usd", 1000.0))
    status = "ok" if amount <= max_quote else "over_max_quote"
    return {
        "adapter": adapter_name,
        "status": status,
        "currency": str(adapter.get("currency", "USD")),
        "estimated_cost_usd": round(amount, 4),
        "merchant": merchant,
        "resource": resource,
        "quote_id": _id("quote", merchant, resource, str(amount)),
        "constraints": {
            "max_quote_usd": max_quote,
            "merchant_allowlist": adapter.get("merchant_allowlist", ["*"]),
        },
    }


def _coinbase_quote(adapter_name: str, adapter: dict[str, Any], tool_name: str, tool_args: dict[str, Any], fallback_estimate_usd: float) -> dict[str, Any]:
    base = _mock_quote(adapter_name, adapter, tool_name, tool_args, fallback_estimate_usd)
    base["protocol"] = "x402"
    base["network"] = str(adapter.get("network", "base"))
    base["wallet_id"] = str(adapter.get("wallet_id", ""))
    if not str(adapter.get("x402_endpoint", "")).strip():
        base["status"] = "unconfigured_adapter"
        base["reason"] = "missing x402_endpoint"
    return base


def _stripe_quote(adapter_name: str, adapter: dict[str, Any], tool_name: str, tool_args: dict[str, Any], fallback_estimate_usd: float) -> dict[str, Any]:
    base = _mock_quote(adapter_name, adapter, tool_name, tool_args, fallback_estimate_usd)
    base["protocol"] = "stripe_agent_commerce"
    base["acp_endpoint"] = str(adapter.get("acp_endpoint", ""))
    if not str(adapter.get("acp_endpoint", "")).strip():
        base["status"] = "unconfigured_adapter"
        base["reason"] = "missing acp_endpoint"
    return base


def _infer_merchant(tool_name: str, tool_args: dict[str, Any]) -> str:
    for key in ("merchant", "vendor", "service", "provider", "site", "domain"):
        if key in tool_args:
            return str(tool_args[key])[:120]
    return f"tool:{tool_name}"


def _infer_resource(tool_name: str, tool_args: dict[str, Any]) -> str:
    for key in ("resource", "product", "item", "endpoint", "api", "sku", "description", "purpose"):
        if key in tool_args:
            return str(tool_args[key])[:200]
    return tool_name


def _extract_amount(args: dict[str, Any]) -> float | None:
    for key, val in (args or {}).items():
        if any(k in str(key).lower() for k in ("amount", "price", "cost", "usd", "spend", "budget", "value")):
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                s = str(val).replace(",", "")
                num = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
                if num and any(ch.isdigit() for ch in num):
                    return float(num)
            except Exception:
                continue
    return None


def _id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{h}"
