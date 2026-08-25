# -*- coding: utf-8 -*-
"""
Trae 企业版用量查询脚本（纯 HTTP 查询，不依赖浏览器）

原理：
  用量页的所有数据都来自 console.enterprise.trae.cn 的 JSON 接口，
  鉴权只需一个会话 Cookie: X-Cloudide-Tob-Session。
  本脚本用该 Cookie 直接请求接口，把“用量管理”关心的字段整理成易读（带颜色）报告。

Cookie 获取方式（会话过期后需重新获取）：
  1. 在已登录的浏览器打开 https://console.enterprise.trae.cn/personal/usage
  2. F12 -> Network，刷新页面，任选一条该域名的 fetch 请求
  3. 在 Request Headers 里复制 Cookie 值（含 X-Cloudide-Tob-Session=...）
  4. 把 Cookie 填到下面三处之一（优先级从高到低）：
       a) 环境变量  TRAE_COOKIE
       b) 同目录文件 trae_session.txt（一行，纯 Cookie 字符串）
       c) 本文件里的 COOKIE 常量

运行：
  python trae_usage.py
  python trae_usage.py --json        # 只看原始 JSON（也总是存到 usage_result.json）
  python trae_usage.py --days 7      # 趋势/明细只看最近 N 天（默认 31）
  python trae_usage.py --no-color    # 关闭颜色（管道/日志重定向时有用）
  python trae_usage.py --width 100   # 强制报告宽度（默认按终端宽度自适应）
  python trae_usage.py --no-zebra    # 关闭隔行底色（斑马纹）
  python trae_usage.py --zebra-theme light  # 浅色终端用浅灰底（默认 auto 探测）
"""
import argparse
import json
import os
import sys
import re
import shutil
import html
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================ Cookie 配置 ============================
# 内置 Cookie 常量（优先级最低）。建议使用环境变量 TRAE_COOKIE 或
# 同目录 trae_session.txt，本常量保持为空即可。
COOKIE = ""
# ====================================================================

BASE = "https://console.enterprise.trae.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0")
OUT_JSON = Path(__file__).parent / "usage_result.json"

# 每日趋势接口的统计维度：1 = 按天（返回每日一行：生成/采纳代码行数等）
TREND_DAILY = 1

# ===================== 模型单价：实时抓取 + 本地缓存 =====================
# 单价数据源为官网计费说明页 https://docs.trae.cn/enterprise_billing-items（无需登录）。
# 每次运行实时抓取并解析；成功后写入本地缓存 model_prices_cache.json
# （记录 fetched_at 与 has_active），官网不可达时读取缓存；两者均失败时价格表显示提示。
PRICE_URL = "https://docs.trae.cn/enterprise_billing-items"
PRICE_CACHE = Path(__file__).parent / "model_prices_cache.json"


def fetch_price_html():
    """抓取官网计费说明页（无需登录），返回 HTML 文本；失败抛异常。"""
    req = urllib.request.Request(PRICE_URL, headers={
        "User-Agent": UA, "Accept-Language": "zh"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def _price_disp(cell):
    """把单价单元格 (纯文本, 划掉原价) 渲染成显示字符串。
    含活动价时显示 '活动价 (刊例原价)'，如 '4.80 (12.00)'；否则仅刊例原价 '6.00'。"""
    txt, orig = cell
    txt = txt.strip()
    if txt in ("-", "", "—"):
        return "-"
    m_act = re.search(r"活动价[:：]\s*([\d.]+)", txt)
    if m_act:
        active = m_act.group(1)
        if orig is not None:
            return f"{float(active):.2f} ({float(orig):.2f})"
        return f"{float(active):.2f}"
    num = orig
    if num is None:
        m = re.search(r"([\d.]+)", txt)
        num = m.group(1) if m else None
    return f"{float(num):.2f}" if num is not None else "-"


def _has_activity(prices):
    """本次价格中是否含活动价（显示串含 ' (' 括号）。"""
    for _, tiers in prices:
        for tier in tiers:
            for d in tier[1:]:
                if isinstance(d, str) and " (" in d:
                    return True
    return False


def _norm_ctx(s):
    s = html.unescape(s).strip()
    if s in ("-", ""):
        return "-"
    # 去掉 “输入/输出/输入&输出/：/:” 等前缀与空格，只保留分档区间（如 [0,1M]）
    for p in ("输入&输出", "输入", "输出", "&", "：", ":", " "):
        s = s.replace(p, "")
    return s


def parse_model_prices(html_text):
    """从计费页 HTML 解析出模型单价。
    返回 [(模型名, [(上下文, 输入显示串, 输出显示串, 缓存显示串), ...]), ...]，失败返回 None。
    显示串已格式化：含活动价时 '活动价 (刊例原价)'（如 '4.80 (12.00)'），否则 '6.00'，无价格 '-'。"""
    tables = re.findall(r"<table.*?</table>", html_text, re.S | re.I)
    target = None
    for t in tables:
        if "模型名称" in t and "缓存" in t:
            target = t
            break
    if not target:
        return None
    tb = re.search(r"<tbody>.*?</tbody>", target, re.S | re.I)
    body = tb.group(0) if tb else target
    rows = re.findall(r"<tr>.*?</tr>", body, re.S | re.I)
    prices, cur = [], None
    for r in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S | re.I)
        if not cells:
            continue
        parsed = []
        for c in cells:
            # 先取被 <s> 划掉的“刊例原价”
            m_del = re.search(r"<s>\s*([\d.]+)\s*</s>", c, re.S | re.I)
            orig = m_del.group(1) if m_del else None
            txt = re.sub(r"<[^>]+>", " ", c)
            txt = re.sub(r"\s+", " ", html.unescape(txt)).strip()
            parsed.append((txt, orig))
        if len(parsed) >= 5:
            name, cur, rest = parsed[0][0].strip(), parsed[0][0].strip(), parsed[1:]
        else:
            name, rest = cur, parsed
        if len(rest) < 4:
            continue
        if name in ("模型名称", "上下文长度"):
            continue
        ctx = _norm_ctx(rest[0][0])
        pin = _price_disp(rest[1])
        pout = _price_disp(rest[2])
        pcache = _price_disp(rest[3])
        if prices and prices[-1][0] == name:
            prices[-1][1].append((ctx, pin, pout, pcache))
        else:
            prices.append((name, [(ctx, pin, pout, pcache)]))
    # 合理性校验：正常页面应有数十个模型，数量过少说明页面结构可能已变化
    if not prices or len(prices) < 3:
        return None
    return prices


def _save_price_cache(prices, has_active):
    """将抓取结果持久化到本地缓存文件（JSON），供官网不可达时离线使用。"""
    try:
        data = {"fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "has_active": has_active, "prices": prices}
        PRICE_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_price_cache():
    """读取本地价格缓存；损坏/缺失返回 None。"""
    try:
        data = json.loads(PRICE_CACHE.read_text(encoding="utf-8"))
        if data.get("prices"):
            return data
    except Exception:
        pass
    return None


def get_model_prices():
    """返回 (prices, source, has_active, fetched_at, parse_hint)。
    prices: [(名, [(上下文, 输入, 输出, 缓存), ...])]，显示串已格式化。
    source: 'live'（实时抓取成功并写入本地缓存）/ 'cache'（官网不可达或解析失败，读取本地缓存）/
            'builtin'（抓取与缓存均失败，返回空列表由渲染层提示）。
    parse_hint: 官网页面结构可能已变化的提示（正常为空字符串）。
    抓取成功后立即更新本地缓存 model_prices_cache.json。"""
    parse_hint = ""
    try:
        parsed = parse_model_prices(fetch_price_html())
        if parsed:
            fa = _has_activity(parsed)
            _save_price_cache(parsed, fa)
            return parsed, "live", fa, datetime.now().strftime("%Y-%m-%d %H:%M"), ""
        parse_hint = "官网计费页结构可能已更新，实时解析失败；当前为缓存数据，请手动检查官网。"
    except Exception:
        pass
    cached = _load_price_cache()
    if cached:
        return cached["prices"], "cache", bool(cached.get("has_active")), cached.get("fetched_at", ""), parse_hint
    # 无缓存且抓取/解析均失败：返回空列表，由渲染层提示
    return [], "builtin", False, "", parse_hint


# ----------------- 表格渲染（Unicode 边框 + 斑马纹）-----------------
def _bg_theme():
    """猜测终端背景是深还是浅，决定斑马底色。返回 'dark' / 'light' / None。"""
    fg_bg = os.environ.get("COLORFGBG", "")
    if fg_bg:
        parts = fg_bg.split(";")
        try:
            bg = int(parts[1]) if len(parts) > 1 else int(parts[0])
        except ValueError:
            bg = None
        if bg is not None:
            return "light" if bg > 7 else "dark"
    if os.environ.get("WT_SESSION"):      # Windows Terminal 默认深色
        return "dark"
    return None


def draw_table(W, headers, rows, widths=None, zebra=True, zebra_theme="auto"):
    """通用表格渲染：Unicode 边框 + 列分隔 + 斑马纹行底，提升行间区分度。
    headers: [(文本, 颜色), ...]   rows: [[(文本, 颜色), ...], ...]
    widths:  可选，各列显示宽度（中文计 2）；省略按内容自适应。保证整体宽度恰好 = W。
    zebra:   隔行铺底色，横向扫读更易区分行；zebra_theme 控制深/浅终端底色。"""
    n = len(headers)
    if widths is None:
        widths = [dwidth(h[0]) for h in headers]
        for row in rows:
            for j, (t, _) in enumerate(row):
                if j < n:
                    widths[j] = max(widths[j], dwidth(t))
    # 收缩/扩张最后一列，使总宽 == W（不折行）
    total = sum(widths) + (n - 1) + 2
    widths[-1] = max(4, widths[-1] + (W - total))
    TL, TR, ML, MR, BL, BR = "┌", "┐", "├", "┤", "└", "┘"
    H, JT, JB, JC = "─", "┬", "┴", "┼"
    VC = paint("│", CYAN)   # 竖线（带颜色，用于边框/表头行）
    VCN = CYAN + "│"        # 竖线（无 RESET，用于斑马行，避免清掉行底底色）
    inner = sum(widths) + (n - 1)
    bounds, acc = set(), 0
    for w in widths[:-1]:
        acc += w + 1
        bounds.add(acc - 1)

    def hline(l, r, joint):
        line = [l]
        for i in range(inner):
            line.append(joint if i in bounds else H)
        line.append(r)
        return paint("".join(line), CYAN)

    # 斑马行底色（仅彩色输出时启用；auto 探测失败则按深色终端处理）
    band = ""
    if zebra and COLOR_ON:
        theme = zebra_theme if zebra_theme != "auto" else (_bg_theme() or "dark")
        band = ZEBRA_DARK if theme == "dark" else ZEBRA_LIGHT

    print(hline(TL, TR, JT))
    head = []
    for j, (t, c) in enumerate(headers):
        if dwidth(t) > widths[j]:
            t = dellipsis(t, widths[j])
        head.append(dpad(t, widths[j], "<" if j == 0 else ">", c))
    print(VC + VC.join(head) + VC)
    print(hline(ML, MR, JC))
    for idx, row in enumerate(rows):
        parts = []
        for j, (t, c) in enumerate(row):
            s = str(t)
            if dwidth(s) > widths[j]:
                s = dellipsis(s, widths[j])
            padded = dpad(s, widths[j], "<" if j == 0 else ">")
            # 斑马行内不插 RESET：底色靠整行首尾各一次 RESET 维持不间断
            parts.append((c + padded) if (c and COLOR_ON) else padded)
        line = VCN + VCN.join(parts) + VCN
        if band and (idx % 2 == 1):
            print(band + line + RESET)
        else:
            print(line + RESET)
    print(hline(BL, BR, JB))


# ----------------------------- 配色 -----------------------------
# 设计：青色做标题/分隔，绿色=健康，黄色=偏高(70%~100%)，红色=超额(>100%)，
#       模型名用蓝色，标签用暗绿。在深色终端下最清晰；Windows 已启用 VT 支持。
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"

# 斑马纹行底：隔行铺浅色底块，提升行间区分度。
# 深色终端使用略亮于背景的灰色；浅色终端使用浅灰。均通过 ANSI 背景色实现。
ZEBRA_DARK = "\033[48;5;235m"            # 约 #262626
ZEBRA_LIGHT = "\033[48;2;238;238;238m"   # 约 #eeeeee

COLOR_ON = True


def _enable_vt():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


# ----------------- 显示宽度（中文按 2 列计）-----------------
def dwidth(s):
    """字符串的“显示宽度”：CJK/全角字符算 2，其余算 1。"""
    w = 0
    for c in str(s):
        w += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return w


def dpad(s, width, align="<", color=None):
    """按“显示宽度”排版（而非字符数），保证中文不撑破列对齐。
    颜色码放在排版结果之外，避免转义序列干扰宽度计算。"""
    s = str(s)
    pad = width - dwidth(s)
    if pad < 0:
        pad = 0
    if align == "<":
        txt = s + " " * pad
    elif align == ">":
        txt = " " * pad + s
    else:  # center
        left = pad // 2
        txt = " " * left + s + " " * (pad - left)
    if color and COLOR_ON:
        return color + txt + RESET
    return txt


def dellipsis(s, width):
    """显示宽度超过 width 时，按显示宽度截断并追加省略号。"""
    if dwidth(s) <= width:
        return s
    res, w = "", 0
    for c in str(s):
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > width - 1:  # 预留 1 列给省略号
            break
        res += c
        w += cw
    return res + "…"


def paint(s, color):
    if not COLOR_ON:
        return str(s)
    return f"{color}{s}{RESET}"


def ratio_color(r):
    if r >= 1.0:
        return RED
    if r >= 0.7:
        return YELLOW
    return GREEN


def share_color(s):
    """“占总量”类比例的配色：占比 ≥50% 红色、≥20% 黄色、其余绿色。"""
    if s >= 0.5:
        return RED
    if s >= 0.2:
        return YELLOW
    return GREEN


def load_cookie():
    env = os.environ.get("TRAE_COOKIE")
    if env and env.strip():
        return env.strip()
    f = Path(__file__).parent / "trae_session.txt"
    if f.exists():
        c = f.read_text(encoding="utf-8").strip()
        if c:
            return c
    if COOKIE and COOKIE.strip():
        return COOKIE.strip()
    return None


def print_cookie_guide(expired=False):
    """打印「如何获取/更新 Cookie」的分步指导（新手向）。
    首次使用找不到 Cookie，或 401/403 判定 Cookie 过期时调用。"""
    f = Path(__file__).parent / "trae_session.txt"
    title = "Cookie 已过期，请按以下步骤重新获取：" if expired else "首次使用，请按以下步骤获取 Cookie："
    print(paint("[i] " + title, YELLOW))
    print(f"  1. 用浏览器（推荐 Edge/Chrome）登录用量页：{BASE}/personal/usage")
    print("  2. 按 F12 打开开发者工具，切到 Network（网络）标签")
    print("  3. 刷新页面，在请求列表里点击任意一条 console.enterprise.trae.cn 的 fetch/XHR 请求")
    print("  4. 在右侧 Request Headers（请求标头）里找到 Cookie 这一行，复制它的完整值")
    print(paint("     （只需包含 X-Cloudide-Tob-Session=... 这一段即可，不要带 “Cookie: ” 前缀）", DIM))
    print(f"  5. 新建或覆盖文件 {f}，把复制的内容粘贴为文件里唯一的一行并保存")
    print("     （也可以改用环境变量 TRAE_COOKIE，效果相同）")
    print(paint("  常见坑：值带了引号、包含换行、或缺少 X-Cloudide-Tob-Session 都会导致鉴权失败。", DIM))
    print(paint("  安全提示：个人电脑用文件最方便；公共/共享环境建议改用环境变量 TRAE_COOKIE（临时注入，终端关闭即失效）。", DIM))


def call(path, body, _retry=True):
    url = BASE + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Language", "zh")
    req.add_header("Cookie", COOKIE_VALUE)
    req.add_header("Origin", BASE)
    req.add_header("Referer", BASE + "/personal/usage")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # _raw 仅保留前 500 字符并做简单脱敏（手机号/邮箱），避免错误页中的敏感信息被完整落盘
        body = e.read().decode("utf-8", "replace")[:500]
        body = re.sub(r"1[3-9]\d{9}", "***", body)
        body = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "***@***", body)
        return {"_error": e.code, "_raw": body}
    except Exception as e:
        # 网络层异常（超时/连接失败/响应非 JSON）：短暂退避后重试一次，缓解瞬时网络波动
        if _retry:
            time.sleep(0.5)
            return call(path, body, _retry=False)
        return {"_error": str(e)}


def scan_api_errors(raw):
    """收集所有带 _error 的接口，返回 [(endpoint, code_or_msg), ...]。"""
    out = []
    for name, r in raw.items():
        if isinstance(r, dict) and "_error" in r:
            out.append((name, r.get("_error")))
    return out


def fmt_dt(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def fmt_date(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def fmt_num(n):
    if n is None:
        return "-"
    return f"{n:,}"


def money(n):
    """金额统一显示：保留两位小数，千分位。"""
    if n is None:
        return "-"
    return f"{n:,.2f}"


def hr(title, W):
    print(paint("=" * W, CYAN))
    print(paint(dellipsis(title, W - 2), BOLD + CYAN))
    print(paint("=" * W, CYAN))


def label_lines(pairs, gap=2, label_color=GREEN):
    """成对渲染「标签 : 值」。标签按“最大显示宽度”对齐，绝不会因中文撑破列。"""
    L = max((dwidth(l) for l, _ in pairs), default=0) + gap
    for label, val in pairs:
        print("  " + dpad(label, L, "<", label_color) + ": " + val)


def get_width(args):
    """报告宽度：优先 --width；否则取终端列数；非 tty 时取 80。"""
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        cols = 80
    if args.width:
        cols = args.width
    # 夹紧到合理范围：太窄列会挤、太宽在窄终端会折行
    return max(48, min(cols, 200))


def print_model_prices(W, zebra=True, zebra_theme="auto", preset=None):
    """渲染模型单价参考表：优先实时抓取官网，成功后写入本地缓存，供下次离线读取。
    preset: 可选，main 中并发预取得到的 (prices, source, has_active, fetched_at)，传入可避免重复请求。"""
    prices, source, has_active, fetched_at, parse_hint = preset if preset is not None else get_model_prices()
    hr("模型单价参考（¥/百万 Tokens）", W)
    if not prices:
        print(paint(f"  [!] 实时抓取与本地缓存均失败，暂无价格数据，请稍后重试或检查网络；"
                    f"以官网实时价格为准：{PRICE_URL}", YELLOW))
        return
    # 价格列宽按内容自适应（活动价串如 '4.80 (12.00)' 较长），模型名列吸收剩余宽度
    def _maxw(idx, mn):
        m = mn
        for _, tiers in prices:
            for t in tiers:
                m = max(m, dwidth(str(t[idx])))
        return m
    CTX_W = _maxw(0, 10)
    IN_W = _maxw(1, 7)
    OUT_W = _maxw(2, 7)
    CACHE_W = _maxw(3, 7)
    NAME_W = max(20, W - (CTX_W + IN_W + OUT_W + CACHE_W + 4 + 2))
    headers = [("模型", BLUE), ("上下文", GREEN), ("输入", GREEN),
               ("输出", GREEN), ("缓存", GREEN)]
    widths = [NAME_W, CTX_W, IN_W, OUT_W, CACHE_W]
    rows = []
    for name, tiers in prices:
        for i, tier in enumerate(tiers):
            ctx, pin, pout, pcache = tier
            nm = name if i == 0 else ""
            nm = dellipsis(nm, NAME_W)
            col = WHITE if i == 0 else DIM
            rows.append([
                (nm, BLUE if i == 0 else DIM),
                (ctx if ctx != "-" else "-", col),
                (pin, col), (pout, col), (pcache, col),
            ])
    draw_table(W, headers, rows, widths, zebra=zebra, zebra_theme=zebra_theme)
    print(paint("  单位：元 / 百万 Tokens。按上下文分档的模型已逐档列出。", DIM))
    if has_active:
        # 从本次数据中选取第一个含活动价的模型作为示例
        for nm, tiers in prices:
            for tier in tiers:
                if any(isinstance(d, str) and " (" in d for d in tier[1:]):
                    _, pin, pout, pcache = tier
                    for lab, d in (("输入", pin), ("输出", pout), ("缓存", pcache)):
                        if isinstance(d, str) and " (" in d:
                            print(paint(f"  含活动价的模型，单元格显示 “活动价 (刊例原价)”，如 {nm} {lab} {d}。", DIM))
                            break
                    break
            else:
                continue
            break
    if source == "live":
        print(paint(f"  来源：{PRICE_URL}（实时抓取，{fetched_at}），已写入本地缓存 model_prices_cache.json，下次离线也可读。", DIM))
    elif source == "cache":
        note = "本次官网不可达" if not parse_hint else "实时解析失败"
        print(paint(f"  来源：本地缓存 model_prices_cache.json（抓取于 {fetched_at or '未知'}，{note}）；以官网实时价格为准。", DIM))
        if parse_hint:
            print(paint(f"  [!] {parse_hint}", YELLOW))
        # 缓存新鲜度：超过 7 天提示可能过时
        try:
            age = (datetime.now() - datetime.strptime(fetched_at, "%Y-%m-%d %H:%M")).days
            if age > 7:
                print(paint(f"  [!] 缓存已 {age} 天，价格可能过时，请联网运行一次以刷新。", YELLOW))
        except Exception:
            pass
    else:
        print(paint(f"  [!] 实时抓取与本地缓存均失败，暂无价格数据，请稍后重试或检查网络；以官网实时价格为准：{PRICE_URL}", YELLOW))
        if parse_hint:
            print(paint(f"  [!] {parse_hint}", YELLOW))


def main():
    global COOKIE_VALUE, COLOR_ON
    _enable_vt()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="只打印原始 JSON")
    ap.add_argument("--days", type=int, default=31, help="趋势/明细统计天数")
    ap.add_argument("--no-color", action="store_true", help="关闭颜色")
    ap.add_argument("--width", type=int, default=0, help="强制报告宽度（列）")
    ap.add_argument("--no-zebra", action="store_true", help="关闭隔行底色（斑马纹）")
    ap.add_argument("--zebra-theme", choices=["auto", "dark", "light"],
                   default="auto", help="隔行底色适用的终端主题（auto=探测，默认深色）")
    args = ap.parse_args()
    if args.no_color:
        COLOR_ON = False
    # 管道/重定向到文件时自动去色，避免一堆转义码；终端里保留颜色
    elif not sys.stdout.isatty():
        COLOR_ON = False

    W = get_width(args)
    zebra = not args.no_zebra
    zebra_theme = args.zebra_theme

    COOKIE_VALUE = load_cookie()
    if not COOKIE_VALUE:
        print(paint("[!] 找不到 Cookie。", RED))
        print_cookie_guide(expired=False)
        sys.exit(1)

    end = int(datetime.now().timestamp() * 1000)
    start = int((datetime.now() - timedelta(days=args.days)).timestamp() * 1000)

    # 并发拉取各接口：仅网络 I/O 并行；结果由主线程统一收集（无并发写），
    # 渲染阶段按固定顺序读取，输出确定性不受并发影响。
    _endpoints = [
        ("GetUserInfo", "/cloudide/api/v3/trae/GetUserInfo",
         {"Attributes": ["ent_limit_usage_model"]}),
        ("get_personal_core_data", "/trae/gtm/tob/api/v1/config/get_personal_core_data", {}),
        ("get_personal_quota", "/trae/gtm/tob/api/v1/config/get_personal_quota", {}),
        ("get_user_model_usage", "/trae/gtm/tob/api/v1/config/get_user_model_usage", {}),
        ("get_model_quota", "/trae/gtm/tob/api/v1/config/get_model_quota", {}),
        ("get_personal_trend_data", "/trae/gtm/tob/api/v1/config/get_personal_trend_data",
         {"TrendType": TREND_DAILY, "StartTime": start, "EndTime": end}),
        ("get_user_token_usage_detail", "/trae/gtm/tob/api/v1/config/get_user_token_usage_detail",
         {"start_time": start, "end_time": end}),
    ]
    raw = {}
    price_result = None
    with ThreadPoolExecutor(max_workers=len(_endpoints) + 1) as _ex:
        _futs = {_ex.submit(call, p, b): n for n, p, b in _endpoints}
        # 模型单价页不依赖 Cookie，可与其他接口一并并发拉取
        _pfut = _ex.submit(get_model_prices)
        for _f in as_completed(_futs):
            _n = _futs[_f]
            raw[_n] = _f.result()  # 仅主线程写回 raw
        price_result = _pfut.result()
    # 按固定 key 解包接口结果，供下方各渲染节使用
    user = raw["GetUserInfo"]
    core = raw["get_personal_core_data"]
    quota = raw["get_personal_quota"]
    model_usage = raw["get_user_model_usage"]
    model_quota = raw["get_model_quota"]
    trend = raw["get_personal_trend_data"]
    detail = raw["get_user_token_usage_detail"]
    OUT_JSON.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return

    # 接口健康度检查：401/403 通常表示 Cookie 过期，需提示用户重新获取
    errors = scan_api_errors(raw)
    auth_hits = [(n, c) for n, c in errors if c in (401, 403)]
    if auth_hits:
        names = ", ".join(n for n, _ in auth_hits)
        code = auth_hits[0][1]
        print(paint(f"[!] 鉴权失败（HTTP {code}）：接口 {names} 被拒绝。", RED))
        print_cookie_guide(expired=True)
        sys.exit(2)
    if errors:
        names = ", ".join(n for n, _ in errors)
        print(paint(f"[!] 以下接口返回错误，报告可能不完整：{names}", YELLOW))

    # ----------------- 顶部标题框（按 W 自适应）-----------------
    print()
    print(paint("#" * W, CYAN))
    print(paint("# " + dpad("Trae 企业版用量报告", W - 3, "<"), BOLD + CYAN))
    print(paint("# " + dpad("生成于 " + datetime.now().strftime("%Y-%m-%d %H:%M")
                            + "   报告宽度 " + str(W) + " 列", W - 3, "<"), CYAN))
    print(paint("#" * W, CYAN))

    # ---- 账号 / 租户 ----
    if "Data" in user:
        d = user["Data"]
        ui = d.get("UserInfo", {})
        ti = d.get("TenantInfoBase", {})
        hr("账号信息", W)
        label_lines([
            ("用户", f"{ui.get('Name','-')}  <{ui.get('Email','-')}>"),
            ("租户", f"{ti.get('TenantName','-')}  (id={ti.get('TenantID','-')})"),
            ("区域", f"{ui.get('Region','-')}"),
            ("席位", f"已用 {paint(fmt_num(d.get('CurSeats')), BLUE)} / 总 {fmt_num(d.get('TotalSeats'))}"),
            ("权益到期", fmt_dt(ti.get("ProductExpireTime"))),
            ("订阅状态", f"{ti.get('SubscriptionStatus','-')}"),
        ])

    # ---- 费用额度（本权益周期）----
    # 总额度来自 get_personal_quota.user_model_quota.seat_pool_currency_quota（动态读取，权威）。
    # 已用费用：网页显示的是“账单结算值”，这些 JSON 接口没有直接字段；
    # 本处用各模型 total_cost_currency 实时求和作为估算，可能与结算值略有差异（均为动态计算）。
    money_total = None
    if "user_model_quota" in quota:
        money_total = quota["user_model_quota"].get("seat_pool_currency_quota")
    used_est = 0.0
    for u in model_usage.get("user_usage_list", []) or []:
        for det in (u.get("in_seat_detail", {}).get("chat_usage_details", []) or []):
            used_est += det.get("total_cost_currency") or 0
    if money_total is not None:
        hr("费用额度（本权益周期）", W)
        mt = money_total
        col = ratio_color(used_est / mt) if mt else GREEN
        pct = f"  ({used_est/mt*100:.1f}%)" if mt else ""
        label_lines([
            ("总额度(费用)", dpad("￥" + money(mt), 16, ">", col) + paint(pct, col)),
            ("已用(实时估算)", dpad("￥" + money(used_est), 16, ">", col) + paint(pct, col)),
        ])
        print(paint("  [i] 网页“已用费用”为账单结算值；本处为各模型花费实时求和的估算，口径可能不同。",
                    DIM))

    # ---- 核心用量 ----
    if "Data" in core:
        c = core["Data"]
        hr("核心用量（本权益周期）", W)
        cu, ct = c.get("ChatUsed", 0), c.get("ChatTotal", 0)
        pu, pt = c.get("PayAsYouGoUsed", 0), c.get("PayAsYouGoTotal", 0)
        tu = c.get("TokenUsage", {})
        cit, cqt = tu.get("ChatUsageTokens", 0), tu.get("ChatUsageQuota", 0)
        cot, cct = tu.get("CompletionUsageTokens", 0), tu.get("CompletionUsageQuota", 0)

        def core_val(label, used, total):
            r = (used / total) if total else 0
            col = ratio_color(r)
            pct_s = f"  ({used/total*100:.1f}%)" if total else ""
            return dpad(fmt_num(used), 18, ">", col) + " / " \
                + dpad(fmt_num(total), 18, ">", col) + paint(pct_s, col)

        label_lines([
            ("对话次数", core_val("对话次数", cu, ct)),
            ("按量付费", core_val("按量付费", pu, pt)),
            ("Token 输入 (Chat)", core_val("Token 输入 (Chat)", cit, cqt)),
            ("Token 输出 (Completion)", core_val("Token 输出 (Completion)", cot, cct)),
            ("周期开始", fmt_dt(c.get("ChatStartTime"))),
            ("额度重置", fmt_dt(c.get("ResetTime"))),
        ])
        if cit > cqt:
            print(paint("  [!] Token 输入已超出基础额度，超额部分按席位额度池结算（以 Trae 账单为准）。",
                        YELLOW))

    # ---- 按模型用量（全部，降序，含已用/额度）----
    quota_map = {}
    for t in model_quota.get("tenant_model_quota_list", []) or []:
        for m in t.get("system_model_quota_list", []) or []:
            if m.get("config_name"):
                quota_map[m["config_name"]] = m.get("per_user_token_quota")
            if m.get("display_name"):
                quota_map[m["display_name"]] = m.get("per_user_token_quota")

    details = []
    for u in model_usage.get("user_usage_list", []) or []:
        for det in (u.get("in_seat_detail", {}).get("chat_usage_details", []) or []):
            details.append(det)
    if details:
        details.sort(key=lambda x: (x.get("total_cost_currency") or 0), reverse=True)
        hr(f"按模型用量（席位内，全部 {len(details)} 个模型，按金额降序）", W)
        total_tok = sum(d.get("total_token_usage", 0) for d in details)
        total_cost = sum(d.get("total_cost_currency") or 0 for d in details)
        # 数值列：固定下限 + 按内容自适应，确保长数字完整显示
        TOK_W, QUOTA_W, COST_W = 14, 12, 8
        for d in details:
            TOK_W = max(TOK_W, dwidth(fmt_num(d.get("total_token_usage", 0))))
            q = quota_map.get(d.get("config_name")) or quota_map.get(d.get("display_name"))
            QUOTA_W = max(QUOTA_W, dwidth(fmt_num(q) if (q and q < 10 ** 9) else "无限制"))
            COST_W = max(COST_W, dwidth(money(round(d.get("total_cost_currency") or 0, 2))))
        TOK_W = max(TOK_W, dwidth(fmt_num(total_tok)))
        COST_W = max(COST_W, dwidth(money(round(total_cost, 2))))
        TK_W, CK_W, TT_W = 9, 9, 12
        NAME_W = max(13, W - (TOK_W + QUOTA_W + COST_W + TK_W + CK_W + TT_W + 6 + 2))
        headers = [("模型", BLUE), ("已用Token", GREEN), ("额度", GREEN),
                   ("花费(¥)", GREEN), ("Token占比", GREEN),
                   ("金额占比", GREEN), ("占费用额度%", GREEN)]
        widths = [NAME_W, TOK_W, QUOTA_W, COST_W, TK_W, CK_W, TT_W]
        rows = []
        for d in details:
            name = d.get("display_name") or d.get("config_name") or "-"
            name = dellipsis(name, NAME_W)
            used = d.get("total_token_usage", 0)
            quota = quota_map.get(d.get("config_name")) or quota_map.get(d.get("display_name"))
            cost = round(d.get("total_cost_currency") or 0, 2)
            tshare = (used / total_tok) if total_tok else 0       # Token 占总量
            cshare = (cost / total_cost) if total_cost else 0      # 金额占总量
            tshare_col = share_color(tshare)
            cshare_col = share_color(cshare)
            if quota and quota < 10 ** 9:
                r = used / quota if quota else 0
                col = ratio_color(r)
                quota_s = fmt_num(quota)
            else:
                col = tshare_col  # 无限额模型按“占总量”着色
                quota_s = "无限制"
            if money_total:
                tt_share = (cost / money_total) if money_total else 0
                tt_s = f"{tt_share*100:.1f}%"
                tt_col = share_color(tt_share)
            else:
                tt_s, tt_col = "-", WHITE
            rows.append([
                (name, BLUE),
                (fmt_num(used), col),
                (quota_s, col),
                (money(cost), cshare_col),
                (f"{tshare*100:.1f}%", tshare_col),
                (f"{cshare*100:.1f}%", cshare_col),
                (tt_s, tt_col),
            ])
        total_tt = (f"{total_cost/money_total*100:.1f}%" if money_total else "-")
        rows.append([
            ("合计", BOLD), (fmt_num(total_tok), BOLD), ("-", BOLD),
            (money(round(total_cost, 2)), BOLD), ("100.0%", BOLD),
            ("100.0%", BOLD), (total_tt, BOLD),
        ])
        draw_table(W, headers, rows, widths, zebra=zebra, zebra_theme=zebra_theme)

    # ---- 模型单价参考 ----
    print_model_prices(W, zebra, zebra_theme, preset=price_result)

    # ---- 每日趋势 ----
    if "Data" in trend and trend["Data"].get("xAxis"):
        d = trend["Data"]
        xs = d["xAxis"]
        series = {s["name"]: s["data"] for s in d.get("series", [])}
        n = min(14, len(xs))
        hr(f"每日趋势（最近 {n} 天）", W)
        for sname in series:
            print("  " + paint(sname, MAGENTA) + ":")
            for i in range(len(xs) - n, len(xs)):
                print("    " + dpad(xs[i], 12, "<") + ": " + fmt_num(series[sname][i]))

    # ---- 近期 token 明细 ----
    items = detail.get("items") if isinstance(detail, dict) else None
    if items:
        items = sorted(items, key=lambda x: x.get("tokens_usage", 0), reverse=True)
        hr(f"近期 Token 调用明细（Top 12 / 共 {len(items)} 条）", W)
        T_W = 12
        TOKEN_W, COSTW = 12, 10
        for it in items[:12]:
            TOKEN_W = max(TOKEN_W, dwidth(fmt_num(it.get("tokens_usage"))))
            COSTW = max(COSTW, dwidth(money(round(it.get("total_cost_currency") or 0, 2))))
        M_W = max(14, W - (T_W + TOKEN_W + COSTW + 3 + 2))
        headers = [("时间", GREEN), ("模型", BLUE), ("Token", GREEN), ("花费(¥)", GREEN)]
        widths = [T_W, M_W, TOKEN_W, COSTW]
        rows = []
        for it in items[:12]:
            t = fmt_date(it.get("request_time", 0) * 1000) if it.get("request_time") else "-"
            mname = dellipsis(it.get("display_name") or it.get("model_name") or "-", M_W)
            rows.append([
                (t, WHITE),
                (mname, BLUE),
                (fmt_num(it.get("tokens_usage")), WHITE),
                (money(round(it.get("total_cost_currency") or 0, 2)), WHITE),
            ])
        draw_table(W, headers, rows, widths, zebra=zebra, zebra_theme=zebra_theme)
        print(paint(f"  [i] 明细按最近 {args.days} 天窗口由接口返回；若条数与实际调用不符，可能受接口条数上限限制。", DIM))

    print()
    print(paint("-" * W, CYAN))
    print(f"原始 JSON 已保存 -> {OUT_JSON}")
    print("提示: Cookie 过期后重新复制 X-Cloudide-Tob-Session 覆盖 trae_session.txt 即可。")


def _main_entry():
    """程序入口：统一 UTF-8 输出，并将未预期异常转为友好提示。"""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        print(paint(f"\n[!] 运行出错：{e}", RED))
        sys.exit(1)


if __name__ == "__main__":
    _main_entry()
