"""Broadcast-style finishing for speech: rumble filter, a gentle speech compressor and two-pass loudness.

* highpass at 80 Hz: desk thumps, handling noise and mic rumble go, voices keep their body;
* a soft-knee compressor (3:1 above -20 dB, 6 dB knee): quiet words come forward, shouting stays in check;
* EBU R128 loudness to -14 LUFS with a -1 dBTP true-peak ceiling and LRA 7, TikTok's own playback target,
  in two passes: the first measures the finished mix, the second applies exactly the gain it needs in
  linear mode, so the result lands within half a LU of the target without pumping.
"""

from __future__ import annotations

import json
import re

TARGET_I = -14.0
TARGET_TP = -1.0
TARGET_LRA = 7.0

HIGHPASS = "highpass=f=80:poles=2"
COMPRESSOR = "acompressor=threshold=-20dB:ratio=3:attack=8:release=160:knee=6:makeup=2"


def loudnorm(measured: dict | None = None) -> str:
    """loudnorm for the first (measuring) pass, or with `measured` for the second."""
    base = f"loudnorm=I={TARGET_I:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}"
    if not measured:
        return base + ":print_format=json"
    try:
        return (base + f":measured_I={float(measured['input_i']):.2f}:measured_TP={float(measured['input_tp']):.2f}"
                f":measured_LRA={float(measured['input_lra']):.2f}:measured_thresh={float(measured['input_thresh']):.2f}"
                f":offset={float(measured['target_offset']):.2f}:linear=true:print_format=summary")
    except (KeyError, TypeError, ValueError):
        return base


def parse_measurement(stderr: str) -> dict | None:
    """The JSON loudnorm prints at the end of the first pass."""
    found = None
    for m in re.finditer(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr):
        found = m.group(0)
    if not found:
        return None
    try:
        data = json.loads(found)
    except ValueError:
        return None
    try:
        if not float(data["input_i"]) > -70:  # silence: -inf, nothing to measure
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return data


def speech_chain(master: bool) -> list[str]:
    return [HIGHPASS, COMPRESSOR] if master else []
