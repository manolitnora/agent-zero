from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_registry(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_skill_registry.v1",
        "default_trust": "unknown",
        "skills": [],
    }


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2) + "\n", "utf-8")


def scan_skills(repo_root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for base_rel in ("skills", "usr/skills"):
        base = repo_root / base_rel
        if not base.exists():
            continue
        for p in base.rglob("SKILL.md"):
            try:
                rel = str(p.relative_to(repo_root))
            except Exception:
                rel = str(p)
            found.append(
                {
                    "id": rel.replace("/", "::"),
                    "name": p.parent.name,
                    "path": rel,
                }
            )
    return found


def merge_scan_into_registry(registry: dict[str, Any], scanned: list[dict[str, Any]]) -> dict[str, Any]:
    existing = {str(s.get("id")): s for s in registry.get("skills", [])}
    for s in scanned:
        e = existing.get(s["id"])
        if e:
            e["path"] = s["path"]
            e["name"] = s["name"]
            e["discovered"] = True
        else:
            existing[s["id"]] = {
                **s,
                "trust_tier": "unknown",
                "status": "discovered",
                "allowed": True,
                "scopes": [],
                "source": "local_scan",
                "discovered": True,
            }
    registry["skills"] = sorted(existing.values(), key=lambda x: (x.get("name", ""), x.get("id", "")))
    return registry


def skill_prompt_fragment(registry: dict[str, Any], *, max_items: int = 12) -> str:
    skills = registry.get("skills", [])
    trusted = [s for s in skills if s.get("allowed", True)]
    trusted.sort(key=lambda s: _skill_score(s), reverse=True)
    lines = ["VERRA SKILL TRUST REGISTRY:"]
    if not trusted:
        lines.append("- no registered skills discovered")
    else:
        for s in trusted[:max_items]:
            lines.append(
                f"- {s.get('name', s.get('id'))} (trust={s.get('trust_tier','unknown')}, status={s.get('status','unknown')}, scopes={','.join(s.get('scopes',[])) or 'none'})"
            )
    lines.append("- Prefer trusted/allowed skills. Avoid unknown skills for high-risk or economic actions.")
    return "\n".join(lines)


def _skill_score(s: dict[str, Any]) -> float:
    trust = str(s.get("trust_tier", "unknown"))
    tw = {
        "user_confirmed": 1.0,
        "verified_repeated": 0.95,
        "provisional_verified": 0.8,
        "unknown": 0.5,
        "blocked": 0.0,
    }.get(trust, 0.5)
    allowed = 1.0 if s.get("allowed", True) else 0.0
    return 0.7 * tw + 0.3 * allowed

