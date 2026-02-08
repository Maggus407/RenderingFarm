# RenderingFarm 100% vibe coded!!!

## Important
- Only use this project on a trusted local network.
- Do not expose it directly to the public internet.

## What This Is
Ive startet it as a side project to share my GPU (AMD 7900 XTX) for Blender rendering for a colleague.

Through the web interface, you can upload `.blend` files.
They are rendered on the machine where this program is running.

## Features
- Local web UI for submitting Blender jobs
- Queue-based single-frame rendering
- Turbo mode and Artist mode
- Basic job history (`output/`, `failed/`, `processing/`, `input/` folders)

## Requirements
- Linux machine (tested on Arch Linux)
- Python 3.10+
- Blender installed and available in `PATH` as `blender`
- AMD GPU + ROCm/HIP setup (for HIP/HIP-RT workflow)

Python packages:
- `flask`
- `werkzeug`

## Installation
1. Clone the repository:
```bash
git clone https://github.com/Maggus407/RenderingFarm.git
cd RenderingFarm
```

2. (Optional but recommended) Create a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install Python dependencies:
```bash
pip install flask werkzeug
```

4. Make sure Blender works:
```bash
blender --version
```

## Configuration
Main configuration file:
- `config.json`

Important defaults:
- Host: `0.0.0.0`
- Port: `5000`
- Blender binary: `blender`

If needed, edit `config.json` before starting.

## Start
Run the server:
```bash
python3 app.py
```

Open in browser:
- On the same machine: `http://127.0.0.1:5000`
- On your LAN: `http://<SERVER_LOCAL_IP>:5000`

## Usage
1. Open the web UI.
2. Upload a `.blend` file.
3. Choose render mode (Artist or Turbo).
4. Start/queue the job.
5. Download output from finished jobs.

## Project Structure
- `backend/` - Flask backend, worker, job handling, Blender optimization script
- `templates/` - UI templates
- `scripts/render_job.sh` - Blender launch wrapper
- `config.json` - Runtime config

## Safety Notes
- This is intended for LAN use only.
- There is no authentication/authorization layer by default.
- Keep firewall/router rules strict.

## Quick Sanity Check
```bash
python3 -m py_compile app.py backend/*.py
```

## License
MIT
