# Codex Agent Instructions

This file provides project-specific guidance for Codex (and other coding agents).

## Project Summary
- Local LAN render farm UI for Blender.
- Backend: Flask (`app.py`).
- Frontend: single HTML (`templates/index.html`).
- Render runner: shell wrapper (`scripts/render_job.sh`) + Blender Python (`turbo_optimize.py`).

## Key Architecture
- Filesystem is the database:
  - `input/` holds queued `.blend` + sidecar `.json`.
  - `processing/<job_id>/` holds active job artifacts.
  - `output/<job_id>/` holds successful jobs.
  - `failed/<job_id>/` holds failed/canceled jobs.
- Worker reads queue and launches Blender via shell script.
- Turbo settings are configurable via UI and persisted in `config.json`.

## Runtime Rules & Safety
- **Simplify subdivision render must never exceed 5** (HIP crash risk).
- Turbo mode enables HIP-RT; Artist mode disables HIP-RT.
- Single-frame rendering only (no animation render).

## Key Files
- `app.py`: Flask API, worker loop, job lifecycle, config endpoints.
- `templates/index.html`: UI and client JS.
- `scripts/render_job.sh`: shell wrapper (calls Blender).
- `turbo_optimize.py`: Blender Python hooks and render settings.
- `config.json`: runtime config + `TURBO_SETTINGS`.

## Common Tasks
- UI changes: update `templates/index.html` only (no build step).
- Backend changes: update `app.py` and restart Flask.
- Render pipeline changes: update `scripts/render_job.sh` and/or `turbo_optimize.py`.

## Testing
- Quick sanity: `python3 -m py_compile app.py turbo_optimize.py`
- UI testing: manual browser refresh on LAN.

## Style Notes
- Keep UI simple, readable, and LAN-friendly.
- Avoid large dependencies or build pipelines.
