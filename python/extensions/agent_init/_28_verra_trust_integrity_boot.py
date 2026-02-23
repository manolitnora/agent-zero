from pathlib import Path

from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController
from python.helpers import verra_trust_integrity


class VerraTrustIntegrityBoot(Extension):
    async def execute(self, **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        try:
            data_dir = Path(runtime.data_dir)
            cfg_path = data_dir / "trust_integrity_config.json"
            state_path = data_dir / "trust_integrity_state.json"
            cfg = verra_trust_integrity.load_config(cfg_path)
            verra_trust_integrity.save_config(cfg_path, cfg)

            if bool(cfg.get("verify_on_boot", True)):
                state = verra_trust_integrity.verify_runtime(data_dir, config=cfg)
            else:
                state = verra_trust_integrity.load_state(state_path)
            verra_trust_integrity.save_state(state_path, state)

            self.agent.set_data(VerraController.TRUST_INTEGRITY_KEY, state)
            if bool(state.get("quarantine_active", False)):
                VerraController._log(
                    self.agent,
                    "warning",
                    f"Verra trust integrity quarantine active: {state.get('quarantine_reason', 'verification failure')}",
                )
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra trust integrity boot failed: {e}")
            except Exception:
                pass

