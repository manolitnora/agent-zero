from pathlib import Path
from datetime import datetime, timezone

from python.helpers.extension import Extension
from python.helpers.errors import RepairableException
from python.helpers.verra_control import VerraController
from python.helpers import verra_policy, verra_economics, verra_payments, verra_tool_sim, verra_sandbox


class VerraPolicyGate(Extension):
    async def execute(self, tool_args: dict | None = None, tool_name: str = "", **kwargs):
        VerraController.bootstrap(self.agent)
        runtime = self.agent.get_data(VerraController.DATA_KEY)
        if not runtime or not getattr(runtime, "enabled", False):
            return

        args = tool_args if isinstance(tool_args, dict) else {}
        policy_path = Path(runtime.data_dir) / "policy.json"
        ledger_path = Path(runtime.data_dir) / "economic_ledger.json"
        payments_path = Path(runtime.data_dir) / "payments.json"
        payment_receipts_path = Path(runtime.data_dir) / "economic_receipts.json"
        tool_sim_receipts_path = Path(runtime.data_dir) / "tool_simulation_receipts.json"
        sandbox_cfg_path = Path(runtime.data_dir) / "sandbox_config.json"
        sandbox_state_path = Path(runtime.data_dir) / "sandbox_state.json"
        policy = verra_policy.load_policy(policy_path)
        ledger = verra_economics.load_ledger(ledger_path)
        payments_cfg = verra_payments.load_payments_config(payments_path)
        payment_receipts = verra_payments.load_receipts(payment_receipts_path)
        tool_sim_receipts = verra_tool_sim.load_receipts(tool_sim_receipts_path)
        verra_economics.set_limit_from_policy(ledger, policy)
        budget = verra_economics.budget_summary(ledger)

        session = VerraController._load_session(self.agent)
        hints = session.get("last_policy_hints") or {}

        flags = verra_policy.parse_verra_tool_flags(args)
        klass = verra_policy.classify_tool(policy, tool_name, args)
        est_cost = verra_policy.estimate_action_cost_usd(tool_name, args) if klass.get("economic") else 0.0
        payment_quote = None
        payment_simulation = None
        tool_preview = None
        if klass.get("economic"):
            payment_quote = verra_payments.quote(
                payments_cfg,
                tool_name=tool_name,
                tool_args=args,
                fallback_estimate_usd=est_cost,
            )
            est_cost = float(payment_quote.get("estimated_cost_usd", est_cost) or est_cost or 0.0)
        decision = verra_policy.evaluate_tool_call(
            policy,
            klass,
            autonomy_mode=str(policy.get("autonomy_mode", "supervised")),
            verra_policy_hints=hints,
            flags=flags,
            estimated_cost_usd=est_cost,
            budget_remaining_usd=budget.get("remaining_usd"),
        )
        if payment_quote:
            decision["payment_quote"] = payment_quote
            q_status = str(payment_quote.get("status", ""))
            if q_status in (
                "disabled",
                "unknown_adapter",
                "simulation_not_supported",
                "execution_not_supported",
                "merchant_blocked",
                "unconfigured_adapter",
            ):
                decision["blocked"] = True
                decision["allowed"] = False
                decision["reason"] = f"payment adapter unavailable ({q_status})"
            elif q_status == "over_max_quote":
                decision["blocked"] = True
                decision["allowed"] = False
                decision["reason"] = "payment quote exceeds adapter max quote limit"

        # Store decision for post-tool receipts / UX.
        self.agent.set_data(
            "_verra_pending_tool_decision",
            {
                "tool_name": tool_name,
                "args_preview": str(args)[:1000],
                "tool_args_clean": dict(args),
                "flags": flags,
                "decision": decision,
            },
        )

        if decision["blocked"]:
            # Simulation previews are synthetic: we record them and skip actual execution.
            if flags.get("simulate"):
                sandbox_cfg = verra_sandbox.load_config(sandbox_cfg_path)
                sandbox_state = verra_sandbox.load_state(sandbox_state_path)
                sandbox_manifest = verra_sandbox.ensure_session_sandbox(
                    Path(runtime.data_dir),
                    session_id=VerraController._session_id(self.agent),
                    source_roots=list(sandbox_cfg.get("source_roots", [])),
                    config=sandbox_cfg,
                )
                sandbox_state["max_sessions"] = int(sandbox_cfg.get("max_sessions", 20) or 20)
                verra_sandbox.update_state_with_session(sandbox_state, sandbox_manifest)
                verra_sandbox.save_state(sandbox_state_path, sandbox_state)
                sandbox_paths = sandbox_manifest.get("paths", {}) or {}
                sim_cwd = str(
                    Path(sandbox_paths.get("mounts") or sandbox_paths.get("current") or Path.cwd())
                )
                tool_preview = verra_tool_sim.simulate_tool_preview(
                    tool_name,
                    args,
                    default_cwd=sim_cwd,
                )
                tool_preview["sandbox_cwd"] = sim_cwd
                tool_preview["sandbox_current"] = str(sandbox_paths.get("current", ""))
                tool_sim_receipt = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "session_id": VerraController._session_id(self.agent),
                    "tool_name": tool_name,
                    **(tool_preview or {}),
                }
                verra_tool_sim.append_receipt(tool_sim_receipts, tool_sim_receipt)
                verra_tool_sim.save_receipts(tool_sim_receipts_path, tool_sim_receipts)
                if payment_quote and klass.get("economic"):
                    payment_simulation = verra_payments.simulate(
                        payments_cfg,
                        tool_name=tool_name,
                        tool_args=args,
                        quote_result=payment_quote,
                    )
                    sim_receipt = {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "type": "payment_simulation_preview",
                        "session_id": VerraController._session_id(self.agent),
                        "tool_name": tool_name,
                        **payment_simulation,
                    }
                    verra_payments.append_receipt(payment_receipts, sim_receipt)
                    verra_payments.save_receipts(payment_receipts_path, payment_receipts)
                verra_economics.record_preview(
                    ledger,
                    at=datetime.now(timezone.utc).isoformat(),
                    session_id=VerraController._session_id(self.agent),
                    tool_name=tool_name,
                    estimated_cost_usd=est_cost,
                    risk_level=decision["risk_level"],
                    reason=decision["reason"],
                )
                verra_economics.save_ledger(ledger_path, ledger)

            pending = self.agent.get_data("_verra_pending_tool_decision") or {}
            if tool_preview is not None:
                pending["tool_preview"] = tool_preview
                self.agent.set_data("_verra_pending_tool_decision", pending)

            msg = self._build_llm_message(
                tool_name,
                decision,
                flags,
                budget,
                payment_quote,
                payment_simulation,
                tool_preview,
            )
            raise RepairableException(msg)

    def _build_llm_message(
        self,
        tool_name: str,
        decision: dict,
        flags: dict,
        budget: dict,
        payment_quote: dict | None = None,
        payment_simulation: dict | None = None,
        tool_preview: dict | None = None,
    ) -> str:
        lines = [
            f"Verra Policy Gate blocked tool '{tool_name}'.",
            f"Reason: {decision['reason']}.",
            f"Risk: {decision['risk_level']} ({decision['risk_class']}).",
            f"Budget remaining: ${budget.get('remaining_usd', 0):.2f}.",
        ]
        if payment_quote:
            lines.append(
                "Payment quote: "
                f"{payment_quote.get('status', 'unknown')} "
                f"{payment_quote.get('merchant', 'unknown')} "
                f"${float(payment_quote.get('estimated_cost_usd', 0.0) or 0.0):.2f} "
                f"{payment_quote.get('currency', 'USD')}"
            )
        if payment_simulation:
            lines.append(
                "Payment preview: "
                f"{payment_simulation.get('status', 'unknown')} "
                f"id={payment_simulation.get('simulation_id', 'n/a')}"
            )
        if tool_preview:
            lines.append(
                "Tool simulation: "
                f"{tool_preview.get('status', 'unknown')} ({tool_preview.get('kind', 'tool')}) - "
                f"{tool_preview.get('summary', '')}"
            )
            if tool_preview.get("sandbox_cwd"):
                lines.append(f"Sandbox preview cwd: {tool_preview.get('sandbox_cwd')}")
            if tool_preview.get("diff"):
                lines.append("Tool diff preview:\n" + str(tool_preview.get("diff", ""))[:2000])
            elif tool_preview.get("git_diff_stat"):
                lines.append("Git diff stat preview:\n" + str(tool_preview.get("git_diff_stat", ""))[:1000])
            elif tool_preview.get("stdout"):
                lines.append("Tool preview output:\n" + str(tool_preview.get("stdout", ""))[:1200])
            elif tool_preview.get("stderr"):
                lines.append("Tool preview stderr:\n" + str(tool_preview.get("stderr", ""))[:800])
        if decision.get("requires_simulation"):
            lines.append(
                "Use a preview first: call the same tool with tool_args including `verra_simulate: true`."
            )
        if decision.get("require_live_approval"):
            lines.append(
                "After review, call it again with `verra_live_approved: true` to request live execution."
            )
        lines.append(
            "Verra runtime strips these `verra_*` flags before tool execution, so they are safe to include."
        )
        return "\n".join(lines)
