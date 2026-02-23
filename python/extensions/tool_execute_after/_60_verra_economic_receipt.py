from pathlib import Path
from datetime import datetime, timezone

from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController
from python.helpers import verra_economics, verra_payments


class VerraEconomicReceipt(Extension):
    async def execute(self, response=None, tool_name: str = "", **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        pending = self.agent.get_data("_verra_pending_tool_decision") or {}
        decision = pending.get("decision") or {}
        if not decision:
            return

        if not (decision.get("economic") or float(decision.get("estimated_cost_usd", 0)) > 0):
            return

        ledger_path = Path(runtime.data_dir) / "economic_ledger.json"
        payments_path = Path(runtime.data_dir) / "payments.json"
        payment_receipts_path = Path(runtime.data_dir) / "economic_receipts.json"
        ledger = verra_economics.load_ledger(ledger_path)
        payments_cfg = verra_payments.load_payments_config(payments_path)
        payment_receipts = verra_payments.load_receipts(payment_receipts_path)

        flags = pending.get("flags") or {}
        tool_args_clean = pending.get("tool_args_clean") or {}
        payment_quote = decision.get("payment_quote") or {}
        payment_exec = None
        effective_tool_name = tool_name or str(pending.get("tool_name", ""))
        if decision.get("economic"):
            payment_exec = verra_payments.execute(
                payments_cfg,
                tool_name=effective_tool_name,
                tool_args=tool_args_clean if isinstance(tool_args_clean, dict) else {},
                quote_result=payment_quote if isinstance(payment_quote, dict) else {},
                live_approved=bool(flags.get("live_approved", False) or not decision.get("require_live_approval", False)),
            )
            vera_receipt = {
                "at": datetime.now(timezone.utc).isoformat(),
                "type": "payment_execution",
                "session_id": VerraController._session_id(self.agent),
                "tool_name": effective_tool_name,
                "tool_success": response is not None,
                **payment_exec,
            }
            verra_payments.append_receipt(payment_receipts, vera_receipt)
            verra_payments.save_receipts(payment_receipts_path, payment_receipts)

        bill_amount = float(decision.get("estimated_cost_usd", 0.0) or 0.0)
        if isinstance(payment_exec, dict):
            bill_amount = float(payment_exec.get("amount", bill_amount) or bill_amount or 0.0)
            if not str(payment_exec.get("status", "")).startswith("executed"):
                bill_amount = 0.0

        verra_economics.record_live_action(
            ledger,
            at=datetime.now(timezone.utc).isoformat(),
            session_id=VerraController._session_id(self.agent),
            tool_name=effective_tool_name,
            estimated_cost_usd=bill_amount,
            risk_level=str(decision.get("risk_level", "unknown")),
            success=response is not None,
            message_preview=(
                f"{str(getattr(response, 'message', ''))} | "
                f"payment={payment_exec.get('status') if isinstance(payment_exec, dict) else 'n/a'}"
            ),
        )
        verra_economics.save_ledger(ledger_path, ledger)
        self.agent.set_data("_verra_pending_tool_decision", {})
