from pathlib import Path
from datetime import datetime, timezone

from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController
from python.helpers import verra_sandbox_exec


class VerraSandboxExecReceipt(Extension):
    async def execute(self, response=None, tool_name: str = "", **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        pending = self.agent.get_data("_verra_pending_sandbox_exec") or {}
        if not pending:
            return
        if str(pending.get("tool_name", "")) != str(tool_name):
            return

        manifest = pending.get("sandbox_manifest") or {}
        if not manifest:
            return

        data_dir = Path(runtime.data_dir)
        receipts_path = data_dir / "sandbox_execution_receipts.json"
        receipts = verra_sandbox_exec.load_receipts(receipts_path)
        before = pending.get("before") or {}
        after = verra_sandbox_exec.post_exec_snapshot(manifest, before=before)
        receipt = {
            "at": datetime.now(timezone.utc).isoformat(),
            "type": "sandbox_exec",
            "session_id": VerraController._session_id(self.agent),
            "tool_name": tool_name,
            "runtime": pending.get("runtime"),
            "sandbox_cwd": pending.get("sandbox_cwd"),
            "command_preview": pending.get("command_preview"),
            "response_ok": response is not None,
            "response_message_preview": str(getattr(response, "message", ""))[:800],
            "before": before,
            "after": after,
        }
        verra_sandbox_exec.append_receipt(receipts, receipt)
        verra_sandbox_exec.save_receipts(receipts_path, receipts)
        self.agent.set_data("_verra_pending_sandbox_exec", {})
