from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController


class VerraDreamPrompt(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.inject_dream_prompt(self.agent, loop_data)

