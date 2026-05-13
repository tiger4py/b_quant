"""下载进度追踪"""
import json
import os
import threading
from datetime import datetime

PROGRESS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "progress.json")
_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%H:%M:%S")


def update(task: str, **kwargs):
    """写入进度字段"""
    data = _read()
    data[task] = {**data.get(task, {}), **kwargs, "ts": _now()}
    _write(data)


def start(task: str, total: int, label: str = ""):
    """开始任务"""
    with _lock:
        data = _read()
        data[task] = {"total": total, "done": 0, "label": label, "ts": _now()}
        _write(data)


def step(task: str, delta: int = 1, label: str = ""):
    """增加完成数"""
    with _lock:
        data = _read()
        if task in data:
            data[task]["done"] = data[task].get("done", 0) + delta
            if label:
                data[task]["label"] = label
            data[task]["ts"] = _now()
            _write(data)


def finish(task: str):
    """标记完成"""
    with _lock:
        data = _read()
        if task in data:
            data[task]["done"] = data[task]["total"]
            data[task]["ts"] = _now()
            _write(data)


def get() -> dict:
    return _read()


def _read() -> dict:
    try:
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(data: dict):
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(data, f)
