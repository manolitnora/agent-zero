from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from python.helpers import verra_vault


def _tok(text: str) -> list[str]:
    out: list[str] = []
    for raw in (text or "").lower().replace("\n", " ").split():
        t = "".join(ch for ch in raw if ch.isalnum())
        if t and len(t) > 2:
            out.append(t)
    return out


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def load_dream_profile(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {
        "schema_version": "verra_dream_profile.v1",
        "updated_at": verra_vault.now_iso(),
        "global": {
            "mirror_signature": [],
            "convergence_baseline": 0.5,
            "hallucination_baseline": 0.3,
            "evidence_baseline": 0.4,
        },
        "compartments": {c: _empty_compartment_profile() for c in verra_vault.COMPARTMENTS},
    }


def save_dream_profile(path: Path, profile: dict[str, Any]) -> None:
    profile["updated_at"] = verra_vault.now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", "utf-8")


def _empty_compartment_profile() -> dict[str, Any]:
    return {
        "mirror_signature": [],
        "preferred_tone_markers": [],
        "recurring_intents": [],
        "convergence_baseline": 0.5,
        "hallucination_baseline": 0.3,
        "evidence_baseline": 0.4,
        "sigma_baseline": 0.5,
        "finalize_bias_baseline": 0.4,
        "mode_distribution": {},
        "last_receipt_at": None,
        "episode_count": 0,
        "receipt_count": 0,
    }


def synthesize_from_vault(vault: dict[str, Any]) -> dict[str, Any]:
    profile = {
        "schema_version": "verra_dream_profile.v1",
        "updated_at": verra_vault.now_iso(),
        "global": {
            "mirror_signature": [],
            "convergence_baseline": 0.5,
            "hallucination_baseline": 0.3,
            "evidence_baseline": 0.4,
        },
        "compartments": {},
    }

    global_words: Counter[str] = Counter()
    global_conv: list[float] = []
    global_hal: list[float] = []
    global_evd: list[float] = []

    for comp in verra_vault.COMPARTMENTS:
        bucket = (vault.get("compartments") or {}).get(comp) or {}
        episodes = bucket.get("episodes", [])
        receipts = bucket.get("receipts", [])
        preferences = bucket.get("preferences", [])

        p = _empty_compartment_profile()
        p["episode_count"] = len(episodes)
        p["receipt_count"] = len(receipts)
        if receipts:
            p["last_receipt_at"] = receipts[-1].get("at")

        # Build mirror signature from user text, preferences, and high-value assistant summaries.
        word_counts: Counter[str] = Counter()
        recurring_intents: Counter[str] = Counter()
        tone_markers: Counter[str] = Counter()
        conv_vals: list[float] = []
        hal_vals: list[float] = []
        evd_vals: list[float] = []
        sigma_vals: list[float] = []
        finalize_vals: list[float] = []
        mode_dist: Counter[str] = Counter()

        for pref in preferences[-80:]:
            text = f"{pref.get('key', '')} {pref.get('value', '')}"
            word_counts.update(_tok(text))

        for ep in episodes[-200:]:
            user = str(ep.get("user", ""))
            assistant = str(ep.get("assistant_summary", ""))
            metrics = ep.get("metrics") or {}
            word_counts.update(_tok(user))

            # crude recurring-intent extraction
            low = user.lower()
            for marker in ("build", "plan", "fix", "design", "integrate", "verify", "automate", "deploy"):
                if marker in low:
                    recurring_intents[marker] += 1
            for marker in ("concise", "detailed", "direct", "rigorous", "creative", "warm", "formal"):
                if marker in low or marker in assistant.lower():
                    tone_markers[marker] += 1

            conv_vals.append(float(metrics.get("convergence_score", 0.5)))
            hal_vals.append(float(metrics.get("hallucination_risk", 0.3)))
            evd_vals.append(float(metrics.get("evidence_coverage", 0.4)))
            sigma_vals.append(float(metrics.get("sigma_tension", 0.5)))
            mode_dist[str(ep.get("kernel_mode", "unknown"))] += 1

        for rc in receipts[-200:]:
            m = rc.get("metrics") or rc.get("snapshot_metrics") or {}
            d = rc.get("kernel_diagnostics") or {}
            ph = rc.get("policy_hints") or {}
            conv_vals.append(float(m.get("convergence_score", d.get("convergence_score", 0.5))))
            hal_vals.append(float(m.get("hallucination_risk", d.get("hallucination_risk", 0.3))))
            evd_vals.append(float(m.get("evidence_coverage", d.get("evidence_coverage", 0.4))))
            sigma_vals.append(float(m.get("sigma_tension", d.get("sigma_tension", 0.5))))
            finalize_vals.append(float(ph.get("finalize_bias", 0.4)))
            mode_dist[str(ph.get("mode", d.get("mode_out", "unknown")))] += 1

        p["mirror_signature"] = [w for w, _ in word_counts.most_common(20)]
        p["preferred_tone_markers"] = [w for w, _ in tone_markers.most_common(10)]
        p["recurring_intents"] = [w for w, _ in recurring_intents.most_common(10)]
        p["convergence_baseline"] = _avg_clamped(conv_vals, 0.5)
        p["hallucination_baseline"] = _avg_clamped(hal_vals, 0.3)
        p["evidence_baseline"] = _avg_clamped(evd_vals, 0.4)
        p["sigma_baseline"] = _avg_clamped(sigma_vals, 0.5)
        p["finalize_bias_baseline"] = _avg_clamped(finalize_vals, 0.4)
        p["mode_distribution"] = dict(mode_dist)

        profile["compartments"][comp] = p

        global_words.update(p["mirror_signature"])
        global_conv.append(p["convergence_baseline"])
        global_hal.append(p["hallucination_baseline"])
        global_evd.append(p["evidence_baseline"])

    profile["global"]["mirror_signature"] = [w for w, _ in global_words.most_common(30)]
    profile["global"]["convergence_baseline"] = _avg_clamped(global_conv, 0.5)
    profile["global"]["hallucination_baseline"] = _avg_clamped(global_hal, 0.3)
    profile["global"]["evidence_baseline"] = _avg_clamped(global_evd, 0.4)
    return profile


def _avg_clamped(vals: list[float], fallback: float) -> float:
    if not vals:
        return fallback
    return _clamp01(sum(vals) / len(vals))


def dream_prompt_fragment(profile: dict[str, Any], compartment: str) -> str:
    comps = profile.get("compartments", {})
    c = comps.get(compartment) or comps.get("general") or _empty_compartment_profile()
    g = profile.get("global", {})
    lines = [
        "VERRA DREAM CYCLE (mirror/convergence prior):",
        f"- compartment: {compartment}",
        f"- convergence_baseline: {round(float(c.get('convergence_baseline', 0.5)), 3)}",
        f"- evidence_baseline: {round(float(c.get('evidence_baseline', 0.4)), 3)}",
        f"- hallucination_baseline: {round(float(c.get('hallucination_baseline', 0.3)), 3)}",
    ]
    if c.get("recurring_intents"):
        lines.append(f"- recurring_intents: {', '.join(c['recurring_intents'][:8])}")
    if c.get("preferred_tone_markers"):
        lines.append(f"- tone_markers: {', '.join(c['preferred_tone_markers'][:8])}")
    if c.get("mirror_signature"):
        lines.append(f"- mirror_signature: {', '.join(c['mirror_signature'][:12])}")
    elif g.get("mirror_signature"):
        lines.append(f"- global_mirror_signature: {', '.join(g['mirror_signature'][:12])}")
    lines.append("- use this as a bias for style and convergence, not as factual truth.")
    return "\n".join(lines)

