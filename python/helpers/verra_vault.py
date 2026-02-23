from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMPARTMENTS = ("general", "personal", "business", "social", "cognitive")
COLLECTIONS = ("preferences", "facts", "procedures", "episodes", "receipts")
TRUST_TIER_WEIGHTS = {
    "user_confirmed": 1.00,
    "verified_repeated": 0.98,
    "provisional_verified": 0.88,
    "system_generated": 0.72,
    "inferred": 0.60,
    "unknown": 0.50,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256(("|".join(parts)).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{h}"


def init_vault() -> dict[str, Any]:
    ts = now_iso()
    return {
        "schema_version": "verra_vault.v1",
        "meta": {
            "created_at": ts,
            "updated_at": ts,
            "encryption": {
                "enabled": False,
                "mode": "none",
                "note": "Vault v1 supports compartmenting and trust metadata. Encryption is not enabled yet."
            }
        },
        "compartments": {
            name: {collection: [] for collection in COLLECTIONS} for name in COMPARTMENTS
        }
    }


def load_vault(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return init_vault()


def save_vault(path: Path, vault: dict[str, Any]) -> None:
    vault.setdefault("meta", {})
    vault["meta"]["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vault, indent=2) + "\n", "utf-8")


def ensure_compartment(vault: dict[str, Any], compartment: str) -> str:
    comp = compartment if compartment in COMPARTMENTS else "general"
    vault.setdefault("compartments", {})
    if comp not in vault["compartments"]:
        vault["compartments"][comp] = {collection: [] for collection in COLLECTIONS}
    for collection in COLLECTIONS:
        vault["compartments"][comp].setdefault(collection, [])
    return comp


def append_receipt(
    vault: dict[str, Any],
    compartment: str,
    *,
    session_id: str,
    snapshot: dict[str, Any],
    kernel_resp: dict[str, Any],
    max_items: int = 500,
) -> None:
    comp = ensure_compartment(vault, compartment)
    diagnostics = kernel_resp.get("diagnostics") or {}
    policy = kernel_resp.get("policy_hints") or {}
    entry = {
        "id": stable_id(
            "receipt",
            session_id,
            snapshot.get("user_text", "")[:120],
            snapshot.get("answer_text", "")[:120],
            str(snapshot.get("metrics_for_session", {}).get("sigma_tension", "")),
        ),
        "at": now_iso(),
        "session_id": session_id,
        "trust_tier": "system_generated",
        "confidence": 0.7,
        "source": "verra_controller",
        "source_authority": float(snapshot.get("verification", {}).get("source_authority", 0.0)),
        "last_verified": now_iso(),
        "tags": [
            "receipt",
            f"mode:{policy.get('mode', 'unknown')}",
            f"kernel_mode:{diagnostics.get('mode_out', 'unknown')}",
        ],
        "summary": {
            "user": str(snapshot.get("user_text", ""))[:200],
            "assistant": str(snapshot.get("answer_text", ""))[:200],
        },
        "metrics": snapshot.get("metrics_for_session", {}),
        "kernel_diagnostics": diagnostics,
        "policy_hints": policy,
        "formula_terms": snapshot.get("formula_terms", {}),
        "receipt_terms": snapshot.get("receipt", {}),
    }
    bucket = vault["compartments"][comp]["receipts"]
    bucket.append(entry)
    del bucket[:-max_items]


def upsert_preference(
    vault: dict[str, Any],
    compartment: str,
    *,
    key: str,
    value: str,
    confidence: float,
    source: str,
    trust_tier: str = "user_confirmed",
    source_authority: float = 1.0,
) -> None:
    comp = ensure_compartment(vault, compartment)
    items = vault["compartments"][comp]["preferences"]
    entry_id = stable_id("pref", comp, key, value)
    existing = next((x for x in items if x.get("id") == entry_id), None)
    payload = {
        "id": entry_id,
        "key": key,
        "value": value,
        "confidence": float(confidence),
        "trust_tier": trust_tier,
        "source": source,
        "source_authority": float(source_authority),
        "last_verified": now_iso(),
        "updated_at": now_iso(),
        "tags": [f"compartment:{comp}", "preference"],
    }
    if existing:
        existing.update(payload)
    else:
        payload["created_at"] = now_iso()
        items.append(payload)
    del items[:-200]


def append_episode(
    vault: dict[str, Any],
    compartment: str,
    *,
    session_id: str,
    user_text: str,
    assistant_text: str,
    metrics: dict[str, Any],
    kernel_mode: str | None,
    max_items: int = 500,
) -> None:
    comp = ensure_compartment(vault, compartment)
    items = vault["compartments"][comp]["episodes"]
    items.append(
        {
            "id": stable_id("ep", session_id, user_text[:120], assistant_text[:120], str(metrics.get("sigma_tension", ""))),
            "at": now_iso(),
            "session_id": session_id,
            "trust_tier": "system_generated",
            "confidence": 0.65,
            "source": "verra_controller",
            "source_authority": 0.5,
            "mode": comp,
            "kernel_mode": kernel_mode,
            "user": user_text[:1000],
            "assistant_summary": assistant_text[:1000],
            "metrics": metrics,
            "tags": [f"compartment:{comp}", "episode"],
        }
    )
    del items[:-max_items]


def export_legacy_memory(vault: dict[str, Any]) -> dict[str, Any]:
    prefs: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    procedures: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []

    for comp_name, comp in vault.get("compartments", {}).items():
        for item in comp.get("preferences", []):
            prefs.append(
                {
                    "key": item.get("key", "preference"),
                    "value": item.get("value", ""),
                    "confidence": item.get("confidence", 0.5),
                    "source": item.get("source", "vault"),
                    "last_verified": item.get("last_verified"),
                    "compartment": comp_name,
                    "trust_tier": item.get("trust_tier", "unknown"),
                }
            )
        for item in comp.get("facts", []):
            facts.append(item)
        for item in comp.get("procedures", []):
            procedures.append(item)
        for item in comp.get("episodes", []):
            episodes.append(item)

    return {
        "schema_version": "verra_memory.v1",
        "preferences": prefs[-100:],
        "facts": facts[-200:],
        "procedures": procedures[-200:],
        "episodes": episodes[-300:],
    }


def upsert_fact(
    vault: dict[str, Any],
    compartment: str,
    *,
    fact_text: str,
    session_id: str,
    source: str,
    confidence: float,
    source_authority: float,
    evidence_coverage: float,
    claim_support_ratio: float,
    hallucination_risk: float,
    trust_tier: str = "provisional_verified",
    tags: list[str] | None = None,
) -> None:
    comp = ensure_compartment(vault, compartment)
    items = vault["compartments"][comp]["facts"]
    canonical = " ".join(fact_text.lower().split())
    entry_id = stable_id("fact", comp, canonical)
    existing = next((x for x in items if x.get("id") == entry_id), None)
    now = now_iso()

    payload = {
        "id": entry_id,
        "fact": fact_text.strip(),
        "canonical": canonical,
        "session_id": session_id,
        "source": source,
        "confidence": float(confidence),
        "source_authority": float(source_authority),
        "evidence_coverage": float(evidence_coverage),
        "claim_support_ratio": float(claim_support_ratio),
        "hallucination_risk": float(hallucination_risk),
        "trust_tier": trust_tier,
        "last_verified": now,
        "updated_at": now,
        "tags": ["fact", f"compartment:{comp}", *(tags or [])],
    }

    if existing:
        support_count = int(existing.get("support_count", 1)) + 1
        existing.update(payload)
        existing["support_count"] = support_count
        existing["confidence"] = min(
            1.0,
            max(float(existing.get("confidence", 0.0)), float(confidence))
            + 0.03 * min(support_count, 5),
        )
        # Escalate trust after repeated validated observations.
        if support_count >= 3 and existing["confidence"] >= 0.80:
            existing["trust_tier"] = "verified_repeated"
    else:
        payload["created_at"] = now
        payload["support_count"] = 1
        items.append(payload)

    del items[:-500]


def query_vault(
    vault: dict[str, Any],
    *,
    query: str,
    compartment: str,
    max_preferences: int = 6,
    max_facts: int = 8,
    max_procedures: int = 4,
    max_episodes: int = 4,
    include_general_fallback: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    comp = ensure_compartment(vault, compartment)
    q_tokens = set(_tok(query))

    search_compartments = [comp]
    if include_general_fallback and comp != "general":
        search_compartments.append("general")

    collected = {
        "preferences": [],
        "facts": [],
        "procedures": [],
        "episodes": [],
    }

    for sc in search_compartments:
        bucket = vault.get("compartments", {}).get(sc, {})
        for coll in collected.keys():
            for item in bucket.get(coll, []):
                scored = dict(item)
                scored["_vault_compartment"] = sc
                scored["_score"] = _score_vault_item(scored, q_tokens, coll, target_compartment=comp)
                collected[coll].append(scored)

    collected["preferences"] = _dedupe_and_sort(collected["preferences"], max_preferences)
    collected["facts"] = _dedupe_and_sort(collected["facts"], max_facts)
    collected["procedures"] = _dedupe_and_sort(collected["procedures"], max_procedures)
    collected["episodes"] = _dedupe_and_sort(collected["episodes"], max_episodes)
    return collected


def vault_prompt_fragment(
    results: dict[str, list[dict[str, Any]]],
    *,
    compartment: str,
    include_scores: bool = False,
) -> str:
    lines: list[str] = [f"VERRA VAULT RECALL ({compartment})"]

    prefs = results.get("preferences", [])
    facts = results.get("facts", [])
    procs = results.get("procedures", [])
    eps = results.get("episodes", [])

    if prefs:
        lines.append("Preferences:")
        for item in prefs:
            suffix = _item_meta_suffix(item, include_scores)
            lines.append(f"- {item.get('key', 'preference')}: {item.get('value', '')}{suffix}")

    if facts:
        lines.append("Facts:")
        for item in facts:
            suffix = _item_meta_suffix(item, include_scores)
            fact = item.get("fact", item.get("value", ""))
            lines.append(f"- {fact}{suffix}")

    if procs:
        lines.append("Procedures:")
        for item in procs:
            label = item.get("name") or item.get("key") or "procedure"
            value = item.get("value") or item.get("procedure") or ""
            suffix = _item_meta_suffix(item, include_scores)
            lines.append(f"- {label}: {value}{suffix}")

    if eps:
        lines.append("Episodes:")
        for item in eps:
            summary = item.get("assistant_summary", "")
            user = item.get("user", "")
            mode = item.get("kernel_mode") or item.get("mode") or "unknown"
            suffix = _item_meta_suffix(item, include_scores)
            lines.append(
                f"- [{mode}] user='{str(user)[:120]}' -> '{str(summary)[:160]}'{suffix}"
            )

    if len(lines) == 1:
        lines.append("- none")

    lines.append("Use vault recall as weighted memory context. Do not treat it as authoritative unless verified in-turn.")
    return "\n".join(lines)


def _dedupe_and_sort(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda x: x.get("_score", 0.0), reverse=True):
        key = str(item.get("id") or item.get("canonical") or item.get("key") or item.get("fact") or item.get("user"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _score_vault_item(item: dict[str, Any], q_tokens: set[str], collection: str, target_compartment: str) -> float:
    text_parts = [
        str(item.get("key", "")),
        str(item.get("value", "")),
        str(item.get("fact", "")),
        str(item.get("procedure", "")),
        str(item.get("user", "")),
        str(item.get("assistant_summary", "")),
        " ".join(item.get("tags", []) if isinstance(item.get("tags", []), list) else []),
    ]
    text = " ".join(text_parts)
    i_tokens = _tok(text)
    if q_tokens:
        overlap = len([t for t in set(i_tokens) if t in q_tokens]) / max(min(len(set(i_tokens)), len(q_tokens)), 1)
    else:
        overlap = 0.3

    confidence = float(item.get("confidence", 0.5))
    source_authority = float(item.get("source_authority", 0.5))
    trust = str(item.get("trust_tier", "unknown"))
    trust_w = TRUST_TIER_WEIGHTS.get(trust, TRUST_TIER_WEIGHTS["unknown"])
    support_count = min(float(item.get("support_count", 1)), 5.0) / 5.0
    comp_bonus = 0.15 if item.get("_vault_compartment") == target_compartment else 0.05

    coll_bias = {
        "preferences": 0.85,
        "facts": 1.0,
        "procedures": 0.9,
        "episodes": 0.7,
    }.get(collection, 0.7)

    return (
        0.45 * overlap
        + 0.18 * confidence
        + 0.15 * source_authority
        + 0.12 * trust_w
        + 0.05 * support_count
        + comp_bonus
    ) * coll_bias


def _item_meta_suffix(item: dict[str, Any], include_scores: bool) -> str:
    trust = item.get("trust_tier")
    conf = item.get("confidence")
    comp = item.get("_vault_compartment")
    parts = []
    if trust is not None:
        parts.append(f"trust={trust}")
    if conf is not None:
        try:
            parts.append(f"conf={float(conf):.2f}")
        except Exception:
            parts.append(f"conf={conf}")
    if comp is not None:
        parts.append(f"comp={comp}")
    if include_scores and item.get("_score") is not None:
        parts.append(f"score={float(item['_score']):.2f}")
    return f" ({', '.join(parts)})" if parts else ""


def _tok(text: str) -> list[str]:
    out: list[str] = []
    for raw in (text or "").lower().replace("\n", " ").split():
        t = "".join(ch for ch in raw if ch.isalnum())
        if t and len(t) > 2:
            out.append(t)
    return out
