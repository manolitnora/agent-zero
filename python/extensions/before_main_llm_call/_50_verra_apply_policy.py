from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController


class VerraApplyPolicy(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        VerraController.apply_policy_before_llm(self.agent, loop_data)

