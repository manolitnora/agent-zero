from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_sandbox.v1",
        "enabled": True,
        "mode": "shadow_workspace",
        "max_sessions": 20,
        "source_roots": [],
        "sandbox_rules": {
            "allow_symlinks": True,
            "create_mounts": True,
            "copy_on_write_hint": True,
            "network_enabled": False,
            "default_shell_exec_enabled": False,
        },
    }


def save_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", "utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_sandbox_state.v1",
        "sessions": {},
        "active_session_id": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    path.write_text(json.dumps(state, indent=2) + "\n", "utf-8")


def ensure_session_sandbox(
    data_dir: Path,
    *,
    session_id: str,
    source_roots: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or {}
    roots = [str(Path(p).expanduser().resolve()) for p in (source_roots or []) if p]
    root_dir = data_dir / "sandboxes" / _safe_name(session_id)
    current_dir = root_dir / "current"
    scratch_dir = current_dir / "scratch"
    patches_dir = current_dir / "patches"
    runs_dir = current_dir / "runs"
    knowledge_dir = current_dir / "knowledge"
    mounts_dir = current_dir / "mounts"

    for p in [root_dir, current_dir, scratch_dir, patches_dir, runs_dir, knowledge_dir]:
        p.mkdir(parents=True, exist_ok=True)
    if bool((cfg.get("sandbox_rules") or {}).get("create_mounts", True)):
        mounts_dir.mkdir(parents=True, exist_ok=True)
        _sync_mounts(mounts_dir, roots, allow_symlinks=bool((cfg.get("sandbox_rules") or {}).get("allow_symlinks", True)))

    manifest = {
        "schema_version": "verra_sandbox_session.v1",
        "session_id": session_id,
        "created_at": _now_iso() if not (current_dir / "manifest.json").exists() else None,
        "updated_at": _now_iso(),
        "mode": str(cfg.get("mode", "shadow_workspace")),
        "network_enabled": bool((cfg.get("sandbox_rules") or {}).get("network_enabled", False)),
        "default_shell_exec_enabled": bool((cfg.get("sandbox_rules") or {}).get("default_shell_exec_enabled", False)),
        "paths": {
            "root": str(root_dir),
            "current": str(current_dir),
            "scratch": str(scratch_dir),
            "patches": str(patches_dir),
            "runs": str(runs_dir),
            "knowledge": str(knowledge_dir),
            "mounts": str(mounts_dir),
        },
        "source_roots": roots,
        "mounts": _mount_records(mounts_dir),
    }
    manifest_path = current_dir / "manifest.json"
    if manifest_path.exists():
        try:
            prev = json.loads(manifest_path.read_text("utf-8"))
            if isinstance(prev, dict) and prev.get("created_at"):
                manifest["created_at"] = prev.get("created_at")
        except Exception:
            pass
    if not manifest.get("created_at"):
        manifest["created_at"] = _now_iso()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")

    readme_path = current_dir / "README.txt"
    if not readme_path.exists():
        readme_path.write_text(
            "Verra Sandbox (shadow workspace)\n"
            "- Use this directory for experiments, generated patches, and learning artifacts.\n"
            "- Source roots may be mounted as symlinks under ./mounts (read-oriented).\n"
            "- Prefer writing outputs to ./scratch or ./patches, not directly to mounted roots.\n",
            "utf-8",
        )

    return manifest


def update_state_with_session(state: dict[str, Any], manifest: dict[str, Any]) -> None:
    sid = str(manifest.get("session_id", "default"))
    state.setdefault("sessions", {})[sid] = {
        "updated_at": manifest.get("updated_at", _now_iso()),
        "current_path": (manifest.get("paths") or {}).get("current"),
        "scratch_path": (manifest.get("paths") or {}).get("scratch"),
        "patches_path": (manifest.get("paths") or {}).get("patches"),
        "knowledge_path": (manifest.get("paths") or {}).get("knowledge"),
        "mounts_path": (manifest.get("paths") or {}).get("mounts"),
        "source_roots": manifest.get("source_roots", []),
        "mount_count": len(manifest.get("mounts", []) or []),
        "mode": manifest.get("mode", "shadow_workspace"),
    }
    state["active_session_id"] = sid
    _trim_sessions(state, max_sessions=int(state.get("max_sessions", 20) or 20))


def sandbox_prompt_fragment(
    *,
    config: dict[str, Any],
    state: dict[str, Any],
    session_id: str,
    world_model: dict[str, Any] | None = None,
) -> str:
    session = (state.get("sessions") or {}).get(session_id, {})
    mounts = session.get("mount_count", 0)
    root = session.get("current_path", "n/a")
    scratch = session.get("scratch_path", "n/a")
    patches = session.get("patches_path", "n/a")
    mode = session.get("mode", config.get("mode", "shadow_workspace"))
    lines = [
        "VERRA SANDBOX (SELF-UPDATING LAB):",
        f"- sandbox_mode: {mode}",
        f"- session_sandbox: {root}",
        f"- scratch_dir: {scratch}",
        f"- patches_dir: {patches}",
        f"- mounted_source_roots: {mounts}",
        f"- network_enabled: {bool((config.get('sandbox_rules') or {}).get('network_enabled', False))}",
        "- Use sandbox-first experimentation: write drafts/diffs/patches to sandbox before proposing live changes.",
        "- For risky actions, request `verra_simulate: true` first to get preview outputs and diffs.",
    ]
    if world_model:
        spatial = (world_model.get("spatial_index") or {}).get("summary", {})
        lines.append(
            f"- world_map_nodes: files={spatial.get('file_nodes', 0)} dirs={spatial.get('dir_nodes', 0)} roots={spatial.get('roots', 0)}"
        )
    return "\n".join(lines)


def _sync_mounts(mounts_dir: Path, roots: list[str], *, allow_symlinks: bool = True) -> None:
    keep_names = set()
    for idx, root in enumerate(roots):
        src = Path(root)
        if not src.exists():
            continue
        name = f"{idx:02d}_{_safe_name(src.name or 'root')}"
        keep_names.add(name)
        dst = mounts_dir / name
        if dst.exists() or dst.is_symlink():
            continue
        if allow_symlinks:
            try:
                os.symlink(str(src), str(dst), target_is_directory=src.is_dir())
                continue
            except Exception:
                pass
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            (dst / ".mount_pointer.txt").write_text(str(src) + "\n", "utf-8")
        else:
            dst.write_text(f"POINTER -> {src}\n", "utf-8")

    for child in mounts_dir.iterdir():
        if child.name in keep_names:
            continue
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                # sandbox mount placeholders are shallow; avoid recursive deletes outside sandbox.
                for sub in child.iterdir():
                    if sub.is_file() or sub.is_symlink():
                        sub.unlink()
                child.rmdir()
        except Exception:
            pass


def _mount_records(mounts_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not mounts_dir.exists():
        return out
    for child in sorted(mounts_dir.iterdir(), key=lambda p: p.name):
        target = None
        try:
            if child.is_symlink():
                target = str(child.resolve())
        except Exception:
            target = None
        out.append(
            {
                "name": child.name,
                "path": str(child),
                "is_symlink": child.is_symlink(),
                "target": target,
            }
        )
    return out


def _trim_sessions(state: dict[str, Any], *, max_sessions: int) -> None:
    sessions = state.get("sessions") or {}
    if len(sessions) <= max_sessions:
        return
    ordered = sorted(
        sessions.items(),
        key=lambda kv: str((kv[1] or {}).get("updated_at", "")),
    )
    for sid, _ in ordered[:-max_sessions]:
        sessions.pop(sid, None)


def _safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(s))[:120] or "default"
