import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import context
import jobs
from turbo_settings import build_turbo_settings_for_job
from utils import now_iso, read_json, write_json


def parse_blender_output(line: str) -> dict:
    payload = {}

    frame_match = re.search(r"Fra:\s*(\d+)", line)
    if frame_match:
        frame = int(frame_match.group(1))
        payload["frame"] = frame

    sample_match = re.search(r"Sample\s+(\d+)/(\d+)", line)
    if sample_match:
        current, total = sample_match.groups()
        current_int = int(current)
        total_int = max(int(total), 1)
        percentage = int((current_int / total_int) * 100)
        payload["sample_current"] = current_int
        payload["sample_total"] = total_int
        payload["progress"] = max(0, min(percentage, 100))
        frame_label = f"Frame {payload['frame']} | " if "frame" in payload else ""
        payload["message"] = f"{frame_label}Sample {current_int}/{total_int}"
    elif "frame" in payload:
        payload["message"] = f"Rendering Frame {payload['frame']}"

    tile_match = re.search(r"\[TILES\]\s*(\d+)\s*/\s*(\d+)", line)
    if not tile_match:
        tile_match = re.search(r"Rendered\s+(\d+)\s*/\s*(\d+)\s*Tiles", line, re.IGNORECASE)
    if not tile_match:
        tile_match = re.search(r"Tile\s+(\d+)\s*/\s*(\d+)", line, re.IGNORECASE)
    if tile_match:
        current, total = tile_match.groups()
        current_int = int(current)
        total_int = max(int(total), 1)
        payload["tile_current"] = current_int
        payload["tile_total"] = total_int

    remaining_match = re.search(r"Remaining:\s*([0-9:.]+)", line)
    if remaining_match:
        payload["remaining"] = remaining_match.group(1)

    return payload


def render_command(
    blend_path: Path,
    processing_job_dir: Path,
    mode: str,
    turbo_settings: dict | None,
    artist_use_hiprt: bool | None,
    artist_simplify_subdiv: int | None,
    artist_render_mode: str | None,
) -> tuple[list[str], dict]:
    render_script = context.resolve_path(context.CONFIG["RENDER_SCRIPT"])
    optimize_script = context.resolve_path(context.CONFIG["OPTIMIZE_SCRIPT"])
    turbo_settings = turbo_settings or build_turbo_settings_for_job({})

    env = os.environ.copy()
    env["HSA_XNACK"] = str(context.CONFIG.get("HSA_XNACK", "1"))
    normalized_mode = jobs.normalize_mode(mode)
    use_hiprt = False
    if normalized_mode == "TURBO":
        use_hiprt = bool(turbo_settings.get("use_hiprt"))
    else:
        use_hiprt = bool(artist_use_hiprt)
    env["USE_HIPRT"] = "1" if use_hiprt else "0"
    env["RENDER_GPU_NAME"] = str(context.CONFIG.get("RENDER_GPU_NAME", "Radeon"))
    env["ARTIST_SIMPLIFY_SUBDIV"] = str(artist_simplify_subdiv or 0)
    env["RENDER_MODE"] = normalized_mode
    env["RENDER_PIPELINE"] = jobs.normalize_artist_render_mode(artist_render_mode)
    env["BLENDER_BIN"] = str(context.CONFIG.get("BLENDER_BIN", "blender"))
    env["OPTIMIZE_SCRIPT"] = str(optimize_script)
    env["RENDER_OUTPUT_DIR"] = str(processing_job_dir / "renders")
    env["RENDER_PREVIEW_DIR"] = str(processing_job_dir / "preview")
    env["RENDER_PREVIEW_INTERVAL"] = "5"
    env["RENDER_PREVIEW_WIDTH"] = "640"
    env["TURBO_SETTINGS_JSON"] = json.dumps(turbo_settings, ensure_ascii=True)

    cmd = [
        str(render_script),
        "--blend",
        str(blend_path),
        "--job-dir",
        str(processing_job_dir),
        "--mode",
        mode,
    ]
    return cmd, env


def worker_loop():
    print("👷 Worker gestartet und wartet auf Arbeit...")

    poll_interval = max(float(context.CONFIG.get("POLL_INTERVAL_SECONDS", 2)), 0.5)

    while True:
        queue = jobs.get_render_queue()
        if not queue:
            context.update_current_job(
                status="IDLE",
                id=None,
                filename=None,
                mode="TURBO",
                progress=0,
                sample_current=None,
                sample_total=None,
                tile_current=None,
                tile_total=None,
                frame=None,
                remaining=None,
                message="Warte auf Start...",
                started_at=None,
            )
            time.sleep(poll_interval)
            continue

        queued_job = queue[0]
        job_id = queued_job["id"]
        filename = queued_job["filename"]
        mode = queued_job["mode"]

        blend_in_queue = context.UPLOAD_DIR / filename
        sidecar_in_queue = jobs.sidecar_for_blend(blend_in_queue)
        if not blend_in_queue.exists():
            time.sleep(0.1)
            continue

        processing_job_dir = context.PROCESSING_DIR / job_id
        if processing_job_dir.exists():
            processing_job_dir = context.PROCESSING_DIR / f"{job_id}__{datetime.now():%Y%m%d-%H%M%S}"
        processing_job_dir.mkdir(parents=True, exist_ok=True)

        processing_blend = processing_job_dir / "job.blend"
        processing_json = processing_job_dir / "job.json"
        processing_log = processing_job_dir / "render.log"
        processing_state = processing_job_dir / "state.json"

        try:
            shutil.move(str(blend_in_queue), processing_blend)
            if sidecar_in_queue.exists():
                shutil.move(str(sidecar_in_queue), processing_json)
            else:
                write_payload = {
                    "id": job_id,
                    "original_filename": filename,
                    "stored_filename": filename,
                    "mode": mode,
                    "created_at": now_iso(),
                    "state": "QUEUED",
                }
                write_json(processing_json, write_payload)
        except Exception as exc:
            print(f"❌ Konnte Job nicht in processing verschieben: {exc}")
            time.sleep(1)
            continue

        job_meta = read_json(processing_json, default={})
        job_meta["id"] = job_id
        job_meta["mode"] = mode
        job_meta["artist_render_mode"] = jobs.normalize_artist_render_mode(job_meta.get("artist_render_mode"))
        job_meta["stored_filename"] = filename
        job_meta["state"] = "RENDERING"
        job_meta["started_at"] = now_iso()
        write_json(processing_json, job_meta)
        write_json(
            processing_state,
            {
                "state": "RENDERING",
                "message": "Render gestartet",
                "updated_at": now_iso(),
            },
        )

        context.update_current_job(
            status="RENDERING",
            id=job_id,
            filename=filename,
            mode=mode,
            progress=0,
            sample_current=None,
            sample_total=None,
            tile_current=None,
            tile_total=None,
            frame=None,
            remaining=None,
            message="Render gestartet...",
            started_at=job_meta["started_at"],
        )
        context.stop_event.clear()

        print(f"🚀 Starte Render für {filename} ({mode})")

        turbo_settings_used = None
        if jobs.normalize_mode(mode) == "TURBO":
            turbo_settings_used = build_turbo_settings_for_job(job_meta)
            job_meta["turbo_settings"] = turbo_settings_used
            job_meta["hiprt_used"] = bool(turbo_settings_used.get("use_hiprt"))
        else:
            job_meta["turbo_settings"] = None
            job_meta["hiprt_used"] = bool(job_meta.get("artist_use_hiprt"))
        write_json(processing_json, job_meta)

        cmd, env = render_command(
            processing_blend,
            processing_job_dir,
            mode,
            turbo_settings_used,
            job_meta.get("artist_use_hiprt"),
            job_meta.get("artist_simplify_subdiv"),
            job_meta.get("artist_render_mode"),
        )

        returncode = -1
        reason = "Blender Fehler"

        try:
            with processing_log.open("a", encoding="utf-8") as log_handle:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    cwd=str(context.BASE_DIR),
                    env=env,
                )
                with context.state_lock:
                    context.active_process = process

                for line in process.stdout:
                    log_handle.write(line)
                    log_handle.flush()

                    if context.stop_event.is_set():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        break

                    payload = parse_blender_output(line)
                    if payload:
                        context.update_current_job(**payload)

                returncode = process.wait()

        except Exception as exc:
            reason = f"Worker Ausnahme: {exc}"
            print(f"❌ {reason}")
            try:
                with processing_log.open("a", encoding="utf-8") as log_handle:
                    log_handle.write(f"\n[worker-error] {exc}\n")
            except Exception:
                pass
        finally:
            with context.state_lock:
                context.active_process = None

        if context.stop_event.is_set():
            state = "CANCELED"
            reason = "Vom Benutzer abgebrochen"
        elif returncode == 0:
            state = "DONE"
            reason = "Render fertig"
        else:
            state = "FAILED"
            reason = f"Render fehlgeschlagen (rc={returncode})"

        auto_retry_job_id = None
        if (
            state == "FAILED"
            and jobs.normalize_mode(mode) == "TURBO"
            and job_meta.get("hiprt_used")
            and not job_meta.get("auto_retry_of")
            and not job_meta.get("auto_retry_done")
        ):
            auto_retry_job_id = jobs.make_job_id()
            job_meta["auto_retry_done"] = True
            job_meta["auto_retry_job_id"] = auto_retry_job_id

        try:
            target_dir = jobs.finalize_job(processing_job_dir, job_meta, state, reason, returncode)
        except Exception as exc:
            print(f"❌ Fehler beim Finalisieren von {job_id}: {exc}")
            target_dir = None

        if state == "FAILED" and auto_retry_job_id and target_dir:
            try:
                jobs.enqueue_auto_retry(target_dir, job_meta, auto_retry_job_id)
            except Exception as exc:
                print(f"❌ Auto-Retry fehlgeschlagen: {exc}")

        context.update_current_job(
            status="IDLE" if state == "DONE" else "ERROR",
            id=None,
            filename=None,
            mode=mode,
            progress=0,
            sample_current=None,
            sample_total=None,
            tile_current=None,
            tile_total=None,
            frame=None,
            remaining=None,
            message=reason,
            started_at=None,
        )

        context.stop_event.clear()
        time.sleep(0.2)
