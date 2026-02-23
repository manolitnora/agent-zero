# Verra AgentZero Integration

This folder stores Verra runtime data for the Agent Zero integration:

- `sessions/*.json` : Verra kernel state per Agent Zero context/session
- `vault.json` : Verra Vault v1 (compartmented preferences/facts/procedures/episodes/receipts)
- `dream_profile.json` : Dream-cycle synthesis (mirror signature + convergence priors from the vault)
- `dream_cycle_state.json` : Dream scheduler state (last run time, receipt count, cadence state)
- `policy.json` : Verra trust policy (autonomy mode, risk classes, simulation-first rules)
- `economic_ledger.json` : Economic action ledger (preview/live receipts and budget accounting)
- `payments.json` : Payment adapter config (mock wallet now, real adapters later)
- `economic_receipts.json` : Payment quote/simulation/live execution receipts
- `tool_simulation_receipts.json` : Tool simulation previews (file diffs, shell/git preview outputs)
- `sandbox_config.json` : Verra sandbox policy/config (shadow workspace, source mounts, safety toggles)
- `sandbox_state.json` : Active sandbox sessions and paths
- `sandbox_execution_receipts.json` : Sandbox-routed code execution receipts (before/after artifact deltas)
- `world_model.json` : Verra self/world model snapshot (codebase map + spatial index + hotspots)
- `promotion_config.json` : Sandbox patch promotion policy (allowed target roots, limits)
- `promotion_eval_config.json` : Preflight eval + canary gate config for promotions
- `promotion_receipts.json` : Patch promotion previews/applies + rollback snapshot references
- `self_update_state.json` : Verra self-update learner state (verifiable reward metrics, adaptive knobs, cursors)
- `receipt_chain_config.json` : Tamper-evident receipt-chain config (hash/HMAC settings)
- `receipt_chain.key` : Local HMAC signing key for receipt/state chain signing (generated if no env key is set)
- `trust_integrity_config.json` : Startup integrity verifier config (receipt-chain verification + quarantine behavior)
- `trust_integrity_state.json` : Latest integrity verification results and quarantine status
- `skill_registry.json` : Skill trust registry (allowed/trust-tier/scopes)
- `memory.json` : lightweight preference + episodic memory learned from turns

Vault v1 notes:
- compartmented by mode (`general`, `personal`, `business`, `social`, `cognitive`)
- includes confidence, source authority, trust tier, and verification timestamps
- encryption is not enabled yet (metadata placeholder only)

Dream cycle notes:
- runs after monologue completion and after vault updates
- also runs on a scheduled tick at message-loop start (time/receipt gated)
- consolidates vault signals into mirror/convergence baselines by compartment
- injects a "dream prior" prompt fragment to bias style/convergence (not facts)

Scheduler env overrides:
- `VERRA_DREAM_INTERVAL_SEC` (default `900`)
- `VERRA_DREAM_MIN_NEW_RECEIPTS` (default `2`)

Vault direct recall notes:
- Agent Zero prompt assembly now injects a Verra vault recall block (compartment + trust-tier aware)
- recall is scored by query overlap + confidence + source authority + trust tier
- "general" compartment can be used as fallback for other compartments

Vault recall env overrides:
- `VERRA_VAULT_RECALL_PREFS` (default `6`)
- `VERRA_VAULT_RECALL_FACTS` (default `8`)
- `VERRA_VAULT_RECALL_PROCS` (default `4`)
- `VERRA_VAULT_RECALL_EPISODES` (default `4`)
- `VERRA_VAULT_RECALL_GENERAL` (`1`/`0`, default `1`)
- `VERRA_VAULT_RECALL_DEBUG` (`1` shows scores in prompt fragment)

Trust / policy layer:
- Tool calls are risk-classified (`low`, `medium`, `high`, `critical`)
- High-risk and economic actions are simulation-first by default
- Use tool args `verra_simulate: true` to request a Verra preview (execution is skipped)
- Use tool args `verra_live_approved: true` to request live execution after preview/review
- Verra strips `verra_*` control flags before tool execution

Economic adapters v1:
- Mock payment adapter provides `quote`, `simulate`, and `execute` receipt flow
- Coinbase/Stripe-style adapter scaffolds are included (`coinbase_agent_wallet`, `stripe_agent_commerce`) for future live connectors
- Merchant/domain allowlists are enforced at quote time (`merchant_allowlist` per adapter)
- Economic actions attach adapter quotes to policy previews and persist payment receipts
- Live economic billing is only booked in `economic_ledger.json` when payment execution status is `executed_*`

Tool-native simulation previews:
- `verra_simulate: true` now records tool simulation receipts and can return:
- file edit/write unified diffs (when path/content or find/replace style args are present)
- safe shell read-only command previews (`pwd`, `ls`, etc.)
- git repo previews (`git status`, `git diff`, and repo-state previews for write-like git commands)
- destructive/network-capable shell commands are not executed in simulation mode
- simulation previews now run against a Verra session sandbox path (shadow workspace/mounts)

Sandbox + self/world model:
- Verra creates a per-session sandbox under `sandboxes/<session>/current`
- sandbox contains `scratch`, `patches`, `runs`, `knowledge`, and `mounts` directories
- mounted source roots give Verra a safe “lab view” of its own codebases for experimentation
- `world_model.json` gives Verra self-knowledge + codebase knowledge (components, hotspots, import graph, spatial path coordinates)
- Agent Zero prompt assembly injects self/world-model and sandbox context so Verra can plan changes in codebase “zones”

Sandbox execution adapter:
- `code_execution` tool calls are routed to the session sandbox `cwd` by default (unless `verra_no_sandbox: true`)
- use `verra_sandbox_subdir` to isolate experiments under sandbox `current/`
- Verra records before/after sandbox artifact deltas in `sandbox_execution_receipts.json`

Promotion pipeline (sandbox -> live):
- Create patch files in sandbox `patches/`
- Preview with `verra_promote_patch_path` (+ optional `verra_simulate: true`)
- Apply with `verra_promote_patch_path`, `verra_promote_target_root`, `verra_promote_live: true`, `verra_live_approved: true`
- Verra stores rollback snapshots before applying and logs receipts in `promotion_receipts.json`

Preflight eval + canary gate:
- Every promotion preview now runs a preflight eval (built-in `git apply --check` + optional configured commands)
- Live promotion can be blocked if preflight does not pass (`promotion_eval_config.json`)
- After live apply, Verra runs canary checks (built-in reverse patch check + optional commands)
- If canary fails and auto-rollback is enabled, Verra automatically restores from the rollback snapshot and logs receipts

Rollback command:
- Preview rollback with `verra_promote_rollback_dir` (and optional `verra_promote_rollback_target_root`)
- Apply rollback with `verra_promote_rollback_dir`, `verra_promote_rollback_apply: true`, `verra_live_approved: true`
- Verra creates a pre-rollback backup snapshot before restoring files

Self-update learner loop (bounded):
- Runs at message-loop end and learns from verifiable receipts only
- Sources: sandbox execution receipts, tool simulation receipts, promotion preflight/canary/apply/rollback receipts
- Updates safe runtime settings automatically (e.g. `promotion_config.require_simulation_first`, policy verification defaults, world-model refresh cadence)
- Does not self-modify model weights or core code automatically

Receipt chain (tamper-evident receipts):
- Verra chains hashes across receipt lists (`*_receipts.json`) and ledger `entries`
- `self_update_state.json` gets both chained `history` records and a top-level state hash/signature
- Per-record metadata is stored under `_chain`; document summaries are stored under `_receipt_chain`
- Signing uses HMAC (`VERRA_RECEIPT_CHAIN_KEY`) if provided, otherwise Verra generates a local `receipt_chain.key`

Startup trust integrity verifier:
- Runs at agent initialization and verifies chained receipts/state files (promotion, sandbox exec, tool sim, economic receipts/ledger, self-update state)
- Raises a Verra quarantine state on verification failures/tamper
- Quarantine blocks live risky/economic/destructive actions in the policy gate but still allows read-only analysis and simulation previews
- Legacy/uninitialized unchained files are warnings by default (configurable)

Autonomy modes (in `policy.json`):
- `assist`
- `supervised` (default)
- `bounded_auto`
- `full_auto`

Configuration source (default):
- `../wrapper/config/default.config.json` from the sibling Verra kernel wrapper

Environment overrides:
- `VERRA_WRAPPER_CONFIG`
- `VERRA_KERNEL_ROOT`
- `VERRA_AGENTZERO_DATA_DIR`
