from __future__ import annotations

import difflib
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python.helpers import verra_receipt_chain


SAFE_SHELL_CMDS = {"pwd", "ls", "dir", "echo", "whoami", "uname"}
SAFE_GIT_SUBCMDS = {"status", "diff", "rev-parse", "branch", "log"}
DANGEROUS_TOKENS = {
    "rm",
    "mv",
    "cp",
    "chmod",
    "chown",
    "sudo",
    "curl",
    "wget",
    "scp",
    "ssh",
    "python",
    "node",
    "bash",
    "sh",
    "zsh",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_receipts(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {"schema_version": "verra_tool_sim_receipts.v1", "receipts": []}


def save_receipts(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verra_receipt_chain.chain_receipt_doc(
        data,
        config_path=path.parent / "receipt_chain_config.json",
        list_key="receipts",
        doc_kind="verra_tool_sim_receipts",
    )
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")


def append_receipt(receipts_doc: dict[str, Any], receipt: dict[str, Any], max_items: int = 1000) -> None:
    receipts_doc.setdefault("receipts", []).append(receipt)
    del receipts_doc["receipts"][:-max_items]


def simulate_tool_preview(tool_name: str, tool_args: dict[str, Any] | None, *, default_cwd: str | None = None) -> dict[str, Any]:
    args = tool_args or {}
    tname = (tool_name or "").lower()
    cwd = _pick_cwd(args, default_cwd)

    # Agent Zero code execution tool shape.
    runtime = str(args.get("runtime", "")).lower().strip()
    if runtime == "terminal" and "code" in args:
        return _simulate_terminal_command(str(args.get("code", "")), cwd=cwd, tool_name=tool_name)
    if runtime in {"python", "nodejs"} and "code" in args:
        code = str(args.get("code", ""))
        return {
            "status": "code_preview_only",
            "kind": runtime,
            "exec_blocked": True,
            "summary": f"{runtime} code preview only (length={len(code)} chars)",
            "snippet": code[:500],
        }

    if "git" in tname:
        command = str(args.get("command") or args.get("cmd") or args.get("code") or "").strip()
        if command:
            return _simulate_git_cli(command, cwd=cwd)

    file_preview = _simulate_file_change(tool_name, args, cwd=cwd)
    if file_preview is not None:
        return file_preview

    return {
        "status": "generic_preview",
        "kind": "tool",
        "exec_blocked": True,
        "summary": f"Simulation preview for '{tool_name}' (no native adapter matched).",
        "args_keys": sorted([str(k) for k in args.keys()])[:50],
    }


def _pick_cwd(args: dict[str, Any], default_cwd: str | None) -> Path:
    for key in ("cwd", "workdir", "workspace", "repo_path", "path"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            p = Path(val).expanduser()
            if p.is_file():
                p = p.parent
            if p.exists():
                return p
    if default_cwd:
        p = Path(default_cwd).expanduser()
        if p.exists():
            return p
    return Path.cwd()


def _simulate_terminal_command(command: str, *, cwd: Path, tool_name: str) -> dict[str, Any]:
    command = (command or "").strip()
    if not command:
        return {
            "status": "empty_command",
            "kind": "shell",
            "exec_blocked": True,
            "summary": "No terminal command provided for simulation.",
        }

    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()

    if not tokens:
        return {
            "status": "empty_command",
            "kind": "shell",
            "exec_blocked": True,
            "summary": "No terminal command provided for simulation.",
        }

    head = tokens[0].lower()
    if head == "git":
        return _simulate_git_cli(command, cwd=cwd)

    if _looks_destructive_shell(command, tokens):
        return {
            "status": "dangerous_shell_blocked",
            "kind": "shell",
            "exec_blocked": True,
            "summary": "Command appears write/destructive/network-capable. Verra did not execute it during simulation.",
            "command": command[:500],
            "cwd": str(cwd),
        }

    if head in SAFE_SHELL_CMDS:
        result = _run_readonly(tokens, cwd=cwd)
        return {
            "status": "shell_readonly_preview",
            "kind": "shell",
            "exec_blocked": True,
            "summary": "Safe read-only shell preview executed.",
            "command": command[:500],
            "cwd": str(cwd),
            **result,
        }

    return {
        "status": "shell_preview_only",
        "kind": "shell",
        "exec_blocked": True,
        "summary": "No safe native preview adapter for this shell command. Execution skipped.",
        "command": command[:500],
        "cwd": str(cwd),
    }


def _simulate_git_cli(command: str, *, cwd: Path) -> dict[str, Any]:
    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()
    if not tokens or tokens[0].lower() != "git":
        return {
            "status": "not_git_command",
            "kind": "git",
            "exec_blocked": True,
            "summary": "Command is not a git command.",
        }
    subcmd = tokens[1].lower() if len(tokens) > 1 else ""

    if not _is_git_repo(cwd):
        return {
            "status": "not_git_repo",
            "kind": "git",
            "exec_blocked": True,
            "summary": f"No git repository detected at {cwd}",
            "cwd": str(cwd),
        }

    if subcmd in SAFE_GIT_SUBCMDS and not _git_tokens_modify_state(tokens):
        result = _run_readonly(tokens, cwd=cwd)
        return {
            "status": "git_readonly_preview",
            "kind": "git",
            "exec_blocked": True,
            "summary": "Safe git read-only preview executed.",
            "command": command[:500],
            "cwd": str(cwd),
            **result,
        }

    status_res = _run_readonly(["git", "status", "--short"], cwd=cwd)
    diffstat_res = _run_readonly(["git", "diff", "--stat"], cwd=cwd)
    return {
        "status": "git_write_preview_only",
        "kind": "git",
        "exec_blocked": True,
        "summary": "Git command may modify repository; Verra skipped execution and returned repo state preview.",
        "command": command[:500],
        "cwd": str(cwd),
        "git_status": _truncate_output(status_res.get("stdout", ""), 1500),
        "git_diff_stat": _truncate_output(diffstat_res.get("stdout", ""), 1500),
        "status_exit_code": status_res.get("exit_code"),
        "diff_stat_exit_code": diffstat_res.get("exit_code"),
    }


def _simulate_file_change(tool_name: str, args: dict[str, Any], *, cwd: Path) -> dict[str, Any] | None:
    path_value = None
    for key in ("path", "file", "filepath", "target", "filename"):
        if key in args and isinstance(args[key], str):
            path_value = args[key]
            break
    if not path_value:
        return None

    p = Path(path_value).expanduser()
    if not p.is_absolute():
        p = (cwd / p).resolve()

    old_text = ""
    existed = p.exists() and p.is_file()
    if existed:
        try:
            old_text = p.read_text("utf-8")
        except Exception:
            return {
                "status": "file_preview_unreadable",
                "kind": "file",
                "exec_blocked": True,
                "summary": f"Could not read existing file for preview: {p}",
                "path": str(p),
            }

    new_text = None
    if any(k in args for k in ("content", "text", "new_content")):
        new_text = str(args.get("content", args.get("new_content", args.get("text", ""))))
    elif any(k in args for k in ("append", "append_text")):
        append_val = str(args.get("append", args.get("append_text", "")))
        new_text = old_text + append_val
    elif any(k in args for k in ("old", "find")) and any(k in args for k in ("new", "replace")):
        needle = str(args.get("old", args.get("find", "")))
        repl = str(args.get("new", args.get("replace", "")))
        if needle:
            new_text = old_text.replace(needle, repl)
    elif "patch" in args and isinstance(args.get("patch"), str):
        return {
            "status": "patch_preview_unsupported",
            "kind": "file",
            "exec_blocked": True,
            "summary": "Patch-based preview not yet applied in simulation helper. Execution skipped.",
            "path": str(p),
            "patch_snippet": str(args.get("patch", ""))[:600],
        }

    if new_text is None:
        return None

    if new_text == old_text:
        return {
            "status": "file_no_change_preview",
            "kind": "file",
            "exec_blocked": True,
            "summary": "Computed file preview resulted in no textual changes.",
            "path": str(p),
        }

    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(p) if existed else f"{p} (new)",
            tofile=str(p),
            n=3,
        )
    )
    return {
        "status": "file_diff_preview",
        "kind": "file",
        "exec_blocked": True,
        "summary": "File write/edit simulation generated a unified diff preview.",
        "path": str(p),
        "existed": existed,
        "diff": _truncate_output(diff, 4000),
        "old_len": len(old_text),
        "new_len": len(new_text),
    }


def _run_readonly(tokens: list[str], *, cwd: Path, timeout_sec: float = 3.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            tokens,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return {
            "exit_code": int(proc.returncode),
            "stdout": _truncate_output(proc.stdout or "", 3000),
            "stderr": _truncate_output(proc.stderr or "", 1500),
        }
    except Exception as e:
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": str(e),
        }


def _is_git_repo(cwd: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        return proc.returncode == 0 and "true" in (proc.stdout or "").lower()
    except Exception:
        return False


def _git_tokens_modify_state(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    subcmd = tokens[1].lower()
    return subcmd not in SAFE_GIT_SUBCMDS


def _looks_destructive_shell(command: str, tokens: list[str]) -> bool:
    lower = command.lower()
    if any(op in lower for op in ("|", ">", ">>", "&&", "||", ";", "$(", "`")):
        return True
    return any(tok.lower() in DANGEROUS_TOKENS for tok in tokens)


def _truncate_output(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "\n...[truncated]\n"
