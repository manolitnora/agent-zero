from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController


class VerraCaptureStream(Extension):
    async def execute(self, stream_data: dict | None = None, **kwargs):
        VerraController.capture_response_stream(self.agent, stream_data)

