from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_config() -> dict[str, Any]:
    return {
        "schema_version": "verra_receipt_chain_config.v1",
        "enabled": True,
        "algorithm": "sha256",
        "signing": {
            "enabled": True,
            "mode": "hmac-sha256",
            "key_env": "VERRA_RECEIPT_CHAIN_KEY",
            "key_file": "receipt_chain.key",
            "auto_generate_local_key": True,
        },
    }


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            cfg = json.loads(path.read_text("utf-8"))
            if isinstance(cfg, dict):
                base = default_config()
                base.update(cfg)
                if isinstance(base.get("signing"), dict) and isinstance(cfg.get("signing"), dict):
                    merged = dict(default_config().get("signing", {}))
                    merged.update(cfg.get("signing", {}))
                    base["signing"] = merged
                return base
        except Exception:
            pass
    return default_config()


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")


def ensure_config(path: Path) -> dict[str, Any]:
    cfg = load_config(path)
    if not path.exists():
        save_config(path, cfg)
    return cfg


def ensure_runtime_material(data_dir: Path) -> dict[str, Any]:
    cfg_path = data_dir / "receipt_chain_config.json"
    cfg = ensure_config(cfg_path)
    _resolve_signing_key(cfg, cfg_path, generate_if_missing=True)
    return cfg


def chain_receipt_doc(
    doc: dict[str, Any],
    *,
    config_path: Path,
    list_key: str = "receipts",
    doc_kind: str = "receipt_doc",
) -> dict[str, Any]:
    cfg = ensure_config(config_path)
    return chain_list_doc(doc, config=cfg, config_path=config_path, list_key=list_key, doc_kind=doc_kind)


def chain_list_doc(
    doc: dict[str, Any],
    *,
    config: dict[str, Any],
    config_path: Path,
    list_key: str,
    doc_kind: str,
) -> dict[str, Any]:
    items = doc.get(list_key)
    if not isinstance(items, list):
        return {
            "ok": False,
            "reason": "list_key_missing",
            "list_key": list_key,
        }
    if not bool(config.get("enabled", True)):
        _set_doc_chain_meta(
            doc,
            list_key=list_key,
            meta={
                "enabled": False,
                "doc_kind": doc_kind,
                "list_key": list_key,
                "count": len(items),
                "updated_at": _now_iso(),
            },
        )
        return {"ok": True, "enabled": False, "count": len(items)}

    alg = str(config.get("algorithm", "sha256")).lower()
    key_bytes, key_meta = _resolve_signing_key(config, config_path)
    prev_hash = ""
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        payload = _strip_chain_fields(item)
        payload_envelope = {
            "doc_kind": doc_kind,
            "list_key": list_key,
            "index": idx,
            "prev_hash": prev_hash,
            "payload": payload,
        }
        payload_json = _canonical_json_bytes(payload_envelope)
        item_hash = _digest_hex(payload_json, alg=alg)
        chain_meta = {
            "index": idx,
            "alg": alg,
            "prev_hash": prev_hash,
            "hash": item_hash,
            "chained_at": _now_iso(),
        }
        if key_bytes is not None:
            chain_meta["sig"] = _hmac_hex(item_hash.encode("utf-8"), key_bytes, alg=alg)
            if key_meta:
                chain_meta["sig_key_source"] = key_meta
        item["_chain"] = chain_meta
        prev_hash = item_hash

    verify = verify_list_doc(doc, config=config, list_key=list_key, doc_kind=doc_kind)
    _set_doc_chain_meta(
        doc,
        list_key=list_key,
        meta={
            "enabled": True,
            "doc_kind": doc_kind,
            "list_key": list_key,
            "alg": alg,
            "signed": key_bytes is not None,
            "count": len(items),
            "tail_hash": prev_hash,
            "verified": bool(verify.get("ok", False)),
            "updated_at": _now_iso(),
        },
    )
    return {"ok": True, "enabled": True, "count": len(items), "tail_hash": prev_hash}


def verify_list_doc(
    doc: dict[str, Any],
    *,
    config: dict[str, Any],
    list_key: str,
    doc_kind: str,
) -> dict[str, Any]:
    items = doc.get(list_key)
    if not isinstance(items, list):
        return {"ok": False, "reason": "list_key_missing", "list_key": list_key}
    if not bool(config.get("enabled", True)):
        return {"ok": True, "enabled": False, "count": len(items)}
    alg = str(config.get("algorithm", "sha256")).lower()
    prev_hash = ""
    key_bytes, _ = _resolve_signing_key(config, None)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        meta = item.get("_chain") or {}
        payload = _strip_chain_fields(item)
        envelope = {
            "doc_kind": doc_kind,
            "list_key": list_key,
            "index": idx,
            "prev_hash": prev_hash,
            "payload": payload,
        }
        expected = _digest_hex(_canonical_json_bytes(envelope), alg=alg)
        if str(meta.get("hash", "")) != expected:
            return {"ok": False, "reason": "hash_mismatch", "index": idx}
        if str(meta.get("prev_hash", "")) != prev_hash:
            return {"ok": False, "reason": "prev_hash_mismatch", "index": idx}
        if key_bytes is not None and "sig" in meta:
            expected_sig = _hmac_hex(expected.encode("utf-8"), key_bytes, alg=alg)
            if str(meta.get("sig", "")) != expected_sig:
                return {"ok": False, "reason": "signature_mismatch", "index": idx}
        prev_hash = expected
    return {"ok": True, "count": len(items), "tail_hash": prev_hash}


def stamp_state_doc(
    doc: dict[str, Any],
    *,
    config_path: Path,
    doc_kind: str = "state_doc",
    chain_list_key: str | None = None,
) -> dict[str, Any]:
    cfg = ensure_config(config_path)
    if chain_list_key and isinstance(doc.get(chain_list_key), list):
        chain_list_doc(doc, config=cfg, config_path=config_path, list_key=chain_list_key, doc_kind=f"{doc_kind}:{chain_list_key}")

    state_payload = copy.deepcopy(doc)
    state_payload.pop("_state_chain", None)
    alg = str(cfg.get("algorithm", "sha256")).lower()
    payload_bytes = _canonical_json_bytes({"doc_kind": doc_kind, "payload": state_payload})
    digest = _digest_hex(payload_bytes, alg=alg)
    key_bytes, key_meta = _resolve_signing_key(cfg, config_path)
    meta = {
        "alg": alg,
        "hash": digest,
        "doc_kind": doc_kind,
        "stamped_at": _now_iso(),
    }
    if key_bytes is not None:
        meta["sig"] = _hmac_hex(digest.encode("utf-8"), key_bytes, alg=alg)
        if key_meta:
            meta["sig_key_source"] = key_meta
    doc["_state_chain"] = meta
    return meta


def verify_state_doc(doc: dict[str, Any], *, config_path: Path, doc_kind: str = "state_doc") -> dict[str, Any]:
    cfg = ensure_config(config_path)
    meta = doc.get("_state_chain") or {}
    alg = str(cfg.get("algorithm", "sha256")).lower()
    payload = copy.deepcopy(doc)
    payload.pop("_state_chain", None)
    digest = _digest_hex(_canonical_json_bytes({"doc_kind": doc_kind, "payload": payload}), alg=alg)
    if str(meta.get("hash", "")) != digest:
        return {"ok": False, "reason": "state_hash_mismatch"}
    key_bytes, _ = _resolve_signing_key(cfg, None)
    if key_bytes is not None and "sig" in meta:
        if str(meta.get("sig", "")) != _hmac_hex(digest.encode("utf-8"), key_bytes, alg=alg):
            return {"ok": False, "reason": "state_sig_mismatch"}
    return {"ok": True, "hash": digest}


def _set_doc_chain_meta(doc: dict[str, Any], *, list_key: str, meta: dict[str, Any]) -> None:
    top = doc.setdefault("_receipt_chain", {})
    if not isinstance(top, dict):
        top = {}
        doc["_receipt_chain"] = top
    top[list_key] = meta


def _strip_chain_fields(item: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(item)
    payload.pop("_chain", None)
    return payload


def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest_hex(data: bytes, *, alg: str) -> str:
    if alg != "sha256":
        alg = "sha256"
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _hmac_hex(data: bytes, key: bytes, *, alg: str) -> str:
    if alg != "sha256":
        alg = "sha256"
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _resolve_signing_key(
    config: dict[str, Any],
    config_path: Path | None,
    *,
    generate_if_missing: bool = False,
) -> tuple[bytes | None, str | None]:
    signing = config.get("signing") or {}
    if not isinstance(signing, dict) or not bool(signing.get("enabled", True)):
        return None, None

    env_name = str(signing.get("key_env", "VERRA_RECEIPT_CHAIN_KEY") or "").strip()
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val.encode("utf-8"), f"env:{env_name}"

    key_file = str(signing.get("key_file", "receipt_chain.key") or "receipt_chain.key").strip()
    if not config_path:
        return None, None
    key_path = (config_path.parent / key_file).resolve()
    if key_path.exists():
        try:
            key_txt = key_path.read_text("utf-8").strip()
            if key_txt:
                return key_txt.encode("utf-8"), f"file:{key_path.name}"
        except Exception:
            return None, None

    auto_gen = bool(signing.get("auto_generate_local_key", True))
    if (generate_if_missing or auto_gen) and config_path is not None:
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_txt = secrets.token_hex(32)
            key_path.write_text(key_txt + "\n", "utf-8")
            try:
                os.chmod(key_path, 0o600)
            except Exception:
                pass
            return key_txt.encode("utf-8"), f"file:{key_path.name}"
        except Exception:
            return None, None
    return None, None
