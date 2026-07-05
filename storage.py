import json
import logging
import os

logger = logging.getLogger("diablo_bot.storage")

DEFAULT_STATE = {
    "d2r_message_id": None,
    "d4_message_id": None,
    "d2r_embeds": {"terror_zone": None, "dclone": None},
    "d4_embeds": {"helltide": None, "legion_event": None, "world_boss": None},
}


def _fresh_default() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE))  # cheap deep copy


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        logger.info("No existing state file at %s, starting fresh.", path)
        return _fresh_default()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read state file (%s), starting fresh: %s", path, exc)
        return _fresh_default()

    # Merge onto defaults so a new field added later doesn't break loading an old file.
    merged = _fresh_default()
    merged.update(data)
    merged["d2r_embeds"] = {**merged["d2r_embeds"], **data.get("d2r_embeds", {})}
    merged["d4_embeds"] = {**merged["d4_embeds"], **data.get("d4_embeds", {})}
    return merged


def save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)  # atomic - avoids a corrupt file if the process dies mid-write
    except OSError as exc:
        logger.error("Failed to save state file (%s): %s", path, exc)
