import json
import os
import threading
from datetime import datetime

from config import DATA_DIR

_lock = threading.Lock()
_path = os.path.join(DATA_DIR, "progress.json")
_state = {}


def _save():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_path, "w", encoding="utf-8") as f:
        json.dump(_state, f, ensure_ascii=False, indent=2)


def start(key, total=0, label=""):
    with _lock:
        _state[key] = {
            "key": key,
            "total": total,
            "current": 0,
            "label": label,
            "status": "running",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save()


def step(key, inc=1, label=None):
    with _lock:
        item = _state.setdefault(key, {"key": key, "total": 0, "current": 0, "status": "running"})
        item["current"] = item.get("current", 0) + inc
        if label is not None:
            item["label"] = label
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save()


def finish(key, label=None):
    with _lock:
        item = _state.setdefault(key, {"key": key, "total": 0, "current": 0})
        if label is not None:
            item["label"] = label
        item["status"] = "finished"
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save()


def get():
    with _lock:
        if not _state and os.path.exists(_path):
            try:
                with open(_path, "r", encoding="utf-8") as f:
                    _state.update(json.load(f))
            except (OSError, json.JSONDecodeError):
                pass
        return _state
