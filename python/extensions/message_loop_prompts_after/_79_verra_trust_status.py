from pathlib import Path

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_policy, verra_economics, verra_trust_integrity


class VerraTrustStatus(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        try:
            session = VerraController._load_session(self.agent)
            policy = verra_policy.load_policy(Path(runtime.data_dir) / "policy.json")
            ledger = verra_economics.load_ledger(Path(runtime.data_dir) / "economic_ledger.json")
            verra_economics.set_limit_from_policy(ledger, policy)
            budget = verra_economics.budget_summary(ledger)
            frag = verra_policy.build_trust_status_fragment(
                autonomy_mode=str(policy.get("autonomy_mode", "supervised")),
                last_verra_hints=session.get("last_policy_hints"),
                budget_summary=budget,
                policy=policy,
            )
            integrity = self.agent.get_data(VerraController.TRUST_INTEGRITY_KEY)
            if not integrity:
                integrity = verra_trust_integrity.load_state(Path(runtime.data_dir) / "trust_integrity_state.json")
                self.agent.set_data(VerraController.TRUST_INTEGRITY_KEY, integrity)
            frag = frag + "\n" + verra_trust_integrity.build_prompt_fragment(integrity)
            loop_data.extras_persistent["verra_trust_status"] = frag
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra trust status failed: {e}")
            except Exception:
                pass
