from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from python.helpers import verra_native_events, verra_receipt_chain


def load_receipts(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return {"schema_version": "verra_policy_decision_receipts.v1", "receipts": []}


def save_receipts(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verra_receipt_chain.chain_receipt_doc(
        data,
        config_path=path.parent / "receipt_chain_config.json",
        list_key="receipts",
        doc_kind="verra_policy_decision_receipts",
    )
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    verra_native_events.rebuild_native_events(path.parent)


def append_receipt(
    receipts_doc: dict[str, Any],
    receipt: dict[str, Any],
    max_items: int = 1000,
) -> None:
    receipts_doc.setdefault("receipts", []).append(receipt)
    del receipts_doc["receipts"][:-max_items]
