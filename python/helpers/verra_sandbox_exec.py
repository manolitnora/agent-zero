from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python.helpers import verra_receipt_chain


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_receipts(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {"schema_version": "verra_sandbox_exec_receipts.v1", "receipts": []}


def save_receipts(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verra_receipt_chain.chain_receipt_doc(
        doc,
        config_path=path.parent / "receipt_chain_config.json",
        list_key="receipts",
        doc_kind="verra_sandbox_exec_receipts",
    )
    path.write_text(json.dumps(doc, indent=2) + "\n", "utf-8")


def append_receipt(doc: dict[str, Any], receipt: dict[str, Any], max_items: int = 1000) -> None:
    doc.setdefault("receipts", []).append(receipt)
    del doc["receipts"][:-max_items]


def consume_sandbox_flags(tool_args: dict[str, Any] | None) -> dict[str, Any]:
    args = tool_args if isinstance(tool_args, dict) else {}
    vals = {
        "use_sandbox": _to_bool(args.pop("verra_use_sandbox", None)),
        "disable_sandbox": _to_bool(args.pop("verra_no_sandbox", None)),
        "sandbox_subdir": str(args.pop("verra_sandbox_subdir", "") or "").strip(),
        "sandbox_target": str(args.pop("verra_sandbox_target", "") or "").strip(),
    }
    return vals


def should_route_to_sandbox(tool_name: str, tool_args: dict[str, Any], flags: dict[str, Any]) -> bool:
    if flags.get("disable_sandbox"):
        return False
    runtime = str((tool_args or {}).get("runtime", "")).lower().strip()
    if runtime in {"terminal", "python", "nodejs"}:
        return True
    tname = (tool_name or "").lower()
    if "code" in tname and ("exec" in tname or "terminal" in tname):
        return True
    return False


def choose_cwd(manifest: dict[str, Any], flags: dict[str, Any]) -> str:
    paths = manifest.get("paths") or {}
    target = flags.get("sandbox_target") or "current"
    subdir = flags.get("sandbox_subdir") or ""
    base = paths.get(target) or paths.get("current") or ""
    p = Path(str(base))
    if subdir:
        p = p / subdir
        p.mkdir(parents=True, exist_ok=True)
    return str(p)


def pre_exec_snapshot(manifest: dict[str, Any], *, tool_name: str, runtime: str, tool_args: dict[str, Any]) -> dict[str, Any]:
    paths = manifest.get("paths") or {}
    current = Path(paths.get("current", "."))
    scratch = Path(paths.get("scratch", current / "scratch"))
    patches = Path(paths.get("patches", current / "patches"))
    runs = Path(paths.get("runs", current / "runs"))
    return {
        "at": _now_iso(),
        "tool_name": tool_name,
        "runtime": runtime,
        "cwd": str(tool_args.get("cwd", "")),
        "counts": {
            "scratch": _file_count(scratch),
            "patches": _file_count(patches),
            "runs": _file_count(runs),
        },
        "listings": {
            "scratch": _list_rel(scratch),
            "patches": _list_rel(patches),
            "runs": _list_rel(runs),
        },
    }


def post_exec_snapshot(manifest: dict[str, Any], *, before: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = manifest.get("paths") or {}
    current = Path(paths.get("current", "."))
    scratch = Path(paths.get("scratch", current / "scratch"))
    patches = Path(paths.get("patches", current / "patches"))
    runs = Path(paths.get("runs", current / "runs"))
    after = {
        "at": _now_iso(),
        "counts": {
            "scratch": _file_count(scratch),
            "patches": _file_count(patches),
            "runs": _file_count(runs),
        },
        "listings": {
            "scratch": _list_rel(scratch),
            "patches": _list_rel(patches),
            "runs": _list_rel(runs),
        },
    }
    if before:
        after["delta"] = {
            area: _diff_lists((before.get("listings") or {}).get(area, []), (after.get("listings") or {}).get(area, []))
            for area in ("scratch", "patches", "runs")
        }
    return after


def sandbox_exec_prompt_fragment(manifest: dict[str, Any]) -> str:
    paths = manifest.get("paths") or {}
    return "\n".join(
        [
            "VERRA SANDBOX EXECUTION ADAPTER:",
            f"- code_execution can be routed into sandbox cwd: {paths.get('current', 'n/a')}",
            f"- write artifacts to scratch: {paths.get('scratch', 'n/a')}",
            f"- write patch proposals to patches: {paths.get('patches', 'n/a')}",
            "- use `verra_no_sandbox: true` only when live execution is explicitly intended and approved.",
            "- use `verra_sandbox_subdir` to isolate experiments (e.g. 'exp-001').",
            "- generate unified diffs/patch files into sandbox patches for promotion.",
        ]
    )


def _file_count(root: Path) -> int:
    if not root.exists():
        return 0
    n = 0
    for p in root.rglob("*"):
        if p.is_file():
            n += 1
    return n


def _list_rel(root: Path, limit: int = 200) -> list[str]:
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out.append(str(p.relative_to(root)))
            except Exception:
                out.append(str(p))
        if len(out) >= limit:
            break
    return out


def _diff_lists(before: list[str], after: list[str]) -> dict[str, list[str]]:
    b = set(before or [])
    a = set(after or [])
    return {"added": sorted(list(a - b))[:100], "removed": sorted(list(b - a))[:100]}


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}
