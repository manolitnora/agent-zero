from pathlib import Path

from python.helpers.extension import Extension
from python.helpers.verra_control import VerraController
from python.helpers import verra_sandbox, verra_sandbox_exec


class VerraSandboxExecAdapter(Extension):
    async def execute(self, tool_args: dict | None = None, tool_name: str = "", **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return
        args = tool_args if isinstance(tool_args, dict) else {}

        flags = verra_sandbox_exec.consume_sandbox_flags(args)
        if not verra_sandbox_exec.should_route_to_sandbox(tool_name, args, flags):
            return

        data_dir = Path(runtime.data_dir)
        cfg_path = data_dir / "sandbox_config.json"
        state_path = data_dir / "sandbox_state.json"
        cfg = verra_sandbox.load_config(cfg_path)
        if not cfg.get("source_roots"):
            cfg["source_roots"] = [str(Path(runtime.kernel_cwd).resolve()), str(Path(__file__).resolve().parents[3])]
            verra_sandbox.save_config(cfg_path, cfg)
        state = verra_sandbox.load_state(state_path)
        manifest = verra_sandbox.ensure_session_sandbox(
            data_dir,
            session_id=VerraController._session_id(self.agent),
            source_roots=list(cfg.get("source_roots", [])),
            config=cfg,
        )
        state["max_sessions"] = int(cfg.get("max_sessions", 20) or 20)
        verra_sandbox.update_state_with_session(state, manifest)
        verra_sandbox.save_state(state_path, state)

        runtime_name = str(args.get("runtime", "")).lower().strip()
        sandbox_cwd = verra_sandbox_exec.choose_cwd(manifest, flags)
        args["cwd"] = sandbox_cwd
        before = verra_sandbox_exec.pre_exec_snapshot(
            manifest,
            tool_name=tool_name,
            runtime=runtime_name,
            tool_args=args,
        )
        self.agent.set_data(
            "_verra_pending_sandbox_exec",
            {
                "tool_name": tool_name,
                "runtime": runtime_name,
                "sandbox_manifest": manifest,
                "before": before,
                "sandbox_cwd": sandbox_cwd,
                "command_preview": str(args.get("code", ""))[:500],
            },
        )
