"""Turn a NAM A2 Lite capture (.nam) into the .wav that NAM A2 Lite plays from a User IR slot.

The conversion is nocaseguy/hx-stomp-nam-lite's own ``tools/nam2ir.py`` (GPL-3.0),
run as a separate program. The app downloads it once, at a pinned commit, from
GitHub into the data directory. It is not copied into this repository. The
firmware and its converter change together, so CONVERTER_COMMIT moves when the
firmware does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import config

CONVERTER_REPO = "nocaseguy/hx-stomp-nam-lite"
CONVERTER_COMMIT = "003bc0dc4d0dbb67b46f69e151cd1fbb3ae5e46c"
CONVERTER_FILES = (
    "tools/nam2ir.py",
    "tools/sharc/nam_lite.py",
    "tools/sharc/nam_pack.py",
    "tools/sharc/sasm.py",
    "tools/sharc/ssim.py",
    "tools/sharc/recips_table.json",
)
#: Points at a checkout of the converter repo instead of downloading it (tests, offline use).
CONVERTER_DIR_ENV = "STOMPXL_NAM2IR_DIR"
#: The .wav nam2ir writes: 44-byte header and 2,048 float32 samples.
WAV_BYTES = 44 + 2048 * 4


class ConvertError(RuntimeError):
    pass


def converter_dir() -> Path:
    """The converter's files, downloaded on first use."""
    override = os.environ.get(CONVERTER_DIR_ENV)
    if override:
        return Path(override).expanduser()
    root = config.data_dir() / "nam2ir" / CONVERTER_COMMIT[:12]
    if all((root / name).is_file() for name in CONVERTER_FILES):
        return root
    base = f"https://raw.githubusercontent.com/{CONVERTER_REPO}/{CONVERTER_COMMIT}/"
    for name in CONVERTER_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(base + name, timeout=30) as response:
                data = response.read()
        except OSError as exc:
            raise ConvertError(f"Couldn't download the converter from GitHub ({name}): {exc}") from exc
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return root


def convert(nam_path: Path, wav_path: Path) -> str:
    """Write ``wav_path`` from ``nam_path`` and return the converter's report.

    Refused (ConvertError, with the converter's reason): a file with no 3-channel
    A2 Lite model, one not shaped like A2, or one trained at another rate than 48 kHz.
    """
    script = converter_dir() / "tools" / "nam2ir.py"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav_path.with_name(wav_path.stem + ".tmp.wav")
    try:
        done = subprocess.run(
            [sys.executable, "-I", str(script), str(nam_path), "-o", str(tmp)],
            capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ConvertError(f"{nam_path.name}: the converter took too long") from exc
    if done.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        reason = (done.stderr or done.stdout).strip().splitlines()
        raise ConvertError(_short(reason[-1] if reason else "the converter failed", nam_path))
    if tmp.stat().st_size != WAV_BYTES:
        tmp.unlink(missing_ok=True)
        raise ConvertError(f"{nam_path.name}: the converter wrote an unexpected file")
    tmp.replace(wav_path)
    return done.stdout.strip()


def _short(reason: str, nam_path: Path) -> str:
    """The converter's message without the temporary path it prints."""
    reason = reason.replace(str(nam_path), nam_path.name)
    if "no 3-channel WaveNet" in reason:
        return f"{nam_path.name}: no A2 Lite model inside (NAM A2 Lite plays A2 Lite captures only)"
    return reason
