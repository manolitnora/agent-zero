import os

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_vault


class VerraVaultRecall(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        try:
            query = ""
            if loop_data.user_message:
                query = loop_data.user_message.output_text()
            if not query:
                query = self.agent.history.output_text()[-1200:]

            compartment = VerraController._persona_mode(self.agent, loop_data)  # pragmatic reuse
            vault = verra_vault.load_vault(runtime.data_dir / "vault.json")

            results = verra_vault.query_vault(
                vault,
                query=query,
                compartment=compartment,
                max_preferences=int(os.getenv("VERRA_VAULT_RECALL_PREFS", "6")),
                max_facts=int(os.getenv("VERRA_VAULT_RECALL_FACTS", "8")),
                max_procedures=int(os.getenv("VERRA_VAULT_RECALL_PROCS", "4")),
                max_episodes=int(os.getenv("VERRA_VAULT_RECALL_EPISODES", "4")),
                include_general_fallback=os.getenv("VERRA_VAULT_RECALL_GENERAL", "1") != "0",
            )

            # Skip injection if nothing useful found.
            if not any(results.values()):
                return

            frag = verra_vault.vault_prompt_fragment(
                results,
                compartment=compartment,
                include_scores=os.getenv("VERRA_VAULT_RECALL_DEBUG", "0") == "1",
            )
            loop_data.extras_persistent["verra_vault_recall"] = frag
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra vault recall failed: {e}")
            except Exception:
                pass

