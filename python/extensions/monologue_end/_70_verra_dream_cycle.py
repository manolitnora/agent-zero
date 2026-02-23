from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController


class VerraDreamCycle(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.run_dream_cycle(self.agent)

