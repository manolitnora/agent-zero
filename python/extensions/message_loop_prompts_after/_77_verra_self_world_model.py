from pathlib import Path

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_world_model, verra_sandbox, verra_sandbox_exec, verra_promotion


class VerraSelfWorldModel(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        try:
            data_dir = Path(runtime.data_dir)
            world_model = self.agent.get_data("_verra_world_model")
            if not world_model:
                world_model = verra_world_model.load_world_model(data_dir / "world_model.json")
                self.agent.set_data("_verra_world_model", world_model)
            sandbox_manifest = self.agent.get_data("_verra_sandbox_manifest")
            if not sandbox_manifest:
                cfg = verra_sandbox.load_config(data_dir / "sandbox_config.json")
                sandbox_manifest = verra_sandbox.ensure_session_sandbox(
                    data_dir,
                    session_id=VerraController._session_id(self.agent),
                    source_roots=list(cfg.get("source_roots", [])),
                    config=cfg,
                )
                self.agent.set_data("_verra_sandbox_manifest", sandbox_manifest)

            mode = VerraController._persona_mode(self.agent, loop_data)
            frag_world = verra_world_model.world_model_prompt_fragment(
                world_model,
                persona_mode=mode,
                sandbox_manifest=sandbox_manifest,
            )
            sandbox_cfg = verra_sandbox.load_config(data_dir / "sandbox_config.json")
            sandbox_state = verra_sandbox.load_state(data_dir / "sandbox_state.json")
            promotion_cfg = verra_promotion.load_config(data_dir / "promotion_config.json")
            frag_sandbox = verra_sandbox.sandbox_prompt_fragment(
                config=sandbox_cfg,
                state=sandbox_state,
                session_id=VerraController._session_id(self.agent),
                world_model=world_model,
            )
            loop_data.extras_persistent["verra_self_world_model"] = frag_world
            loop_data.extras_persistent["verra_sandbox"] = frag_sandbox
            loop_data.extras_persistent["verra_sandbox_exec"] = verra_sandbox_exec.sandbox_exec_prompt_fragment(
                sandbox_manifest
            )
            loop_data.extras_persistent["verra_promotion"] = verra_promotion.promotion_prompt_fragment(
                promotion_cfg
            )
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra self/world prompt injection failed: {e}")
            except Exception:
                pass
