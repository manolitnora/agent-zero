from pathlib import Path

from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController
from python.helpers import verra_policy, verra_economics, verra_skills, verra_payments, verra_tool_sim


class VerraTrustBootstrap(Extension):
    async def execute(self, **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        data_dir = runtime.data_dir
        policy_path = Path(data_dir) / "policy.json"
        ledger_path = Path(data_dir) / "economic_ledger.json"
        payments_path = Path(data_dir) / "payments.json"
        payment_receipts_path = Path(data_dir) / "economic_receipts.json"
        tool_sim_receipts_path = Path(data_dir) / "tool_simulation_receipts.json"
        registry_path = Path(data_dir) / "skill_registry.json"

        policy = verra_policy.load_policy(policy_path)
        verra_policy.save_policy(policy_path, policy)

        ledger = verra_economics.load_ledger(ledger_path)
        verra_economics.set_limit_from_policy(ledger, policy)
        verra_economics.save_ledger(ledger_path, ledger)

        payments_cfg = verra_payments.load_payments_config(payments_path)
        verra_payments.save_payments_config(payments_path, payments_cfg)

        receipts_doc = verra_payments.load_receipts(payment_receipts_path)
        verra_payments.save_receipts(payment_receipts_path, receipts_doc)

        tool_sim_doc = verra_tool_sim.load_receipts(tool_sim_receipts_path)
        verra_tool_sim.save_receipts(tool_sim_receipts_path, tool_sim_doc)

        registry = verra_skills.load_registry(registry_path)
        registry = verra_skills.merge_scan_into_registry(registry, verra_skills.scan_skills(Path(__file__).resolve().parents[3]))
        verra_skills.save_registry(registry_path, registry)
