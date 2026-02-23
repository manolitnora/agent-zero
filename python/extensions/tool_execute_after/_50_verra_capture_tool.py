from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController


class VerraCaptureTool(Extension):
    async def execute(self, response=None, tool_name: str = "", **kwargs):
        VerraController.capture_tool_result(self.agent, tool_name, response)

