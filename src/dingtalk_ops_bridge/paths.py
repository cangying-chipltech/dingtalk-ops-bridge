from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    configured = os.getenv("CONFIG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    container_dir = Path("/app/config")
    if container_dir.is_dir():
        return container_dir
    return PROJECT_ROOT / "config"


def data_dir() -> Path:
    configured = os.getenv("DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    container_dir = Path("/app/data")
    if container_dir.is_dir():
        return container_dir
    return PROJECT_ROOT / "data"


def plugins_dir() -> Path:
    configured = os.getenv("ACTION_PLUGIN_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    container_dir = Path("/app/plugins")
    if container_dir.is_dir():
        return container_dir
    return PROJECT_ROOT / "plugins"


def actions_file() -> Path:
    configured = os.getenv("ACTIONS_FILE")
    return Path(configured).expanduser().resolve() if configured else config_dir() / "actions.json"


def monitors_file() -> Path:
    configured = os.getenv("MONITORS_FILE")
    return Path(configured).expanduser().resolve() if configured else config_dir() / "monitors.json"


def monitor_state_file() -> Path:
    configured = os.getenv("MONITOR_STATE_FILE")
    return Path(configured).expanduser().resolve() if configured else data_dir() / "monitor-state.json"
