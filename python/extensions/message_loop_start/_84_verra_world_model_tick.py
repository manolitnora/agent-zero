from pathlib import Path

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_world_model, verra_sandbox, verra_self_update


class VerraWorldModelTick(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        try:
            data_dir = Path(runtime.data_dir)
            world_model_path = data_dir / "world_model.json"
            sandbox_cfg_path = data_dir / "sandbox_config.json"
            sandbox_state_path = data_dir / "sandbox_state.json"
            cfg = verra_sandbox.load_config(sandbox_cfg_path)
            if not cfg.get("source_roots"):
                cfg["source_roots"] = [str(Path(runtime.kernel_cwd).resolve()), str(Path(__file__).resolve().parents[3])]
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

            self_update_state = verra_self_update.load_state(data_dir / "self_update_state.json")
            adaptive = self_update_state.get("adaptive") or {}
            refresh_interval_sec = int(adaptive.get("world_model_refresh_interval_sec", 300) or 300)

            model = verra_world_model.maybe_refresh_world_model(
                world_model_path,
                verra_kernel_root=Path(runtime.kernel_cwd).resolve(),
                agent_zero_root=Path(__file__).resolve().parents[3],
                data_dir=data_dir,
                min_interval_sec=refresh_interval_sec,
                max_files=700,
                force=False,
            )
            self.agent.set_data("_verra_world_model", model)
            self.agent.set_data("_verra_sandbox_manifest", manifest)
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra world model tick failed: {e}")
            except Exception:
                pass
