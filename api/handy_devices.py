"""Feature 3 — Handy device registry (multi-device support).

The proxy can drive several Handy devices at once (see docs/handy_integration.md §9a). Each device is
one entry in a JSON list persisted at LOG_DIR/handy_devices.json (mirrors LiveTV's channels.json:
in-memory list + atomic tmp+replace). A device is identified by its Handy connection key.

The ApplicationID (HANDY_APPLICATION_ID) is application-level and stays global — it is NOT per-device.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

# In-memory device list (ordered). Loaded from disk at import; mutated via the CRUD helpers.
_devices: List[Dict[str, Any]] = []
_loaded = False

# Per-device sync-mode options; default is HSP (local) per product decision.
SYNC_MODES = ("auto", "hosted", "local")
DEFAULT_SYNC_MODE = "local"

# Fields a client may set on create/update; everything else (id, source) is server-owned.
_EDITABLE = ("label", "key", "sync_mode", "funscript_offset",
             "hsp_buffer_min_s", "hsp_buffer_max_s", "hsp_poll_interval_s", "enabled")
# Optional integer knobs; blank/None means "inherit the Stash/global default".
_INT_OR_NONE = ("funscript_offset", "hsp_buffer_min_s", "hsp_buffer_max_s", "hsp_poll_interval_s")


def _path() -> str:
    return os.path.join(getattr(config, "LOG_DIR", "/config"), "handy_devices.json")


def load_devices() -> None:
    """Load the device list from disk (idempotent). Safe to call repeatedly."""
    global _loaded
    _loaded = True
    path = _path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _devices[:] = data.get("devices", [])
        logger.info(f"[handy] loaded {len(_devices)} device(s) from disk")
    except Exception as e:
        logger.warning(f"[handy] could not load handy_devices.json: {e}")


def save_devices() -> None:
    try:
        path = _path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"devices": _devices}, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[handy] could not save handy_devices.json: {e}")


def _ensure_loaded() -> None:
    if not _loaded:
        load_devices()


def _coerce(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a client-supplied device patch: whitelist keys, coerce types, default sync_mode."""
    out: Dict[str, Any] = {}
    for k in _EDITABLE:
        if k not in fields:
            continue
        v = fields[k]
        if k == "sync_mode":
            v = str(v or "").strip().lower()
            v = v if v in SYNC_MODES else DEFAULT_SYNC_MODE
        elif k == "enabled":
            v = bool(v)
        elif k in _INT_OR_NONE:
            if v in (None, "", "null"):
                v = None
            else:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = None
        else:  # label, key
            v = str(v or "").strip()
        out[k] = v
    return out


def list_devices() -> List[Dict[str, Any]]:
    """All devices, ordered. Callers must not mutate the returned dicts in place."""
    _ensure_loaded()
    return sorted(_devices, key=lambda d: d.get("order", 0))


def enabled_devices() -> List[Dict[str, Any]]:
    return [d for d in list_devices() if d.get("enabled", True) and (d.get("key") or "").strip()]


def get_device(device_id: str) -> Optional[Dict[str, Any]]:
    _ensure_loaded()
    return next((d for d in _devices if d.get("id") == device_id), None)


def add_device(fields: Dict[str, Any], source: str = "manual") -> Dict[str, Any]:
    """Create a device from a (partial) client patch, filling defaults. Returns the stored dict."""
    _ensure_loaded()
    patch = _coerce(fields)
    device = {
        "id": uuid.uuid4().hex[:12],
        "label": patch.get("label") or "Handy",
        "key": patch.get("key", ""),
        "sync_mode": patch.get("sync_mode", DEFAULT_SYNC_MODE),
        "funscript_offset": patch.get("funscript_offset"),
        "hsp_buffer_min_s": patch.get("hsp_buffer_min_s"),
        "hsp_buffer_max_s": patch.get("hsp_buffer_max_s"),
        "hsp_poll_interval_s": patch.get("hsp_poll_interval_s"),
        "enabled": patch.get("enabled", True),
        "source": source,
        "order": len(_devices),
    }
    _devices.append(device)
    save_devices()
    return device


def update_device(device_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _ensure_loaded()
    device = get_device(device_id)
    if device is None:
        return None
    device.update(_coerce(fields))
    save_devices()
    return device


def delete_device(device_id: str) -> bool:
    _ensure_loaded()
    before = len(_devices)
    _devices[:] = [d for d in _devices if d.get("id") != device_id]
    if len(_devices) == before:
        return False
    for i, d in enumerate(_devices):
        d["order"] = i
    save_devices()
    return True


def ensure_stash_seed(stash_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Auto-add the Stash-configured connection key as a device if it isn't already registered.

    Never deletes or rewrites existing devices — if the Stash key later changes, the new key is added
    as an additional device (deletion is manual only). Returns the newly-added device, or None."""
    key = (stash_key or "").strip()
    if not key:
        return None
    _ensure_loaded()
    if any((d.get("key") or "").strip() == key for d in _devices):
        return None
    device = add_device({"label": "Stash device", "key": key, "sync_mode": DEFAULT_SYNC_MODE},
                        source="stash")
    logger.info(f"[handy] auto-added Stash-configured device (key ...{key[-4:]})")
    return device
