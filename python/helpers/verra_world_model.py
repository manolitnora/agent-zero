from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODE_EXTS = {".py", ".rs", ".js", ".ts", ".json", ".md", ".toml"}
IMPORT_RE_PY_FROM = re.compile(r"^\s*from\s+([\w\.]+)\s+import\s+", re.M)
IMPORT_RE_PY_IMPORT = re.compile(r"^\s*import\s+([\w\.]+)", re.M)
IMPORT_RE_RS_USE = re.compile(r"^\s*use\s+([\w:]+)", re.M)
IMPORT_RE_JS = re.compile(r"from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_world_model(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_world_model.v1",
        "generated_at": None,
        "self_model": {},
        "codebases": [],
        "spatial_index": {"summary": {}, "nodes": []},
        "world_domains": {},
    }


def save_world_model(path: Path, model: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=2) + "\n", "utf-8")


def build_world_model(
    *,
    verra_kernel_root: Path,
    agent_zero_root: Path,
    data_dir: Path,
    max_files: int = 600,
) -> dict[str, Any]:
    roots = _discover_roots(verra_kernel_root, agent_zero_root)
    file_records: list[dict[str, Any]] = []
    dir_nodes: dict[str, dict[str, Any]] = {}
    imports: list[dict[str, Any]] = []
    per_root_counts: dict[str, int] = defaultdict(int)

    for root_idx, root in enumerate(roots):
        base = Path(root["path"])
        if not base.exists():
            continue
        rel_dirs = set()
        for fp in _iter_code_files(base):
            if len(file_records) >= max_files:
                break
            try:
                st = fp.stat()
                rel = fp.relative_to(base)
            except Exception:
                continue
            parts = rel.parts
            depth = len(parts)
            y = _stable_index(parts[-1] if parts else fp.name)
            rec = {
                "root": root["name"],
                "path": str(fp),
                "rel": str(rel),
                "ext": fp.suffix.lower(),
                "size": int(getattr(st, "st_size", 0) or 0),
                "mtime": float(getattr(st, "st_mtime", 0.0) or 0.0),
                "spatial": {"x": depth, "y": y, "z": root_idx},
            }
            file_records.append(rec)
            per_root_counts[root["name"]] += 1
            rel_dirs.update(_all_rel_dirs(rel.parent))
            imports.extend(_extract_import_edges(fp, root_name=root["name"], rel=str(rel)))

        for d in sorted(rel_dirs, key=lambda p: str(p)):
            key = f"{root['name']}::{d}"
            if key in dir_nodes:
                continue
            d_parts = d.parts if isinstance(d, Path) else Path(d).parts
            dir_nodes[key] = {
                "root": root["name"],
                "rel": str(d),
                "path": str(base / d),
                "spatial": {
                    "x": len(d_parts),
                    "y": _stable_index(d_parts[-1] if d_parts else root["name"]),
                    "z": root_idx,
                },
            }

    file_records.sort(key=lambda r: (r["root"], r["rel"]))
    imports = imports[:1000]
    changed = _recent_hotspots(file_records)

    self_model = _build_self_model(verra_kernel_root, agent_zero_root, data_dir)
    codebases = [
        {
            "name": r["name"],
            "path": r["path"],
            "exists": Path(r["path"]).exists(),
            "category": r["category"],
            "file_count": int(per_root_counts.get(r["name"], 0)),
        }
        for r in roots
    ]
    spatial_nodes = list(dir_nodes.values())[:500] + [
        {"root": r["root"], "rel": r["rel"], "path": r["path"], "spatial": r["spatial"], "type": "file"}
        for r in file_records[:800]
    ]

    return {
        "schema_version": "verra_world_model.v1",
        "generated_at": _now_iso(),
        "self_model": self_model,
        "codebases": codebases,
        "spatial_index": {
            "summary": {
                "roots": len(codebases),
                "file_nodes": len(file_records),
                "dir_nodes": len(dir_nodes),
                "import_edges": len(imports),
            },
            "nodes": spatial_nodes,
            "imports": imports,
            "hotspots": changed,
        },
        "world_domains": {
            "reasoning": {"kernel": True, "world_model": True, "dream_cycle": True},
            "memory": {"vault": True, "episodic": True, "procedural": True},
            "trust": {"policy": True, "receipts": True, "verification": True},
            "economics": {"ledger": True, "payments": True, "agent_wallets": True},
            "execution": {"skills": True, "tool_simulation": True, "sandbox": True},
            "spatial": {"path_graph": True, "tree_coordinates": True, "hotspots": True},
        },
    }


def maybe_refresh_world_model(
    path: Path,
    *,
    verra_kernel_root: Path,
    agent_zero_root: Path,
    data_dir: Path,
    min_interval_sec: int = 300,
    max_files: int = 600,
    force: bool = False,
) -> dict[str, Any]:
    if not force and path.exists():
        try:
            cur = json.loads(path.read_text("utf-8"))
            ts = cur.get("generated_at")
            if ts:
                last = _parse_iso(ts)
                if last and (time.time() - last) < max(10, int(min_interval_sec)):
                    return cur
        except Exception:
            pass
    model = build_world_model(
        verra_kernel_root=verra_kernel_root,
        agent_zero_root=agent_zero_root,
        data_dir=data_dir,
        max_files=max_files,
    )
    save_world_model(path, model)
    return model


def world_model_prompt_fragment(
    model: dict[str, Any],
    *,
    persona_mode: str = "general",
    sandbox_manifest: dict[str, Any] | None = None,
) -> str:
    self_model = model.get("self_model") or {}
    spatial = (model.get("spatial_index") or {}).get("summary", {})
    hotspots = (model.get("spatial_index") or {}).get("hotspots", [])[:5]
    codebases = model.get("codebases") or []
    codebase_labels = [f"{c.get('name')}({c.get('file_count', 0)})" for c in codebases[:8]]
    lines = [
        "VERRA SELF/WORLD MODEL:",
        f"- identity: {self_model.get('identity', 'verra')} schema={self_model.get('schema_version', 'v1')}",
        f"- role: {self_model.get('role', 'agent control kernel + trust runtime')}",
        f"- persona_mode: {persona_mode}",
        f"- capabilities: {', '.join(self_model.get('capabilities', [])[:12])}",
        f"- codebases: {', '.join(codebase_labels)}",
        f"- spatial_map: roots={spatial.get('roots',0)} files={spatial.get('file_nodes',0)} dirs={spatial.get('dir_nodes',0)} imports={spatial.get('import_edges',0)}",
        "- navigation_rule: think in zones (kernel, agent extensions, helpers, runtime data, wrapper) before editing.",
        "- learning_rule: stage experiments/patches in sandbox first, then propose promoted changes with receipts.",
    ]
    if sandbox_manifest:
        paths = sandbox_manifest.get("paths") or {}
        lines.append(f"- sandbox_current: {paths.get('current', 'n/a')}")
        lines.append(f"- sandbox_scratch: {paths.get('scratch', 'n/a')}")
        lines.append(f"- sandbox_patches: {paths.get('patches', 'n/a')}")
    if hotspots:
        lines.append("- hotspots: " + ", ".join(str(h.get("rel", h.get("path", "?"))) for h in hotspots))
    return "\n".join(lines)


def _discover_roots(verra_kernel_root: Path, agent_zero_root: Path) -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    candidates = [
        ("verra_kernel_src", verra_kernel_root / "src", "kernel"),
        ("verra_wrapper_src", verra_kernel_root / "wrapper" / "src", "wrapper"),
        ("agent_zero_helpers", agent_zero_root / "python" / "helpers", "agent_zero"),
        ("agent_zero_extensions", agent_zero_root / "python" / "extensions", "agent_zero"),
        ("agent_zero_tools", agent_zero_root / "python" / "tools", "agent_zero"),
        ("verra_runtime_data", agent_zero_root / "usr" / "verra", "runtime"),
    ]
    for name, path, category in candidates:
        roots.append({"name": name, "path": str(path), "category": category})
    return roots


def _iter_code_files(base: Path):
    if not base.exists():
        return
    count = 0
    for p in sorted(base.rglob("*")):
        if count > 2000:
            break
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in CODE_EXTS:
            continue
        if "__pycache__" in p.parts:
            continue
        count += 1
        yield p


def _extract_import_edges(fp: Path, *, root_name: str, rel: str) -> list[dict[str, str]]:
    try:
        text = fp.read_text("utf-8")
    except Exception:
        return []
    edges: list[dict[str, str]] = []
    if fp.suffix == ".py":
        mods = [m.group(1) for m in IMPORT_RE_PY_FROM.finditer(text)] + [m.group(1) for m in IMPORT_RE_PY_IMPORT.finditer(text)]
        for mod in mods[:40]:
            edges.append({"from": f"{root_name}:{rel}", "to": mod, "lang": "py"})
    elif fp.suffix == ".rs":
        for m in IMPORT_RE_RS_USE.finditer(text):
            edges.append({"from": f"{root_name}:{rel}", "to": m.group(1), "lang": "rs"})
    elif fp.suffix in {".js", ".ts"}:
        for m in IMPORT_RE_JS.finditer(text):
            to = m.group(1) or m.group(2)
            if to:
                edges.append({"from": f"{root_name}:{rel}", "to": to, "lang": "js"})
    return edges[:60]


def _build_self_model(verra_kernel_root: Path, agent_zero_root: Path, data_dir: Path) -> dict[str, Any]:
    capabilities = []
    checks = [
        ("kernel_v3", verra_kernel_root / "src" / "lib.rs"),
        ("agentzero_integration", agent_zero_root / "python" / "helpers" / "verra_control.py"),
        ("vault", agent_zero_root / "python" / "helpers" / "verra_vault.py"),
        ("dream_cycle", agent_zero_root / "python" / "helpers" / "verra_dream.py"),
        ("trust_policy", agent_zero_root / "python" / "helpers" / "verra_policy.py"),
        ("economics", agent_zero_root / "python" / "helpers" / "verra_economics.py"),
        ("payments", agent_zero_root / "python" / "helpers" / "verra_payments.py"),
        ("tool_simulation", agent_zero_root / "python" / "helpers" / "verra_tool_sim.py"),
        ("sandbox", agent_zero_root / "python" / "helpers" / "verra_sandbox.py"),
        ("sandbox_exec", agent_zero_root / "python" / "helpers" / "verra_sandbox_exec.py"),
        ("promotion_pipeline", agent_zero_root / "python" / "helpers" / "verra_promotion.py"),
        ("self_update_loop", agent_zero_root / "python" / "helpers" / "verra_self_update.py"),
        ("world_model", agent_zero_root / "python" / "helpers" / "verra_world_model.py"),
    ]
    for name, path in checks:
        if path.exists():
            capabilities.append(name)
    return {
        "schema_version": "verra_self_model.v1",
        "identity": "verra",
        "role": "metacognitive control runtime for agent orchestration",
        "kernel_root": str(verra_kernel_root),
        "agent_zero_root": str(agent_zero_root),
        "data_dir": str(data_dir),
        "capabilities": capabilities,
        "self_update_mode": "sandbox-first, receipt-bound, policy-gated",
    }


def _recent_hotspots(file_records: list[dict[str, Any]], n: int = 8) -> list[dict[str, Any]]:
    if not file_records:
        return []
    ranked = sorted(file_records, key=lambda r: (float(r.get("mtime", 0.0)), int(r.get("size", 0))), reverse=True)
    out = []
    for r in ranked[:n]:
        out.append({"root": r["root"], "rel": r["rel"], "mtime": r["mtime"], "size": r["size"]})
    return out


def _all_rel_dirs(p: Path) -> list[Path]:
    out: list[Path] = []
    cur = p
    while str(cur) not in ("", "."):
        out.append(cur)
        cur = cur.parent
    return out


def _stable_index(name: str) -> int:
    return sum(ord(ch) for ch in str(name)) % 97


def _parse_iso(s: str) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
