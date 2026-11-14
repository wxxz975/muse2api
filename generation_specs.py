"""Prompt-level media targets. Muse may return different actual geometry."""
from __future__ import annotations
import math
import re

_DIMENSIONS = re.compile(r"^(\d{2,5})\s*[xX×]\s*(\d{2,5})$")
_RATIO = re.compile(r"^(\d{1,5})\s*[:/]\s*(\d{1,5})$")


def dimensions(value):
    if not isinstance(value, str):
        return None
    match = _DIMENSIONS.fullmatch(value.strip())
    if not match:
        return None
    width, height = map(int, match.groups())
    if not 64 <= width <= 8192 or not 64 <= height <= 8192:
        raise ValueError("目标宽高必须在64至8192像素之间")
    return width, height


def ratio(value):
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    aliases = {"portrait": (9,16), "竖屏": (9,16), "landscape": (16,9),
               "横屏": (16,9), "square": (1,1), "正方形": (1,1)}
    if text in aliases:
        return aliases[text]
    match = _RATIO.fullmatch(text)
    if not match:
        return None
    left, right = map(int, match.groups())
    if not left or not right:
        raise ValueError("画幅比例必须为正数")
    divisor = math.gcd(left, right)
    return left // divisor, right // divisor


def validate_geometry(size=None, aspect_ratio=None, resolution=None):
    values = [dimensions(size), dimensions(resolution)]
    if values[0] and values[1] and values[0] != values[1]:
        raise ValueError("尺寸与分辨率目标不一致，请选择相同宽高")
    pixels = values[0] or values[1]
    desired = ratio(aspect_ratio) or (ratio(size) if not values[0] else None)
    if pixels and desired:
        # Rounding one output dimension to integer pixels must not create a false conflict.
        if abs(pixels[0] * desired[1] - pixels[1] * desired[0]) > max(desired):
            raise ValueError("目标尺寸与画幅比例冲突")
    return pixels, desired


def geometry_instruction(size=None, aspect_ratio=None, resolution=None):
    pixels, desired = validate_geometry(size, aspect_ratio, resolution)
    parts = []
    if desired:
        parts.append(f"画幅比例目标：{desired[0]}:{desired[1]}")
    elif pixels:
        divisor = math.gcd(*pixels)
        parts.append(f"画幅比例目标：{pixels[0]//divisor}:{pixels[1]//divisor}")
    if pixels:
        parts.append(f"输出分辨率目标：{pixels[0]}x{pixels[1]}像素")
    elif isinstance(resolution, str) and resolution.strip().lower() not in ("", "auto"):
        parts.append(f"输出画质目标：{resolution.strip()}")
    return "；".join(parts)
