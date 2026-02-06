import atexit
import os
import shutil
import subprocess
import sys
import threading

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import context
from jobs import (
    build_history_for_job,
    find_input_job,
    find_job_archive,
    find_primary_render_image,
    find_processing_job_dir,
    get_queue,
    is_allowed_blend,
    is_valid_job_id,
    delete_render_images,
    list_archived_jobs,
    list_artifacts,
    make_job_id,
    normalize_mode,
    safe_job_dir,
    sidecar_for_blend,
    unique_filename,
    ensure_within,
)
from turbo_settings import DEFAULT_TURBO_SETTINGS, get_turbo_settings, validate_turbo_settings
from utils import now_iso, now_ms, read_json, write_json
from worker import worker_loop
import jobs

app = Flask(__name__)

SLEEP_INHIBITOR_PROCESS = None


def start_sleep_inhibitor():
    global SLEEP_INHIBITOR_PROCESS
    if SLEEP_INHIBITOR_PROCESS is not None:
        return
    if not context.CONFIG.get("INHIBIT_SLEEP", True):
        print("Sleep inhibit disabled by config.")
        return
    env_override = os.environ.get("WG_RENDERFARM_INHIBIT_SLEEP", "").strip().lower()
    if env_override in {"0", "false", "no"}:
        print("Sleep inhibit disabled by WG_RENDERFARM_INHIBIT_SLEEP.")
        return

    inhibitor = shutil.which("systemd-inhibit")
    if not inhibitor:
        print("systemd-inhibit not found; sleep may still occur.")
        return

    cmd = [
        inhibitor,
        "--what=sleep",
        "--mode=block",
        "--who=wg-renderfarm",
        "--why=wg-renderfarm server running",
        sys.executable,
        "-c",
        "import time; time.sleep(10**9)",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"Failed to start sleep inhibitor: {exc}")
        return

    SLEEP_INHIBITOR_PROCESS = proc
    print("Sleep inhibit active (systemd-inhibit).")

    def _cleanup():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    atexit.register(_cleanup)


def save_runtime_config() -> None:
    with context.config_lock:
        write_json(context.CONFIG_PATH, context.CONFIG)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    return jsonify({"job": context.current_job_snapshot(), "queue": get_queue()})


@app.route("/config/turbo", methods=["GET"])
def get_turbo_config():
    return jsonify({"settings": get_turbo_settings(), "defaults": dict(DEFAULT_TURBO_SETTINGS)})


@app.route("/config/turbo", methods=["POST"])
def set_turbo_config():
    payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    incoming = payload.get("settings") if isinstance(payload, dict) and isinstance(payload.get("settings"), dict) else payload
    settings = validate_turbo_settings(incoming if isinstance(incoming, dict) else {})
    context.CONFIG["TURBO_SETTINGS"] = settings
    save_runtime_config()
    return jsonify({"ok": True, "settings": settings})


@app.route("/config/turbo/reset", methods=["POST"])
def reset_turbo_config():
    settings = validate_turbo_settings(dict(DEFAULT_TURBO_SETTINGS))
    context.CONFIG["TURBO_SETTINGS"] = settings
    save_runtime_config()
    return jsonify({"ok": True, "settings": settings})


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Keine Datei"}), 400

    file = request.files["file"]
    raw_name = secure_filename(file.filename or "")
    if not raw_name:
        return jsonify({"error": "Leerer Dateiname"}), 400

    if not is_allowed_blend(raw_name):
        return jsonify({"error": "Nur .blend Dateien sind erlaubt"}), 400

    mode = normalize_mode(request.form.get("mode", "artist"))
    stored_name = unique_filename(raw_name)

    blend_path = context.UPLOAD_DIR / stored_name
    file.save(str(blend_path))

    metadata = {
        "id": make_job_id(),
        "original_filename": raw_name,
        "stored_filename": stored_name,
        "mode": mode,
        "created_at": now_iso(),
        "state": "PENDING",
        "queued_at": None,
    }
    write_json(sidecar_for_blend(blend_path), metadata)

    return jsonify(
        {
            "job_id": metadata["id"],
            "filename": stored_name,
            "mode": metadata["mode"],
            "state": metadata["state"],
        }
    )


@app.route("/jobs/<job_id>/mode", methods=["POST"])
def set_job_mode(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    metadata, blend_path, json_path = find_input_job(job_id)
    if not metadata or not blend_path or not json_path:
        return jsonify({"error": "Job nicht gefunden"}), 404

    payload = request.get_json(silent=True) if request.is_json else None
    raw_mode = payload.get("mode") if isinstance(payload, dict) else request.form.get("mode")
    mode = normalize_mode(raw_mode)
    metadata["mode"] = mode
    write_json(json_path, metadata)
    return jsonify({"ok": True, "job_id": job_id, "mode": mode, "state": metadata.get("state", "PENDING")})


@app.route("/jobs/<job_id>/note", methods=["POST"])
def set_job_note(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    metadata, blend_path, json_path = find_input_job(job_id)
    if not metadata or not blend_path or not json_path:
        return jsonify({"error": "Job nicht gefunden"}), 404

    payload = request.get_json(silent=True) if request.is_json else None
    note = payload.get("note") if isinstance(payload, dict) else request.form.get("note")
    metadata["note"] = str(note or "")
    write_json(json_path, metadata)
    return jsonify({"ok": True, "job_id": job_id, "note": metadata["note"]})


@app.route("/jobs/<job_id>/queue/move", methods=["POST"])
def move_queue_job(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    payload = request.get_json(silent=True) if request.is_json else None
    direction = (payload.get("direction") if isinstance(payload, dict) else request.form.get("direction")) or "up"
    direction = str(direction).lower()
    if direction not in {"up", "down", "top"}:
        return jsonify({"error": "Ungültige Richtung"}), 400

    queued = []
    for item in get_queue():
        if item.get("state") != "QUEUED":
            continue
        meta, blend_path, json_path = find_input_job(item["id"])
        if not meta or not json_path:
            continue
        priority = meta.get("queue_priority")
        queued.append(
            {
                "id": item["id"],
                "priority": priority if priority is not None else float("inf"),
                "queued_at": item.get("queued_at") or item.get("created_at") or "",
                "meta": meta,
                "json_path": json_path,
            }
        )

    queued.sort(key=lambda x: (x["priority"], x["queued_at"]))
    ids = [q["id"] for q in queued]
    if job_id not in ids:
        return jsonify({"error": "Job nicht in Queue"}), 404

    idx = ids.index(job_id)
    if direction == "up":
        target = max(0, idx - 1)
    elif direction == "down":
        target = min(len(queued) - 1, idx + 1)
    else:
        target = 0

    if idx != target:
        item = queued.pop(idx)
        queued.insert(target, item)

    for order, entry in enumerate(queued):
        entry["meta"]["queue_priority"] = order
        write_json(entry["json_path"], entry["meta"])

    return jsonify({"ok": True, "job_id": job_id, "direction": direction})


@app.route("/queue/reorder", methods=["POST"])
def reorder_queue():
    payload = request.get_json(silent=True) if request.is_json else None
    order = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(order, list) or not order:
        return jsonify({"error": "Ungültige Reihenfolge"}), 400

    queue_items = get_queue()
    id_to_item = {item["id"]: item for item in queue_items}

    normalized = []
    for job_id in order:
        job_id = str(job_id)
        if job_id in id_to_item and job_id not in normalized:
            normalized.append(job_id)

    for item in queue_items:
        if item["id"] not in normalized:
            normalized.append(item["id"])

    for idx, job_id in enumerate(normalized):
        item = id_to_item.get(job_id)
        if not item:
            continue
        blend_path = context.UPLOAD_DIR / item["filename"]
        json_path = sidecar_for_blend(blend_path)
        meta = read_json(json_path, default={})
        if not meta:
            meta = jobs.ensure_queue_sidecar(blend_path)
        meta["queue_priority"] = idx
        write_json(json_path, meta)

    return jsonify({"ok": True, "count": len(normalized)})


@app.route("/queue/start-all", methods=["POST"])
def start_all_jobs():
    queued = [item for item in get_queue() if item.get("state") == "QUEUED"]
    max_priority = max((item.get("queue_priority") or 0 for item in queued), default=0)

    started = 0
    for item in get_queue():
        if item.get("state") != "PENDING":
            continue
        metadata, blend_path, json_path = find_input_job(item["id"])
        if not metadata or not json_path:
            continue
        metadata["state"] = "QUEUED"
        metadata["queued_at"] = now_iso()
        max_priority += 1
        metadata["queue_priority"] = max_priority
        write_json(json_path, metadata)
        started += 1

    return jsonify({"ok": True, "queued": started})


@app.route("/queue/clear", methods=["POST"])
def clear_queue():
    payload = request.get_json(silent=True) if request.is_json else None
    cancel_active = True
    if isinstance(payload, dict) and "cancel_active" in payload:
        cancel_active = bool(payload.get("cancel_active"))

    canceled = False
    if cancel_active:
        snapshot = context.current_job_snapshot()
        if snapshot["status"] == "RENDERING":
            context.stop_event.set()
            with context.state_lock:
                process = context.active_process
            if process and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
            canceled = True

    removed = 0
    for item in get_queue():
        blend_path = context.UPLOAD_DIR / item["filename"]
        json_path = sidecar_for_blend(blend_path)
        if blend_path.exists():
            blend_path.unlink()
            removed += 1
        if json_path.exists():
            json_path.unlink()
    return jsonify({"ok": True, "removed": removed, "canceled": canceled})


@app.route("/jobs/<job_id>/start", methods=["POST"])
def start_job(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    snapshot = context.current_job_snapshot()
    if snapshot["status"] == "RENDERING" and snapshot["id"] == job_id:
        return jsonify({"error": "Job läuft bereits"}), 409

    metadata, blend_path, json_path = find_input_job(job_id)
    if not metadata or not blend_path or not json_path:
        return jsonify({"error": "Job nicht gefunden"}), 404

    current_state = str(metadata.get("state") or "PENDING").upper()
    if current_state == "QUEUED":
        return jsonify({"ok": True, "job_id": job_id, "state": "QUEUED", "mode": normalize_mode(metadata.get("mode"))})
    if current_state != "PENDING":
        return jsonify({"error": f"Job-Status {current_state} kann nicht gestartet werden"}), 400

    payload = request.get_json(silent=True) if request.is_json else None
    raw_mode = payload.get("mode") if isinstance(payload, dict) else request.form.get("mode")
    if raw_mode:
        metadata["mode"] = normalize_mode(raw_mode)
    else:
        metadata["mode"] = normalize_mode(metadata.get("mode"))

    if not metadata.get("root_id"):
        metadata["root_id"] = metadata.get("id") or job_id
    if not metadata.get("attempt"):
        metadata["attempt"] = 1
    if metadata.get("note") is None:
        metadata["note"] = ""

    metadata["state"] = "QUEUED"
    metadata["queued_at"] = now_iso()
    if metadata.get("queue_priority") is None:
        metadata["queue_priority"] = now_ms()
    write_json(json_path, metadata)
    return jsonify({"ok": True, "job_id": job_id, "state": "QUEUED", "mode": metadata["mode"]})


@app.route("/cancel", methods=["POST"])
def cancel():
    snapshot = context.current_job_snapshot()
    if snapshot["status"] == "RENDERING":
        context.stop_event.set()
        with context.state_lock:
            process = context.active_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        return jsonify({"ok": True, "message": "Abbruch gesendet"})
    return jsonify({"ok": False, "error": "Kein aktiver Render"}), 400


@app.route("/delete/<job_id>", methods=["POST"])
def delete_job(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    snapshot = context.current_job_snapshot()
    if snapshot["status"] == "RENDERING" and snapshot["id"] == job_id:
        return jsonify({"error": "Aktiver Job kann nicht gelöscht werden. Nutze Cancel."}), 409

    target = None
    for item in get_queue():
        if item["id"] == job_id:
            target = item
            break

    if not target:
        return jsonify({"error": "Job nicht in Queue gefunden"}), 404

    blend_path = context.UPLOAD_DIR / target["filename"]
    json_path = sidecar_for_blend(blend_path)

    if blend_path.exists():
        blend_path.unlink()
    if json_path.exists():
        json_path.unlink()

    return jsonify({"ok": True, "deleted": job_id})


@app.route("/jobs/finished")
def jobs_finished():
    return jsonify({"jobs": list_archived_jobs(context.OUTPUT_DIR, "DONE")})


@app.route("/jobs/failed")
def jobs_failed():
    return jsonify({"jobs": list_archived_jobs(context.FAILED_DIR, "FAILED")})


@app.route("/jobs/<job_id>/delete-images", methods=["POST"])
def delete_job_images(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    job_dir = safe_job_dir(context.OUTPUT_DIR, job_id)
    if not job_dir or not job_dir.exists() or not job_dir.is_dir():
        return jsonify({"error": "Fertiger Job nicht gefunden"}), 404

    result = delete_render_images(job_dir)
    return jsonify({"ok": True, **result})


@app.route("/jobs/failed/retry-all", methods=["POST"])
def retry_all_failed():
    retried = 0
    for entry in context.FAILED_DIR.iterdir():
        if not entry.is_dir():
            continue
        job_meta = read_json(entry / "job.json", default={})
        source_blend = entry / "job.blend"
        if not source_blend.exists():
            continue
        base_name = job_meta.get("stored_filename") or job_meta.get("original_filename") or f"{entry.name}.blend"
        if not str(base_name).lower().endswith(".blend"):
            base_name = f"{base_name}.blend"
        new_filename = unique_filename(base_name)
        queued_blend = context.UPLOAD_DIR / new_filename
        shutil.copy2(source_blend, queued_blend)

        attempt = int(job_meta.get("attempt") or 1) + 1
        root_id = job_meta.get("root_id") or job_meta.get("id") or entry.name
        new_meta = {
            "id": make_job_id(),
            "original_filename": job_meta.get("original_filename") or base_name,
            "stored_filename": new_filename,
            "mode": normalize_mode(job_meta.get("mode")),
            "created_at": now_iso(),
            "state": "QUEUED",
            "queued_at": now_iso(),
            "retry_of": job_meta.get("id") or entry.name,
            "root_id": root_id,
            "attempt": attempt,
            "note": job_meta.get("note", ""),
            "queue_priority": now_ms(),
        }
        write_json(sidecar_for_blend(queued_blend), new_meta)
        retried += 1

    return jsonify({"ok": True, "queued": retried})


@app.route("/jobs/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    failed_job_dir = safe_job_dir(context.FAILED_DIR, job_id)
    if not failed_job_dir or not failed_job_dir.exists() or not failed_job_dir.is_dir():
        return jsonify({"error": "Fehlgeschlagener Job nicht gefunden"}), 404

    job_meta = read_json(failed_job_dir / "job.json", default={})
    source_blend = failed_job_dir / "job.blend"
    if not source_blend.exists():
        return jsonify({"error": "Original .blend fehlt im Failed-Job"}), 400

    base_name = job_meta.get("stored_filename") or job_meta.get("original_filename") or f"{job_id}.blend"
    if not str(base_name).lower().endswith(".blend"):
        base_name = f"{base_name}.blend"

    new_filename = unique_filename(base_name)
    queued_blend = context.UPLOAD_DIR / new_filename
    shutil.copy2(source_blend, queued_blend)

    attempt = int(job_meta.get("attempt") or 1) + 1
    root_id = job_meta.get("root_id") or job_id

    new_meta = {
        "id": make_job_id(),
        "original_filename": job_meta.get("original_filename") or base_name,
        "stored_filename": new_filename,
        "mode": normalize_mode(job_meta.get("mode")),
        "created_at": now_iso(),
        "state": "QUEUED",
        "retry_of": job_id,
        "root_id": root_id,
        "attempt": attempt,
        "note": job_meta.get("note", ""),
        "queued_at": now_iso(),
        "queue_priority": now_ms(),
    }
    write_json(sidecar_for_blend(queued_blend), new_meta)

    return jsonify(
        {
            "ok": True,
            "job_id": new_meta["id"],
            "filename": new_filename,
            "mode": new_meta["mode"],
        }
    )


@app.route("/jobs/<job_id>/artifacts")
def job_artifacts(job_id):
    job_dir, archive_state = find_job_archive(job_id)
    if not job_dir:
        return jsonify({"error": "Job nicht gefunden"}), 404

    return jsonify(
        {
            "job_id": job_id,
            "state": archive_state,
            "artifacts": list_artifacts(job_dir, job_id),
        }
    )


@app.route("/jobs/<job_id>/history")
def job_history(job_id):
    history = build_history_for_job(job_id)
    if not history:
        return jsonify({"error": "Job nicht gefunden"}), 404
    return jsonify(history)


@app.route("/jobs/<job_id>/download/<path:artifact>")
def job_download(job_id, artifact):
    job_dir, _ = find_job_archive(job_id)
    if not job_dir:
        return jsonify({"error": "Job nicht gefunden"}), 404

    target = (job_dir / artifact).resolve()
    if not ensure_within(job_dir, target) or not target.exists() or not target.is_file():
        return jsonify({"error": "Ungültiger Artifact-Pfad"}), 404

    return send_file(str(target), as_attachment=True)


@app.route("/jobs/<job_id>/download-image")
def job_download_image(job_id):
    job_dir, _ = find_job_archive(job_id)
    if not job_dir:
        return jsonify({"error": "Job nicht gefunden"}), 404

    target = find_primary_render_image(job_dir)
    if not target:
        return jsonify({"error": "Kein Render-Bild gefunden"}), 404
    if not ensure_within(job_dir, target.resolve()):
        return jsonify({"error": "Ungültiger Pfad"}), 404

    return send_file(str(target), as_attachment=True)


@app.route("/jobs/<job_id>/preview-image")
def job_preview_image(job_id):
    job_dir, _ = find_job_archive(job_id)
    if not job_dir:
        return jsonify({"error": "Job nicht gefunden"}), 404

    target = find_primary_render_image(job_dir)
    if not target:
        return jsonify({"error": "Kein Render-Bild gefunden"}), 404
    if not ensure_within(job_dir, target.resolve()):
        return jsonify({"error": "Ungültiger Pfad"}), 404

    return send_file(str(target), as_attachment=False)


@app.route("/jobs/<job_id>/preview-live")
def job_preview_live(job_id):
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Ungültige Job-ID"}), 400

    job_dir = find_processing_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Kein aktiver Render"}), 404

    preview_dir = job_dir / "preview"
    if not preview_dir.exists():
        return jsonify({"error": "Kein Preview vorhanden"}), 404

    target = preview_dir / "preview.png"
    if not target.exists():
        pngs = sorted(preview_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not pngs:
            return jsonify({"error": "Kein Preview vorhanden"}), 404
        target = pngs[0]

    if not ensure_within(job_dir, target.resolve()):
        return jsonify({"error": "Ungültiger Pfad"}), 404

    response = send_file(str(target), as_attachment=False)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/preview-live")
def preview_live_current():
    snapshot = context.current_job_snapshot()
    job_id = snapshot.get("id")
    if snapshot.get("status") != "RENDERING" or not job_id:
        return jsonify({"error": "Kein aktiver Render"}), 404
    return job_preview_live(job_id)


start_sleep_inhibitor()
jobs.recover_processing_jobs()
threading.Thread(target=worker_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(
        host=str(context.CONFIG.get("HOST", "0.0.0.0")),
        port=int(context.CONFIG.get("PORT", 5000)),
        debug=False,
    )
