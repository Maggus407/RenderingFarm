import re
import threading
from pathlib import Path

from backend.utils import read_json, write_json

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "UPLOAD_FOLDER": "input",
    "PROCESSING_FOLDER": "processing",
    "OUTPUT_FOLDER": "output",
    "FAILED_FOLDER": "failed",
    "BLENDER_BIN": "blender",
    "RENDER_SCRIPT": "scripts/render_job.sh",
    "OPTIMIZE_SCRIPT": "backend/turbo_optimize.py",
    "POLL_INTERVAL_SECONDS": 2,
    "HOST": "0.0.0.0",
    "PORT": 5000,
    "HSA_XNACK": "1",
    "USE_HIPRT": "0",
    "RENDER_GPU_NAME": "Radeon",
    "INHIBIT_SLEEP": True,
}

ALLOWED_EXTENSIONS = {".blend"}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
INPUT_JOB_STATES = {"PENDING", "QUEUED"}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        data = read_json(CONFIG_PATH, default={})
        if isinstance(data, dict):
            config.update(data)
    else:
        write_json(CONFIG_PATH, config)
    return config


def resolve_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return (BASE_DIR / candidate).resolve()


CONFIG = load_config()

UPLOAD_DIR = resolve_path(CONFIG["UPLOAD_FOLDER"])
PROCESSING_DIR = resolve_path(CONFIG["PROCESSING_FOLDER"])
OUTPUT_DIR = resolve_path(CONFIG["OUTPUT_FOLDER"])
FAILED_DIR = resolve_path(CONFIG["FAILED_FOLDER"])

for folder in [UPLOAD_DIR, PROCESSING_DIR, OUTPUT_DIR, FAILED_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

state_lock = threading.Lock()
config_lock = threading.Lock()
stop_event = threading.Event()
active_process = None

current_job = {
    "status": "IDLE",
    "id": None,
    "filename": None,
    "mode": "TURBO",
    "progress": 0,
    "sample_current": None,
    "sample_total": None,
    "tile_current": None,
    "tile_total": None,
    "frame": None,
    "remaining": None,
    "message": "Warte auf Dateien...",
    "started_at": None,
}


def update_current_job(**kwargs):
    with state_lock:
        current_job.update(kwargs)


def current_job_snapshot() -> dict:
    with state_lock:
        return dict(current_job)
