from pathlib import Path

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_self_update


class VerraSelfUpdateLoop(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        try:
            result = verra_self_update.run_learning_cycle(Path(runtime.data_dir))
            self.agent.set_data("_verra_self_update_state", result.get("state"))
            self.agent.set_data("_verra_self_update_cycle", result.get("cycle"))
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra self-update loop failed: {e}")
            except Exception:
                pass
