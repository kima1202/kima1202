#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_scene.py — 读取天气与季节，生成四个日夜「动态」场景 SVG（可附带导出 GIF）。

场景划分（PHASES）：dawn 黎明 / day 白昼 / dusk 黄昏 / night 夜晚。
同一份天气 + 季节数据驱动四个时段，输出 assets/scene-<phase>.svg 与汇总 assets/scenes.json。

SVG 默认自带声明式 CSS 动画（无 JavaScript、无外部资源）：云随风漂移、树冠绕树干
摇摆、雨/雪斜落、雾带流动、星星闪烁、日月呼吸、雷阵雨闪电；风力由天气强弱决定，
并遵循 prefers-reduced-motion。加 --static 可退回纯静态 SVG。
加 --gif 会逐帧渲染导出兼容任何图片查看器的动图，加 --preview 再出一张四宫格预览动图。

--single 只输出一张 assets/scene-live.svg（+ 同名 GIF）：时段按北京时间自动判定
（05-08 黎明 / 08-17 白昼 / 17-20 黄昏 / 其余夜晚），因此 profile 上只贴一张图，
却能随季节、天气、昼夜一起变化；--phase 可强制时段。默认是四张分时段图。

天气来源优先级：
  1) --weather-json 指定的文件（默认 data/weather.json，若存在）
     —— 兼容前序 workflow 写下的天气文件，也兼容 Open-Meteo / wttr.in 的原始结构
  2) Open-Meteo 实时接口（免 API key，按经纬度查询）
  3) 按季节给出兜底天气（离线也能生成）

季节来源优先级：--season / SCENE_SEASON > 天气文件里的 season > 按当前月份推算。

用法：
  python scripts/gen_scene.py
  python scripts/gen_scene.py --offline                 # 不联网，只用本地文件/默认值
  python scripts/gen_scene.py --gif --preview           # 同时导出动图（需 cairosvg + Pillow）
  python scripts/gen_scene.py --static                 # 纯静态矢量图
  python scripts/gen_scene.py --season autumn --weather rain --temp 18
  python scripts/gen_scene.py --weather-json data/weather.example.json --out-dir assets

环境变量：SCENE_LAT, SCENE_LON, SCENE_CITY, SCENE_SEASON, SCENE_HEMISPHERE,
          SCENE_WEATHER_JSON, SCENE_TIMEOUT, SCENE_OFFLINE
SVG 生成仅依赖 Python 标准库；GIF 导出为可选依赖（cairosvg + Pillow）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
PHASES = ("dawn", "day", "dusk", "night")
PHASE_LABELS = {"dawn": "黎明", "day": "白昼", "dusk": "黄昏", "night": "夜晚"}
SEASON_LABELS = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
WEATHER_LABELS = {
    "clear": "晴",
    "partly_cloudy": "多云",
    "overcast": "阴",
    "fog": "雾",
    "rain": "雨",
    "snow": "雪",
    "thunder": "雷阵雨",
}
WIDTH, HEIGHT = 1200, 400
HORIZON = 300
# 含中文字体回退，浏览器与 SVG 渲染器（如 cairosvg）都能正常显示中文标注
FONT_FAMILY = (
    "'WenQuanYi Micro Hei', 'Noto Sans CJK SC', 'Noto Sans SC', "
    "'Microsoft YaHei', 'PingFang SC', system-ui, -apple-system, "
    "'Segoe UI', sans-serif"
)

# 风越强，云漂得越快、树摇得越明显；t=0~1 是一个完整循环
WIND = {
    "clear": 0.55, "partly_cloudy": 0.80, "overcast": 1.00, "fog": 0.40,
    "rain": 1.35, "snow": 0.85, "thunder": 1.80,
}

# 声明式动画：浏览器 / <object> / <iframe> 直接播放，<img> 中亦大多可播放。
# 不含脚本，GitHub Pages 与本地打开均可用；prefers-reduced-motion 自动禁用。
MOTION_CSS = """
<style type="text/css">
  .cloud,.fogband,.tree,.star,.drop,.halo,.bolt { will-change: transform, opacity; }
  .cloud   { animation: drift 18s ease-in-out infinite alternate; }
  .fogband { animation: drift 26s ease-in-out infinite alternate; }
  .tree    { animation: sway 4.2s ease-in-out infinite alternate; transform-box: view-box; }
  .star    { animation: twinkle 3.4s ease-in-out infinite alternate; }
  .drop    { animation: fall 1.1s linear infinite; }
  .halo    { animation: breathe 6s ease-in-out infinite alternate; }
  .bolt    { animation: flash 7s steps(1, end) infinite; }
  @keyframes drift   { from { transform: translateX(-26px); } to { transform: translateX(26px); } }
  @keyframes sway    { from { transform: rotate(-2.2deg); }  to { transform: rotate(2.2deg); } }
  @keyframes twinkle { from { opacity: .28; } to { opacity: 1; } }
  @keyframes breathe { from { opacity: .55; } to { opacity: 1; } }
  @keyframes fall    {
    0%   { transform: translateY(-60px); opacity: 0; }
    12%  { opacity: 1; }
    88%  { opacity: 1; }
    100% { transform: translateY(170px); opacity: 0; }
  }
  @keyframes flash {
    0%, 90%, 93%, 96%, 100% { opacity: 0; }
    91%, 95% { opacity: .95; }
  }
  @media (prefers-reduced-motion: reduce) {
    .cloud,.fogband,.tree,.star,.drop,.halo,.bolt { animation: none !important; }
  }
</style>
"""

# 每个季节：四个时段的天空渐变 + 地形/植被配色
PALETTES = {
    "spring": {
        "sky": {
            "dawn": ("#f3b9a6", "#ffe4b5"),
            "day": ("#7cc3e8", "#e2f3ff"),
            "dusk": ("#ef9673", "#b47aa8"),
            "night": ("#101d3b", "#365274"),
        },
        "hill_far": "#9fc8b1", "hill_near": "#6da889", "ground": "#a8d59d",
        "leaf": "#8fce72", "leaf_alt": "#f2a9bf", "accent": "#ffe0e9",
        "trunk": "#7c5a46", "sun": "#ffd39a", "text": "#22314b",
    },
    "summer": {
        "sky": {
            "dawn": ("#ffc987", "#ffedbb"),
            "day": ("#3fa4df", "#cbeaff"),
            "dusk": ("#ff8b5b", "#7b5ba8"),
            "night": ("#0b1a39", "#28446e"),
        },
        "hill_far": "#4f9c6b", "hill_near": "#2f7a52", "ground": "#7ec169",
        "leaf": "#2f8f4e", "leaf_alt": "#57b263", "accent": "#bfe89a",
        "trunk": "#6b4c37", "sun": "#fff0b8", "text": "#1d3049",
    },
    "autumn": {
        "sky": {
            "dawn": ("#eeb287", "#ffdfae"),
            "day": ("#8eb8d8", "#f0e3c9"),
            "dusk": ("#dd7047", "#6d4a7c"),
            "night": ("#111a34", "#3b3455"),
        },
        "hill_far": "#b58458", "hill_near": "#8a5c40", "ground": "#c99a5b",
        "leaf": "#e07b32", "leaf_alt": "#c4492f", "accent": "#eec45a",
        "trunk": "#6a4632", "sun": "#ffcf8f", "text": "#38281f",
    },
    "winter": {
        "sky": {
            "dawn": ("#c8d6e8", "#f2d3c1"),
            "day": ("#a5c5dd", "#edf6fc"),
            "dusk": ("#b48eaa", "#657d9f"),
            "night": ("#0a1430", "#2d4266"),
        },
        "hill_far": "#a7bbcb", "hill_near": "#8298ab", "ground": "#e9eff4",
        "leaf": "#dfe9f0", "leaf_alt": "#c7d8e4", "accent": "#ffffff",
        "trunk": "#6b7a88", "sun": "#ffeed6", "text": "#25384d",
    },
}

# 天气 → 天空压暗系数、云量、云色
WEATHER_STYLE = {
    "clear":         {"dim": 0.00, "clouds": 1, "cloud": "#ffffff", "opacity": 0.75},
    "partly_cloudy": {"dim": 0.06, "clouds": 3, "cloud": "#f4f8fc", "opacity": 0.85},
    "overcast":      {"dim": 0.17, "clouds": 5, "cloud": "#c8d2dd", "opacity": 0.95},
    "fog":           {"dim": 0.14, "clouds": 4, "cloud": "#dde5ec", "opacity": 0.80},
    "rain":          {"dim": 0.28, "clouds": 5, "cloud": "#9fb0c1", "opacity": 0.95},
    "snow":          {"dim": 0.18, "clouds": 5, "cloud": "#dbe6ef", "opacity": 0.92},
    "thunder":       {"dim": 0.34, "clouds": 5, "cloud": "#8593a8", "opacity": 0.96},
}

WMO_TO_WEATHER = [
    (lambda c: c == 0, "clear"),
    (lambda c: c in (1, 2), "partly_cloudy"),
    (lambda c: c == 3, "overcast"),
    (lambda c: c in (45, 48), "fog"),
    (lambda c: 51 <= c <= 57, "rain"),
    (lambda c: 61 <= c <= 67, "rain"),
    (lambda c: 71 <= c <= 77, "snow"),
    (lambda c: 80 <= c <= 82, "rain"),
    (lambda c: c in (85, 86), "snow"),
    (lambda c: 95 <= c <= 99, "thunder"),
]


# ---------------------------------------------------------------- 基础工具

def _rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(x))) for x in c)


def mix(a: str, b: str, t: float) -> str:
    """把颜色 a 朝 b 混合 t (0~1)。"""
    return _hex(tuple(x + (y - x) * t for x, y in zip(_rgb(a), _rgb(b))))


def dim(color: str, amount: float) -> str:
    return mix(color, "#0a1224", amount)


def lighten(color: str, amount: float) -> str:
    return mix(color, "#ffffff", amount)


def normalize_weather(value) -> str | None:
    """把任意来源的天气描述归一到内部 key。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return weather_from_wmo(int(value))
    s = str(value).strip().lower()
    if not s:
        return None
    if any(k in s for k in ("thunder", "storm", "雷")):
        return "thunder"
    if any(k in s for k in ("snow", "sleet", "雪")):
        return "snow"
    if any(k in s for k in ("fog", "mist", "haze", "雾", "霾")):
        return "fog"
    if any(k in s for k in ("rain", "drizzle", "shower", "雨")):
        return "rain"
    if any(k in s for k in ("overcast", "阴")):
        return "overcast"
    if any(k in s for k in ("cloud", "多云", "云")):
        return "partly_cloudy"
    if any(k in s for k in ("clear", "sunny", "sun", "晴")):
        return "clear"
    return None


def weather_from_wmo(code: int) -> str:
    for match, key in WMO_TO_WEATHER:
        if match(code):
            return key
    return "partly_cloudy"


def season_now(today: dt.date, hemisphere: str = "north") -> str:
    m = today.month
    idx = {12: 3, 1: 3, 2: 3, 3: 0, 4: 0, 5: 0, 6: 1, 7: 1, 8: 1,
           9: 2, 10: 2, 11: 2}[m]
    if hemisphere == "south":
        idx = (idx + 2) % 4
    return ("spring", "summer", "autumn", "winter")[idx]


def phase_now(moment: dt.datetime | None = None) -> str:
    """按北京时间判定当前时段；单张模式下让同一张图随时间自动换景。"""
    if moment is None:
        moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    hour = moment.hour
    if 5 <= hour < 8:
        return "dawn"
    if 8 <= hour < 17:
        return "day"
    if 17 <= hour < 20:
        return "dusk"
    return "night"


def beijing_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)


def beijing_stamp() -> str:
    """只精确到天：内容（季节/天气/时段）不变时产物字节完全一致，
    workflow 的 diff 检查就能真正跳过提交，避免撑大 git 历史。"""
    return beijing_now().strftime("%Y-%m-%d")


def season_from_value(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    direct = {"spring": "spring", "summer": "summer", "autumn": "autumn",
              "fall": "autumn", "winter": "winter", "春": "spring", "夏": "summer",
              "秋": "autumn", "冬": "winter"}
    if s in direct:
        return direct[s]
    if s.isdigit() and 1 <= int(s) <= 12:
        return season_now(dt.date(2026, int(s), 15))
    return None


# ---------------------------------------------------------------- 天气读取

def _first_str(*values):
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        if isinstance(v, list) and v:
            item = v[0]
            if isinstance(item, dict):
                return _first_str(item.get("value"), item.get("weatherDesc"), item.get("desc"))
            return item
        if isinstance(v, dict):
            return _first_str(v.get("value"), v.get("desc"), v.get("weatherDesc"))
    return None


def parse_weather_file(obj: dict) -> dict:
    """宽松解析天气文件：自定义结构 / Open-Meteo / wttr.in 都能读。"""
    out: dict = {}
    if not isinstance(obj, dict):
        return out
    cur = obj.get("current") if isinstance(obj.get("current"), dict) else {}
    cc = obj.get("current_condition")
    cc0 = cc[0] if isinstance(cc, list) and cc and isinstance(cc[0], dict) else {}

    out["season"] = season_from_value(_first_str(obj.get("season")))
    out["city"] = _first_str(obj.get("city"), obj.get("location"), obj.get("name"))
    out["source"] = _first_str(obj.get("source"))

    raw_weather = _first_str(
        obj.get("weather"), obj.get("condition"), obj.get("weather_label"),
        obj.get("weatherDesc"), cur.get("weather_label"),
        cc0.get("weatherDesc"),
    )
    code = _first_str(
        obj.get("weather_code"), obj.get("code"), obj.get("weatherCode"),
        cur.get("weather_code"), cur.get("code"), cc0.get("weatherCode"),
    )
    code_int = None
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        pass
    out["weather"] = normalize_weather(raw_weather) or (
        weather_from_wmo(code_int) if code_int is not None else None
    )

    temp = _first_str(
        obj.get("temperature"), obj.get("temp"), obj.get("temp_C"),
        cur.get("temperature_2m"), cur.get("temp"), cc0.get("temp_C"),
    )
    if isinstance(temp, (int, float)):
        out["temperature"] = round(float(temp), 1)
    elif isinstance(temp, str):
        try:
            out["temperature"] = round(float(temp.replace("°C", "").strip()), 1)
        except ValueError:
            pass

    is_day = _first_str(obj.get("is_day"), cur.get("is_day"))
    if isinstance(is_day, (int, float)):
        out["is_day"] = int(is_day)
    return {k: v for k, v in out.items() if v is not None}


def read_weather_file(path: Path) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        return parse_weather_file(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        print(f"[warn] 天气文件读取失败 {path}: {exc}", file=sys.stderr)
        return {}


def weather_file_is_stale(path: Path, max_hours: float = 3.0) -> bool:
    """带 updated_at 的缓存超过 max_hours 视为过期，需要重新拉取实时天气。"""
    if not path or not path.is_file():
        return True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = raw.get("updated_at")
        if not ts:
            return False  # 无时间戳的外部天气文件，交由其生产者负责更新
        moment = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - moment).total_seconds() > max_hours * 3600
    except (OSError, ValueError, TypeError):
        return False


def fetch_open_meteo(lat: float, lon: float, timeout: float) -> dict:
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,is_day",
        "timezone": "Asia/Shanghai",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "scene-svg-generator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    current = data.get("current") or {}
    code = current.get("weather_code")
    result = {
        "weather": weather_from_wmo(int(code)) if code is not None else "partly_cloudy",
        "source": "open-meteo",
        "weather_code": code,
    }
    if isinstance(current.get("temperature_2m"), (int, float)):
        result["temperature"] = round(float(current["temperature_2m"]), 1)
    if isinstance(current.get("is_day"), (int, float)):
        result["is_day"] = int(current["is_day"])
    return result


def resolve_context(args) -> dict:
    """汇总天气与季节，返回生成 SVG 所需的完整上下文。"""
    weather_path = Path(args.weather_json) if args.weather_json else ROOT / "data" / "weather.json"
    if not weather_path.is_absolute():
        weather_path = ROOT / weather_path

    file_data = read_weather_file(weather_path)
    ctx: dict = {
        "season": season_now(dt.date.today(), args.hemisphere),
        "weather": "clear",
        "temperature": None,
        "city": args.city,
        "is_day": None,
        "source": "default",
    }
    ctx.update({k: v for k, v in file_data.items() if v is not None})

    # 命令行 / 环境变量显式覆盖
    if args.season:
        ctx["season"] = season_from_value(args.season) or ctx["season"]
    if args.weather:
        ctx["weather"] = normalize_weather(args.weather) or ctx["weather"]
    if args.temp is not None:
        ctx["temperature"] = args.temp
    env_season = season_from_value(os.getenv("SCENE_SEASON"))
    if env_season:
        ctx["season"] = env_season

    need_online = (not args.offline) and (
        args.refresh or not file_data.get("weather") or weather_file_is_stale(weather_path)
    )
    if need_online:
        try:
            live = fetch_open_meteo(args.lat, args.lon, args.timeout)
            ctx.update({k: v for k, v in live.items() if k in ("weather", "temperature", "is_day", "source")})
            print(f"[info] 实时天气：{ctx['weather']} / {ctx.get('temperature')}°C ({live['source']})")
            # 写回本地缓存，供下一条 workflow 或离线运行使用（不纳入提交）
            try:
                cache = dict(ctx)
                cache["city"] = args.city
                cache["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                weather_path.parent.mkdir(parents=True, exist_ok=True)
                weather_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
        except Exception as exc:  # 网络不可用时静默回退，保证一定能出图
            print(f"[warn] 实时天气获取失败，使用现有数据：{exc}", file=sys.stderr)

    if not ctx.get("weather"):
        ctx["weather"] = "clear"
    print(f"[info] 季节={ctx['season']} 天气={ctx['weather']} 城市={ctx['city']} 来源={ctx['source']}")
    return ctx


# ---------------------------------------------------------------- SVG 元素

def motion_group(content: str, cls: str, *, t: float, animate: bool,
                 dur: float, delay: float = 0.0, amp: float = 26.0,
                 origin: tuple[float, float] | None = None, mode: str = "drift") -> str:
    """同一段内容，animate=True 交给 CSS 播放；否则按相位 t 烘成静态帧（导出 GIF 用）。"""
    if animate:
        style = f"animation-duration:{dur:.2f}s;animation-delay:-{delay * dur:.2f}s"
        if origin:
            style += f";transform-origin:{origin[0]}px {origin[1]}px"
        return f'<g class="{cls}" style="{style}">{content}</g>'

    wave = math.sin(2 * math.pi * (t + delay))
    if mode == "rotate" and origin:
        return (f'<g transform="rotate({amp * wave:.2f} {origin[0]} {origin[1]})">'
                f'{content}</g>')
    if mode == "breathe":
        return f'<g opacity="{(0.775 + 0.225 * wave):.2f}">{content}</g>'
    if mode == "flash":
        frac = (t + delay) % 1.0
        return f'<g opacity="{0.95 if 0.90 < frac < 0.96 else 0.0}">{content}</g>'
    return f'<g transform="translate({amp * wave:.2f} 0)">{content}</g>'


def cloud_group(cx: float, cy: float, scale: float, color: str, opacity: float) -> str:
    e = lambda dx, dy, rx, ry: (
        f'<ellipse cx="{cx + dx * scale:.1f}" cy="{cy + dy * scale:.1f}" '
        f'rx="{rx * scale:.1f}" ry="{ry * scale:.1f}"/>'
    )
    return (
        f'<g fill="{color}" opacity="{opacity:.2f}">'
        f'{e(-46, 6, 44, 20)}{e(-16, -12, 38, 28)}{e(20, -4, 42, 24)}{e(48, 8, 36, 18)}'
        f'<rect x="{cx - 46 * scale:.1f}" y="{cy + 2 * scale:.1f}" '
        f'width="{94 * scale:.1f}" height="{20 * scale:.1f}" rx="{10 * scale:.1f}"/>'
        f'</g>'
    )


def stars(phase: str, season: str, rng: random.Random, *, t: float, animate: bool) -> str:
    if phase == "night":
        count, opacity = 46, 0.9
    elif phase == "dawn":
        count, opacity = 9, 0.35
    elif phase == "dusk":
        count, opacity = 6, 0.30
    else:
        return ""
    out = []
    for i in range(count):
        x = rng.uniform(20, WIDTH - 20)
        y = rng.uniform(12, HORIZON - 70)
        r = rng.choice((0.8, 1.0, 1.3, 1.7))
        base = opacity * rng.uniform(0.5, 1.0)
        delay = rng.uniform(0, 1)
        twinkle_dur = rng.uniform(2.2, 5.0)  # 两种模式消耗相同随机数，保证帧间构图稳定
        if animate:
            out.append(f'<circle class="star" cx="{x:.1f}" cy="{y:.1f}" r="{r}" '
                       f'fill="#fdfbe8" opacity="{base:.2f}" '
                       f'style="animation-duration:{twinkle_dur:.2f}s;'
                       f'animation-delay:-{delay * 4:.2f}s"/>')
        else:
            value = base * (0.68 + 0.32 * math.sin(2 * math.pi * (t + delay)))
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#fdfbe8" '
                       f'opacity="{max(0.05, value):.2f}"/>')
    return "".join(out)


def sun_or_moon(phase: str, palette: dict, *, t: float, animate: bool) -> str:
    sun_color = palette["sun"]
    if phase == "night":
        x, y, r = 905, 92, 34
        halo = "".join(
            f'<circle cx="{x}" cy="{y}" r="{r + 18 * i}" fill="#fff6cf" '
            f'opacity="{0.10 - 0.02 * i:.2f}"/>' for i in range(1, 4)
        )
        craters = "".join(
            f'<circle cx="{x + dx}" cy="{y + dy}" r="{cr}" fill="#e6dcb8" opacity="0.35"/>'
            for dx, dy, cr in ((-11, -8, 7), (9, 6, 5), (-4, 14, 4))
        )
        body = f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fdf6d8"/>' + craters

    else:
        positions = {"dawn": (250, 232, 46), "day": (610, 96, 40), "dusk": (955, 230, 46)}
        x, y, r = positions[phase]
        halo = "".join(
            f'<circle cx="{x}" cy="{y}" r="{r + 26 * i}" fill="{sun_color}" '
            f'opacity="{0.16 - 0.035 * i:.2f}"/>' for i in range(1, 4)
        )
        body = f'<circle cx="{x}" cy="{y}" r="{r}" fill="{sun_color}"/>'

    glow = motion_group(halo, "halo", t=t, animate=animate, dur=6.0,
                        delay=0.15, mode="breathe")
    return glow + body


def precipitation(weather: str, phase: str, rng: random.Random, *, t: float, animate: bool) -> str:
    out = []
    if weather in ("rain", "thunder"):
        color = "#cfe4f5" if phase == "night" else "#e8f3fb"
        for _ in range(70):
            x = rng.uniform(10, WIDTH - 10)
            y0 = rng.uniform(100, HORIZON + 40)
            length = rng.uniform(12, 24)
            op = rng.uniform(0.35, 0.8)
            speed = rng.uniform(0.7, 1.5)
            delay = rng.uniform(0, 1)
            if animate:
                dur = 1.1 / speed
                out.append(
                    f'<line class="drop" x1="{x:.1f}" y1="{y0:.1f}" '
                    f'x2="{x - 7:.1f}" y2="{y0 + length:.1f}" stroke="{color}" '
                    f'stroke-width="1.6" stroke-linecap="round" '
                    f'style="animation-duration:{dur:.2f}s;animation-delay:-{delay * dur:.2f}s"/>'
                )
            else:
                prog = (t * speed + delay) % 1.0
                y = y0 - 60 + prog * 230
                fade = min(1.0, prog * 9, (1 - prog) * 9)
                out.append(
                    f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x - 7:.1f}" '
                    f'y2="{y + length:.1f}" stroke="{color}" stroke-width="1.6" '
                    f'stroke-linecap="round" opacity="{op * fade:.2f}"/>'
                )
    elif weather == "snow":
        for _ in range(60):
            x = rng.uniform(10, WIDTH - 10)
            y0 = rng.uniform(90, HORIZON + 50)
            r = rng.uniform(1.4, 3.4)
            op = rng.uniform(0.5, 0.95)
            speed = rng.uniform(0.45, 0.95)
            delay = rng.uniform(0, 1)
            if animate:
                dur = 1.1 / speed
                out.append(
                    f'<circle class="drop" cx="{x:.1f}" cy="{y0:.1f}" r="{r:.1f}" '
                    f'fill="#ffffff" '
                    f'style="animation-duration:{dur:.2f}s;animation-delay:-{delay * dur:.2f}s"/>'
                )
            else:
                prog = (t * speed + delay) % 1.0
                y = y0 - 60 + prog * 230
                x += 12 * math.sin(2 * math.pi * (t * 0.5 + delay))
                fade = min(1.0, prog * 9, (1 - prog) * 9)
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" '
                           f'fill="#ffffff" opacity="{op * fade:.2f}"/>')
    if weather == "thunder":
        bolt = (
            '<path d="M 700 150 L 672 214 L 704 214 L 676 282 L 746 196 L 712 196 '
            'L 738 150 Z" fill="#ffe98a" opacity="0.92"/>'
            f'<rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" '
            f'fill="#ffffff" opacity="0.08"/>'
        )
        out.append(motion_group(bolt, "bolt", t=t, animate=animate, dur=7.0,
                                delay=0.3, mode="flash"))
    return "".join(out)


def fog_bands(*, t: float, animate: bool) -> str:
    out = []
    for i, (cx, cy, op) in enumerate(
        ((300, 250, 0.22), (820, 275, 0.26), (560, 305, 0.20), (1020, 320, 0.16))
    ):
        band = (f'<ellipse cx="{cx}" cy="{cy}" rx="420" ry="26" '
                f'fill="#eef3f7" opacity="{op:.2f}"/>')
        out.append(motion_group(band, "fogband", t=t, animate=animate,
                                dur=26.0, delay=i * 0.17, amp=34))
    return "".join(out)


def clouds(weather: str, phase: str, style: dict, rng: random.Random,
           *, t: float, animate: bool, wind: float) -> str:
    n = style["clouds"]
    color = dim(style["cloud"], 0.30 if phase == "night" else 0.0)
    out = []
    for i in range(n):
        cx = 130 + i * (940 / max(n, 1)) + rng.uniform(-45, 45)
        cy = rng.uniform(70, 165)
        scale = rng.uniform(0.85, 1.35) * (1.15 if weather in ("rain", "snow", "thunder", "overcast") else 1.0)
        cloud = cloud_group(cx, cy, scale, color, style["opacity"])
        out.append(motion_group(cloud, "cloud", t=t, animate=animate,
                                dur=max(9.0, 20.0 / wind), delay=rng.uniform(0, 1)))
    return "".join(out)


def mountains(palette: dict, weather: str, phase: str, season: str) -> str:
    far = dim(palette["hill_far"], 0.22 if phase == "night" else 0.0)
    near = dim(palette["hill_near"], 0.28 if phase == "night" else 0.0)
    far_p = (f'<path d="M0 300 L0 244 L120 214 L232 252 L352 196 L470 246 L600 206 '
             f'L742 250 L870 208 L1000 248 L1120 216 L1200 242 L1200 300 Z" fill="{far}"/>')
    near_p = (f'<path d="M0 302 L0 278 L150 258 L286 286 L420 262 L566 292 L700 264 '
              f'L838 290 L980 260 L1120 288 L1200 270 L1200 302 Z" fill="{near}"/>')
    ground_p = (f'<path d="M0 300 Q 300 292 600 300 T 1200 298 L1200 400 L0 400 Z" '
                f'fill="url(#ground)"/>')
    snow_cap = ""
    if weather == "snow" or (season == "winter" and weather in ("clear", "partly_cloudy")):
        snow_cap = ('<path d="M0 300 L0 244 L120 214 L150 226 L232 252 L352 196 L382 212 '
                    'L470 246 L600 206 L630 222 L742 250 L870 208 L900 222 L1000 248 '
                    'L1120 216 L1142 230 L1200 242 L1200 258 Z" fill="#f2f7fb" opacity="0.55"/>')
    return far_p + snow_cap + near_p + ground_p


def tree(x: float, base_y: float, scale: float, season: str, palette: dict, phase: str,
         *, t: float, animate: bool, wind: float, delay: float) -> str:
    trunk = dim(palette["trunk"], 0.25 if phase == "night" else 0.0)
    leaf = dim(palette["leaf"], 0.24 if phase == "night" else 0.0)
    alt = dim(palette["leaf_alt"], 0.24 if phase == "night" else 0.0)
    accent = palette["accent"]
    trunk_p = (
        f'<path d="M {x - 7 * scale:.1f} {base_y:.1f} L {x - 4 * scale:.1f} '
        f'{base_y - 62 * scale:.1f} L {x + 4 * scale:.1f} {base_y - 62 * scale:.1f} '
        f'L {x + 7 * scale:.1f} {base_y:.1f} Z" fill="{trunk}"/>'
    )
    out = []
    if season == "winter":
        out.append(
            f'<g stroke="{trunk}" stroke-width="{2.4 * scale:.1f}" fill="none">'
            f'<path d="M {x:.1f} {base_y - 44 * scale:.1f} L {x - 34 * scale:.1f} {base_y - 76 * scale:.1f}"/>'
            f'<path d="M {x:.1f} {base_y - 34 * scale:.1f} L {x + 32 * scale:.1f} {base_y - 70 * scale:.1f}"/>'
            f'<path d="M {x:.1f} {base_y - 56 * scale:.1f} L {x + 6 * scale:.1f} {base_y - 92 * scale:.1f}"/>'
            f'</g>'
        )
        for dx, dy, r in ((-30, -74, 15), (28, -68, 13), (4, -90, 12), (0, -62, 14)):
            out.append(f'<circle cx="{x + dx * scale:.1f}" cy="{base_y + dy * scale:.1f}" '
                       f'r="{r * scale:.1f}" fill="{leaf}" opacity="0.9"/>')
    else:
        blobs = ((-30, -70, 27), (28, -74, 25), (0, -100, 30), (-14, -96, 24), (16, -96, 24))
        for i, (dx, dy, r) in enumerate(blobs):
            color = alt if (season in ("spring", "autumn") and i % 2 == 0) else leaf
            out.append(f'<circle cx="{x + dx * scale:.1f}" cy="{base_y + dy * scale:.1f}" '
                       f'r="{r * scale:.1f}" fill="{color}"/>')
        if season == "spring":
            rng = random.Random(f"blossom-{x}")
            for _ in range(12):
                dx = rng.uniform(-48, 48) * scale
                dy = rng.uniform(-116, -58) * scale
                out.append(f'<circle cx="{x + dx:.1f}" cy="{base_y + dy:.1f}" '
                           f'r="{rng.uniform(1.6, 3.0) * scale:.1f}" fill="{accent}" opacity="0.9"/>')
    canopy = motion_group("".join(out), "tree", t=t, animate=animate,
                          dur=max(2.2, 4.4 / wind), delay=delay, amp=2.2,
                          origin=(x, base_y - 62 * scale), mode="rotate")
    return trunk_p + canopy


def labels(ctx: dict, phase: str, palette: dict) -> str:
    night = phase == "night"
    main = "#f2f6ff" if night else palette["text"]
    sub = "#b9c9e2" if night else dim(palette["text"], 0.0)
    city = escape(str(ctx.get("city") or ""))
    season = SEASON_LABELS.get(ctx["season"], ctx["season"])
    weather = WEATHER_LABELS.get(ctx["weather"], ctx["weather"])
    temp = ctx.get("temperature")
    temp_txt = f" · {temp:g}°C" if isinstance(temp, (int, float)) else ""
    title = f"{season} · {weather} · {PHASE_LABELS[phase]}"
    subtitle = f"{escape(ctx['season'].upper())} / {escape(ctx['weather'].upper())} · {city}{temp_txt}"
    if ctx.get("mode") == "single":
        corner = f"{phase.upper()} · {escape(str(ctx.get('stamp') or ''))} CST"
    else:
        corner = f"SCENE {PHASES.index(phase) + 1:02d} / {phase.upper()}"
    return (
        f'<text x="36" y="48" font-family="{FONT_FAMILY}" '
        f'font-size="27" font-weight="700" fill="{main}" letter-spacing="1">{escape(title)}</text>'
        f'<text x="37" y="74" font-family="{FONT_FAMILY}" '
        f'font-size="14" fill="{sub}" opacity="0.85" letter-spacing="1.5">{subtitle}</text>'
        f'<text x="{WIDTH - 36}" y="{HEIGHT - 26}" text-anchor="end" '
        f'font-family="{FONT_FAMILY}" font-size="13" '
        f'fill="{main}" opacity="0.65" letter-spacing="2">{corner}</text>'
    )


def build_svg(ctx: dict, phase: str, *, t: float = 0.0, animate: bool = True) -> str:
    """animate=True 输出自带 CSS 动画的矢量 SVG；False 则按相位 t 烘成静态帧。"""
    season = ctx["season"]
    weather = ctx["weather"]
    palette = PALETTES[season]
    style = WEATHER_STYLE.get(weather, WEATHER_STYLE["clear"])
    wind = WIND.get(weather, 0.8)
    rng = random.Random(f"{season}-{weather}-{phase}")
    sky_top, sky_bottom = palette["sky"][phase]
    dim_v = style["dim"] + (0.10 if phase == "night" else 0.0)
    sky_top, sky_bottom = dim(sky_top, dim_v), dim(sky_bottom, dim_v)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{escape(SEASON_LABELS[season])}·{escape(WEATHER_LABELS[weather])}·{PHASE_LABELS[phase]}动态场景">',
        '<defs>',
        f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{sky_top}"/>'
        f'<stop offset="100%" stop-color="{sky_bottom}"/></linearGradient>',
        f'<linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{lighten(dim(palette["ground"], 0.16 if phase == "night" else 0.0), 0.10)}"/>'
        f'<stop offset="100%" stop-color="{dim(palette["ground"], 0.30 if phase == "night" else 0.12)}"/></linearGradient>',
        '</defs>',
        MOTION_CSS if animate else '',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#sky)"/>',
        stars(phase, season, rng, t=t, animate=animate),
        sun_or_moon(phase, palette, t=t, animate=animate),
        clouds(weather, phase, style, rng, t=t, animate=animate, wind=wind),
    ]
    if weather == "fog":
        parts.append(fog_bands(t=t, animate=animate))
    parts.append(mountains(palette, weather, phase, season))
    parts.append(precipitation(weather, phase, rng, t=t, animate=animate))
    parts.append(tree(128, HORIZON + 62, 1.05, season, palette, phase,
                      t=t, animate=animate, wind=wind, delay=0.05))
    parts.append(tree(1062, HORIZON + 78, 1.35, season, palette, phase,
                      t=t, animate=animate, wind=wind, delay=0.42))
    parts.append(tree(972, HORIZON + 52, 0.68, season, palette, phase,
                      t=t, animate=animate, wind=wind, delay=0.76))
    if weather == "fog":
        parts.append(f'<rect x="0" y="{HORIZON - 60}" width="{WIDTH}" height="140" '
                     f'fill="#eef3f7" opacity="0.12"/>')
    parts.append(labels(ctx, phase, palette))
    parts.append('</svg>')
    return "\n".join(p for p in parts if p) + "\n"


# ---------------------------------------------------------------- GIF 导出（可选依赖）

def _svg_frame(svg_text: str, width: int, height: int):
    """把一张（已烘焙好相位的）静态帧 SVG 渲染成 PIL RGB 图像。"""
    import io
    import cairosvg
    from PIL import Image

    png = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"),
                           output_width=width, output_height=height)
    return Image.open(io.BytesIO(png)).convert("RGB")


def _save_gif(images: list, path: Path, frame_ms: int) -> None:
    from PIL import Image

    # 全片共用一张自适应调色板，避免逐帧量化造成的颜色跳动
    palette = images[0].quantize(colors=128, method=Image.MEDIANCUT)
    try:
        frames = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    except AttributeError:  # 老版 Pillow
        frames = [im.quantize(palette=palette) for im in images]
    frames[0].save(
        str(path), save_all=True, append_images=frames[1:],
        duration=frame_ms, loop=0, optimize=True, disposal=2,
    )


def export_gif(ctx: dict, phase: str, path: Path, *, frames: int,
               frame_ms: int, width: int = 600, height: int = 200) -> None:
    images = [
        _svg_frame(build_svg(ctx, phase, t=k / frames, animate=False), width, height)
        for k in range(frames)
    ]
    _save_gif(images, path, frame_ms)


def export_preview_gif(ctx: dict, path: Path, *, frames: int, frame_ms: int,
                       cell: tuple[int, int] = (420, 140)) -> None:
    from PIL import Image

    pad, gap = 12, 8
    w, h = cell
    size = (w * 2 + pad * 2 + gap, h * 2 + pad * 2 + gap)
    images = []
    for k in range(frames):
        t = k / frames
        canvas = Image.new("RGB", size, "#eef1f5")
        for i, phase in enumerate(PHASES):
            frame = _svg_frame(build_svg(ctx, phase, t=t, animate=False), w, h)
            canvas.paste(frame, (pad + (i % 2) * (w + gap), pad + (i // 2) * (h + gap)))
        images.append(canvas)
    _save_gif(images, path, frame_ms)


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="按天气与季节生成四个日夜场景 SVG")
    p.add_argument("--out-dir", default=os.getenv("SCENE_OUT_DIR", "assets"), help="输出目录")
    p.add_argument("--weather-json", default=os.getenv("SCENE_WEATHER_JSON"),
                   help="天气输入文件（默认 data/weather.json，存在时优先读取）")
    p.add_argument("--lat", type=float, default=float(os.getenv("SCENE_LAT", "23.09")), help="纬度")
    p.add_argument("--lon", type=float, default=float(os.getenv("SCENE_LON", "110.18")), help="经度")
    p.add_argument("--city", default=os.getenv("SCENE_CITY", "玉林"), help="画面上的城市名")
    p.add_argument("--season", default=os.getenv("SCENE_SEASON"), help="强制季节 spring/summer/autumn/winter")
    p.add_argument("--weather", default=os.getenv("SCENE_WEATHER"), help="强制天气 key 或描述")
    p.add_argument("--temp", type=float, default=None, help="强制温度（°C）")
    p.add_argument("--hemisphere", choices=("north", "south"),
                   default=os.getenv("SCENE_HEMISPHERE", "north"), help="半球，影响季节推算")
    p.add_argument("--timeout", type=float, default=float(os.getenv("SCENE_TIMEOUT", "10")),
                   help="实时天气请求超时（秒）")
    p.add_argument("--offline", action="store_true", default=os.getenv("SCENE_OFFLINE") == "1",
                   help="不联网，仅用本地天气文件与默认值")
    p.add_argument("--refresh", action="store_true",
                   help="即使已有本地天气文件也强制拉取实时天气")
    p.add_argument("--single", action="store_true", default=os.getenv("SCENE_SINGLE") == "1",
                   help="只输出一张 scene-live：时段按当前北京时间自动判定（默认四张）")
    p.add_argument("--phase", choices=PHASES, default=os.getenv("SCENE_PHASE"),
                   help="单张模式下强制时段（默认按时间自动判定）")
    p.add_argument("--static", action="store_true", default=os.getenv("SCENE_STATIC") == "1",
                   help="输出不含动画的静态 SVG（默认输出动态 SVG）")
    p.add_argument("--gif", action="store_true", default=os.getenv("SCENE_GIF") == "1",
                   help="额外导出 GIF 动图（可选依赖 cairosvg + Pillow）")
    p.add_argument("--preview", action="store_true", default=os.getenv("SCENE_PREVIEW") == "1",
                   help="额外导出四宫格预览动图（隐含 --gif 的渲染能力）")
    p.add_argument("--frames", type=int, default=int(os.getenv("SCENE_FRAMES", "20")),
                   help="动图帧数（默认 20）")
    p.add_argument("--frame-ms", type=int, default=int(os.getenv("SCENE_FRAME_MS", "90")),
                   help="每帧时长，毫秒（默认 90）")
    return p


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    args = build_parser().parse_args()
    ctx = resolve_context(args)
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    single = args.single
    if single:
        phase = args.phase or phase_now()
        ctx["mode"] = "single"
        ctx["stamp"] = beijing_stamp()
        targets: list[tuple[str, str]] = [("scene-live", phase)]
        print(f"[info] 单张模式：当前时段 = {phase}"
              f"（北京时间 {beijing_now().strftime('%Y-%m-%d %H:%M')}）")
    else:
        targets = [(f"scene-{p}", p) for p in PHASES]

    animated = not args.static
    gif_ok = False
    generated = []
    for stem, phase in targets:
        svg = build_svg(ctx, phase, animate=animated)
        try:
            ET.fromstring(svg)  # 生成即校验，避免产出非法 SVG
        except ET.ParseError as exc:
            print(f"[error] {stem} SVG 非法：{exc}", file=sys.stderr)
            return 1
        path = out_dir / f"{stem}.svg"
        path.write_text(svg, encoding="utf-8")
        generated.append(path.name)
        print(f"[ok] {display_path(path)}")

    want_gif = args.gif or args.preview
    if want_gif:
        try:
            if args.gif:
                for stem, phase in targets:
                    gif_path = out_dir / f"{stem}.gif"
                    export_gif(ctx, phase, gif_path, frames=args.frames, frame_ms=args.frame_ms)
                    generated.append(gif_path.name)
                    print(f"[ok] {display_path(gif_path)}")
            if args.preview and not single:
                preview_path = out_dir / "scene-preview.gif"
                export_preview_gif(ctx, preview_path, frames=args.frames, frame_ms=args.frame_ms)
                generated.append(preview_path.name)
                print(f"[ok] {display_path(preview_path)}")
            gif_ok = True
        except ImportError as exc:
            print(f"[warn] 动图导出跳过（缺少可选依赖）：{exc}", file=sys.stderr)
            print("[warn] 安装：pip install cairosvg Pillow", file=sys.stderr)

    meta = {
        "mode": "single" if single else "quad",
        "season": ctx["season"],
        "season_label": SEASON_LABELS[ctx["season"]],
        "weather": ctx["weather"],
        "weather_label": WEATHER_LABELS[ctx["weather"]],
        "temperature": ctx.get("temperature"),
        "city": ctx.get("city"),
        "source": ctx.get("source", "default"),
        "animated": animated,
        "gif": gif_ok,
        "frames": args.frames if gif_ok else None,
        "frame_ms": args.frame_ms if gif_ok else None,
        "updated_at": ctx.get("stamp"),
        "phase": targets[0][1] if single else None,
        "phase_label": PHASE_LABELS[targets[0][1]] if single else None,
        "live_file": "scene-live.svg" if single else None,
        "live_gif": "scene-live.gif" if (single and gif_ok) else None,
        "phases": [
            {
                "key": p, "label": PHASE_LABELS[p],
                "file": f"{stem}.svg",
                "gif": f"{stem}.gif" if gif_ok else None,
            }
            for stem, p in targets
        ],
    }
    (out_dir / "scenes.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[ok] {display_path(out_dir / 'scenes.json')}")
    print(f"[done] {'单张' if single else '四张'} × {'动态' if animated else '静态'} SVG"
          + (f" + {len(targets)} 张 GIF" + (" + 四宫格预览" if (args.preview and not single) else "")
             if gif_ok else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
