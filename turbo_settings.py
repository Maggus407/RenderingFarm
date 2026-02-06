from utils import as_bool, as_float, as_int
import context

DEFAULT_TURBO_SETTINGS = {
    "use_simplify": True,
    "simplify_subdivision_render": 4,
    "use_adaptive_sampling": True,
    "samples": 4096,
    "adaptive_threshold": 0.001,
    "use_denoising": False,
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


def validate_turbo_settings(raw: dict | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    defaults = dict(DEFAULT_TURBO_SETTINGS)
    use_simplify = as_bool(source.get("use_simplify", defaults["use_simplify"]), defaults["use_simplify"])
    use_adaptive_sampling = as_bool(
        source.get("use_adaptive_sampling", defaults["use_adaptive_sampling"]),
        defaults["use_adaptive_sampling"],
    )
    use_denoising = as_bool(source.get("use_denoising", defaults["use_denoising"]), defaults["use_denoising"])
    use_persistent_data = as_bool(
        source.get("use_persistent_data", defaults["use_persistent_data"]),
        defaults["use_persistent_data"],
    )
    use_hiprt = as_bool(source.get("use_hiprt", defaults["use_hiprt"]), defaults["use_hiprt"])
    caustics_reflective = as_bool(
        source.get("caustics_reflective", defaults["caustics_reflective"]),
        defaults["caustics_reflective"],
    )
    caustics_refractive = as_bool(
        source.get("caustics_refractive", defaults["caustics_refractive"]),
        defaults["caustics_refractive"],
    )
    denoiser_raw = str(source.get("denoiser", defaults["denoiser"])).strip().upper()
    allowed_denoisers = {"OPENIMAGEDENOISE", "OPTIX", "NLM"}
    denoiser = denoiser_raw if denoiser_raw in allowed_denoisers else defaults["denoiser"]
    return {
        "use_simplify": use_simplify,
        # Safety requirement: simplify > 5 can crash with HIP on this setup.
        "simplify_subdivision_render": as_int(
            source.get("simplify_subdivision_render"),
            defaults["simplify_subdivision_render"],
            0,
            5,
        ),
        "use_adaptive_sampling": use_adaptive_sampling,
        "samples": as_int(source.get("samples"), defaults["samples"], 1, 65536),
        "adaptive_threshold": as_float(
            source.get("adaptive_threshold"),
            defaults["adaptive_threshold"],
            0.000001,
            1.0,
        ),
        "use_denoising": use_denoising,
        "denoiser": denoiser,
        "max_bounces": as_int(source.get("max_bounces"), defaults["max_bounces"], 0, 64),
        "diffuse_bounces": as_int(source.get("diffuse_bounces"), defaults["diffuse_bounces"], 0, 64),
        "glossy_bounces": as_int(source.get("glossy_bounces"), defaults["glossy_bounces"], 0, 64),
        "transmission_bounces": as_int(
            source.get("transmission_bounces"),
            defaults["transmission_bounces"],
            0,
            64,
        ),
        "transparent_max_bounces": as_int(
            source.get("transparent_max_bounces"),
            defaults["transparent_max_bounces"],
            0,
            64,
        ),
        "volume_bounces": as_int(source.get("volume_bounces"), defaults["volume_bounces"], 0, 64),
        "clamp_direct": as_float(source.get("clamp_direct"), defaults["clamp_direct"], 0.0, 100.0),
        "clamp_indirect": as_float(source.get("clamp_indirect"), defaults["clamp_indirect"], 0.0, 100.0),
        "filter_glossy": as_float(source.get("filter_glossy"), defaults["filter_glossy"], 0.0, 10.0),
        "caustics_reflective": caustics_reflective,
        "caustics_refractive": caustics_refractive,
        "tile_size": as_int(source.get("tile_size"), defaults["tile_size"], 64, 4096),
        "use_persistent_data": use_persistent_data,
        "use_hiprt": use_hiprt,
    }


def get_turbo_settings() -> dict:
    return validate_turbo_settings(context.CONFIG.get("TURBO_SETTINGS", DEFAULT_TURBO_SETTINGS))


def build_turbo_settings_for_job(job_meta: dict) -> dict:
    base = dict(get_turbo_settings())
    override = job_meta.get("turbo_settings_override")
    if isinstance(override, dict):
        base.update(override)
    return validate_turbo_settings(base)


context.CONFIG["TURBO_SETTINGS"] = validate_turbo_settings(context.CONFIG.get("TURBO_SETTINGS"))
