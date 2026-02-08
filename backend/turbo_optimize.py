import json
import os
import re
import time

import bpy


def setup_gpu():
    """Enable GPU rendering for Cycles if available."""
    print("[SETUP] Looking for GPU devices...")
    prefs = bpy.context.preferences
    if "cycles" not in prefs.addons:
        print("[SETUP] Cycles addon not available")
        return

    cycles_prefs = prefs.addons["cycles"].preferences
    device_type = os.environ.get("CYCLES_DEVICE_TYPE", "HIP").upper()

    try:
        cycles_prefs.compute_device_type = device_type
    except Exception as exc:
        print(f"[SETUP] Could not set device type {device_type}: {exc}")

    # HIP-RT can be unstable on some scenes/drivers: make it configurable.
    use_hiprt = os.environ.get("USE_HIPRT", "0").strip().lower() in {"1", "true", "yes", "on"}
    try:
        cycles_prefs.use_hiprt = use_hiprt
        print(f"[SETUP] HIP-RT {'enabled' if use_hiprt else 'disabled'}")
    except Exception:
        print("[SETUP] HIP-RT setting not available on this Blender build")

    try:
        cycles_prefs.get_devices()
    except Exception:
        pass

    preferred_gpu = os.environ.get("RENDER_GPU_NAME", "Radeon").strip().lower()
    enabled_count = 0
    for device in getattr(cycles_prefs, "devices", []):
        # Some Blender/HIP builds expose CPU-like HIP devices. Keep only real discrete GPU devices.
        is_target_type = device.type == device_type
        is_cpu_like = "cpu" in device.name.lower() or "ryzen" in device.name.lower()
        name_matches = preferred_gpu in device.name.lower() if preferred_gpu else True
        use_it = is_target_type and (not is_cpu_like) and name_matches
        device.use = use_it
        if use_it:
            enabled_count += 1
        marker = "ENABLED" if use_it else "disabled"
        print(f"[SETUP] {marker}: {device.name} ({device.type})")

    # Fallback: if no preferred match found, enable the first non-CPU HIP device.
    if enabled_count == 0:
        for device in getattr(cycles_prefs, "devices", []):
            is_target_type = device.type == device_type
            is_cpu_like = "cpu" in device.name.lower() or "ryzen" in device.name.lower()
            if is_target_type and not is_cpu_like:
                device.use = True
                enabled_count = 1
                print(f"[SETUP] fallback enabled: {device.name} ({device.type})")
                break

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "GPU"


def force_managed_output():
    output_dir = os.environ.get("RENDER_OUTPUT_DIR")
    if not output_dir:
        return
    os.makedirs(output_dir, exist_ok=True)
    # Prefix path; Blender appends frame numbers/extension as needed.
    bpy.context.scene.render.filepath = os.path.join(output_dir, "frame_")
    print(f"[SETUP] Managed output path: {bpy.context.scene.render.filepath}")


def configure_exr(scene: bpy.types.Scene, multilayer: bool) -> None:
    try:
        image_settings = scene.render.image_settings
        image_settings.file_format = "OPEN_EXR_MULTILAYER" if multilayer else "OPEN_EXR"
        image_settings.color_depth = "16"
        if hasattr(image_settings, "exr_codec"):
            image_settings.exr_codec = "ZIP"
        if hasattr(image_settings, "color_mode"):
            image_settings.color_mode = "RGBA"
    except Exception as exc:
        print(f"[PIPELINE] Failed to set EXR settings: {exc}")


def wants_compositor_output() -> bool:
    raw = os.environ.get("RENDER_COMPOSITOR_OUTPUT", "")
    if raw is None or str(raw).strip() == "":
        return False
    raw = str(raw).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def set_compositor_enabled(scene: bpy.types.Scene, enabled: bool) -> None:
    if hasattr(scene.render, "use_compositing"):
        scene.render.use_compositing = bool(enabled)


def apply_artist_simplify_override(scene: bpy.types.Scene) -> None:
    raw = os.environ.get("ARTIST_SIMPLIFY_SUBDIV", "").strip()
    if not raw:
        return
    try:
        value = int(raw)
    except ValueError:
        return
    value = max(0, min(5, value))
    if value <= 0:
        return
    scene.render.use_simplify = True
    scene.render.simplify_subdivision_render = value
    print(f"[ARTIST] Simplify subdivision render override: {value}")


def ensure_compositor_exr(_scene=None):
    raw = os.environ.get("RENDER_PIPELINE", "NORMAL")
    pipeline = raw.strip().upper()
    if pipeline in {"COMP", "COMPOSITE", "COMPOSITOR"}:
        scene = bpy.context.scene
        if wants_compositor_output():
            scene.use_nodes = True
            set_compositor_enabled(scene, True)
        else:
            set_compositor_enabled(scene, False)


def apply_render_pipeline():
    raw = os.environ.get("RENDER_PIPELINE", "NORMAL")
    pipeline = raw.strip().upper()
    if pipeline in {"COMP", "COMPOSITE", "COMPOSITOR"}:
        pipeline = "COMPOSITOR"
    else:
        pipeline = "NORMAL"

    scene = bpy.context.scene
    if pipeline == "COMPOSITOR":
        if wants_compositor_output():
            scene.use_nodes = True
            set_compositor_enabled(scene, True)
            print("[PIPELINE] COMPOSITOR: compositor output enabled")
        else:
            set_compositor_enabled(scene, False)
            print("[PIPELINE] COMPOSITOR: compositor output disabled; using blend output settings")
    else:
        set_compositor_enabled(scene, False)
        print("[PIPELINE] NORMAL: compositor disabled")


def setup_preview_writer():
    preview_dir = os.environ.get("RENDER_PREVIEW_DIR")
    if not preview_dir:
        return

    try:
        interval = max(1.0, float(os.environ.get("RENDER_PREVIEW_INTERVAL", "5")))
    except ValueError:
        interval = 5.0

    try:
        target_width = max(64, int(os.environ.get("RENDER_PREVIEW_WIDTH", "640")))
    except ValueError:
        target_width = 640

    os.makedirs(preview_dir, exist_ok=True)
    state = {"last": 0.0}

    def write_preview():
        now = time.time()
        if now - state["last"] < interval:
            return interval
        state["last"] = now

        try:
            render_result = bpy.data.images.get("Render Result")
            if render_result is None:
                return interval

            width, height = render_result.size[0], render_result.size[1]
            if width <= 0 or height <= 0:
                return interval

            scaled_height = max(1, int((height * target_width) / width))
            preview_path = os.path.join(preview_dir, "preview.png")

            temp = render_result.copy()
            temp.scale(target_width, scaled_height)
            temp.file_format = "PNG"
            temp.filepath_raw = preview_path
            temp.save()
            bpy.data.images.remove(temp)
            print(f"[PREVIEW] updated {preview_path}")
        except Exception as exc:
            print(f"[PREVIEW] failed: {exc}")

        return interval

    try:
        bpy.app.timers.register(write_preview, first_interval=interval)
        print(f"[PREVIEW] enabled: every {interval}s at {target_width}px")
    except Exception as exc:
        print(f"[PREVIEW] timer failed: {exc}")


def setup_tile_progress_logger():
    try:
        handlers = bpy.app.handlers
    except Exception:
        return

    if not hasattr(handlers, "render_stats"):
        return

    last = {"current": None, "total": None}

    def render_stats_handler(_scene, stats):
        if not stats:
            return
        match = re.search(r"Rendered\s+(\d+)\s*/\s*(\d+)\s*Tiles", stats, re.IGNORECASE)
        if not match:
            match = re.search(r"Tile\s+(\d+)\s*/\s*(\d+)", stats, re.IGNORECASE)
        if not match:
            return
        current = int(match.group(1))
        total = max(int(match.group(2)), 1)
        if last["current"] == current and last["total"] == total:
            return
        last["current"] = current
        last["total"] = total
        print(f"[TILES] {current}/{total}")

    handlers.render_stats.append(render_stats_handler)
    print("[TILES] render_stats handler attached")


def load_turbo_settings():
    defaults = {
        "use_simplify": True,
        "simplify_subdivision_render": 4,
        "use_adaptive_sampling": True,
        "samples": 4096,
        "adaptive_threshold": 0.001,
        "use_denoising": True,
        "denoiser": "OPENIMAGEDENOISE",
        "max_bounces": 8,
        "diffuse_bounces": 2,
        "glossy_bounces": 2,
        "transmission_bounces": 4,
        "transparent_max_bounces": 4,
        "volume_bounces": 0,
        "clamp_direct": 0.0,
        "clamp_indirect": 0.0,
        "filter_glossy": 0.0,
        "caustics_reflective": False,
        "caustics_refractive": False,
        "tile_size": 1024,
        "use_persistent_data": False,
        "use_hiprt": True,
    }
    raw = os.environ.get("TURBO_SETTINGS_JSON")
    if not raw:
        return defaults

    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return defaults
    except Exception:
        return defaults

    settings = dict(defaults)
    settings.update(parsed)
    # Hard safety guard for HIP stability on this machine.
    settings["simplify_subdivision_render"] = max(0, min(5, int(settings.get("simplify_subdivision_render", 4))))
    settings["samples"] = max(1, int(settings.get("samples", 4096)))
    settings["adaptive_threshold"] = max(0.000001, float(settings.get("adaptive_threshold", 0.001)))
    settings["max_bounces"] = max(0, int(settings.get("max_bounces", 8)))
    settings["diffuse_bounces"] = max(0, int(settings.get("diffuse_bounces", 2)))
    settings["glossy_bounces"] = max(0, int(settings.get("glossy_bounces", 2)))
    settings["transmission_bounces"] = max(0, int(settings.get("transmission_bounces", 4)))
    settings["transparent_max_bounces"] = max(0, int(settings.get("transparent_max_bounces", 4)))
    settings["volume_bounces"] = max(0, int(settings.get("volume_bounces", 0)))
    settings["clamp_direct"] = max(0.0, min(100.0, float(settings.get("clamp_direct", 0.0))))
    settings["clamp_indirect"] = max(0.0, min(100.0, float(settings.get("clamp_indirect", 0.0))))
    settings["filter_glossy"] = max(0.0, min(10.0, float(settings.get("filter_glossy", 0.0))))
    settings["tile_size"] = max(64, int(settings.get("tile_size", 1024)))
    settings["use_simplify"] = bool(settings.get("use_simplify", True))
    settings["use_adaptive_sampling"] = bool(settings.get("use_adaptive_sampling", True))
    settings["use_denoising"] = bool(settings.get("use_denoising", True))
    denoiser = str(settings.get("denoiser", "OPENIMAGEDENOISE")).strip().upper()
    settings["denoiser"] = denoiser if denoiser in {"OPENIMAGEDENOISE", "OPTIX", "NLM"} else "OPENIMAGEDENOISE"
    settings["caustics_reflective"] = bool(settings.get("caustics_reflective", False))
    settings["caustics_refractive"] = bool(settings.get("caustics_refractive", False))
    settings["use_persistent_data"] = bool(settings.get("use_persistent_data", False))
    settings["use_hiprt"] = bool(settings.get("use_hiprt", True))
    return settings


def apply_turbo_settings():
    print("[TURBO] Applying performance-focused Cycles settings")
    scene = bpy.context.scene
    cycles = scene.cycles
    settings = load_turbo_settings()

    # Geometry stability/perf settings requested for turbo profile.
    scene.render.use_simplify = settings["use_simplify"]
    scene.render.simplify_subdivision_render = settings["simplify_subdivision_render"]

    # RDNA/AMD-friendly tiling defaults.
    if hasattr(cycles, "use_tiling"):
        cycles.use_tiling = True
    if hasattr(cycles, "tile_size"):
        cycles.tile_size = settings["tile_size"]

    if hasattr(cycles, "use_adaptive_sampling"):
        cycles.use_adaptive_sampling = settings["use_adaptive_sampling"]
    if hasattr(cycles, "adaptive_threshold"):
        cycles.adaptive_threshold = settings["adaptive_threshold"]
    cycles.samples = settings["samples"]
    cycles.max_bounces = settings["max_bounces"]
    cycles.diffuse_bounces = settings["diffuse_bounces"]
    cycles.glossy_bounces = settings["glossy_bounces"]
    cycles.transmission_bounces = settings["transmission_bounces"]
    cycles.transparent_max_bounces = settings["transparent_max_bounces"]
    cycles.volume_bounces = settings["volume_bounces"]
    if hasattr(cycles, "clamp_direct"):
        cycles.clamp_direct = settings["clamp_direct"]
    if hasattr(cycles, "clamp_indirect"):
        cycles.clamp_indirect = settings["clamp_indirect"]
    if hasattr(cycles, "filter_glossy"):
        cycles.filter_glossy = settings["filter_glossy"]
    if hasattr(cycles, "caustics_reflective"):
        cycles.caustics_reflective = settings["caustics_reflective"]
    if hasattr(cycles, "caustics_refractive"):
        cycles.caustics_refractive = settings["caustics_refractive"]
    if hasattr(cycles, "use_denoising"):
        cycles.use_denoising = settings["use_denoising"]
    if hasattr(cycles, "denoiser"):
        cycles.denoiser = settings["denoiser"]
    if hasattr(scene.render, "use_persistent_data"):
        scene.render.use_persistent_data = settings["use_persistent_data"]
    print(
        "[TURBO] simplify=%s subdiv=%s samples=%s noise=%.6f tile=%s"
        % (
            scene.render.use_simplify,
            scene.render.simplify_subdivision_render,
            cycles.samples,
            cycles.adaptive_threshold,
            getattr(cycles, "tile_size", "n/a"),
        )
    )


def main():
    setup_gpu()
    force_managed_output()
    setup_preview_writer()
    setup_tile_progress_logger()
    try:
        handlers = bpy.app.handlers.render_pre
        if ensure_compositor_exr not in handlers:
            handlers.append(ensure_compositor_exr)
    except Exception as exc:
        print(f"[PIPELINE] render_pre handler failed: {exc}")

    mode = os.environ.get("RENDER_MODE", "TURBO").strip().upper()
    if mode == "ARTIST":
        print("[MODE] ARTIST: keep scene settings")
        apply_render_pipeline()
        apply_artist_simplify_override(bpy.context.scene)
    else:
        print("[MODE] TURBO: override settings")
        apply_turbo_settings()


if __name__ == "__main__":
    main()
