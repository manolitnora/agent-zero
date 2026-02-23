from pathlib import Path

from python.helpers.extension import Extension
from python.helpers.errors import RepairableException
from python.helpers.verra_control import VerraController
from python.helpers import verra_promotion, verra_sandbox, verra_eval_canary


class VerraPromotionPipeline(Extension):
    async def execute(self, tool_args: dict | None = None, tool_name: str = "", **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        args = tool_args if isinstance(tool_args, dict) else {}
        req = verra_promotion.consume_promotion_flags(args)
        if not req:
            return

        data_dir = Path(runtime.data_dir)
        cfg_path = data_dir / "promotion_config.json"
        eval_cfg_path = data_dir / "promotion_eval_config.json"
        receipts_path = data_dir / "promotion_receipts.json"
        sandbox_cfg_path = data_dir / "sandbox_config.json"
        sandbox_state_path = data_dir / "sandbox_state.json"

        cfg = verra_promotion.load_config(cfg_path)
        eval_cfg = verra_eval_canary.load_config(eval_cfg_path)
        if not cfg.get("allowed_target_roots"):
            cfg["allowed_target_roots"] = [str(Path(runtime.kernel_cwd).resolve()), str(Path(__file__).resolve().parents[3])]
            verra_promotion.save_config(cfg_path, cfg)
        verra_eval_canary.save_config(eval_cfg_path, eval_cfg)
        receipts = verra_promotion.load_receipts(receipts_path)

        sandbox_cfg = verra_sandbox.load_config(sandbox_cfg_path)
        if not sandbox_cfg.get("source_roots"):
            sandbox_cfg["source_roots"] = [str(Path(runtime.kernel_cwd).resolve()), str(Path(__file__).resolve().parents[3])]
            verra_sandbox.save_config(sandbox_cfg_path, sandbox_cfg)
        sandbox_state = verra_sandbox.load_state(sandbox_state_path)
        manifest = verra_sandbox.ensure_session_sandbox(
            data_dir,
            session_id=VerraController._session_id(self.agent),
            source_roots=list(sandbox_cfg.get("source_roots", [])),
            config=sandbox_cfg,
        )
        sandbox_state["max_sessions"] = int(sandbox_cfg.get("max_sessions", 20) or 20)
        verra_sandbox.update_state_with_session(sandbox_state, manifest)
        verra_sandbox.save_state(sandbox_state_path, sandbox_state)

        live_approved = self._to_bool(args.get("verra_live_approved"))
        simulate = self._to_bool(args.get("verra_simulate"))
        mode = str(req.get("mode", "patch"))

        if mode == "rollback":
            preview = verra_promotion.preview_rollback(req, cfg=cfg)
            verra_promotion.append_receipt(
                receipts,
                {
                    "at": self._now(),
                    "type": "promotion_rollback_preview",
                    "session_id": VerraController._session_id(self.agent),
                    "tool_name": tool_name,
                    "request": req,
                    "preview": preview,
                },
            )
            wants_live = bool(req.get("rollback_apply"))
            if wants_live and live_approved:
                apply_res = verra_promotion.apply_rollback_live(preview, data_dir=data_dir, request=req)
                verra_promotion.append_receipt(
                    receipts,
                    {
                        "at": self._now(),
                        "type": "promotion_rollback_apply",
                        "session_id": VerraController._session_id(self.agent),
                        "tool_name": tool_name,
                        "request": req,
                        "result": apply_res,
                    },
                )
                verra_promotion.save_receipts(receipts_path, receipts)
                raise RepairableException(self._format_rollback_apply_message(preview, apply_res))
            verra_promotion.save_receipts(receipts_path, receipts)
            raise RepairableException(self._format_rollback_preview_message(preview))

        preview = verra_promotion.preview_patch(req, sandbox_manifest=manifest, cfg=cfg)
        preflight = verra_eval_canary.run_preflight_eval(preview=preview, cfg=eval_cfg, request=req)
        verra_promotion.append_receipt(
            receipts,
            {
                "at": self._now(),
                "type": "promotion_preview",
                "session_id": VerraController._session_id(self.agent),
                "tool_name": tool_name,
                "request": req,
                "preview": preview,
            },
        )
        verra_promotion.append_receipt(
            receipts,
            {
                "at": self._now(),
                "type": "promotion_preflight_eval",
                "session_id": VerraController._session_id(self.agent),
                "tool_name": tool_name,
                "request": req,
                "result": preflight,
            },
        )
        wants_live = bool(req.get("apply_live"))

        if wants_live and live_approved:
            if bool(eval_cfg.get("require_preflight_pass_for_live", True)) and not bool(preflight.get("passed", False)):
                verra_promotion.save_receipts(receipts_path, receipts)
                raise RepairableException(self._format_apply_blocked_by_preflight(preview, preflight))
            apply_res = verra_promotion.apply_patch_live(preview, data_dir=data_dir, request=req)
            apply_receipt = {
                "at": self._now(),
                "type": "promotion_apply",
                "session_id": VerraController._session_id(self.agent),
                "tool_name": tool_name,
                "request": req,
                "result": apply_res,
            }
            verra_promotion.append_receipt(receipts, apply_receipt)
            canary = verra_eval_canary.run_canary_checks(preview=preview, apply_result=apply_res, cfg=eval_cfg, request=req)
            verra_promotion.append_receipt(
                receipts,
                {
                    "at": self._now(),
                    "type": "promotion_canary_check",
                    "session_id": VerraController._session_id(self.agent),
                    "tool_name": tool_name,
                    "request": req,
                    "result": canary,
                },
            )
            auto_rollback_result = None
            if (
                str(apply_res.get("status", "")) == "applied"
                and not bool(canary.get("passed", True))
                and bool(eval_cfg.get("auto_rollback_on_canary_fail", True))
                and apply_res.get("rollback_dir")
            ):
                rb_req = {
                    "mode": "rollback",
                    "rollback_dir": str(apply_res.get("rollback_dir")),
                    "rollback_apply": True,
                    "target_root": str(apply_res.get("target_root", "")),
                    "label": str(req.get("label") or "canary_auto_rollback"),
                }
                rb_preview = {
                    "status": "rollback_preview_ready",
                    "rollback_dir": str(apply_res.get("rollback_dir")),
                    "target_root": str(apply_res.get("target_root", "")),
                }
                auto_rollback_result = verra_promotion.apply_rollback_live(rb_preview, data_dir=data_dir, request=rb_req)
                verra_promotion.append_receipt(
                    receipts,
                    {
                        "at": self._now(),
                        "type": "promotion_rollback_apply",
                        "source": "canary_auto",
                        "session_id": VerraController._session_id(self.agent),
                        "tool_name": tool_name,
                        "request": rb_req,
                        "result": auto_rollback_result,
                    },
                )
            verra_promotion.save_receipts(receipts_path, receipts)
            raise RepairableException(self._format_apply_message(preview, apply_res, canary=canary, auto_rollback=auto_rollback_result))

        verra_promotion.save_receipts(receipts_path, receipts)
        raise RepairableException(self._format_preview_message(preview, cfg, simulate=simulate, preflight=preflight, eval_cfg=eval_cfg))

    def _format_preview_message(self, preview: dict, cfg: dict, *, simulate: bool, preflight: dict | None = None, eval_cfg: dict | None = None) -> str:
        lines = [
            "Verra Promotion Pipeline intercepted a sandbox patch promotion request.",
            f"Preview status: {preview.get('status', 'unknown')}",
        ]
        if preview.get("patch_file"):
            lines.append(f"Patch: {preview.get('patch_file')}")
        if preview.get("target_root"):
            lines.append(f"Target root: {preview.get('target_root')}")
        if preview.get("targets"):
            lines.append("Targets: " + ", ".join([str(t) for t in preview.get("targets", [])[:10]]))
        if isinstance(preview.get("git_apply_check"), dict):
            chk = preview.get("git_apply_check") or {}
            lines.append(f"git apply --check: ok={chk.get('ok')} returncode={chk.get('returncode')}")
            if chk.get("stderr"):
                lines.append("git check stderr:\n" + str(chk.get("stderr", ""))[:1200])
        if isinstance(preflight, dict):
            lines.append(verra_eval_canary.summary_fragment(preflight=preflight))
            if bool((eval_cfg or {}).get("require_preflight_pass_for_live", True)):
                lines.append("Live promotion requires passing preflight eval checks.")
        lines.append(
            "Apply live by calling a tool with: `verra_promote_patch_path`, `verra_promote_target_root`, `verra_promote_live: true`, and `verra_live_approved: true`."
        )
        if bool(cfg.get("require_simulation_first", True)) and not simulate:
            lines.append("Tip: include `verra_simulate: true` on the preview call for an explicit simulation step.")
        return "\n".join(lines)

    def _format_apply_message(self, preview: dict, apply_res: dict, *, canary: dict | None = None, auto_rollback: dict | None = None) -> str:
        lines = [
            "Verra Promotion Pipeline executed a live sandbox patch promotion.",
            f"Result: {apply_res.get('status', 'unknown')}",
            f"Patch: {apply_res.get('patch_file', preview.get('patch_file', 'n/a'))}",
            f"Target root: {apply_res.get('target_root', preview.get('target_root', 'n/a'))}",
        ]
        if apply_res.get("rollback_dir"):
            lines.append(f"Rollback snapshot: {apply_res.get('rollback_dir')}")
        git_apply = apply_res.get("git_apply") or {}
        if isinstance(git_apply, dict):
            lines.append(f"git apply: ok={git_apply.get('ok')} returncode={git_apply.get('returncode')}")
            if git_apply.get("stderr"):
                lines.append("git apply stderr:\n" + str(git_apply.get("stderr", ""))[:1200])
        if isinstance(canary, dict):
            lines.append(verra_eval_canary.summary_fragment(canary=canary))
        if isinstance(auto_rollback, dict):
            lines.append(f"Auto-rollback: {auto_rollback.get('status', 'unknown')}")
            if auto_rollback.get("before_backup_dir"):
                lines.append(f"Auto-rollback pre-backup: {auto_rollback.get('before_backup_dir')}")
        lines.append("No underlying tool was executed; Verra handled the promotion request directly.")
        return "\n".join(lines)

    def _format_apply_blocked_by_preflight(self, preview: dict, preflight: dict) -> str:
        lines = [
            "Verra Promotion Pipeline blocked live promotion because preflight eval did not pass.",
            f"Preview status: {preview.get('status', 'unknown')}",
            verra_eval_canary.summary_fragment(preflight=preflight),
            "Fix the failing checks (or adjust `promotion_eval_config.json`) and retry preview/apply.",
        ]
        return "\n".join([x for x in lines if x])

    def _format_rollback_preview_message(self, preview: dict) -> str:
        lines = [
            "Verra Promotion Pipeline intercepted a rollback request.",
            f"Rollback preview status: {preview.get('status', 'unknown')}",
        ]
        if preview.get("rollback_dir"):
            lines.append(f"Rollback dir: {preview.get('rollback_dir')}")
        if preview.get("target_root"):
            lines.append(f"Target root: {preview.get('target_root')}")
        if preview.get("file_count") is not None:
            lines.append(f"Rollback files: {preview.get('file_count')}")
        if preview.get("files"):
            lines.append("Files: " + ", ".join([str(f) for f in (preview.get("files") or [])[:10]]))
        lines.append(
            "Apply rollback by calling with `verra_promote_rollback_dir`, `verra_promote_rollback_apply: true`, and `verra_live_approved: true`."
        )
        return "\n".join(lines)

    def _format_rollback_apply_message(self, preview: dict, apply_res: dict) -> str:
        lines = [
            "Verra Promotion Pipeline executed a rollback restore.",
            f"Result: {apply_res.get('status', 'unknown')}",
            f"Rollback dir: {apply_res.get('rollback_dir', preview.get('rollback_dir', 'n/a'))}",
            f"Target root: {apply_res.get('target_root', preview.get('target_root', 'n/a'))}",
            f"Copied: {apply_res.get('copied_count', 0)} Failed: {apply_res.get('failed_count', 0)}",
        ]
        if apply_res.get("before_backup_dir"):
            lines.append(f"Pre-rollback backup: {apply_res.get('before_backup_dir')}")
        lines.append("No underlying tool was executed; Verra handled the rollback request directly.")
        return "\n".join(lines)

    def _now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _to_bool(self, v) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
