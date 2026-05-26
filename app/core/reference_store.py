"""Load stored exercise reference sequences for sync comparison."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.models.sync import SyncReference, SyncReferenceList


REFERENCE_DATA_DIR = Path(__file__).resolve().parents[2] / "reference_data"
KKW_SYNC_DATASET_PATH = REFERENCE_DATA_DIR / "kkw_squat_sync_dataset.json"


@lru_cache(maxsize=1)
def load_kkw_sync_dataset() -> SyncReferenceList:
    payload = json.loads(KKW_SYNC_DATASET_PATH.read_text(encoding="utf-8"))
    return SyncReferenceList.model_validate(payload)


def list_kkw_references() -> list[SyncReference]:
    return load_kkw_sync_dataset().references


def get_kkw_reference(reference_id: str) -> SyncReference | None:
    for reference in load_kkw_sync_dataset().references:
        if reference.id == reference_id:
            return reference
    return None
