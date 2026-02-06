import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import context
from utils import as_bool, as_int, now_iso, now_ms, read_json, write_json

IMAGE_EXTS = [".png", ".exr", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"]
IMAGE_EXTS_SET = set(IMAGE_EXTS)

ARTIST_RENDER_MODES = {"NORMAL", "COMPOSITOR"}


def normalize_mode(raw_mode: str) -> str:
    mode = (raw_mode or "TURBO").strip().upper()
    if mode in {"CUSTOM", "ARTIST"}:
        return "ARTIST"
    return "TURBO"


def normalize_artist_render_mode(raw_mode: str | None) -> str:
    mode = (raw_mode or "NORMAL").strip().upper()
    if mode in {"COMP", "COMPOSITE", "COMPOSITOR"}:
        return "COMPOSITOR"
    if mode not in ARTIST_RENDER_MODES:
        return "NORMAL"
    return mode


def is_allowed_blend(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return suffix in context.ALLOWED_EXTENSIONS


def is_valid_job_id(job_id: str) -> bool:
    return bool(context.JOB_ID_RE.match(job_id))


def make_job_id() -> str:
    return f"job-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"


def sidecar_for_blend(blend_path: Path) -> Path:
    return Path(str(blend_path) + ".json")


def ensure_within(base_dir: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def safe_job_dir(base_dir: Path, job_id: str) -> Path | None:
    if not is_valid_job_id(job_id):
        return None
    path = (base_dir / job_id).resolve()
    if not ensure_within(base_dir, path):
        return None
    return path


def find_job_archive(job_id: str) -> tuple[Path, str] | tuple[None, None]:
    out_dir = safe_job_dir(context.OUTPUT_DIR, job_id)
    if out_dir and out_dir.exists() and out_dir.is_dir():
        return out_dir, "DONE"
    failed_dir = safe_job_dir(context.FAILED_DIR, job_id)
    if failed_dir and failed_dir.exists() and failed_dir.is_dir():
        return failed_dir, "FAILED"
    return None, None


def unique_filename(name: str) -> str:
    base = Path(name).stem
    suffix = Path(name).suffix
    candidate = name
    counter = 1
    while True:
        with context.state_lock:
            rendering_same_name = (
                context.current_job["status"] == "RENDERING" and context.current_job["filename"] == candidate
            )
        if not (context.UPLOAD_DIR / candidate).exists() and not rendering_same_name:
            return candidate
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        postfix = f"{stamp}-{counter}" if counter > 1 else stamp
        candidate = f"{base}__{postfix}{suffix}"
        counter += 1


def ensure_queue_sidecar(blend_path: Path) -> dict:
    json_path = sidecar_for_blend(blend_path)
    metadata = read_json(json_path, default={}) if json_path.exists() else {}

    inferred_mode = "ARTIST" if "_CUSTOM" in blend_path.name else "TURBO"
    changed = False

    if not metadata.get("id"):
        metadata["id"] = make_job_id()
        changed = True

    if not metadata.get("original_filename"):
        metadata["original_filename"] = blend_path.name
        changed = True

    if not metadata.get("root_id"):
        metadata["root_id"] = metadata.get("id") or make_job_id()
        changed = True

    if not metadata.get("attempt"):
        metadata["attempt"] = 1
        changed = True

    if metadata.get("note") is None:
        metadata["note"] = ""
        changed = True

    if metadata.get("stored_filename") != blend_path.name:
        metadata["stored_filename"] = blend_path.name
        changed = True

    mode = normalize_mode(metadata.get("mode") or inferred_mode)
    if metadata.get("mode") != mode:
        metadata["mode"] = mode
        changed = True

    artist_render_mode = normalize_artist_render_mode(metadata.get("artist_render_mode"))
    if metadata.get("artist_render_mode") != artist_render_mode:
        metadata["artist_render_mode"] = artist_render_mode
        changed = True

    artist_use_hiprt = as_bool(metadata.get("artist_use_hiprt"), False)
    if metadata.get("artist_use_hiprt") != artist_use_hiprt:
        metadata["artist_use_hiprt"] = artist_use_hiprt
        changed = True

    artist_simplify_subdiv = as_int(metadata.get("artist_simplify_subdiv"), 0, 0, 5)
    if metadata.get("artist_simplify_subdiv") != artist_simplify_subdiv:
        metadata["artist_simplify_subdiv"] = artist_simplify_subdiv
        changed = True

    if not metadata.get("created_at"):
        metadata["created_at"] = now_iso()
        changed = True

    current_state = str(metadata.get("state") or "").upper()
    if current_state not in context.INPUT_JOB_STATES:
        metadata["state"] = "PENDING"
        changed = True
    elif metadata.get("state") != current_state:
        metadata["state"] = current_state
        changed = True

    if current_state == "QUEUED" and metadata.get("queue_priority") is None:
        metadata["queue_priority"] = now_ms()
        changed = True

    if changed or not json_path.exists():
        write_json(json_path, metadata)

    return metadata


def get_queue() -> list[dict]:
    blend_files = [p for p in context.UPLOAD_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".blend"]
    blend_files.sort(key=lambda p: p.stat().st_mtime)

    queue_items = []
    for blend_path in blend_files:
        metadata = ensure_queue_sidecar(blend_path)
        queue_items.append(
            {
                "id": metadata["id"],
                "filename": blend_path.name,
                "mode": metadata["mode"],
                "artist_render_mode": metadata.get("artist_render_mode") or "NORMAL",
                "state": metadata.get("state", "PENDING"),
                "created_at": metadata.get("created_at"),
                "queued_at": metadata.get("queued_at"),
                "note": metadata.get("note", ""),
                "queue_priority": metadata.get("queue_priority"),
                "attempt": metadata.get("attempt", 1),
                "root_id": metadata.get("root_id"),
            }
        )
    return queue_items


def get_render_queue() -> list[dict]:
    queue = [item for item in get_queue() if item.get("state") == "QUEUED"]
    queue.sort(
        key=lambda item: (
            item.get("queue_priority") if item.get("queue_priority") is not None else float("inf"),
            item.get("queued_at") or item.get("created_at") or "",
        )
    )
    return queue


def find_input_job(job_id: str) -> tuple[dict, Path, Path] | tuple[None, None, None]:
    for item in get_queue():
        if item["id"] == job_id:
            blend_path = context.UPLOAD_DIR / item["filename"]
            json_path = sidecar_for_blend(blend_path)
            metadata = read_json(json_path, default={})
            if not metadata:
                metadata = ensure_queue_sidecar(blend_path)
            return metadata, blend_path, json_path
    return None, None, None


def build_manifest(job_meta: dict, state: str, reason: str, returncode: int) -> dict:
    return {
        "id": job_meta.get("id"),
        "state": state,
        "mode": normalize_mode(job_meta.get("mode")),
        "original_filename": job_meta.get("original_filename"),
        "stored_filename": job_meta.get("stored_filename"),
        "created_at": job_meta.get("created_at"),
        "started_at": job_meta.get("started_at"),
        "completed_at": now_iso(),
        "reason": reason,
        "returncode": returncode,
        "retry_of": job_meta.get("retry_of"),
    }


def finalize_job(processing_job_dir: Path, job_meta: dict, state: str, reason: str, returncode: int) -> Path:
    state_file = processing_job_dir / "state.json"
    job_meta["state"] = state
    job_meta["completed_at"] = now_iso()
    job_meta["returncode"] = returncode
    write_json(processing_job_dir / "job.json", job_meta)

    manifest = build_manifest(job_meta, state, reason, returncode)
    write_json(processing_job_dir / "manifest.json", manifest)
    write_json(
        state_file,
        {
            "state": state,
            "message": reason,
            "updated_at": manifest["completed_at"],
            "returncode": returncode,
        },
    )

    target_root = context.OUTPUT_DIR if state == "DONE" else context.FAILED_DIR
    target_dir = target_root / job_meta["id"]
    if target_dir.exists():
        target_dir = target_root / f"{job_meta['id']}__{datetime.now():%Y%m%d-%H%M%S}"
    shutil.move(str(processing_job_dir), target_dir)
    return target_dir


def recover_processing_jobs() -> None:
    for entry in context.PROCESSING_DIR.iterdir():
        if not entry.is_dir():
            continue

        blend_file = entry / "job.blend"
        json_file = entry / "job.json"

        if not blend_file.exists() or not json_file.exists():
            target = context.FAILED_DIR / f"recovery-broken-{entry.name}"
            if target.exists():
                target = context.FAILED_DIR / f"recovery-broken-{entry.name}-{uuid.uuid4().hex[:6]}"
            shutil.move(str(entry), target)
            continue

        metadata = read_json(json_file, default={})
        mode = normalize_mode(metadata.get("mode"))
        desired_name = metadata.get("stored_filename") or metadata.get("original_filename") or f"{entry.name}.blend"
        if not desired_name.lower().endswith(".blend"):
            desired_name = f"{desired_name}.blend"

        restored_name = unique_filename(desired_name)
        restored_blend = context.UPLOAD_DIR / restored_name
        shutil.move(str(blend_file), restored_blend)

        metadata["id"] = metadata.get("id") or make_job_id()
        metadata["mode"] = mode
        metadata["artist_render_mode"] = normalize_artist_render_mode(metadata.get("artist_render_mode"))
        metadata["stored_filename"] = restored_name
        metadata["state"] = "QUEUED"
        metadata["recovered_at"] = now_iso()
        metadata["attempt"] = int(metadata.get("attempt") or 1) + 1
        if not metadata.get("root_id"):
            metadata["root_id"] = metadata.get("id")
        write_json(sidecar_for_blend(restored_blend), metadata)

        shutil.rmtree(entry, ignore_errors=True)


def list_artifacts(job_dir: Path, job_id: str) -> list[dict]:
    artifacts = []
    for file_path in sorted(job_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(job_dir).as_posix()
        stat = file_path.stat()
        artifacts.append(
            {
                "path": rel,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "download_url": f"/jobs/{job_id}/download/{rel}",
            }
        )
    return artifacts


def has_render_images(job_dir: Path) -> bool:
    try:
        for file_path in job_dir.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTS_SET:
                return True
    except Exception:
        return False
    return False


def delete_render_images(job_dir: Path) -> dict:
    removed = 0
    removed_bytes = 0
    for file_path in job_dir.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTS_SET:
            continue
        try:
            removed_bytes += file_path.stat().st_size
        except Exception:
            pass
        try:
            file_path.unlink()
            removed += 1
        except Exception:
            pass

    for folder_name in ("renders", "preview"):
        folder = job_dir / folder_name
        if not folder.exists() or not folder.is_dir():
            continue
        try:
            if not any(folder.iterdir()):
                folder.rmdir()
        except Exception:
            pass

    return {"removed": removed, "removed_bytes": removed_bytes}


def find_primary_render_image(job_dir: Path) -> Path | None:
    preferred_exts = IMAGE_EXTS
    files = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS_SET]
    if not files:
        return None

    ranked = []
    for f in files:
        ext = f.suffix.lower()
        priority = preferred_exts.index(ext)
        ranked.append((priority, -f.stat().st_mtime, f))
    ranked.sort(key=lambda x: (x[0], x[1]))
    best = ranked[0][2]
    return best if best.is_file() else None


def find_processing_job_dir(job_id: str) -> Path | None:
    direct = context.PROCESSING_DIR / job_id
    if direct.exists() and direct.is_dir():
        return direct
    prefix = f"{job_id}__"
    for entry in context.PROCESSING_DIR.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            return entry
    return None


def extract_log_highlights(log_path: Path, limit: int = 3) -> list[str]:
    if not log_path.exists():
        return []
    highlights = []
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                lowered = line.lower()
                if "error" in lowered or "warning" in lowered or "exception" in lowered:
                    text = line.strip()
                    if text:
                        highlights.append(text)
        if len(highlights) > limit:
            highlights = highlights[-limit:]
    except Exception:
        return []
    return highlights


def list_archived_jobs(folder: Path, default_state: str) -> list[dict]:
    jobs = []
    dirs = [d for d in folder.iterdir() if d.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for job_dir in dirs:
        job_json = read_json(job_dir / "job.json", default={})
        manifest = read_json(job_dir / "manifest.json", default={})
        state = manifest.get("state") or job_json.get("state") or default_state
        log_highlights = []
        if state == "FAILED":
            log_highlights = extract_log_highlights(job_dir / "render.log")

        jobs.append(
            {
                "id": job_json.get("id") or job_dir.name,
                "filename": job_json.get("stored_filename") or job_json.get("original_filename") or "job.blend",
                "mode": normalize_mode(job_json.get("mode")),
                "artist_render_mode": normalize_artist_render_mode(job_json.get("artist_render_mode")),
                "state": state,
                "created_at": job_json.get("created_at"),
                "started_at": job_json.get("started_at"),
                "completed_at": manifest.get("completed_at") or job_json.get("completed_at"),
                "reason": manifest.get("reason"),
                "retry_of": job_json.get("retry_of"),
                "root_id": job_json.get("root_id"),
                "attempt": job_json.get("attempt", 1),
                "turbo_settings": job_json.get("turbo_settings"),
                "hiprt_used": job_json.get("hiprt_used"),
                "log_highlights": log_highlights,
                "has_image": has_render_images(job_dir),
            }
        )

    return jobs


def enqueue_auto_retry(failed_dir: Path, job_meta: dict, new_job_id: str) -> str:
    source_blend = failed_dir / "job.blend"
    if not source_blend.exists():
        raise FileNotFoundError("Original .blend fehlt für Auto-Retry")

    base_name = job_meta.get("stored_filename") or job_meta.get("original_filename") or f"{new_job_id}.blend"
    if not str(base_name).lower().endswith(".blend"):
        base_name = f"{base_name}.blend"

    new_filename = unique_filename(base_name)
    queued_blend = context.UPLOAD_DIR / new_filename
    shutil.copy2(source_blend, queued_blend)

    attempt = int(job_meta.get("attempt") or 1) + 1
    root_id = job_meta.get("root_id") or job_meta.get("id") or new_job_id

    new_meta = {
        "id": new_job_id,
        "original_filename": job_meta.get("original_filename") or base_name,
        "stored_filename": new_filename,
        "mode": normalize_mode(job_meta.get("mode")),
        "artist_render_mode": normalize_artist_render_mode(job_meta.get("artist_render_mode")),
        "created_at": now_iso(),
        "state": "QUEUED",
        "queued_at": now_iso(),
        "retry_of": job_meta.get("id"),
        "root_id": root_id,
        "attempt": attempt,
        "note": job_meta.get("note", ""),
        "auto_retry_of": job_meta.get("id"),
        "queue_priority": now_ms(),
        "turbo_settings_override": {"use_hiprt": False},
    }
    write_json(sidecar_for_blend(queued_blend), new_meta)
    return new_job_id


def build_history_for_job(job_id: str) -> dict | None:
    job_dir, state = find_job_archive(job_id)
    if not job_dir:
        return None

    job_meta = read_json(job_dir / "job.json", default={})
    root_id = job_meta.get("root_id") or job_meta.get("retry_of") or job_meta.get("id") or job_id
    attempts = []
    for base_dir, base_state in [(context.OUTPUT_DIR, "DONE"), (context.FAILED_DIR, "FAILED")]:
        for entry in base_dir.iterdir():
            if not entry.is_dir():
                continue
            meta = read_json(entry / "job.json", default={})
            if not meta:
                continue
            if (meta.get("root_id") or meta.get("retry_of") or meta.get("id")) != root_id:
                continue
            manifest = read_json(entry / "manifest.json", default={})
            attempts.append(
                {
                    "id": meta.get("id") or entry.name,
                    "state": manifest.get("state") or meta.get("state") or base_state,
                    "attempt": meta.get("attempt", 1),
                    "mode": normalize_mode(meta.get("mode")),
                    "started_at": meta.get("started_at"),
                    "completed_at": manifest.get("completed_at") or meta.get("completed_at"),
                    "reason": manifest.get("reason"),
                    "hiprt_used": meta.get("hiprt_used"),
                }
            )

    attempts.sort(key=lambda x: x.get("attempt", 1))
    return {"job_id": job_id, "root_id": root_id, "attempts": attempts}
