"""Check the model catalog against HX Edit's own model table (``Helix.sym``).

The catalog came from a community dataset and some models are missing
parameters (Simple Pitch had no Interval). A parameter the catalog lacks is
never written to the pedal, so this lists, per model, what HX Edit has that the
catalog doesn't, and the reverse.
"""

from __future__ import annotations

from typing import Any

from stompxl.helixgen.device.symbols import normalize_parameter_name

from . import config, engine, xl

#: HX Edit parameters that aren't tone settings: a cab's IR payload, the
#: looper's runtime controls, an on/off the block's own bypass covers.
INTERNAL = {"IrData", "Once", "OverdubEnable", "PendingState", "Reverse", "Slow", "Undo",
            "Enable"}
#: The I/O blocks, configured in the pedal's own settings rather than presets.
INTERNAL_MODELS = ("HelixStomp_AppDSPFlow",)


def audit(symbols: Any, cat: Any = None) -> dict[str, dict[str, Any]]:
    """``{model: {"symbol", "missing", "unknown"}}`` for every model with a gap."""
    cat = cat or engine.catalog()
    out: dict[str, dict[str, Any]] = {}
    for model in cat.models():
        variants = [s for v in symbols.variants_of(model.internal_name)
                    if (s := symbols.by_symbol(model.internal_name + v)) is not None]
        if not variants or model.internal_name.startswith(INTERNAL_MODELS):
            continue
        device: dict[str, str] = {}
        for variant in variants:
            for name in variant.parameters:
                device.setdefault(normalize_parameter_name(name), name)
        ours = {normalize_parameter_name(n): n for n in model.parameter_names()
                if not n.startswith("@")}
        missing = sorted(name for key, name in device.items()
                         if key not in ours and name not in INTERNAL)
        unknown = sorted(name for key, name in ours.items()
                         if key not in device and not xl.is_stereo_only(name))
        if missing or unknown:
            out[model.display_name] = {
                "symbol": "/".join(v.symbol for v in variants),
                "missing": missing,
                "unknown": unknown,
            }
    return out


def run() -> tuple[str, list[str]]:
    import json

    from stompxl.helixgen.device.hxedit import find_symbol_table
    from stompxl.helixgen.device.symbols import DeviceSymbols

    gaps = audit(DeviceSymbols.load(find_symbol_table(None)))
    lines = [f"{len(gaps)} model(s) differ from HX Edit:"]
    for name, gap in sorted(gaps.items()):
        parts = []
        if gap["missing"]:
            parts.append("missing " + ", ".join(gap["missing"]))
        if gap["unknown"]:
            parts.append("not in HX Edit " + ", ".join(gap["unknown"]))
        lines.append(f"  {name} ({gap['symbol']}): " + "; ".join(parts))
    path = config.data_dir() / "model-audit.json"
    path.write_text(json.dumps(gaps, indent=1))
    return str(path), lines
