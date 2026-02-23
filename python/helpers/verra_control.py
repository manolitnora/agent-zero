from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from python.helpers import verra_vault
from python.helpers import verra_dream


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        v = 0.0
    if math.isnan(v) or math.isinf(v):
        v = 0.0
    return _clamp(v, 0.0, 1.0)


def _tok(text: str) -> list[str]:
    out = []
    for raw in (text or "").lower().replace("\n", " ").split():
        t = "".join(ch for ch in raw if ch.isalnum())
        if t:
            out.append(t)
    return out


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _unique(vals: list[str]) -> list[str]:
    return list(dict.fromkeys(vals))


def _repo_root() -> Path:
    # .../agent-zero/python/helpers/verra_control.py -> agent-zero
    return Path(__file__).resolve().parents[2]


def _kernel_root() -> Path:
    env = os.getenv("VERRA_KERNEL_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root().parent


def _default_wrapper_config_path() -> Path:
    return _kernel_root() / "wrapper" / "config" / "default.config.json"


def _default_verra_data_dir() -> Path:
    return _repo_root() / "usr" / "verra"


@dataclass
class VerraRuntimeConfig:
    kernel_command: str
    kernel_args: list[str]
    kernel_cwd: Path
    kernel_config: dict[str, Any]
    initial_state: dict[str, Any]
    data_dir: Path
    enabled: bool = True


class VerraController:
    DATA_KEY = "_verra_runtime"
    SNAPSHOT_KEY = "_verra_snapshot"
    TOOL_LOG_KEY = "_verra_tool_log"
    STREAM_KEY = "_verra_last_stream_response"
    DREAM_PROFILE_KEY = "_verra_dream_profile"
    DREAM_SCHEDULER_KEY = "_verra_dream_scheduler"
    TRUST_INTEGRITY_KEY = "_verra_trust_integrity"

    @classmethod
    def bootstrap(cls, agent) -> None:
        if agent.get_data(cls.DATA_KEY):
            return

        try:
            cfg_path = Path(os.getenv("VERRA_WRAPPER_CONFIG", str(_default_wrapper_config_path())))
            wrapper_cfg = json.loads(cfg_path.read_text("utf-8"))
            kernel_cfg = wrapper_cfg["kernel"]

            data_dir = Path(
                os.getenv("VERRA_AGENTZERO_DATA_DIR", str(_default_verra_data_dir()))
            ).expanduser()
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "sessions").mkdir(parents=True, exist_ok=True)

            runtime = VerraRuntimeConfig(
                kernel_command=kernel_cfg["command"],
                kernel_args=list(kernel_cfg.get("args", [])),
                kernel_cwd=(cfg_path.parent / kernel_cfg.get("cwd", "..")).resolve(),
                kernel_config=kernel_cfg["config"],
                initial_state=kernel_cfg["initial_state"],
                data_dir=data_dir,
                enabled=True,
            )
        except Exception as e:
            runtime = VerraRuntimeConfig(
                kernel_command="",
                kernel_args=[],
                kernel_cwd=_repo_root(),
                kernel_config={},
                initial_state={},
                data_dir=_default_verra_data_dir(),
                enabled=False,
            )
            cls._log(agent, "warning", f"Verra disabled (config load failed): {e}")

        agent.set_data(cls.DATA_KEY, runtime)
        agent.set_data(cls.TOOL_LOG_KEY, [])

    @classmethod
    def apply_policy_before_llm(cls, agent, loop_data) -> None:
        runtime: VerraRuntimeConfig | None = agent.get_data(cls.DATA_KEY)
        if not runtime or not runtime.enabled:
            return

        session = cls._load_session(agent)
        hints = session.get("last_policy_hints") or {}

        kwargs = agent.config.chat_model.kwargs
        if not isinstance(kwargs, dict):
            kwargs = {}
            agent.config.chat_model.kwargs = kwargs

        # Apply Verra policy hints directly to LiteLLM call kwargs.
        if "llm_temperature" in hints:
            kwargs["temperature"] = float(hints["llm_temperature"])
        if "top_p" in hints:
            kwargs["top_p"] = float(hints["top_p"])

        loop_data.params_temporary["verra_policy_applied"] = {
            "temperature": kwargs.get("temperature"),
            "top_p": kwargs.get("top_p"),
            "mode": hints.get("mode", "stabilize"),
            "verification_required": bool(hints.get("verification_required", False)),
        }

    @classmethod
    def capture_response_stream(cls, agent, stream_data: dict[str, Any] | None) -> None:
        if not stream_data:
            return
        full = stream_data.get("full")
        if isinstance(full, str):
            agent.set_data(cls.STREAM_KEY, full)

    @classmethod
    def capture_tool_result(cls, agent, tool_name: str, response) -> None:
        buf = agent.get_data(cls.TOOL_LOG_KEY) or []
        item = {
            "at": _now_iso(),
            "tool_name": tool_name,
            "ok": bool(getattr(response, "break_loop", False) is not None),
            "break_loop": bool(getattr(response, "break_loop", False)),
            "message_preview": str(getattr(response, "message", ""))[:500],
        }
        buf.append(item)
        agent.set_data(cls.TOOL_LOG_KEY, buf[-20:])

    @classmethod
    def run_dream_cycle(cls, agent, force: bool = False, reason: str = "manual") -> None:
        runtime: VerraRuntimeConfig | None = agent.get_data(cls.DATA_KEY)
        if not runtime or not runtime.enabled:
            return
        try:
            vault_path = runtime.data_dir / "vault.json"
            dream_path = runtime.data_dir / "dream_profile.json"
            vault = verra_vault.load_vault(vault_path)
            scheduler = cls._load_dream_scheduler(agent, runtime)
            total_receipts = cls._count_vault_receipts(vault)
            if not force and not cls._dream_cycle_due(scheduler, total_receipts):
                return
            profile = verra_dream.synthesize_from_vault(vault)
            verra_dream.save_dream_profile(dream_path, profile)
            agent.set_data(cls.DREAM_PROFILE_KEY, profile)
            scheduler["last_run_at"] = _now_iso()
            scheduler["last_receipt_count"] = total_receipts
            scheduler["runs"] = int(scheduler.get("runs", 0)) + 1
            scheduler["last_reason"] = reason
            cls._save_dream_scheduler(agent, runtime, scheduler)
        except Exception as e:
            cls._log(agent, "warning", f"Verra dream cycle failed: {e}")

    @classmethod
    def inject_dream_prompt(cls, agent, loop_data) -> None:
        runtime: VerraRuntimeConfig | None = agent.get_data(cls.DATA_KEY)
        if not runtime or not runtime.enabled:
            return
        try:
            profile = agent.get_data(cls.DREAM_PROFILE_KEY)
            if not profile:
                dream_path = runtime.data_dir / "dream_profile.json"
                profile = verra_dream.load_dream_profile(dream_path)
                agent.set_data(cls.DREAM_PROFILE_KEY, profile)
            compartment = cls._persona_mode(agent, loop_data)
            frag = verra_dream.dream_prompt_fragment(profile, compartment)
            loop_data.extras_persistent["verra_dream_profile"] = frag
        except Exception as e:
            cls._log(agent, "warning", f"Verra dream prompt injection failed: {e}")

    @classmethod
    def update_after_loop(cls, agent, loop_data) -> None:
        runtime: VerraRuntimeConfig | None = agent.get_data(cls.DATA_KEY)
        if not runtime or not runtime.enabled:
            return

        # Only run Verra control when we have an assistant response in this iteration.
        response_text = (agent.get_data(cls.STREAM_KEY) or "").strip()
        if not response_text:
            response_text = str(getattr(loop_data, "last_response", "") or "").strip()
        if not response_text:
            return

        try:
            snapshot = cls._build_snapshot(agent, loop_data, response_text)
            agent.set_data(cls.SNAPSHOT_KEY, snapshot)

            session = cls._load_session(agent)
            kernel_req = cls._build_kernel_request(runtime, session, snapshot)
            kernel_resp = cls._call_kernel(runtime, kernel_req)

            session["kernel_state"] = kernel_resp["next_state"]
            session["last_policy_hints"] = kernel_resp.get("policy_hints")
            session["last_metrics"] = snapshot["metrics_for_session"]
            session.setdefault("receipts", []).append(
                {
                    "at": _now_iso(),
                    "kernel_diagnostics": kernel_resp.get("diagnostics"),
                    "policy_hints": kernel_resp.get("policy_hints"),
                    "snapshot_metrics": snapshot["metrics_for_session"],
                }
            )
            session["receipts"] = session["receipts"][-100:]
            cls._save_session(agent, session)

            cls._learn_memory(runtime, agent, snapshot, kernel_resp)
        except Exception as e:
            cls._log(agent, "warning", f"Verra loop update failed: {e}")
        finally:
            # Clear per-iteration transient buffers
            agent.set_data(cls.STREAM_KEY, None)
            agent.set_data(cls.TOOL_LOG_KEY, [])

    @classmethod
    def _session_id(cls, agent) -> str:
        try:
            return str(agent.context.id)
        except Exception:
            return "default"

    @classmethod
    def _session_path(cls, agent) -> Path:
        runtime: VerraRuntimeConfig = agent.get_data(cls.DATA_KEY)
        return runtime.data_dir / "sessions" / f"{cls._session_id(agent)}.json"

    @classmethod
    def _load_session(cls, agent) -> dict[str, Any]:
        runtime: VerraRuntimeConfig = agent.get_data(cls.DATA_KEY)
        path = cls._session_path(agent)
        if path.exists():
            return json.loads(path.read_text("utf-8"))
        return {
            "schema_version": "verra_agentzero_session.v1",
            "id": cls._session_id(agent),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "kernel_state": runtime.initial_state,
            "last_policy_hints": None,
            "last_metrics": None,
            "receipts": [],
        }

    @classmethod
    def _save_session(cls, agent, session: dict[str, Any]) -> None:
        path = cls._session_path(agent)
        session["updated_at"] = _now_iso()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2) + "\n", "utf-8")

    @classmethod
    def _persona_mode(cls, agent, loop_data) -> str:
        mode = "general"
        try:
            profile = (agent.config.profile or "").lower()
            if profile in ("business", "social", "personal", "cognitive", "general"):
                mode = profile
            elif "research" in profile:
                mode = "cognitive"
            elif "dev" in profile:
                mode = "business"
        except Exception:
            pass
        return mode

    @classmethod
    def _extract_user_text(cls, agent) -> str:
        try:
            last = agent.last_user_message
            if last:
                return last.output_text()
        except Exception:
            pass
        try:
            return agent.concat_messages([])  # fallback
        except Exception:
            return ""

    @classmethod
    def _build_snapshot(cls, agent, loop_data, answer_text: str) -> dict[str, Any]:
        user_text = cls._extract_user_text(agent)
        mode = cls._persona_mode(agent, loop_data)
        tool_log = agent.get_data(cls.TOOL_LOG_KEY) or []

        user_tokens = _tok(user_text)
        answer_tokens = _tok(answer_text)
        history_text = ""
        try:
            history_text = agent.history.output_text(human_label="user", ai_label="assistant")
        except Exception:
            history_text = ""
        prior_tail = history_text[-4000:]
        prior_tokens = set(_tok(prior_tail))

        unique_answer = _unique(answer_tokens)
        overlap_user = (
            len([t for t in _unique(user_tokens) if t in set(answer_tokens)])
            / max(len(_unique(user_tokens)), 1)
        )
        novelty = len([t for t in unique_answer if t not in prior_tokens]) / max(
            len(unique_answer), 1
        )
        repetition = cls._repetition_score(answer_tokens)
        uncertainty = cls._uncertainty_score(answer_text)
        progress = cls._progress_score(answer_text, tool_log)
        hypothesis_stability = cls._hypothesis_stability(answer_tokens, prior_tokens)

        verification = cls._verification_score(answer_text, mode, tool_log)
        dream_prior = cls._get_dream_prior(agent, mode)
        convergence = _clamp01(
            0.35 * overlap_user
            + 0.25 * progress
            + 0.20 * verification["evidence_coverage"]
            + 0.15 * hypothesis_stability
            + 0.10 * dream_prior["convergence_alignment"]
            - 0.15 * uncertainty
        )
        sigma_tension = _clamp01(
            0.45 * convergence
            + 0.20 * progress
            + 0.20 * (1.0 - verification["contradiction_rate"])
            + 0.10 * dream_prior["sigma_alignment"]
            + 0.15 * abs(0.5 - uncertainty) * 2.0
        )
        persistence_total = (
            _clamp01(0.4 * (len(unique_answer) / max(len(answer_tokens), 1)) + 0.3 * novelty + 0.3 * (1 - repetition))
            * 2.0
            + 0.1
        )

        # receipt / semantic mass proxies
        token_cost = max(0.1, len(answer_text) / 800.0)
        proof_strength = _clamp01(
            0.6 * verification["evidence_coverage"] + 0.4 * verification["claim_support_ratio"]
        )
        receipt = {
            "proof_strength": proof_strength,
            "compute_cost": token_cost,
            "authority_tier": 1 + verification["source_authority"] * 6,
            "delta_h": _clamp01(0.2 * convergence + max(0.0, 0.5 - uncertainty)),
            "receipt_flux": _clamp01(0.5 * progress + 0.5 * convergence),
        }

        formula_terms = {
            "l_pred": _clamp01(1 - overlap_user + 0.2 * uncertainty),
            "l_int": _clamp01(repetition + verification["contradiction_rate"] * 0.8),
            "l_count": _clamp01(len(answer_tokens) / 1200.0),
            "cost_sb": _clamp01(0.25 + 0.25 * len(tool_log)),
            "l_ethic": _clamp01(verification["hallucination_risk"] * 0.7),
            "phi_3d": _clamp01(0.5 * novelty + 0.5 * (1 - repetition)),
            "value_goal": _clamp01(0.6 * overlap_user + 0.4 * progress),
            "grad_h_pinch_norm": _clamp01(sigma_tension * (0.6 + 0.4 * convergence)),
            "entropy_rate": _clamp01(0.5 * uncertainty + 0.5 * repetition),
            "empowerment": _clamp01(
                0.35 * novelty
                + 0.25 * progress
                + 0.20 * (1 - verification["hallucination_risk"])
                + 0.20 * min(len(tool_log), 3) / 3.0
            ),
        }

        grad = [
            _clamp((repetition - novelty) + 0.15 * uncertainty, -1.0, 1.0),
            _clamp((1 - overlap_user) - 0.2 * progress, -1.0, 1.0),
            _clamp((1 - verification["evidence_coverage"]) + 0.2 * verification["hallucination_risk"], -1.0, 1.0),
            _clamp(
                verification["hallucination_risk"]
                + verification["contradiction_rate"]
                - 0.4 * verification["evidence_coverage"],
                -1.0,
                1.0,
            ),
        ]

        return {
            "user_text": user_text,
            "answer_text": answer_text,
            "persona_mode": mode,
            "topology": {
                "provided_persistence_total": persistence_total,
                "activation_window": None,
            },
            "grad_v_total_q": grad,
            "convergence": {
                "convergence_score": convergence,
                "sigma_tension": sigma_tension,
                "contradiction_rate": verification["contradiction_rate"],
                "progress_score": progress,
                "uncertainty_score": uncertainty,
                "hypothesis_stability": hypothesis_stability,
            },
            "verification": {
                "evidence_coverage": verification["evidence_coverage"],
                "claim_support_ratio": verification["claim_support_ratio"],
                "hallucination_risk": verification["hallucination_risk"],
                "tool_success_rate": verification["tool_success_rate"],
                "source_authority": verification["source_authority"],
                "source_count": verification["source_count"],
            },
            "receipt": receipt,
            "formula_terms": formula_terms,
            "dream_prior": dream_prior,
            "metrics_for_session": {
                "repetition_score": repetition,
                "novelty_score": novelty,
                "intent_alignment": overlap_user,
                "uncertainty_score": uncertainty,
                "progress_score": progress,
                "convergence_score": convergence,
                "sigma_tension": sigma_tension,
                "persistence_total": persistence_total,
                "hallucination_risk": verification["hallucination_risk"],
                "evidence_coverage": verification["evidence_coverage"],
                "dream_convergence_alignment": dream_prior["convergence_alignment"],
                "dream_mirror_alignment": dream_prior["mirror_alignment"],
            },
        }

    @classmethod
    def _verification_score(cls, answer_text: str, mode: str, tool_log: list[dict[str, Any]]):
        claims = cls._extract_claims(answer_text)
        citations = cls._extract_urls(answer_text)
        claim_count = max(len(claims), 1)
        mode_bias = 1.0 if mode in ("business", "cognitive") else 0.6 if mode == "social" else 0.8
        evidence_coverage = _clamp01((len(citations) / claim_count) * mode_bias + (0.15 if tool_log else 0.0))
        claim_support_ratio = _clamp01(0.8 * evidence_coverage + (0.2 if citations else 0.0))
        contradiction_markers = len(
            [
                1
                for m in ("however", "but", "yet", "on the other hand")
                if m in answer_text.lower()
            ]
        )
        contradiction_rate = _clamp01(contradiction_markers / max(6, len(claims) * 2))
        certainty_count = sum(
            answer_text.lower().count(w) for w in ["definitely", "certainly", "always", "never", "guaranteed"]
        )
        uncertainty_count = sum(
            answer_text.lower().count(w)
            for w in ["maybe", "might", "possibly", "uncertain", "not sure"]
        )
        hallucination_risk = _clamp01(
            (1 - evidence_coverage) * (0.55 + 0.15 * mode_bias)
            + (0.15 if certainty_count > uncertainty_count else 0.0)
            + contradiction_rate * 0.4
        )
        source_authority = _clamp01(0.7 + min(len(citations), 4) * 0.07 if citations else 0.3)
        return {
            "evidence_coverage": evidence_coverage,
            "claim_support_ratio": claim_support_ratio,
            "contradiction_rate": contradiction_rate,
            "hallucination_risk": hallucination_risk,
            "source_authority": source_authority,
            "source_count": len(citations),
            "tool_success_rate": _clamp01(0.9 if tool_log else 0.6),
        }

    @classmethod
    def _extract_claims(cls, text: str) -> list[str]:
        claims: list[str] = []
        for sentence in str(text).replace("\n", " ").split("."):
            s = sentence.strip()
            if not s:
                continue
            lowered = s.lower()
            if any(v in lowered for v in [" is ", " are ", " was ", " were ", " can ", " will ", " has ", " have "]):
                claims.append(s)
            elif any(ch.isdigit() for ch in s):
                claims.append(s)
        return claims[:50]

    @classmethod
    def _extract_urls(cls, text: str) -> list[str]:
        urls = []
        for part in str(text).split():
            if part.startswith("http://") or part.startswith("https://"):
                urls.append(part.rstrip("),.;"))
        return _unique(urls)

    @classmethod
    def _repetition_score(cls, tokens: list[str]) -> float:
        if len(tokens) < 4:
            return 0.1
        grams = [f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1)]
        counts: dict[str, int] = {}
        for g in grams:
            counts[g] = counts.get(g, 0) + 1
        repeats = sum(c - 1 for c in counts.values() if c > 1)
        return _clamp01(repeats / max(len(grams), 1))

    @classmethod
    def _uncertainty_score(cls, text: str) -> float:
        low = text.lower()
        hedges = sum(low.count(w) for w in ["maybe", "might", "possibly", "likely", "uncertain", "not sure"])
        anchors = sum(low.count(w) for w in ["because", "therefore", "evidence", "verified", "source", "confirmed"])
        return _clamp01(0.25 + hedges * 0.08 - anchors * 0.04)

    @classmethod
    def _progress_score(cls, text: str, tool_log: list[dict[str, Any]]) -> float:
        low = text.lower()
        action_words = sum(low.count(w) for w in ["next", "first", "then", "plan", "implement", "verify", "deliver", "use", "run"])
        length_factor = _clamp01(len(text.strip()) / 800.0)
        tool_bonus = 0.15 if tool_log else 0.0
        return _clamp01(0.25 + 0.05 * action_words + 0.35 * length_factor + tool_bonus)

    @classmethod
    def _hypothesis_stability(cls, answer_tokens: list[str], prior_tokens: set[str]) -> float:
        cur = set(_unique(answer_tokens[:80]))
        if not cur or not prior_tokens:
            return 0.6
        overlap = len([t for t in cur if t in prior_tokens])
        return _clamp01(overlap / max(min(len(cur), len(prior_tokens)), 1))

    @classmethod
    def _build_kernel_request(
        cls, runtime: VerraRuntimeConfig, session: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "schema_version": "verra_kernel.v3",
            "config": runtime.kernel_config,
            "state": session["kernel_state"],
            "observation": {
                "topology": snapshot["topology"],
                "grad_v_total_q": snapshot["grad_v_total_q"],
                "convergence": snapshot["convergence"],
                "verification": snapshot["verification"],
                "receipt": snapshot["receipt"],
                "formula_terms": snapshot["formula_terms"],
                "persona_mode_hint": snapshot["persona_mode"],
                "rng_seed": cls._seed_from_session(session.get("id", "default")),
            },
        }

    @classmethod
    def _seed_from_session(cls, session_id: str) -> int:
        h = 2166136261
        for ch in f"{session_id}:{int(datetime.now().timestamp())}":
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        return h

    @classmethod
    def _call_kernel(cls, runtime: VerraRuntimeConfig, request: dict[str, Any]) -> dict[str, Any]:
        proc = subprocess.run(
            [runtime.kernel_command, *runtime.kernel_args],
            cwd=str(runtime.kernel_cwd),
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Kernel exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return json.loads(proc.stdout)

    @classmethod
    def _learn_memory(cls, runtime: VerraRuntimeConfig, agent, snapshot: dict[str, Any], kernel_resp: dict[str, Any]) -> None:
        mem_path = runtime.data_dir / "memory.json"
        vault_path = runtime.data_dir / "vault.json"
        vault = verra_vault.load_vault(vault_path)

        compartment = snapshot.get("persona_mode", "general")
        session_id = cls._session_id(agent)
        user_text = snapshot["user_text"]
        pref_markers = ["i prefer", "i want", "i like"]
        low = user_text.lower()
        for marker in pref_markers:
            idx = low.find(marker)
            if idx != -1:
                pref = user_text[idx : idx + 180].strip()
                verra_vault.upsert_preference(
                    vault,
                    compartment,
                    key="user_stated_preference",
                    value=pref,
                    confidence=0.75,
                    source="user_explicit",
                    trust_tier="user_confirmed",
                    source_authority=1.0,
                )
                break

        verra_vault.append_episode(
            vault,
            compartment,
            session_id=session_id,
            user_text=user_text,
            assistant_text=snapshot["answer_text"],
            metrics=snapshot["metrics_for_session"],
            kernel_mode=(kernel_resp.get("policy_hints") or {}).get("mode"),
        )
        verra_vault.append_receipt(
            vault,
            compartment,
            session_id=session_id,
            snapshot=snapshot,
            kernel_resp=kernel_resp,
        )
        cls._promote_facts_to_vault(vault, agent, snapshot, session_id)

        verra_vault.save_vault(vault_path, vault)
        # Refresh dream cycle after vault update so future loops receive new mirror/convergence priors.
        cls.run_dream_cycle(agent, force=False, reason="post_vault_update")

        # Legacy compatibility export for existing tooling.
        mem = verra_vault.export_legacy_memory(vault)
        mem_path.write_text(json.dumps(mem, indent=2) + "\n", "utf-8")

    @classmethod
    def _promote_facts_to_vault(cls, vault: dict[str, Any], agent, snapshot: dict[str, Any], session_id: str) -> None:
        verification = snapshot.get("verification", {})
        convergence = snapshot.get("convergence", {})
        receipt = snapshot.get("receipt", {})
        dream_prior = snapshot.get("dream_prior", {})
        compartment = snapshot.get("persona_mode", "general")

        evidence_coverage = float(verification.get("evidence_coverage", 0.0))
        claim_support_ratio = float(verification.get("claim_support_ratio", 0.0))
        hallucination_risk = float(verification.get("hallucination_risk", 1.0))
        source_authority = float(verification.get("source_authority", 0.0))
        convergence_score = float(convergence.get("convergence_score", 0.0))
        contradiction_rate = float(convergence.get("contradiction_rate", 1.0))
        dream_conv = float(dream_prior.get("convergence_alignment", 0.5))
        dream_mirror = float(dream_prior.get("mirror_alignment", 0.5))
        proof_strength = float(receipt.get("proof_strength", 0.0))

        # Dream-gated fact promotion: only promote when the turn is both verified and convergent.
        evidence_thresh = max(0.75, 0.65 + 0.2 * dream_conv)
        hallucination_thresh = min(0.35, 0.15 + 0.3 * (1.0 - dream_conv))
        convergence_thresh = max(0.55, 0.45 + 0.2 * dream_mirror)

        if evidence_coverage < evidence_thresh:
            return
        if hallucination_risk > hallucination_thresh:
            return
        if convergence_score < convergence_thresh:
            return
        if contradiction_rate > 0.25:
            return

        claims = cls._extract_claims(str(snapshot.get("answer_text", "")))
        max_claims = 8 if compartment in ("business", "cognitive") else 4
        for claim in claims[:max_claims]:
            txt = claim.strip()
            if len(txt) < 20:
                continue
            if len(_tok(txt)) < 4:
                continue
            confidence = _clamp01(
                0.30 * evidence_coverage
                + 0.25 * claim_support_ratio
                + 0.20 * convergence_score
                + 0.15 * proof_strength
                + 0.10 * source_authority
                - 0.25 * hallucination_risk
            )
            if confidence < 0.65:
                continue
            verra_vault.upsert_fact(
                vault,
                compartment,
                fact_text=txt,
                session_id=session_id,
                source="verra_receipt",
                confidence=confidence,
                source_authority=source_authority,
                evidence_coverage=evidence_coverage,
                claim_support_ratio=claim_support_ratio,
                hallucination_risk=hallucination_risk,
                trust_tier="provisional_verified",
                tags=[
                    "dream_gated",
                    f"convergence:{round(convergence_score,2)}",
                    f"evidence:{round(evidence_coverage,2)}",
                ],
            )

    @classmethod
    def _get_dream_prior(cls, agent, compartment: str) -> dict[str, float]:
        runtime: VerraRuntimeConfig | None = agent.get_data(cls.DATA_KEY)
        if not runtime or not runtime.enabled:
            return {
                "convergence_alignment": 0.5,
                "mirror_alignment": 0.5,
                "sigma_alignment": 0.5,
            }
        try:
            profile = agent.get_data(cls.DREAM_PROFILE_KEY)
            if not profile:
                profile = verra_dream.load_dream_profile(runtime.data_dir / "dream_profile.json")
                agent.set_data(cls.DREAM_PROFILE_KEY, profile)
            comp = (profile.get("compartments") or {}).get(compartment) or (profile.get("compartments") or {}).get("general") or {}
            glob = profile.get("global") or {}

            user_text = cls._extract_user_text(agent)
            user_tokens = set(_tok(user_text))
            sig = comp.get("mirror_signature") or glob.get("mirror_signature") or []
            if user_tokens and sig:
                overlap = len([t for t in sig[:20] if t in user_tokens]) / max(min(len(sig[:20]), len(user_tokens)), 1)
            else:
                overlap = 0.5

            conv_base = _clamp01(float(comp.get("convergence_baseline", glob.get("convergence_baseline", 0.5))))
            sigma_base = _clamp01(float(comp.get("sigma_baseline", 0.5)))
            return {
                "convergence_alignment": _clamp01(0.5 * conv_base + 0.5 * overlap),
                "mirror_alignment": _clamp01(overlap),
                "sigma_alignment": _clamp01(sigma_base),
            }
        except Exception:
            return {
                "convergence_alignment": 0.5,
                "mirror_alignment": 0.5,
                "sigma_alignment": 0.5,
            }

    @classmethod
    def _count_vault_receipts(cls, vault: dict[str, Any]) -> int:
        total = 0
        for comp in (vault.get("compartments") or {}).values():
            total += len((comp or {}).get("receipts", []))
        return total

    @classmethod
    def _dream_scheduler_path(cls, runtime: VerraRuntimeConfig) -> Path:
        return runtime.data_dir / "dream_cycle_state.json"

    @classmethod
    def _load_dream_scheduler(cls, agent, runtime: VerraRuntimeConfig) -> dict[str, Any]:
        cached = agent.get_data(cls.DREAM_SCHEDULER_KEY)
        if cached:
            return cached
        path = cls._dream_scheduler_path(runtime)
        if path.exists():
            state = json.loads(path.read_text("utf-8"))
        else:
            state = {
                "schema_version": "verra_dream_cycle_state.v1",
                "last_run_at": None,
                "last_receipt_count": 0,
                "runs": 0,
                "last_reason": None,
            }
        agent.set_data(cls.DREAM_SCHEDULER_KEY, state)
        return state

    @classmethod
    def _save_dream_scheduler(cls, agent, runtime: VerraRuntimeConfig, state: dict[str, Any]) -> None:
        path = cls._dream_scheduler_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + "\n", "utf-8")
        agent.set_data(cls.DREAM_SCHEDULER_KEY, state)

    @classmethod
    def _dream_cycle_due(cls, scheduler: dict[str, Any], total_receipts: int) -> bool:
        interval_sec = int(os.getenv("VERRA_DREAM_INTERVAL_SEC", "900"))
        min_new_receipts = int(os.getenv("VERRA_DREAM_MIN_NEW_RECEIPTS", "2"))

        last_run_at = scheduler.get("last_run_at")
        last_receipt_count = int(scheduler.get("last_receipt_count", 0))
        new_receipts = max(0, total_receipts - last_receipt_count)

        if not last_run_at:
            return new_receipts >= 1 or total_receipts == 0

        try:
            last_dt = datetime.fromisoformat(str(last_run_at).replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            elapsed = (now_dt - last_dt).total_seconds()
        except Exception:
            return True

        return elapsed >= interval_sec or new_receipts >= min_new_receipts

    @classmethod
    def _log(cls, agent, level: str, text: str) -> None:
        try:
            agent.context.log.log(type=level, content=text)
        except Exception:
            pass
