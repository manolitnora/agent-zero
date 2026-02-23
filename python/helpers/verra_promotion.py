from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python.helpers import verra_receipt_chain

DIFF_HEADER_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:a/|b/)?(.+)$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_promotion.v1",
        "enabled": True,
        "allowed_target_roots": [],
        "max_patch_bytes": 500_000,
        "require_simulation_first": True,
        "backup_mode": "copies",
    }


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")


def load_receipts(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {"schema_version": "verra_promotion_receipts.v1", "receipts": []}


def save_receipts(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verra_receipt_chain.chain_receipt_doc(
        doc,
        config_path=path.parent / "receipt_chain_config.json",
        list_key="receipts",
        doc_kind="verra_promotion_receipts",
    )
    path.write_text(json.dumps(doc, indent=2) + "\n", "utf-8")


def append_receipt(doc: dict[str, Any], receipt: dict[str, Any], max_items: int = 1000) -> None:
    doc.setdefault("receipts", []).append(receipt)
    del doc["receipts"][:-max_items]


def consume_promotion_flags(tool_args: dict[str, Any] | None) -> dict[str, Any] | None:
    args = tool_args if isinstance(tool_args, dict) else {}
    rollback_dir = str(args.pop("verra_promote_rollback_dir", "") or "").strip()
    rollback_apply = _to_bool(args.pop("verra_promote_rollback_apply", None))
    rollback_target_root = str(args.pop("verra_promote_rollback_target_root", "") or "").strip()
    if rollback_dir:
        return {
            "mode": "rollback",
            "rollback_dir": rollback_dir,
            "rollback_apply": rollback_apply,
            "target_root": rollback_target_root,
            "label": str(args.pop("verra_promote_label", "") or "").strip(),
        }
    patch_path = str(args.pop("verra_promote_patch_path", "") or "").strip()
    if not patch_path:
        return None
    return {
        "mode": "patch",
        "patch_path": patch_path,
        "target_root": str(args.pop("verra_promote_target_root", "") or "").strip(),
        "label": str(args.pop("verra_promote_label", "") or "").strip(),
        "apply_live": _to_bool(args.pop("verra_promote_live", None)),
        "rollback_tag": str(args.pop("verra_promote_rollback_tag", "") or "").strip(),
    }


def preview_patch(
    request: dict[str, Any],
    *,
    sandbox_manifest: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    patch_file = _resolve_patch_file(request.get("patch_path", ""), sandbox_manifest)
    if patch_file is None:
        return {"status": "patch_not_found", "reason": "Patch path not found inside sandbox.", "request": request}
    if not patch_file.exists() or not patch_file.is_file():
        return {"status": "patch_not_found", "reason": "Patch file missing.", "patch_file": str(patch_file)}
    size = patch_file.stat().st_size
    if size > int(cfg.get("max_patch_bytes", 500_000) or 500_000):
        return {"status": "patch_too_large", "patch_file": str(patch_file), "size": int(size)}
    text = patch_file.read_text("utf-8", errors="ignore")
    targets = _patch_targets(text)
    target_root = _resolve_target_root(request.get("target_root", ""), cfg, sandbox_manifest)
    if target_root is None:
        return {
            "status": "target_root_unset",
            "patch_file": str(patch_file),
            "targets": targets,
            "allowed_target_roots": list(cfg.get("allowed_target_roots", [])),
        }
    if not _target_allowed(target_root, cfg):
        return {
            "status": "target_root_blocked",
            "patch_file": str(patch_file),
            "target_root": str(target_root),
            "allowed_target_roots": list(cfg.get("allowed_target_roots", [])),
        }

    git_check = _git_apply_check(target_root, patch_file)
    return {
        "status": "preview_ready",
        "patch_file": str(patch_file),
        "target_root": str(target_root),
        "patch_size": int(size),
        "targets": targets[:100],
        "git_apply_check": git_check,
        "label": request.get("label") or "",
    }


def apply_patch_live(
    preview: dict[str, Any],
    *,
    data_dir: Path,
    request: dict[str, Any],
) -> dict[str, Any]:
    if preview.get("status") != "preview_ready":
        return {"status": "blocked", "reason": "preview not ready", "preview_status": preview.get("status")}
    patch_file = Path(str(preview.get("patch_file", "")))
    target_root = Path(str(preview.get("target_root", "")))
    if not patch_file.exists() or not target_root.exists():
        return {"status": "blocked", "reason": "patch or target root missing at apply time"}

    rollback_dir = data_dir / "promotion_rollbacks" / _safe_name(request.get("rollback_tag") or request.get("label") or _now_iso())
    rollback_dir.mkdir(parents=True, exist_ok=True)
    backup = _backup_target_files(target_root, preview.get("targets", []), rollback_dir)

    git_apply = _git_apply(target_root, patch_file)
    return {
        "status": "applied" if git_apply.get("ok") else "apply_failed",
        "patch_file": str(patch_file),
        "target_root": str(target_root),
        "rollback_dir": str(rollback_dir),
        "backup": backup,
        "git_apply": git_apply,
        "applied_at": _now_iso(),
    }


def promotion_prompt_fragment(cfg: dict[str, Any]) -> str:
    return "\n".join(
        [
            "VERRA PROMOTION PIPELINE:",
            "- Generate patch files into sandbox `patches/` before attempting promotion.",
            "- Preview promotion with tool args: `verra_promote_patch_path`, `verra_simulate: true`.",
            "- Apply after review using: `verra_promote_patch_path`, `verra_promote_target_root`, `verra_live_approved: true`, `verra_promote_live: true`.",
            "- Verra records promotion receipts and rollback snapshots before live apply.",
            "- Rollback preview/apply: `verra_promote_rollback_dir`, optional `verra_promote_rollback_target_root`, and `verra_promote_rollback_apply: true` (+ `verra_live_approved: true`).",
            f"- require_simulation_first: {bool(cfg.get('require_simulation_first', True))}",
        ]
    )


def preview_rollback(request: dict[str, Any], *, cfg: dict[str, Any]) -> dict[str, Any]:
    rollback_dir = Path(str(request.get("rollback_dir", ""))).expanduser().resolve()
    if not rollback_dir.exists() or not rollback_dir.is_dir():
        return {"status": "rollback_not_found", "rollback_dir": str(rollback_dir)}
    target_root = _resolve_rollback_target_root(request, cfg, rollback_dir)
    if target_root is None:
        return {"status": "rollback_target_unset", "rollback_dir": str(rollback_dir)}
    if not _target_allowed(target_root, cfg):
        return {
            "status": "target_root_blocked",
            "rollback_dir": str(rollback_dir),
            "target_root": str(target_root),
            "allowed_target_roots": list(cfg.get("allowed_target_roots", [])),
        }
    files = [str(p.relative_to(rollback_dir)) for p in sorted(rollback_dir.rglob("*")) if p.is_file()][:500]
    return {
        "status": "rollback_preview_ready",
        "rollback_dir": str(rollback_dir),
        "target_root": str(target_root),
        "files": files,
        "file_count": len(files),
    }


def apply_rollback_live(preview: dict[str, Any], *, data_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    if preview.get("status") != "rollback_preview_ready":
        return {"status": "blocked", "reason": "rollback preview not ready", "preview_status": preview.get("status")}
    rollback_dir = Path(str(preview.get("rollback_dir", "")))
    target_root = Path(str(preview.get("target_root", "")))
    if not rollback_dir.exists() or not target_root.exists():
        return {"status": "blocked", "reason": "rollback dir or target root missing"}

    before_backup_dir = data_dir / "promotion_rollbacks_applied" / _safe_name(
        request.get("label") or f"rollback_{_now_iso()}"
    )
    before_backup_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    failed = []
    for src in sorted(rollback_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(rollback_dir)
        dst = (target_root / rel).resolve()
        try:
            dst.relative_to(target_root.resolve())
        except Exception:
            failed.append(str(rel))
            continue
        if dst.exists() and dst.is_file():
            bdst = before_backup_dir / rel
            bdst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(dst, bdst)
            except Exception:
                pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied.append(str(rel))
        except Exception:
            failed.append(str(rel))
    return {
        "status": "rollback_applied" if not failed else "rollback_partial",
        "rollback_dir": str(rollback_dir),
        "target_root": str(target_root),
        "before_backup_dir": str(before_backup_dir),
        "copied_count": len(copied),
        "failed_count": len(failed),
        "copied": copied[:200],
        "failed": failed[:200],
        "applied_at": _now_iso(),
    }


def _resolve_patch_file(patch_path: str, sandbox_manifest: dict[str, Any]) -> Path | None:
    if not patch_path:
        return None
    current = Path((sandbox_manifest.get("paths") or {}).get("current", ".")).resolve()
    p = Path(patch_path).expanduser()
    if not p.is_absolute():
        candidate = p.resolve()
        try:
            candidate.relative_to(current)
            p = candidate
        except Exception:
            p = (current / p).resolve()
    try:
        p.relative_to(current)
    except Exception:
        return None
    return p


def _resolve_target_root(requested: str, cfg: dict[str, Any], sandbox_manifest: dict[str, Any]) -> Path | None:
    if requested:
        return Path(requested).expanduser().resolve()
    roots = cfg.get("allowed_target_roots", []) or []
    if roots:
        return Path(str(roots[0])).expanduser().resolve()
    mounts = (sandbox_manifest.get("mounts") or [])
    if mounts:
        target = mounts[0].get("target")
        if target:
            return Path(str(target)).resolve()
    return None


def _resolve_rollback_target_root(request: dict[str, Any], cfg: dict[str, Any], rollback_dir: Path) -> Path | None:
    requested = str(request.get("target_root", "") or "").strip()
    if requested:
        return Path(requested).expanduser().resolve()
    parent = rollback_dir.parent.parent if rollback_dir.parent.name in {"promotion_rollbacks", "promotion_rollbacks_applied"} else None
    if parent and parent.exists():
        # no reliable inference; keep explicit target preferred.
        pass
    roots = cfg.get("allowed_target_roots", []) or []
    if roots:
        return Path(str(roots[0])).expanduser().resolve()
    return None


def _target_allowed(target_root: Path, cfg: dict[str, Any]) -> bool:
    allowed = [Path(str(p)).expanduser().resolve() for p in (cfg.get("allowed_target_roots", []) or []) if str(p).strip()]
    if not allowed:
        return False
    for root in allowed:
        try:
            target_root.relative_to(root)
            return True
        except Exception:
            if target_root == root:
                return True
    return False


def _patch_targets(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        m = DIFF_HEADER_RE.match(line)
        if not m:
            continue
        path = m.group(1).strip()
        if path == "/dev/null":
            continue
        if path not in out:
            out.append(path)
    return out


def _git_apply_check(target_root: Path, patch_file: Path) -> dict[str, Any]:
    if not (target_root / ".git").exists():
        return {"supported": False, "ok": None, "reason": "target root is not a git repo"}
    return _run(["git", "apply", "--check", str(patch_file)], cwd=target_root)


def _git_apply(target_root: Path, patch_file: Path) -> dict[str, Any]:
    if not (target_root / ".git").exists():
        return {"supported": False, "ok": False, "reason": "target root is not a git repo"}
    return _run(["git", "apply", str(patch_file)], cwd=target_root)


def _backup_target_files(target_root: Path, targets: list[str], rollback_dir: Path) -> dict[str, Any]:
    copied = []
    missing = []
    for rel in (targets or [])[:200]:
        src = (target_root / rel).resolve()
        try:
            src.relative_to(target_root.resolve())
        except Exception:
            missing.append(rel)
            continue
        if not src.exists() or not src.is_file():
            missing.append(rel)
            continue
        dst = rollback_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied.append(rel)
        except Exception:
            missing.append(rel)
    return {"copied": copied[:200], "missing": missing[:200], "copied_count": len(copied), "missing_count": len(missing)}


def _run(cmd: list[str], *, cwd: Path, timeout_sec: float = 10.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_sec, check=False)
        return {
            "supported": True,
            "ok": proc.returncode == 0,
            "returncode": int(proc.returncode),
            "stdout": (proc.stdout or "")[:4000],
            "stderr": (proc.stderr or "")[:4000],
        }
    except Exception as e:
        return {"supported": True, "ok": False, "returncode": None, "stdout": "", "stderr": str(e)}


def _safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(s))[:120] or "promotion"


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
