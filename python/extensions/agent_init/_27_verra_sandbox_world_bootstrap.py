from pathlib import Path

from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController
from python.helpers import (
    verra_sandbox,
    verra_world_model,
    verra_sandbox_exec,
    verra_promotion,
    verra_eval_canary,
    verra_self_update,
    verra_receipt_chain,
)


class VerraSandboxWorldBootstrap(Extension):
    async def execute(self, **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        data_dir = Path(runtime.data_dir)
        sandbox_cfg_path = data_dir / "sandbox_config.json"
        sandbox_state_path = data_dir / "sandbox_state.json"
        world_model_path = data_dir / "world_model.json"
        sandbox_exec_receipts_path = data_dir / "sandbox_execution_receipts.json"
        promotion_cfg_path = data_dir / "promotion_config.json"
        promotion_eval_cfg_path = data_dir / "promotion_eval_config.json"
        promotion_receipts_path = data_dir / "promotion_receipts.json"
        self_update_state_path = data_dir / "self_update_state.json"
        receipt_chain_cfg_path = data_dir / "receipt_chain_config.json"

        kernel_root = Path(runtime.kernel_cwd).resolve()
        agent_zero_root = Path(__file__).resolve().parents[3]

        cfg = verra_sandbox.load_config(sandbox_cfg_path)
        if not cfg.get("source_roots"):
            cfg["source_roots"] = [str(kernel_root), str(agent_zero_root)]
        verra_sandbox.save_config(sandbox_cfg_path, cfg)

        state = verra_sandbox.load_state(sandbox_state_path)
        manifest = verra_sandbox.ensure_session_sandbox(
            data_dir,
            session_id=VerraController._session_id(self.agent),
            source_roots=list(cfg.get("source_roots", [])),
            config=cfg,
        )
        state["max_sessions"] = int(cfg.get("max_sessions", 20) or 20)
        verra_sandbox.update_state_with_session(state, manifest)
        verra_sandbox.save_state(sandbox_state_path, state)

        sandbox_exec_doc = verra_sandbox_exec.load_receipts(sandbox_exec_receipts_path)
        verra_sandbox_exec.save_receipts(sandbox_exec_receipts_path, sandbox_exec_doc)

        promotion_cfg = verra_promotion.load_config(promotion_cfg_path)
        if not promotion_cfg.get("allowed_target_roots"):
            promotion_cfg["allowed_target_roots"] = [str(kernel_root), str(agent_zero_root)]
        verra_promotion.save_config(promotion_cfg_path, promotion_cfg)
        promotion_eval_cfg = verra_eval_canary.load_config(promotion_eval_cfg_path)
        verra_eval_canary.save_config(promotion_eval_cfg_path, promotion_eval_cfg)
        promotion_doc = verra_promotion.load_receipts(promotion_receipts_path)
        verra_promotion.save_receipts(promotion_receipts_path, promotion_doc)

        self_update_state = verra_self_update.load_state(self_update_state_path)
        verra_self_update.save_state(self_update_state_path, self_update_state)
        verra_receipt_chain.ensure_runtime_material(data_dir)
        # ensure config file exists even if key material is provided by env
        if not receipt_chain_cfg_path.exists():
            verra_receipt_chain.save_config(receipt_chain_cfg_path, verra_receipt_chain.default_config())

        # Build initial world model if missing. Ongoing refresh handled by message loop tick.
        if not world_model_path.exists():
            model = verra_world_model.build_world_model(
                verra_kernel_root=kernel_root,
                agent_zero_root=agent_zero_root,
                data_dir=data_dir,
                max_files=500,
            )
            verra_world_model.save_world_model(world_model_path, model)
