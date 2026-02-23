from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController


class VerraControlLoop(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.update_after_loop(self.agent, loop_data)

