#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
brushset2abr - 把 Procreate .brushset 转换为 Photoshop .abr

转换内容：
  1. 笔尖图像（Shape）         -> ABR 的 samp 采样笔刷（默认反色）
  2. 纹理（Grain）             -> ABR 的 patt 图案 + 纹理混合模式（grainBlendMode）
  3. 间距（plotSpacing）       -> ABR 的 Spcn 间距（按 v7 算法开平方换算，1%~1000%）
  4. 尺寸 / 角度 / 圆度 / 透明度 / 流量
  5. 压感：尺寸、不透明度、流量的最小值和压力控制（由压感曲线/最小值换算）
  6. 笔刷预览图汇总文件夹（预览图反色）

用法：
  brushset2abr input.brushset [-o output.abr] [--no-invert]
  或者直接把 .brushset 拖到 exe / 窗口上。
"""

import argparse
import hashlib
import io
import math
import os
import plistlib
import struct
import sys
import traceback
import uuid
import zipfile

from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def pad4(data):
    return data + b"\x00" * ((4 - len(data) % 4) % 4)


def u16(value):
    return struct.pack(">H", int(value) & 0xFFFF)


def i16(value):
    return struct.pack(">h", int(value))


def u32(value):
    return struct.pack(">I", int(value) & 0xFFFFFFFF)


def i32(value):
    return struct.pack(">i", int(value))


def f64(value):
    return struct.pack(">d", float(value))


def ascii_key(text):
    """Photoshop 的 4 字节 key/classID 编码（短 key 不带长度，长 key 带长度）。"""
    raw = text.encode("ascii", errors="replace")
    if len(raw) == 4 and text not in ("warp", "time", "hold", "list"):
        return b"\x00\x00\x00\x00" + raw
    return u32(len(raw)) + raw


def unicode_padded(text):
    """带结尾 NUL 的 UTF-16BE 字符串。"""
    encoded = text.encode("utf-16-be")
    return u32(len(encoded) // 2 + 1) + encoded + b"\x00\x00"


def clamp(value, low, high):
    return max(low, min(high, value))


# v14：间距换算参数。按 v7 算法：Procreate 存档值是 (百分比/100)^2，
# 开方后直接转成 Photoshop 百分比，不再保底或放大。
SPACING_SCALE = 1.0
MIN_SPACING = 1.0


# ---------------------------------------------------------------------------
# Action Descriptor 写入（ABR 的 desc 块使用这个结构）
# ---------------------------------------------------------------------------

def write_ostype(ostype, value):
    if ostype == "Objc":
        name, class_id, fields = value
        out = unicode_padded(name)
        out += ascii_key(class_id)
        out += u32(len(fields))
        for key, ftype, fval in fields:
            out += ascii_key(key)
            out += ftype.encode("ascii")
            out += write_ostype(ftype, fval)
        return out
    if ostype == "VlLs":
        item_type, items = value
        out = u32(len(items))
        for item in items:
            out += item_type.encode("ascii")
            out += write_ostype(item_type, item)
        return out
    if ostype == "UntF":
        unit, num = value
        return unit.encode("ascii") + f64(num)
    if ostype == "TEXT":
        return unicode_padded(value)
    if ostype == "bool":
        return b"\x01" if value else b"\x00"
    if ostype == "long":
        return i32(value)
    if ostype == "doub":
        return f64(value)
    if ostype == "enum":
        enum_type, enum_value = value
        return ascii_key(enum_type) + ascii_key(enum_value)
    raise ValueError("不支持的 descriptor 类型: %s" % ostype)


def descriptor(name, class_id, fields):
    return (name, class_id, fields)


# ---------------------------------------------------------------------------
# Procreate brushset 解析
# ---------------------------------------------------------------------------

def resolve_uids(objects, obj):
    if isinstance(obj, plistlib.UID):
        return resolve_uids(objects, objects[obj.data])
    if isinstance(obj, dict):
        return {k: resolve_uids(objects, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_uids(objects, item) for item in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def read_brush_params(archive, member):
    with archive.open(member) as f:
        root = plistlib.load(f)
    objects = root.get("$objects", [])
    if len(objects) < 2:
        return {}
    return resolve_uids(objects, objects[1])


def sanitize_name(name):
    if not name or name == "$null":
        return "Brush"
    out = []
    for ch in str(name):
        if ch in '\\/:*?"<>|':
            out.append("_")
        else:
            out.append(ch)
    text = "".join(out).strip(". ").strip()
    return text or "Brush"


class BrushInfo:
    def __init__(self, name, shape_img, grain_img, params, preview_img=None,
                 dual_shape_img=None, dual_params=None):
        self.name = name
        self.shape_img = shape_img
        self.grain_img = grain_img
        self.params = params
        self.preview_img = preview_img
        self.dual_shape_img = dual_shape_img
        self.dual_params = dual_params


def collect_brushes(brushset_path):
    """从 brushset 里收集所有带笔尖图像的笔刷。"""
    brushes = []
    resource_dirs = []
    base_dirs = {os.path.dirname(os.path.abspath(brushset_path))}
    if getattr(sys, "frozen", False):
        base_dirs.add(os.path.dirname(os.path.abspath(sys.executable)))
    base_dirs.add(os.path.dirname(os.path.abspath(__file__)))
    for base_dir in base_dirs:
        for sub in ("Images", "images"):
            candidate = os.path.join(base_dir, sub)
            if os.path.isdir(candidate):
                resource_dirs.append(candidate)

    def resolve_bundled(name, prefer_dir=None):
        """zip 内找不到的内置材质，去仓库的 Images 目录里找。"""
        if name == "$null" or not name:
            return None
        basename = os.path.basename(str(name)).replace("\\", "/")
        if prefer_dir:
            local = (prefer_dir + "/" + basename) if prefer_dir else basename
            if local in members:
                return local
        candidates = [m for m in members
                      if m.replace("\\", "/").endswith(basename)]
        if candidates:
            return candidates[0]
        for folder in resource_dirs:
            path = os.path.join(folder, basename)
            if os.path.isfile(path):
                return path
        return None

    with zipfile.ZipFile(brushset_path) as archive:
        members = archive.namelist()
        for member in members:
            if "Reset" in member or not member.endswith("Brush.archive"):
                continue
            # 双重画笔的副笔刷（Sub01）只作为主笔刷的双重画笔数据使用，
            # 不能当作独立主笔刷导出（否则 ABR 里会出现一堆名为 Brush 的重复项）。
            if "Sub01/Brush.archive" in member:
                continue
            params = read_brush_params(archive, member)
            base = os.path.dirname(member)

            # 笔尖图像：优先取参数里指定的 bundledShapePath，否则取同目录 Shape.png
            bundled = params.get("bundledShapePath", "$null")
            shape_member = resolve_bundled(bundled, base)
            if shape_member is None:
                guess = ("Shape.png" if not base else base + "/Shape.png")
                if guess in members:
                    shape_member = guess

            if shape_member is None:
                continue

            # 纹理图像
            bundled_grain = params.get("bundledGrainPath", "$null")
            grain_member = resolve_bundled(bundled_grain, base)
            if grain_member is None:
                guess = ("Grain.png" if not base else base + "/Grain.png")
                if guess in members:
                    grain_member = guess

            def load_image(m):
                """加载图像并转 RGB；损坏的图返回 None 而不是中断。"""
                if m is None:
                    return None
                try:
                    if os.path.isfile(m):
                        return Image.open(m).convert("RGB")
                    with archive.open(m) as f:
                        return Image.open(io.BytesIO(f.read())).convert("RGB")
                except Exception:
                    return None

            def load_preview_image(m):
                """预览图保留原始通道（含透明），反色时只反颜色不反透明度。"""
                if m is None:
                    return None
                try:
                    if os.path.isfile(m):
                        return Image.open(m)
                    with archive.open(m) as f:
                        return Image.open(io.BytesIO(f.read()))
                except Exception:
                    return None

            shape_rgb = load_image(shape_member)
            grain_rgb = load_image(grain_member)
            if shape_rgb is None:
                continue

            name = params.get("name", "$null")
            # 笔刷预览图：优先取 QuickLook/Thumbnail.png，找不到就用笔尖图
            preview_img = None
            if base:
                for guess in ("QuickLook/Thumbnail.png", "Reset/QuickLook/Thumbnail.png"):
                    cand = base + "/" + guess
                    if cand in members:
                        preview_img = load_preview_image(cand)
                        break
            if preview_img is None:
                preview_img = shape_rgb

            # 双重画笔：同目录 Sub01/Brush.archive + Sub01/Shape.png
            dual_shape_rgb = None
            dual_params = None
            dual_base = (base + "/Sub01") if base else "Sub01"
            dual_member = dual_base + "/Brush.archive"
            if dual_member in members:
                dual_params = read_brush_params(archive, dual_member)
                dual_bundled = dual_params.get("bundledShapePath", "$null")
                dual_shape_member = resolve_bundled(dual_bundled, dual_base)
                if dual_shape_member is None:
                    guess = dual_base + "/Shape.png"
                    if guess in members:
                        dual_shape_member = guess
                if dual_shape_member is not None:
                    dual_shape_rgb = load_image(dual_shape_member)

            brushes.append(BrushInfo(sanitize_name(name), shape_rgb, grain_rgb,
                                     params, preview_img,
                                     dual_shape_rgb, dual_params))
    return brushes


# ---------------------------------------------------------------------------
# 图像处理
# ---------------------------------------------------------------------------

def decide_invert(image):
    """自动判断笔尖图是否需要反色。

    Photoshop 的 ABR 笔尖约定：白=笔迹（不透明）、黑=透明（背景）。
    Procreate 的 Shape 有两种：白形黑底（多数，保持即可）和黑形白底
    （需要反色成白形黑底）。逐张判断：
    - 接近纯色的图（实心笔刷）：偏暗就反成纯白，偏亮保持不变；
    - 有图案的图：看边缘背景颜色，边缘白（黑形白底）才反色，
      边缘黑（白形黑底）保持不变。
    """
    try:
        if "A" in image.getbands():
            # 带透明通道的图：透明度本身就是形状，直接当 alpha 用，不需要反色。
            return False

        gray = image.convert("L")
        w, h = gray.size
        hist = gray.histogram()
        total = max(1, sum(hist))
        mean = sum(v * c for v, c in enumerate(hist)) / total
        variance = sum((v - mean) ** 2 * c for v, c in enumerate(hist)) / total
        std = math.sqrt(variance)
        if std < 6:
            # 接近纯色：ABR 需要白色=不透明，偏暗就反成纯白。
            return mean < 128

        border = []
        if w >= 2 and h >= 2:
            for x in range(w):
                border.append(gray.getpixel((x, 0)))
                border.append(gray.getpixel((x, h - 1)))
            for y in range(h):
                border.append(gray.getpixel((0, y)))
                border.append(gray.getpixel((w - 1, y)))
        else:
            border = list(gray.getdata())
        border_mean = sum(border) / max(1, len(border))
        # 边缘白 -> 黑形白底，反色；边缘黑 -> 白形黑底，保持。
        return border_mean >= 128
    except Exception:
        return False


def shape_to_alpha(shape_rgb, max_side=1024, invert="auto"):
    """Procreate 的 Shape 转成 ABR 的 8bit 灰度笔尖。

    invert: True=强制反色，False=不反色，"auto"=按图像内容逐张自动判断。
    ABR 约定白=笔迹、黑=透明；带透明通道的图直接拿 alpha 当形状，
    不透明=白，无需再判断。
    """
    if "A" in shape_rgb.getbands():
        source = shape_rgb.getchannel("A")
    else:
        if invert == "auto":
            invert = decide_invert(shape_rgb)
        source = shape_rgb.convert("L")
        if invert:
            source = source.point(lambda v: 255 - v)
    mask = source.point(lambda v: 255 if v > 8 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    cropped = source.crop(bbox)
    w, h = cropped.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        cropped = cropped.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return cropped


def grain_to_rgb(grain_img, max_side=512):
    rgb = grain_img.convert("RGB")
    w, h = rgb.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        rgb = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return rgb


# ---------------------------------------------------------------------------
# ABR 写入
# ---------------------------------------------------------------------------

def packbits_row(row):
    """把一行灰度数据按 PackBits 压缩，和真实 ABR 一致。"""
    out = bytearray()
    i = 0
    length = len(row)
    while i < length:
        run = 1
        while i + run < length and row[i + run] == row[i] and run < 128:
            run += 1
        if run >= 3:
            out.append(257 - run)
            out.append(row[i])
            i += run
            continue
        literal = bytearray()
        while i < length and len(literal) < 128:
            run2 = 1
            while i + run2 < length and row[i + run2] == row[i] and run2 < 128:
                run2 += 1
            if run2 >= 3 and literal:
                break
            literal.append(row[i])
            i += 1
        out.append(len(literal) - 1)
        out.extend(literal)
    return bytes(out)


def build_samp_item(alpha_img, brush_id):
    """一个采样笔刷条目：固定头 + 8bit 灰度数据。"""
    w, h = alpha_img.size
    raw = alpha_img.tobytes()
    rle_rows = [packbits_row(raw[y * w:(y + 1) * w]) for y in range(h)]
    rle_data = b"".join(rle_rows)
    row_counts = b"".join(u16(len(r)) for r in rle_rows)
    # 真实 ABR 在图像数据后面还固定带 8 字节尾部填充
    payload = row_counts + rle_data + b"\x00" * 8
    data_len = len(payload)
    id_bytes = brush_id.encode("ascii")

    # 与真实 ABR 一致的 264 字节 VirtualMemoryArray 头部
    unknown = (
        b"\x00\x01\x00\x00"
        + u32(3)
        + u32(data_len + 271)
        + u32(0) + u32(0) + u32(h) + u32(w)
        + u32(56)
        + b"\x00" * (55 * 4)
        + u32(1) + u32(data_len + 15) + u32(8)
    )
    assert len(unknown) == 264

    tail = i32(0) + i32(0) + i32(h) + i32(w) + u16(8) + b"\x01" + payload
    body = bytes([len(id_bytes)]) + id_bytes + unknown + tail
    return u32(len(body)) + pad4(body)


def build_patt_item(rgb_img, pattern_id, name):
    """一个 RGB 图案条目（3 个通道，RAW 压缩）。"""
    w, h = rgb_img.size
    channels = [
        rgb_img.getchannel(0).tobytes(),
        rgb_img.getchannel(1).tobytes(),
        rgb_img.getchannel(2).tobytes(),
    ]
    id_bytes = pattern_id.encode("ascii")

    vmal_body = u32(0) + u32(0) + u32(h) + u32(w) + u32(24)
    for i in range(26):
        if i < 3:
            data = channels[i]
            vmal_body += (
                u32(1)
                + u32(len(data) + 23)
                + u32(8)
                + u32(0) + u32(0) + u32(h) + u32(w)
                + u16(8)
                + b"\x00"
                + data
            )
        else:
            vmal_body += u32(0)

    body = (
        u32(1)                    # pattern version
        + u32(3)                  # RGB
        + i16(0) + i16(0)
        + unicode_padded(name)
        + bytes([len(id_bytes)]) + id_bytes
        + u32(3)                  # VMAL version
        + u32(len(vmal_body))
        + vmal_body
    )
    return u32(len(body)) + pad4(body)


def build_phry_block():
    """笔刷层级块，照真实 6.2 abr 的空层级结构生成。"""
    payload = u32(16) + write_ostype("Objc", ("", "null", [
        ("hierarchy", "VlLs", ("Objc", [])),
    ]))
    return payload


# ---------------------------------------------------------------------------
# Procreate 参数 -> ABR 字段换算
# ---------------------------------------------------------------------------

# Procreate grainBlendMode / blendMode 数字 -> Photoshop ABR 的 BlnM 枚举值。
# 依据开源研究（Krita 的 Procreate 转换脚本作者整理）：
#   1=Multiply, 4=Lighten, 7=Subtract, 8=Linear Burn, 9=Color Dodge,
#   10=Color Burn, 11=Overlay, 19=Darken, 20=Hard Mix, 27=Height, 28=Linear Height...
PROCREATE_BLEND_TO_ABR = {
    0: "Nrml",
    1: "Mltp",
    2: "Scrn",
    3: "linearDodge",
    4: "Lghn",
    5: "Xclu",
    6: "Dfrn",
    7: "blendSubtraction",
    8: "linearBurn",
    9: "CDdg",
    10: "CBrn",
    11: "Ovrl",
    12: "HrdL",
    13: "Lmns",
    14: "Clr ",
    15: "H   ",
    16: "Strt",
    17: "SftL",
    19: "Drkn",
    20: "hardMix",
    21: "vividLight",
    22: "linearLight",
    23: "pinLight",
    24: "lighterColor",
    25: "darkerColor",
    26: "blendDivide",
    27: "Hght",
    28: "linearHeight",
}

# Procreate dualBlendMode -> ABR 双重画笔 BlnM 枚举。
# v14 按 v7 做法：27/28（Height / Linear Height）直接写成对应的高度模式，
# 不再回退成 Color Burn。
PROCREATE_DUAL_BLEND_TO_ABR = {
    0: "Nrml",
    1: "Mltp",
    2: "Scrn",
    3: "linearDodge",
    4: "Lghn",
    5: "Xclu",
    6: "Dfrn",
    7: "blendSubtraction",
    8: "linearBurn",
    9: "CDdg",
    10: "CBrn",
    11: "Ovrl",
    12: "HrdL",
    13: "Lmns",
    14: "Clr ",
    15: "H   ",
    16: "Strt",
    17: "SftL",
    19: "Drkn",
    20: "hardMix",
    21: "vividLight",
    22: "linearLight",
    23: "pinLight",
    24: "lighterColor",
    25: "darkerColor",
    26: "blendDivide",
    27: "Hght",
    28: "linearHeight",
}


def fnum(params, key, default=0.0):
    try:
        value = params.get(key, default)
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def curve_points(params, key):
    """从 Procreate 压感曲线参数里取出 [(x, y), ...]，x/y 均为 0..1。"""
    value = params.get(key)
    if not isinstance(value, dict):
        return []
    points = value.get("points", {}).get("NS.objects", [])
    out = []
    for item in points:
        if not isinstance(item, str):
            continue
        try:
            nums = item.strip("{} ").split(",")
            if len(nums) == 2:
                out.append((float(nums[0]), float(nums[1])))
        except ValueError:
            continue
    return out


def curve_min(params, key):
    """压感曲线在 0 压力时的输出值（0..1），作为最小尺寸/最小不透明度的补充。"""
    pts = curve_points(params, key)
    if not pts:
        return 0.0
    x0, y0 = pts[0]
    if len(pts) == 1 or x0 <= 0.0:
        return clamp(y0, 0.0, 1.0)
    x1, y1 = pts[1]
    if x1 <= x0:
        return clamp(y0, 0.0, 1.0)
    t = -x0 / (x1 - x0)
    return clamp(y0 + (y1 - y0) * t, 0.0, 1.0)


def brush_dynamics(params):
    """由 Procreate 压感参数计算 ABR 的尺寸/不透明度/流量动力学。

    返回：
      minimumDiameter : 压感最小尺寸（%）
      size            : (控制类型, 抖动%, 最小%)，控制 2=压力、0=关闭
      opacity         : (控制类型, 抖动%, 最小%)
      flow            : (控制类型, 抖动%, 最小%)
    """
    paint_size = fnum(params, "paintSize", 0.0)
    min_size = fnum(params, "minSize", 0.0)
    size_pressure = fnum(params, "dynamicsPressureSize", 0.0)
    size_jitter = fnum(params, "dynamicsJitterSize", 0.0)
    size_min_ratio = clamp(min_size / paint_size, 0.0, 1.0) if paint_size > 0 else 0.0
    min_diameter = clamp(
        max(size_min_ratio, curve_min(params, "dynamicsPressureSizeCurve")) * 100.0,
        0.0, 100.0)

    opacity_pressure = fnum(params, "dynamicsPressureOpacity", 0.0)
    min_opacity = fnum(params, "minOpacity", 0.0)
    opacity_jitter = fnum(params, "dynamicsJitterOpacity", 0.0)
    opacity_min = clamp(
        max(min_opacity, curve_min(params, "dynamicsPressureOpacityCurve")) * 100.0,
        0.0, 100.0)

    # Procreate 的 Flow 滑块就是流量大小（dynamicsGlazedFlow），没有单独的
    # 流量压感曲线；这里把它作为流量最小值写入，保持和透明度一致的结构。
    flow_pct = clamp(fnum(params, "dynamicsGlazedFlow", 1.0) * 100.0, 0.0, 100.0)

    size_control = 2 if abs(size_pressure) > 0.05 else 0
    opacity_control = 2 if abs(opacity_pressure) > 0.05 else 0
    # 流量本身在 Procreate 里不受压力控制；只有 Flow<100% 且笔刷有压感时
    # 才打开压力控制，让流量从 Flow% 往 100% 走，尽量贴合“流量同理”。
    flow_control = 2 if (flow_pct < 100.0 and (size_control or opacity_control)) else 0

    return {
        "minimumDiameter": min_diameter,
        "size": (size_control, clamp(size_jitter * 100.0, 0.0, 100.0), min_diameter),
        "opacity": (opacity_control, clamp(opacity_jitter * 100.0, 0.0, 100.0), opacity_min),
        "flow": (flow_control, 0.0, flow_pct),
    }


def abr_blend_for_grain(params):
    """Procreate grainBlendMode -> ABR BlnM 枚举；未知值回退 Multiply。"""
    return PROCREATE_BLEND_TO_ABR.get(
        int(fnum(params, "grainBlendMode", 1.0)), "Mltp")


def abr_blend_for_dual(params):
    """Procreate dualBlendMode -> ABR 双重画笔 BlnM 枚举。

    返回 (模式, 提示或 None)。Height/Linear Height（27/28）按 v7 做法
    直接写成 Hght / linearHeight；完全未知的数值用 Multiply 代替。
    """
    mode = int(fnum(params, "dualBlendMode", 0.0))
    blend = PROCREATE_DUAL_BLEND_TO_ABR.get(mode)
    if blend is not None:
        return blend, None
    return "Mltp", ("双重画笔混合模式 %d 未收录，已按 Multiply 处理" % mode)


def procreate_spacing_percent(params):
    """Procreate plotSpacing -> Photoshop 间距百分比。

    Procreate 的 plotSpacing 存的是 (百分比/100)^2，所以要先开方再乘 100。
    例如 0.042 ≈ 20.5%、0.63 ≈ 79.5%、1.0 = 100%、2.0 ≈ 141.4%。
    v14 按 v7 算法：不保底、不放大，开平方后直接转成 Photoshop 百分比，
    只在 1%~1000% 范围内夹紧。
    """
    value = max(0.0, fnum(params, "plotSpacing", 0.0))
    return clamp(math.sqrt(value) * 100.0 * SPACING_SCALE,
                 MIN_SPACING, 1000.0)


def procreate_texture_each_tip(params):
    """ABR 的 TxtC（纹理应用于每个笔尖）。

    现在统一写 true：Procreate 大量笔刷的 textureApplication=0（纹理化），
    如果原样转换，Photoshop 里就会变成“应用到整个笔画”，和用户实际想要
    的材质笔触效果不符；逐笔尖应用才能保留每次盖章的纹理。
    """
    return True


def procreate_jitter_percent(value):
    """Procreate plotJitter（横向/纵向抖动）-> Photoshop 百分比。

    存档值是 3*(百分比/100)^2，滑杆范围 0~200%（开源研究：100%=3、200%≈13.78）。
    """
    return clamp(math.sqrt(max(0.0, float(value)) / 3.0) * 100.0, 0.0, 200.0)


def procreate_scatter_percent(value):
    """Procreate shapeScatter（旋转散布）-> Photoshop 百分比。

    散布滑杆最大 200%，存档值是 0.5*(百分比/100)^2（2.0 = 200%）。
    """
    return clamp(math.sqrt(max(0.0, float(value)) / 0.5) * 100.0, 0.0, 200.0)


def procreate_count(value):
    """Procreate shapeCount（存档 0.0625~1.0 = 个数/16）-> Photoshop 数量 1~16。"""
    return int(clamp(round(max(0.0, float(value)) * 16.0), 1, 16))


def procreate_count_jitter(value):
    """Procreate shapeCountJitter（0~100%，存档值为平方）-> Photoshop 百分比。"""
    return clamp(math.sqrt(max(0.0, float(value))) * 100.0, 0.0, 100.0)


def normalize_angle_deg(degrees):
    """把角度归一化到 [-180, 180)。"""
    degrees = math.fmod(degrees + 180.0, 360.0)
    if degrees < 0:
        degrees += 360.0
    return degrees - 180.0


def make_sampled_brush_descriptor(name, tip_id, size, angle_deg, roundness,
                                  spacing, flip_x=False, flip_y=False):
    """构造 ABR 的 sampledBrush 描述块（主笔尖和双重画笔共用）。"""
    return descriptor("", "sampledBrush", [
        ("Dmtr", "UntF", ("#Pxl", size)),
        ("Angl", "UntF", ("#Ang", angle_deg)),
        ("Rndn", "UntF", ("#Prc", roundness)),
        ("Nm  ", "TEXT", name),
        ("Spcn", "UntF", ("#Prc", spacing)),
        ("Intr", "bool", True),
        ("flipX", "bool", flip_x),
        ("flipY", "bool", flip_y),
        ("sampledData", "TEXT", tip_id),
    ])


def make_brush_preset_descriptor(brush, tip_id, pattern, spacing, size,
                                 opacity, flow, dual=None):
    """构造 desc 块里的一支笔刷。"""
    params = brush.params
    name = brush.name
    dy = brush_dynamics(params)

    def percent(value):
        return ("UntF", ("#Prc", float(value)))

    def pixels(value):
        return ("UntF", ("#Pxl", float(value)))

    def angle(value):
        return ("UntF", ("#Ang", float(value)))

    def dynamics(bvty=0, fstp=25, jitter=0, mnm=0):
        return descriptor("", "brVr", [
            ("bVTy", "long", bvty),
            ("fStp", "long", fstp),
            ("jitter", "UntF", ("#Prc", jitter)),
            ("Mnm ", "UntF", ("#Prc", mnm)),
        ])

    roundness = clamp(params.get("shapeRoundness", 1.0), 0.0, 1.0) * 100.0
    # shapeAngle 才是笔尖的基础角度（弧度，可多圈）；shapeRotation 是
    # “跟随描边”滑杆（-100%~100%，存档 -1~1），不能拿来当角度用。
    shape_angle_deg = normalize_angle_deg(
        math.degrees(fnum(params, "shapeAngle", 0.0)))

    flip_x = bool(params.get("shapeFlipXJitter", False))
    flip_y = bool(params.get("shapeFlipYJitter", False))
    sampled = make_sampled_brush_descriptor(
        name, tip_id, size, shape_angle_deg, roundness, spacing,
        flip_x, flip_y)

    # 角度动态：按 Procreate 输入样式映射到 Photoshop 的角控制。
    # Procreate 输入样式：1=仅触控 2=方位 3=方位与侧旋 4=存档第 4 值（推测仅侧旋）。
    # Photoshop bVTy：3=钢笔倾斜 6=方向 7=初始旋转 8=旋转。
    rot_follow = fnum(params, "shapeRotation", 0.0)
    orientation = int(round(fnum(params, "shapeOrientation", 1.0)))
    has_azimuth = bool(params.get("shapeAzimuth", False))
    has_roll = bool(params.get("shapeRoll", False))
    if orientation == 2:
        angle_ctrl = 3        # 方位 -> 钢笔倾斜
    elif orientation == 3:
        # 方位与侧旋：普通笔默认退化为方位输入，用钢笔倾斜近似最稳。
        angle_ctrl = 3
    elif orientation == 4:
        # 存档第 4 值含义未公开；若同时开了跟随描边就按方向处理，
        # 否则按“旋转（侧旋）”处理。
        if abs(rot_follow) >= 0.5:
            angle_ctrl = 6
            if rot_follow < 0:
                shape_angle_deg = normalize_angle_deg(shape_angle_deg + 180.0)
        else:
            angle_ctrl = 8
    elif has_azimuth:
        angle_ctrl = 3        # 旧版笔刷（海怪/吹石等）用方位开关
    elif has_roll:
        angle_ctrl = 8        # 旧版笔刷开了侧旋
    elif rot_follow >= 0.5:
        angle_ctrl = 6        # 跟随描边 -> Photoshop“方向”（之前误写成 7=初始旋转）
    elif rot_follow <= -0.5:
        # 反向跟随：Photoshop 没有反向方向，用“方向+180°”近似。
        angle_ctrl = 6
        shape_angle_deg = normalize_angle_deg(shape_angle_deg + 180.0)
    else:
        angle_ctrl = 0        # 方向固定
    angle_jitter = clamp(
        procreate_scatter_percent(fnum(params, "shapeScatter", 0.0)),
        0.0, 100.0)
    if params.get("shapeRandomise"):
        angle_jitter = clamp(angle_jitter + 25.0, 0.0, 100.0)

    shape_count = procreate_count(fnum(params, "shapeCount", 1.0))
    count_jitter = clamp(
        procreate_count_jitter(fnum(params, "shapeCountJitter", 0.0)),
        0.0, 100.0)
    # 横向/纵向抖动（Procreate 可到 200%）；Photoshop 散布抖动可超过 100%
    # （真实 ABR 里见过 223%），所以上限放宽到 1000%。
    scatter_jitter = clamp(max(
        procreate_jitter_percent(fnum(params, "plotJitter", 0.0)),
        procreate_jitter_percent(fnum(params, "plotJitterLongitudinal", 0.0))),
        0.0, 1000.0)
    both_axes = fnum(params, "plotJitterLongitudinal", 0.0) > 0.05

    fields = [
        ("Nm  ", "TEXT", name),
        ("Brsh", "Objc", sampled),
        ("useTipDynamics", "bool", True),
        ("flipX", "bool", flip_x),
        ("flipY", "bool", flip_y),
        ("brushProjection", "bool", False),
        ("minimumDiameter", "UntF", ("#Prc", dy["minimumDiameter"])),
        ("minimumRoundness", "UntF", ("#Prc", 25.0)),
        ("tiltScale", "UntF", ("#Prc", 200.0)),
        ("szVr", "Objc", dynamics(dy["size"][0], 25, dy["size"][1], dy["size"][2])),
        ("angleDynamics", "Objc", dynamics(angle_ctrl, 25, angle_jitter, 0.0)),
        ("roundnessDynamics", "Objc", dynamics(0, 25, 0.0, 0.0)),
        ("useScatter", "bool", True),
        ("Spcn", "UntF", ("#Prc", 100.0)),
        ("Cnt ", "doub", float(shape_count)),
        ("bothAxes", "bool", both_axes),
        ("countDynamics", "Objc", dynamics(0, 25, count_jitter, 0.0)),
        ("scatterDynamics", "Objc", dynamics(0, 25, scatter_jitter, 0.0)),
        ("dualBrush", "Objc", descriptor("", "dualBrush", [
            ("useDualBrush", "bool", dual is not None),
        ] + ([] if dual is None else [
            ("Flip", "bool", dual.get("flip", False)),
            ("Brsh", "Objc", dual["sampled"]),
            ("BlnM", "enum", ("BlnM", dual["blend"])),
            ("useScatter", "bool", True),
            # 真实 ABR 里双重画笔的外层 Spcn 固定 100%，实际间距写在
            # 子笔尖 sampledBrush 的 Spcn 里。
            ("Spcn", "UntF", ("#Prc", 100.0)),
            ("Cnt ", "doub", dual.get("count", 1.0)),
            ("bothAxes", "bool", dual.get("both_axes", False)),
            ("countDynamics", "Objc", dynamics(0, 25, dual.get("count_jitter", 0.0), 0.0)),
            ("scatterDynamics", "Objc", dynamics(0, 25, dual.get("scatter_jitter", 0.0), 0.0)),
        ]))),
        ("brushGroup", "Objc", descriptor("", "brushGroup", [
            ("useBrushGroup", "bool", False),
        ])),
        ("useTexture", "bool", pattern is not None),
    ]

    if pattern is not None:
        grain_depth = clamp(params.get("grainDepth", 1.0), 0.0, 1.0) * 100.0
        grain_min = clamp(params.get("grainDepthMinimum", 0.0), 0.0, 1.0) * 100.0
        texture_scale = params.get("textureScale", 1.0)
        if texture_scale <= 0:
            texture_scale = 1.0
        texture_scale_pct = clamp(texture_scale * 100.0, 1.0, 1000.0)
        brightness = clamp(params.get("textureBrightness", 0.0), -1.0, 1.0) * 100.0
        contrast = clamp(params.get("textureContrast", 0.0), -1.0, 1.0) * 100.0

        fields += [
            ("TxtC", "bool", procreate_texture_each_tip(params)),
            ("interpretation", "bool", True),
            ("textureBlendMode", "enum", ("BlnM", abr_blend_for_grain(params))),
            ("textureDepth", "UntF", ("#Prc", grain_depth)),
            ("minimumDepth", "UntF", ("#Prc", grain_min)),
            ("textureDepthDynamics", "Objc", dynamics(0, 25, 0.0, 0.0)),
            ("Txtr", "Objc", descriptor("", "Ptrn", [
                ("Nm  ", "TEXT", pattern["name"]),
                ("Idnt", "TEXT", pattern["id"]),
            ])),
            ("textureScale", "UntF", ("#Prc", texture_scale_pct)),
            ("InvT", "bool", bool(params.get("textureInverted", False))),
            ("protectTexture", "bool", False),
            ("textureBrightness", "long", int(round(brightness))),
            ("textureContrast", "long", int(round(contrast))),
        ]

    fields += [
        ("usePaintDynamics", "bool", True),
        ("prVr", "Objc", dynamics(dy["flow"][0], 25, dy["flow"][1], dy["flow"][2])),
        ("opVr", "Objc", dynamics(dy["opacity"][0], 25, dy["opacity"][1], dy["opacity"][2])),
        ("wtVr", "Objc", dynamics(0, 25, 0.0, 0.0)),
        ("mxVr", "Objc", dynamics(0, 25, 0.0, 0.0)),
        ("useColorDynamics", "bool", False),
        ("Wtdg", "bool", False),
        ("Nose", "bool", False),
        ("Rpt ", "bool", False),
        ("useBrushSize", "bool", True),
        ("useBrushPose", "bool", False),
        ("toolOptions", "Objc", descriptor("", "PbTl", [
            ("brushPreset", "bool", True),
            ("flow", "long", int(round(flow))),
            ("Smoo", "long", 0),
            ("Md  ", "enum", ("BlnM", "Nrml")),
            ("Opct", "long", int(round(opacity))),
            ("smoothing", "bool", True),
            ("smoothingValue", "doub", 0.0),
            ("smoothingRadiusMode", "bool", False),
            ("smoothingCatchup", "bool", True),
            ("smoothingCatchupAtEnd", "bool", False),
            ("smoothingZoomCompensation", "bool", True),
            ("pressureSmoothing", "bool", False),
            ("usePressureOverridesSize", "bool", False),
            ("usePressureOverridesOpacity", "bool", False),
            ("useLegacy", "bool", False),
        ])),
    ]
    return descriptor("", "brushPreset", fields)


def build_abr(brush_infos, max_tip=1024, max_grain=512, invert_tips="auto"):
    """把收集到的笔刷写成 ABR 字节。"""
    brushes = []
    dual_tips = []
    patterns = []
    pattern_cache = {}
    warnings = []

    for info in brush_infos:
        alpha = shape_to_alpha(info.shape_img, max_tip, invert=invert_tips)
        if alpha is None:
            continue
        tip_id = str(uuid.uuid4())

        pattern = None
        if info.grain_img is not None:
            grain = grain_to_rgb(info.grain_img, max_grain)
            key = hashlib.sha1(grain.tobytes()).hexdigest()
            if key in pattern_cache:
                pattern = pattern_cache[key]
            else:
                pat_id = str(uuid.uuid4())
                pat_name = os.path.splitext(
                    os.path.basename(str(info.params.get("bundledGrainPath", "Texture")))
                )[0] or "Texture"
                pattern = {"id": pat_id, "name": pat_name}
                pattern_cache[key] = pattern
                patterns.append((grain, pat_id, pat_name))

        spacing = procreate_spacing_percent(info.params)
        paint_size = float(info.params.get("paintSize", 0.0) or 0.0)
        if paint_size > 0:
            size = float(max(1, round(clamp(paint_size * 1000.0, 1.0, 5000.0))))
        else:
            size = float(max(1, min(max(alpha.size), 5000)))
        opacity_value = info.params.get("paintOpacity")
        if opacity_value is None:
            opacity_value = info.params.get("maxOpacity", 1.0)
        opacity = clamp(float(opacity_value or 0.0) * 100.0, 0.0, 100.0)
        flow = clamp(float(info.params.get("dynamicsGlazedFlow", 1.0) or 1.0) * 100.0, 0.0, 100.0)

        # 双重画笔：Sub01 有独立笔尖和参数时启用
        dual = None
        if info.dual_shape_img is not None and info.dual_params is not None:
            dual_alpha = shape_to_alpha(info.dual_shape_img, max_tip,
                                        invert=invert_tips)
            if dual_alpha is not None:
                dparams = info.dual_params
                dual_tip_id = str(uuid.uuid4())
                dual_paint = fnum(dparams, "paintSize", 0.0)
                if dual_paint > 0:
                    dual_size = float(max(1, round(clamp(dual_paint * 1000.0, 1.0, 5000.0))))
                else:
                    dual_size = float(max(1, min(max(dual_alpha.size), 5000)))
                dual_angle_deg = normalize_angle_deg(
                    math.degrees(fnum(dparams, "shapeAngle", 0.0)))
                dual_roundness = clamp(fnum(dparams, "shapeRoundness", 1.0),
                                       0.0, 1.0) * 100.0
                dual_name = sanitize_name(dparams.get("name") or (info.name + "-副"))
                dual_spacing = procreate_spacing_percent(dparams)
                dual_blend, dual_blend_warn = abr_blend_for_dual(info.params)
                if dual_blend_warn:
                    warnings.append("%s：%s" % (info.name, dual_blend_warn))
                dual_flip_x = bool(dparams.get("shapeFlipXJitter", False))
                dual_flip_y = bool(dparams.get("shapeFlipYJitter", False))
                dual_count = procreate_count(fnum(dparams, "shapeCount", 1.0))
                dual_count_jitter = clamp(
                    procreate_count_jitter(fnum(dparams, "shapeCountJitter", 0.0)),
                    0.0, 100.0)
                dual_scatter_jitter = clamp(max(
                    procreate_jitter_percent(fnum(dparams, "plotJitter", 0.0)),
                    procreate_jitter_percent(fnum(dparams, "plotJitterLongitudinal", 0.0))),
                    0.0, 1000.0)
                dual_both_axes = fnum(dparams, "plotJitterLongitudinal", 0.0) > 0.05
                dual = {
                    "sampled": make_sampled_brush_descriptor(
                        dual_name, dual_tip_id, dual_size, dual_angle_deg,
                        dual_roundness, dual_spacing,
                        dual_flip_x, dual_flip_y),
                    "blend": dual_blend,
                    "count": float(dual_count),
                    "count_jitter": dual_count_jitter,
                    "scatter_jitter": dual_scatter_jitter,
                    "both_axes": dual_both_axes,
                    "flip": bool(dual_flip_x or dual_flip_y),
                }
                dual_tips.append((dual_alpha, dual_tip_id))

        brushes.append((info, alpha, tip_id, pattern, spacing, size,
                        opacity, flow, dual))

    if not brushes:
        raise RuntimeError("没有找到带笔尖图像的笔刷。")

    samp_payload = b"".join(
        [build_samp_item(alpha, tip_id)
         for _, alpha, tip_id, _, _, _, _, _, _ in brushes]
        + [build_samp_item(alpha, tip_id) for alpha, tip_id in dual_tips])
    patt_payload = b"".join(build_patt_item(grain, pid, name)
                            for grain, pid, name in patterns)

    desc_list = []
    for info, _, tip_id, pattern, spacing, size, opacity, flow, dual in brushes:
        desc_list.append(make_brush_preset_descriptor(
            info, tip_id, pattern, spacing, size, opacity, flow, dual))
    desc_payload = u32(16) + write_ostype("Objc", ("", "null", [
        ("Brsh", "VlLs", ("Objc", desc_list)),
    ]))

    def block(key, payload):
        return b"8BIM" + key.encode("ascii") + u32(len(payload)) + pad4(payload)

    out = u16(6) + u16(2)
    out += block("samp", samp_payload)
    out += block("patt", patt_payload)
    out += block("desc", desc_payload)
    out += block("phry", build_phry_block())
    return out, len(brushes), len(patterns), warnings


def export_previews(brush_infos, preview_dir, invert="auto"):
    """把每支笔刷的预览图（原 brushset 的 Thumbnail.png）汇总输出。

    invert: True=强制反色，False=不反色，"auto"=按图像内容自动判断，
    和笔尖反色保持一致。
    """
    os.makedirs(preview_dir, exist_ok=True)
    written = 0
    for i, info in enumerate(brush_infos, 1):
        img = info.preview_img
        if img is None:
            continue
        do_invert = decide_invert(img) if invert == "auto" else bool(invert)
        if do_invert:
            if "A" in img.getbands():
                rgba = img.convert("RGBA")
                r, g, b, a = rgba.split()
                rgb = Image.merge("RGB", (r, g, b))
                rgb = Image.eval(rgb, lambda v: 255 - v)
                out_img = Image.merge("RGBA", (rgb.split()[0], rgb.split()[1],
                                               rgb.split()[2], a))
            else:
                out_img = Image.eval(img.convert("RGB"), lambda v: 255 - v)
        else:
            out_img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        name = "%02d_%s.png" % (i, info.name)
        out_img.save(os.path.join(preview_dir, name), "PNG")
        written += 1
    return written


def strip_download_suffix(name):
    """去掉百度网盘下载临时后缀，得到正常文件名。"""
    for suffix in (".baiduyun.p.downloading", ".downloading"):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def convert_file(input_path, output_path=None, invert_tips="auto"):
    if not os.path.isfile(input_path):
        raise FileNotFoundError("找不到文件: %s" % input_path)
    if output_path is None:
        clean = strip_download_suffix(input_path)
        base = os.path.splitext(clean)[0]
        output_path = base + ".abr"
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError("输入和输出不能是同一个文件。")

    brushes = collect_brushes(input_path)
    abr_bytes, brush_count, pattern_count, warnings = build_abr(
        brushes, invert_tips=invert_tips)
    with open(output_path, "wb") as f:
        f.write(abr_bytes)
    preview_dir = os.path.join(
        os.path.dirname(os.path.abspath(output_path)),
        "预览汇总",
        os.path.splitext(strip_download_suffix(os.path.basename(input_path)))[0],
    )
    preview_count = export_previews(brushes, preview_dir, invert=invert_tips)
    return output_path, brush_count, pattern_count, preview_dir, preview_count, warnings


def error_log_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "brushset2abr_error.log")


def write_error_log():
    try:
        with open(error_log_path(), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="brushset -> abr 转换器")
    parser.add_argument("input", nargs="?", help="Procreate .brushset 文件")
    parser.add_argument("-o", "--output", help="输出 .abr 文件路径")
    parser.add_argument("--no-invert", action="store_true",
                        help="不反色")
    parser.add_argument("--force-invert", action="store_true",
                        help="强制所有笔尖和预览图反色")
    args = parser.parse_args()

    if args.input:
        try:
            if args.force_invert:
                invert_tips = True
            elif args.no_invert:
                invert_tips = False
            else:
                invert_tips = "auto"
            out, n, p, preview_dir, pc, warnings = convert_file(
                args.input, args.output, invert_tips=invert_tips)
            preview_note = {
                True: "%d 张，已反色" % pc,
                False: "%d 张，未反色" % pc,
                "auto": "%d 张，程序自动判断" % pc,
            }[invert_tips]
            warning_text = (("\n\n提示：\n" + "\n".join(warnings))
                            if warnings else "")
            message = ("已生成：%s\n\n笔刷 %d 支，纹理 %d 张\n"
                       "预览汇总：%s（%s）%s"
                       % (out, n, p, preview_dir, preview_note, warning_text))
            if getattr(sys, "frozen", False) and not os.environ.get("BRUSHSET2ABR_NO_GUI"):
                try:
                    import tkinter as tk
                    from tkinter import messagebox
                    root = tk.Tk()
                    root.withdraw()
                    messagebox.showinfo("转换完成", message)
                    root.destroy()
                except Exception:
                    pass
            else:
                print("完成：%s" % out)
                print("笔刷 %d 支，纹理 %d 张" % (n, p))
                print("预览汇总：%s（%d 张）" % (preview_dir, pc))
                for warn in warnings:
                    print("提示：%s" % warn)
        except Exception as exc:
            if getattr(sys, "frozen", False) and not os.environ.get("BRUSHSET2ABR_NO_GUI"):
                try:
                    import tkinter as tk
                    from tkinter import messagebox
                    root = tk.Tk()
                    root.withdraw()
                    messagebox.showerror("转换失败", str(exc))
                    root.destroy()
                except Exception:
                    pass
            else:
                print("转换失败：%s" % exc)
                sys.exit(1)
        return

    # 没有命令行参数时打开一个简单的图形界面，支持拖拽
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception as exc:
        write_error_log()
        print("无法启动图形界面：%s" % exc)
        return

    try:
        with open(error_log_path(), "a", encoding="utf-8") as f:
            f.write("GUI starting\n")
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
            tcl_dir = os.path.join(meipass, "_tcl_data")
            tk_dir = os.path.join(meipass, "_tk_data")
            if os.path.isdir(tcl_dir):
                os.environ["TCL_LIBRARY"] = tcl_dir
            if os.path.isdir(tk_dir):
                os.environ["TK_LIBRARY"] = tk_dir
            with open(error_log_path(), "a", encoding="utf-8") as f:
                f.write("TCL=%s init=%s TK=%s tk=%s\n" % (
                    tcl_dir,
                    os.path.exists(os.path.join(tcl_dir, "init.tcl")),
                    tk_dir,
                    os.path.exists(os.path.join(tk_dir, "tk.tcl")),
                ))
        root = tk.Tk()
    except Exception:
        write_error_log()
        raise
    root.title("brushset -> abr 笔刷转换器 v14")
    root.geometry("600x440")
    root.configure(bg="#f2f2f2")

    label = tk.Label(root, text="选择 Procreate .brushset 笔刷文件，\n转换成 Photoshop 的 .abr 笔刷",
                     bg="#f2f2f2", font=("Microsoft YaHei UI", 12))
    label.pack(pady=20)

    status = tk.StringVar(value="等待选择文件…")
    status_label = tk.Label(root, textvariable=status, bg="#f2f2f2",
                            font=("Microsoft YaHei UI", 10), fg="#555555", wraplength=460)
    status_label.pack(pady=6)

    tk.Label(root, text="v14：间距按 v7 算法 / 纹理统一逐笔尖应用 / 双重画笔模式含线性高度 / 反色自动判断 / 方向 / 散布 / 压感 / 预览汇总",
             bg="#f2f2f2", fg="#888888", font=("Microsoft YaHei UI", 9)).pack()

    invert_mode = tk.StringVar(value="自动判断（推荐）")
    invert_map = {
        "自动判断（推荐）": "auto",
        "全部反色": "yes",
        "不反色": "no",
    }
    tk.Label(root, text="笔尖与预览图反色：", bg="#f2f2f2",
             font=("Microsoft YaHei UI", 10)).pack(pady=(2, 0))
    tk.OptionMenu(root, invert_mode, *invert_map.keys()).pack(pady=(0, 2))
    tk.Label(root, text="自动判断会逐张看笔尖：黑形白底才反色，白形黑底保持不变。",
             bg="#f2f2f2", fg="#888888", font=("Microsoft YaHei UI", 9)).pack()

    def do_convert(path):
        try:
            out, n, p, preview_dir, pc, warnings = convert_file(
                path, invert_tips=invert_map[invert_mode.get()])
            warning_text = (("\n\n提示：\n" + "\n".join(warnings))
                            if warnings else "")
            status.set("完成：%s\n（%d 支笔刷，%d 张纹理，预览 %d 张）"
                       % (out, n, p, pc))
            messagebox.showinfo(
                "完成",
                "已生成：\n%s\n\n笔刷 %d 支，纹理 %d 张\n预览汇总：%s"
                "%s" % (out, n, p, preview_dir, warning_text))
        except Exception as exc:
            status.set("转换失败：%s" % exc)
            messagebox.showerror("转换失败", str(exc))

    def choose():
        path = filedialog.askopenfilename(
            title="选择 brushset 文件",
            filetypes=[("Procreate 笔刷", "*.brushset;*.brush;*.downloading"),
                       ("所有文件", "*.*")])
        if path:
            status.set("正在转换：%s" % os.path.basename(path))
            root.update_idletasks()
            do_convert(path)

    tk.Button(root, text="选择 .brushset 文件", command=choose,
              bg="#4a90d9", fg="white", font=("Microsoft YaHei UI", 11),
              padx=18, pady=8, relief="flat").pack(pady=16)
    tk.Label(root, text="输出 .abr 和“预览汇总”文件夹会生成在 brushset 同目录。",
             bg="#f2f2f2", fg="#888888", font=("Microsoft YaHei UI", 9)).pack(pady=4)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        write_error_log()
        raise
