from pathlib import Path

from python.helpers.extension import Extension
from agent import LoopData
from python.helpers.verra_control import VerraController
from python.helpers import verra_skills


class VerraSkillRegistry(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        try:
            repo_root = Path(__file__).resolve().parents[3]
            reg_path = Path(runtime.data_dir) / "skill_registry.json"
            reg = verra_skills.load_registry(reg_path)
            reg = verra_skills.merge_scan_into_registry(reg, verra_skills.scan_skills(repo_root))
            verra_skills.save_registry(reg_path, reg)
            loop_data.extras_persistent["verra_skill_registry"] = verra_skills.skill_prompt_fragment(reg)
        except Exception as e:
            try:
                self.agent.context.log.log(type="warning", content=f"Verra skill registry injection failed: {e}")
            except Exception:
                pass

