from pathlib import Path

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_self_update


class VerraSelfUpdateStatus(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        try:
            state = self.agent.get_data("_verra_self_update_state")
            if not state:
                state = verra_self_update.load_state(Path(runtime.data_dir) / "self_update_state.json")
                self.agent.set_data("_verra_self_update_state", state)
            loop_data.extras_persistent["verra_self_update"] = verra_self_update.self_update_prompt_fragment(state)
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra self-update prompt injection failed: {e}")
            except Exception:
                pass
