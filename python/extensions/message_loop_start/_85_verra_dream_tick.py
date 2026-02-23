from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController


class VerraDreamTick(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        # Scheduled dream-cycle pass (time/receipt gated) for mirror consolidation.
        VerraController.run_dream_cycle(self.agent, force=False, reason="message_loop_start")

