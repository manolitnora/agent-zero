from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                cfg = default_config()
                cfg.update(data)
                for section in ("preflight", "canary", "timeouts"):
                    if isinstance(cfg.get(section), dict) and isinstance(data.get(section), dict):
                        merged = dict(default_config().get(section, {}))
                        merged.update(data.get(section, {}))
                        cfg[section] = merged
                return cfg
        except Exception:
            pass
    return default_config()


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")


def default_config() -> dict[str, Any]:
    return {
        "schema_version": "verra_promotion_eval_canary.v1",
        "enabled": True,
        "require_preflight_pass_for_live": True,
        "auto_rollback_on_canary_fail": True,
        "preflight": {
            "enabled": True,
            "commands": [],
            "require_all": True,
        },
        "canary": {
            "enabled": True,
            "commands": [],
            "require_all": True,
            "reverse_patch_check": True,
        },
        "timeouts": {
            "preflight_sec": 90,
            "canary_sec": 90,
        },
    }


def run_preflight_eval(
    *,
    preview: dict[str, Any],
    cfg: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    if not bool(cfg.get("enabled", True)) or not bool((cfg.get("preflight") or {}).get("enabled", True)):
        return {"status": "disabled", "passed": True, "checks": []}
    if preview.get("status") != "preview_ready":
        return {
            "status": "blocked",
            "passed": False,
            "reason": "preview_not_ready",
            "preview_status": preview.get("status"),
            "checks": [],
        }

    checks: list[dict[str, Any]] = []
    git_chk = preview.get("git_apply_check") or {}
    if isinstance(git_chk, dict):
        checks.append(
            {
                "name": "git_apply_check",
                "kind": "builtin",
                "ok": bool(git_chk.get("ok")),
                "returncode": git_chk.get("returncode"),
                "stderr": str(git_chk.get("stderr", ""))[:1200],
            }
        )

    target_root = Path(str(preview.get("target_root", ""))) if preview.get("target_root") else None
    patch_file = Path(str(preview.get("patch_file", ""))) if preview.get("patch_file") else None
    for idx, cmd in enumerate(_commands((cfg.get("preflight") or {}).get("commands"))):
        checks.append(
            _run_command_check(
                cmd,
                cwd=target_root,
                timeout_sec=int((cfg.get("timeouts") or {}).get("preflight_sec", 90) or 90),
                context={"target_root": str(target_root or ""), "patch_file": str(patch_file or ""), "label": str(request.get("label", ""))},
                name=f"preflight_cmd_{idx+1}",
            )
        )

    passed = _aggregate_pass(checks, require_all=bool((cfg.get("preflight") or {}).get("require_all", True)))
    return {
        "status": "preflight_passed" if passed else "preflight_failed",
        "passed": passed,
        "checks": checks,
        "evaluated_at": _now_iso(),
    }


def run_canary_checks(
    *,
    preview: dict[str, Any],
    apply_result: dict[str, Any],
    cfg: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    if not bool(cfg.get("enabled", True)) or not bool((cfg.get("canary") or {}).get("enabled", True)):
        return {"status": "disabled", "passed": True, "checks": []}
    if str(apply_result.get("status", "")) != "applied":
        return {
            "status": "blocked",
            "passed": False,
            "reason": "apply_not_successful",
            "apply_status": apply_result.get("status"),
            "checks": [],
        }

    checks: list[dict[str, Any]] = []
    target_root = Path(str(apply_result.get("target_root", preview.get("target_root", ""))))
    patch_file = Path(str(apply_result.get("patch_file", preview.get("patch_file", ""))))
    if bool((cfg.get("canary") or {}).get("reverse_patch_check", True)):
        checks.append(_reverse_patch_check(target_root=target_root, patch_file=patch_file))

    for idx, cmd in enumerate(_commands((cfg.get("canary") or {}).get("commands"))):
        checks.append(
            _run_command_check(
                cmd,
                cwd=target_root,
                timeout_sec=int((cfg.get("timeouts") or {}).get("canary_sec", 90) or 90),
                context={"target_root": str(target_root), "patch_file": str(patch_file), "label": str(request.get("label", ""))},
                name=f"canary_cmd_{idx+1}",
            )
        )

    passed = _aggregate_pass(checks, require_all=bool((cfg.get("canary") or {}).get("require_all", True)))
    return {
        "status": "canary_passed" if passed else "canary_failed",
        "passed": passed,
        "checks": checks,
        "evaluated_at": _now_iso(),
    }


def summary_fragment(preflight: dict[str, Any] | None = None, canary: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    if isinstance(preflight, dict):
        lines.append(f"Preflight: {preflight.get('status', 'unknown')} passed={preflight.get('passed')}")
        checks = preflight.get("checks") or []
        if checks:
            lines.append("Preflight checks: " + ", ".join([_check_brief(c) for c in checks[:6]]))
    if isinstance(canary, dict):
        lines.append(f"Canary: {canary.get('status', 'unknown')} passed={canary.get('passed')}")
        checks = canary.get("checks") or []
        if checks:
            lines.append("Canary checks: " + ", ".join([_check_brief(c) for c in checks[:6]]))
    return "\n".join(lines)


def _check_brief(check: dict[str, Any]) -> str:
    name = str(check.get("name", check.get("kind", "check")))
    ok = check.get("ok")
    rc = check.get("returncode")
    return f"{name}(ok={ok},rc={rc})"


def _commands(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out[:20]


def _aggregate_pass(checks: list[dict[str, Any]], *, require_all: bool) -> bool:
    if not checks:
        return True
    oks = [bool(c.get("ok")) for c in checks if "ok" in c]
    if not oks:
        return True
    return all(oks) if require_all else any(oks)


def _reverse_patch_check(*, target_root: Path, patch_file: Path) -> dict[str, Any]:
    if not target_root.exists():
        return {"name": "reverse_patch_check", "kind": "builtin", "ok": False, "reason": "target_root_missing"}
    if not patch_file.exists():
        return {"name": "reverse_patch_check", "kind": "builtin", "ok": False, "reason": "patch_file_missing"}
    if not (target_root / ".git").exists():
        return {"name": "reverse_patch_check", "kind": "builtin", "ok": False, "reason": "not_git_repo"}
    return _run_subprocess(
        ["git", "apply", "--check", "--reverse", str(patch_file)],
        cwd=target_root,
        timeout_sec=30,
        name="reverse_patch_check",
        kind="builtin",
    )


def _run_command_check(
    command: str,
    *,
    cwd: Path | None,
    timeout_sec: int,
    context: dict[str, str],
    name: str,
) -> dict[str, Any]:
    rendered = _format_command(command, context)
    try:
        tokens = shlex.split(rendered)
    except Exception as e:
        return {"name": name, "kind": "command", "ok": False, "reason": f"parse_error: {e}", "command": rendered[:400]}
    if not tokens:
        return {"name": name, "kind": "command", "ok": False, "reason": "empty_command", "command": rendered[:400]}
    return _run_subprocess(tokens, cwd=cwd, timeout_sec=timeout_sec, name=name, kind="command", rendered_command=rendered)


def _run_subprocess(
    argv: list[str],
    *,
    cwd: Path | None,
    timeout_sec: int,
    name: str,
    kind: str,
    rendered_command: str | None = None,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
            check=False,
        )
        return {
            "name": name,
            "kind": kind,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "command": (rendered_command or " ".join(argv))[:400],
            "cwd": str(cwd) if cwd else "",
            "stdout": (proc.stdout or "")[:1600],
            "stderr": (proc.stderr or "")[:1600],
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "kind": kind,
            "ok": False,
            "reason": "timeout",
            "command": (rendered_command or " ".join(argv))[:400],
            "cwd": str(cwd) if cwd else "",
        }
    except Exception as e:
        return {
            "name": name,
            "kind": kind,
            "ok": False,
            "reason": f"exec_error: {e}",
            "command": (rendered_command or " ".join(argv))[:400],
            "cwd": str(cwd) if cwd else "",
        }


def _format_command(command: str, context: dict[str, str]) -> str:
    out = str(command)
    for k, v in (context or {}).items():
        out = out.replace("{" + str(k) + "}", str(v or ""))
    return out
