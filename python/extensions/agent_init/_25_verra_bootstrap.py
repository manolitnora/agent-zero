from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController


class VerraBootstrap(Extension):
    async def execute(self, **kwargs):
        VerraController.bootstrap(self.agent)

