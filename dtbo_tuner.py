# -*- coding: utf-8 -*-
"""
DTBO Tuner  —  小米 17 Pro Max (popsicle) 充电参数调节器

纯标准库实现（tkinter + ctypes），无第三方依赖。
把 dtbo 镜像拖进窗口即可调参，导出可直接 fastboot 刷入的镜像。

作者：为 popsicle / SM8850 定制
"""

import os
import re
import sys
import copy
import struct
import argparse

APP_TITLE = "DTBO Tuner · 小米17 Pro Max"
APP_VER = "1.1"

DTBO_MAGIC = 0xD7B7AB1E
FDT_MAGIC = 0xD00DFEED

# SPECS 里的节点路径统一以这个前缀书写；真正的片段编号由 detect_fragment()
# 在载入镜像时自动定位，再通过 Tuner._p() 重定向过去。
# popsicle 恰好是 fragment@33，但换机型后编号一定不同，绝不能写死依赖它。
NODE_PREFIX = "/fragment@33/__overlay__"

# 用来在镜像里认出"承载充电参数的那个 overlay 片段"的锚点节点名。
# 命中越多越可信；一个都命中就说明这镜像里没有我们认识的充电框架。
ANCHOR_NODES = (
    "mca_strategy_quick_charge",
    "mca_buckchg_jeita",
    "mca_charger_thermal",
    "mca_strategy_buckchg_class",
    "connector_antiburn",
    "mca_vbat_ovp_mon",
)

FRAG_RE = re.compile(r"^fragment@(\d+)$")

# ---------------------------------------------------------------- 设备指纹
# 小米 17 Pro Max = popsicle / SM8850
STOCK_MODEL_HINT = "popsicle"
STOCK_BOARD_ID = (8, 0)
STOCK_MIBOARD_ID = (1, 0)

# ================================================================ DTBO 核心


class Node:
    __slots__ = ("name", "props", "children")

    def __init__(self, name):
        self.name = name
        self.props = []      # list[[name:str, value:bytes]]
        self.children = []

    def find(self, path):
        cur = self
        for part in [p for p in path.split("/") if p]:
            nxt = next((c for c in cur.children if c.name == part), None)
            if nxt is None:
                return None
            cur = nxt
        return cur

    def get(self, name):
        for n, v in self.props:
            if n == name:
                return v
        return None

    def put(self, name, value):
        for p in self.props:
            if p[0] == name:
                p[1] = value
                return True
        self.props.append([name, value])
        return True

    def clone(self):
        n = Node(self.name)
        n.props = [[k, bytes(v)] for k, v in self.props]
        n.children = [c.clone() for c in self.children]
        return n


def read_dtbo(path):
    d = open(path, "rb").read()
    magic, total, hsz, esz, cnt, eoff, psz, ver = struct.unpack_from(">8I", d, 0)
    if magic != DTBO_MAGIC:
        raise ValueError("不是合法的 DTBO 镜像 (magic=0x%08X)" % magic)
    entries = []
    for i in range(cnt):
        size, off, tid, rev = struct.unpack_from(">4I", d, eoff + i * esz)
        entries.append(dict(size=size, offset=off, id=tid, rev=rev,
                            data=d[off:off + size]))
    return dict(total=total, page=psz or 4096, entries=entries,
                filesize=len(d), raw=d)


def build_dtbo(entries, page=4096, minsize=0):
    n = len(entries)
    hdr_len = 32 + 32 * n
    off = (hdr_len + 3) & ~3
    blobs = []
    for e in entries:
        blob = e["data"]
        blobs.append(blob + b"\x00" * ((-len(blob)) % 4))
    offsets, body = [], b""
    for b in blobs:
        offsets.append(off + len(body))
        body += b
    total = ((off + len(body) + page - 1) // page) * page
    out = bytearray()
    out += struct.pack(">8I", DTBO_MAGIC, total, 32, 32, n, 32, page, 0)
    for i, e in enumerate(entries):
        out += struct.pack(">4I", len(blobs[i]), offsets[i], e["id"], e["rev"])
        out += b"\x00" * 16
    out += b"\x00" * (off - len(out))
    out += body
    if len(out) < total:
        out += b"\x00" * (total - len(out))
    if minsize and len(out) < minsize:          # 保持与原分区同样大小，便于直接刷入
        out += b"\x00" * (minsize - len(out))
    return bytes(out)


def parse_fdt(blob):
    (magic, totalsize, off_struct, off_strings, off_rsv, version,
     last_comp, boot_cpuid, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
    if magic != FDT_MAGIC:
        raise ValueError("内部 DTB 损坏 (magic=0x%08X)" % magic)
    strings = blob[off_strings:off_strings + size_strings]
    pos, root, stack = off_struct, None, []
    while True:
        tok = struct.unpack_from(">I", blob, pos)[0]
        pos += 4
        if tok == 1:
            end = blob.index(b"\x00", pos)
            node = Node(blob[pos:end].decode("utf-8", "replace"))
            pos = (end + 4) & ~3
            if stack:
                stack[-1].children.append(node)
            else:
                root = node
            stack.append(node)
        elif tok == 2:
            stack.pop()
        elif tok == 3:
            ln, nameoff = struct.unpack_from(">2I", blob, pos)
            pos += 8
            val = blob[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
            nm = strings[nameoff:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            stack[-1].props.append([nm, val])
        elif tok == 4:
            pass
        elif tok == 9:
            break
        else:
            raise ValueError("未知 FDT token %d" % tok)
    return root


def serialize_fdt(root):
    strings = bytearray()
    str_map = {}

    def str_off(name):
        if name not in str_map:
            str_map[name] = len(strings)
            strings.extend(name.encode() + b"\x00")
        return str_map[name]

    body = bytearray()

    def emit(node):
        body.extend(struct.pack(">I", 1))
        nb = node.name.encode() + b"\x00"
        body.extend(nb + b"\x00" * ((-len(nb)) % 4))
        for name, val in node.props:
            body.extend(struct.pack(">III", 3, len(val), str_off(name)))
            body.extend(val + b"\x00" * ((-len(val)) % 4))
        for c in node.children:
            emit(c)
        body.extend(struct.pack(">I", 2))

    emit(root)
    body.extend(struct.pack(">I", 9))

    off_rsv = 40
    rsv = b"\x00" * 16
    off_struct = off_rsv + len(rsv)
    off_strings = off_struct + len(body)
    total = off_strings + len(strings)
    hdr = struct.pack(">10I", FDT_MAGIC, total, off_struct, off_strings, off_rsv,
                      17, 16, 0, len(strings), len(body))
    return hdr + rsv + bytes(body) + bytes(strings)


# ---------------------------------------------------------------- 属性读写
def u32s(val):
    if not val or len(val) % 4:
        return []
    return list(struct.unpack(">%dI" % (len(val) // 4), val))


def pack_u32s(vals):
    return b"".join(struct.pack(">I", v & 0xFFFFFFFF) for v in vals)


def strtab(val):
    if not val:
        return []
    return [p.decode("utf-8", "replace") for p in val.split(b"\x00") if p]


def pack_strtab(items):
    if not items:
        return b"\x00"
    return b"\x00".join(s.encode("utf-8") for s in items) + b"\x00"


# ---------------------------------------------------------------- 片段自动探测
def detect_fragment(root):
    """在 DTB 树里找出承载充电参数的 overlay 片段。

    返回 dict：
        prefix  —— "/fragment@N/__overlay__"；一个锚点都没找到时为 None
        index   —— 片段编号 N（int）；找不到时为 None
        hits    —— {锚点名: 该片段里命中的锚点个数}
        nodes   —— 该片段 __overlay__ 下的子节点名集合（用于诊断）

    评分规则：命中锚点越多越可信；同分时取片段号小的（小米把业务 overlay
    放在编号靠后的片段里，但小号更早被 dtc 应用，取小号更安全）。
    """
    scores = {}
    for frag in root.children:
        m = FRAG_RE.match(frag.name)
        if not m:
            continue
        ov = frag.find("__overlay__")
        if ov is None:
            continue
        names = {c.name for c in ov.children}
        hit = sum(1 for a in ANCHOR_NODES if a in names)
        if hit:
            scores[int(m.group(1))] = (hit, names)

    if not scores:
        return dict(prefix=None, index=None, hits={}, nodes=set())

    best = sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))[0]
    idx, (hit, names) = best
    return dict(prefix="/fragment@%d/__overlay__" % idx, index=idx,
                hits={a: hit for a in ANCHOR_NODES},
                nodes=names,
                all_scores={k: v[0] for k, v in scores.items()})


class MissingNodeError(RuntimeError):
    """镜像里找不到某个参数所属的节点 —— 说明片段选错或该机型没有这套框架。

    这是硬错误：以前静默 continue 会产出一个"刷得进去但什么都没改"的镜像，
    比直接失败危险得多。
    """

    def __init__(self, missing, prefix):
        self.missing = missing          # [(节点路径, [属性名...])]
        self.prefix = prefix
        lines = ["镜像里找不到下面 %d 个节点，导出的镜像不会有任何实际改动：" % len(missing)]
        for node, props in missing[:12]:
            lines.append("  %s  (属性: %s)" % (node, ", ".join(props)))
        if len(missing) > 12:
            lines.append("  … 还有 %d 个" % (len(missing) - 12))
        if prefix is None:
            lines.append("")
            lines.append("未能在镜像里定位到任何充电 overlay 片段"
                         "（找过 %s）。" % "、".join(ANCHOR_NODES[:3]) + " …")
            lines.append("这通常意味着：该机型的充电参数不在 DTBO 里，"
                         "或不在这套 MCA 框架下。")
        else:
            lines.append("")
            lines.append("当前使用的片段前缀: %s" % prefix)
        lines.append("")
        lines.append("如果确认要跳过缺失项继续导出，加 --allow-missing（CLI）"
                     "或在 GUI 里勾选\"允许缺失\"。")
        RuntimeError.__init__(self, "\n".join(lines))


# ================================================================ 参数规格
class Spec:
    """一个可调参数。kind 决定写入方式：
       u32         —— 整个属性写成单个 u32
       u32[idx]    —— 写 u32 数组的第 idx 项
       jeita|字段|温度阈值 —— jeita_para 里 tmin>=阈值 的分组，第 字段 项设为该值
       atl|字段|温度阈值   —— batt_para_atl 同上
       thermal_cap —— wired_thermal 整表逐项取 min(原值, 值)
    """

    def __init__(self, key, group, label, desc, node, prop, kind, mn, mx, step,
                 unit, stock, danger=None, fmt=None, note=None):
        self.key = key
        self.group = group
        self.label = label
        self.desc = desc
        self.node = node
        self.prop = prop
        self.kind = kind
        self.mn, self.mx, self.step = mn, mx, step
        self.unit = unit
        self.stock = stock
        self.danger = danger      # 超过该值时给出红色警示
        self.fmt = fmt
        self.note = note
        self.missing = False

    def show(self, v):
        if self.fmt:
            return self.fmt(v)
        return "%s %s" % (v, self.unit) if self.unit else str(v)


G_WIRED = "有线快充"
G_WIRELESS = "无线充电"
G_SAFE = "温控与保护"

N_Q = NODE_PREFIX + "/mca_strategy_quick_charge"
N_BC = NODE_PREFIX + "/mca_strategy_buckchg_class"
N_JEITA = NODE_PREFIX + "/mca_buckchg_jeita"
N_AB = NODE_PREFIX + "/connector_antiburn"
N_THERM = NODE_PREFIX + "/mca_charger_thermal"
N_SUB = NODE_PREFIX + "/mca_qcom_subpmic_proxy"
N_OVP = NODE_PREFIX + "/mca_vbat_ovp_mon"
N_QB = NODE_PREFIX + "/mca_quick_charge_batt_para_gbl"
N_WB = NODE_PREFIX + "/mca_strategy_basic_wireless_class"
N_WQ = NODE_PREFIX + "/mca_strategy_quick_wireless_class"

MA = "mA"

SPECS = [
    # ------------------------------------------------ 有线快充
    Spec("real_curr_max", G_WIRED, "单电芯电流硬上限",
         "电池侧允许的最大充电电流（real_curr_max）。原厂 17500mA，"
         "这是充电器的最后一道电流闸门。", N_Q, "real_curr_max", "u32",
         4000, 20000, 100, MA, 17500, danger=18000),
    Spec("qc3_ibat_max_limit", G_WIRED, "QC3 电池电流上限",
         "QC3 协议下的电池电流上限。原厂 5000mA，加速版拉到 20000mA。",
         N_Q, "qc3_ibat_max_limit", "u32", 2000, 20000, 100, MA, 5000, danger=12000),
    Spec("qc3p5_ibat_max_limit", G_WIRED, "QC3+ 电池电流上限",
         "QC3+（PPS）协议下的电池电流上限。原厂 4400mA。",
         N_Q, "qc3p5_ibat_max_limit", "u32", 2000, 20000, 100, MA, 4400, danger=12000),
    Spec("div_max_curr0", G_WIRED, "分压档①最大电流",
         "双电芯分压充电第 1 档允许的最大电流（低压段）。原厂 5000mA。",
         N_Q, "div_max_curr", "u32[0]", 2000, 20000, 100, MA, 5000, danger=12000),
    Spec("div_max_curr1", G_WIRED, "分压档②最大电流",
         "双电芯分压充电第 2 档最大电流（中压段）。原厂 12000mA。",
         N_Q, "div_max_curr", "u32[1]", 2000, 20000, 100, MA, 12000, danger=16000),
    Spec("div_max_curr2", G_WIRED, "分压档③最大电流",
         "双电芯分压充电第 3 档最大电流（高压段）。原厂 18000mA。",
         N_Q, "div_max_curr", "u32[2]", 2000, 20000, 100, MA, 18000, danger=20000),
    Spec("max_vbat", G_WIRED, "快充截止电压",
         "快充策略认为\"充满\"的电压点（max_vbat）。原厂 4455mV。"
         "每提高 10mV，循环寿命下降明显，超过 4500mV 属于过充。",
         N_Q, "max_vbat", "u32", 4200, 4520, 1, "mV", 4455, danger=4500),
    Spec("max_vbat_threshold", G_WIRED, "充满判定阈值",
         "触发停止充电的电压阈值（max_vbat_threshold）。原厂 4500mV。",
         N_Q, "max_vbat_threshold", "u32", 4200, 4530, 1, "mV", 4500, danger=4515),
    Spec("jeita_ge13", G_WIRED, "JEITA 主表电流上限（13°C 以上）",
         "jeita_para 中 13°C 以上各温度档的电流值。原厂分档 4000mA。"
         "加速版把这 7 档全部拉平到 20000mA —— 这是充电变快的核心开关。",
         N_JEITA, "jeita_para", "jeita|4|13", 500, 20000, 100, MA, 4000, danger=12000),
    Spec("jeita_iterm", G_WIRED, "充电截止电流",
         "JEITA 主表里的终止电流（iterm）。原厂 362mA，"
         "调大后更早判定充满、减少末端涓流时间（会更\"满得快\"但实际没充满）。",
         N_JEITA, "jeita_para", "jeita|6|13", 50, 1000, 10, MA, 362, danger=500),
    Spec("atl_ge13", G_WIRED, "快充曲线电流上限（13°C 以上）",
         "batt_para_atl 里 13°C 以上各档的目标电流。原厂 4338→18000mA 递增。",
         N_QB, "batt_para_atl", "atl|4|13", 1000, 20000, 100, MA, 18000, danger=20000),
    Spec("chg_pd", G_WIRED, "PD 输入限流",
         "PD 适配器下的入口电流限制。原厂 3000mA。",
         N_BC, "chg_pd", "u32", 1000, 5000, 100, MA, 3000, danger=4000),
    Spec("chg_hvdcp3", G_WIRED, "HVDCP3 输入限流",
         "HVDCP3 适配器下的入口电流限制。原厂 3600mA。",
         N_BC, "chg_hvdcp3", "u32", 1000, 5000, 100, MA, 3600, danger=4500),
    Spec("chg_dcp", G_WIRED, "DCP 输入限流",
         "普通 DCP 充电器下的入口电流限制。原厂 2000mA。",
         N_BC, "chg_dcp", "u32", 500, 4000, 100, MA, 2000, danger=3000),

    # ------------------------------------------------ 无线充电
    Spec("soc_limit_fcc", G_WIRELESS, "无线充电电流上限",
         "无线充电时电池允许的最大电流（soc_limit_fcc）。原厂 6000mA。",
         N_WB, "soc_limit_fcc", "u32", 2000, 12000, 100, MA, 6000, danger=8000),
    Spec("dyn4to2", G_WIRELESS, "无线 4:2 分压切换阈值",
         "无线充电切换到 4:2 分压模式的阈值。原厂 4000。",
         N_WB, "dynamic-cp-4to2-threshold", "u32", 2000, 8000, 100, "", 4000),
    Spec("dyn21", G_WIRELESS, "无线 2:1 最大电流",
         "无线 2:1 分压模式下的最大电流。原厂 4000mA。",
         N_WB, "dynamic-cp-2-1-max-fcc", "u32", 2000, 8000, 100, MA, 4000, danger=6000),
    Spec("wls_max_vbat", G_WIRELESS, "无线充电截止电压",
         "无线快充的截止电压。原厂 4490mV。",
         N_WQ, "max_vbat", "u32", 4100, 4520, 1, "mV", 4490, danger=4500),
    Spec("rx_max_iout", G_WIRELESS, "无线接收最大输出",
         "无线接收线圈侧最大输出电流。原厂 2650mA。",
         N_WB, "rx_max_iout", "u32[0]", 1000, 5000, 50, MA, 2650, danger=3500),

    # ------------------------------------------------ 温控与保护
    Spec("ab_trigger", G_SAFE, "接口防烧触发温度",
         "充电接口/连接器防烧保护的触发温度。原厂 65°C，"
         "加速版改成 120°C 等于让防烧永远不触发。⚠ 这是防止充电口熔毁的保护。",
         N_AB, "trigger_temp", "u32", 40000, 120000, 1000, "m°C", 65000,
         danger=80000, fmt=lambda v: "%.0f °C" % (v / 1000.0)),
    Spec("ab_conn", G_SAFE, "连板防烧触发温度",
         "连接器子板（comb）的防烧触发温度。原厂 35°C。",
         N_AB, "comb_rate_conn_trigger_temp", "u32", 30000, 120000, 1000, "m°C", 35000,
         danger=60000, fmt=lambda v: "%.0f °C" % (v / 1000.0)),
    Spec("ab_rate", G_SAFE, "温升速率门限",
         "温度上升速率超过该值即判定异常发热（单位 m°C/s）。原厂 4000，"
         "加速版 30000 —— 等于不再检测快速升温。",
         N_AB, "max_temp_increase_rate", "u32", 1000, 30000, 500, "m°C/s", 4000, danger=15000),
    Spec("ab_board", G_SAFE, "主板温度上限",
         "充电时允许的主板温度上限。原厂 50°C，加速版 120°C。",
         N_AB, "max_thermal_board_temp", "u32", 40, 120, 1, "°C", 50, danger=70),
    Spec("adp_temp_max", G_SAFE, "适配器过温保护点",
         "适配器侧最高允许温度。原厂 125°C，加速版 150°C。",
         N_Q, "adp_temp_max", "u32", 90, 150, 1, "°C", 125, danger=135),
    Spec("die_temp_max", G_SAFE, "充电芯片过温保护点",
         "充电 IC（die）过温保护阈值。原厂 125°C。",
         N_Q, "die_temp_max", "u32", 90, 150, 1, "°C", 125, danger=135),
    Spec("ffc_temp_high", G_SAFE, "快充高温截止",
         "电池温度超过该值即退出快充。原厂 48°C，加速版 120°C（=永不退出）。",
         N_BC, "ffc_temp_high", "u32", 40, 120, 1, "°C", 48, danger=60),
    Spec("curr_mon", G_SAFE, "过流监控电流",
         "电流监控的告警门限。原厂 17600mA，加速版 65535（=关闭监控）。",
         N_Q, "curr_monitor_current", "u32", 10000, 65535, 100, MA, 17600, danger=25000),
    Spec("curr_mon_t", G_SAFE, "过流监控时间",
         "持续超流多久触发保护（秒）。原厂 120s，加速版 65535（=关闭）。",
         N_Q, "curr_monitor_time_s", "u32", 10, 65535, 10, "s", 120, danger=1000),
    Spec("shutdown_v", G_SAFE, "关机电压",
         "电池放电到该电压时关机。原厂 3350mV，加速版 2700mV —— 过放会损伤电芯。",
         N_SUB, "qcom,shutdown-voltage", "u32", 2700, 3500, 10, "mV", 3350, danger=3000),
    Spec("ovp_ffc", G_SAFE, "快充过压保护点",
         "快充状态下的电池过压保护阈值。原厂 4555mV。",
         N_OVP, "vbat_ovp_threshold_ffc", "u32", 4400, 4800, 10, "mV", 4555, danger=4750),
    Spec("thermal_cap", G_SAFE, "有线温控曲线电流上限",
         "限制有线充电温控表允许的最大电流。默认 13500 = 与原厂一致（不干预）；"
         "调低会让高温时提前降流；设到 20000 也只是保持原厂 13500 上限不变。",
         N_THERM, "wired_thermal", "thermal_cap", 2000, 20000, 100, MA, 13500),
]

SPEC_BY_KEY = {s.key: s for s in SPECS}


# ================================================================ 预设
def _P(**kw):
    return kw


PRESETS = {
    "原厂": {},
    "温和（推荐）": _P(
        real_curr_max=12000, qc3_ibat_max_limit=9000, qc3p5_ibat_max_limit=9000,
        div_max_curr0=6000, div_max_curr1=12000, div_max_curr2=18000,
        max_vbat=4495, max_vbat_threshold=4500, jeita_ge13=9000, jeita_iterm=200,
        atl_ge13=18000, chg_pd=3000, chg_hvdcp3=3600, chg_dcp=2000,
        soc_limit_fcc=7000, dyn4to2=4500, dyn21=4500, wls_max_vbat=4490,
        rx_max_iout=3000, thermal_cap=20000,
    ),
    "激进（加速版文件）": _P(
        real_curr_max=20000, qc3_ibat_max_limit=20000, qc3p5_ibat_max_limit=20000,
        div_max_curr0=20000, div_max_curr1=20000, div_max_curr2=20000,
        max_vbat=4515, max_vbat_threshold=4515, jeita_ge13=20000, jeita_iterm=500,
        atl_ge13=20000, chg_pd=3000, chg_hvdcp3=3600, chg_dcp=2000,
        soc_limit_fcc=8000, dyn4to2=4500, dyn21=4500, wls_max_vbat=4490,
        rx_max_iout=3000,
        ab_trigger=120000, ab_conn=120000, ab_rate=30000, ab_board=120,
        adp_temp_max=150, die_temp_max=150, ffc_temp_high=120,
        curr_mon=65535, curr_mon_t=65535, shutdown_v=2700, ovp_ffc=4700,
        thermal_cap=20000,
    ),
    "只加速无线": _P(
        soc_limit_fcc=8000, dyn4to2=4500, dyn21=4500,
        wls_max_vbat=4490, rx_max_iout=3000,
    ),
}


# ================================================================ 补丁引擎
class Tuner:
    def __init__(self):
        self.path = None
        self.info = None
        self.baseline = []          # 每个 entry 的 Node 树（原样）
        self.entries_meta = []
        self.device_ok = False
        self.device_name = ""
        self.changes = []           # 最近一次导出的变更说明
        self.prefix = None          # 实际使用的 overlay 片段前缀（DTB #0，用于展示）
        self.frags = []             # 每个 DTB 各自的片段前缀（可能各不相同）
        self.frag = dict(prefix=None, index=None, hits={}, nodes=set())
        self.missing = []           # 载入后就静态缺失的参数 Spec 列表
        self.allow_missing = False
        self.touched = set()        # 最近一次 build 真正写过的参数 key

    # -------------------------------------------------- 片段重定向
    def _p(self, path, i=0):
        """把 SPECS 里以 NODE_PREFIX 书写的路径，重定向到第 i 个 DTB 的实际片段。"""
        pref = self.frags[i] if i < len(self.frags) else self.prefix
        if pref is None or pref == NODE_PREFIX:
            return path
        if path.startswith(NODE_PREFIX):
            return pref + path[len(NODE_PREFIX):]
        return path

    # -------------------------------------------------- 载入
    def load(self, path):
        info = read_dtbo(path)
        trees, metas = [], []
        for e in info["entries"]:
            trees.append(parse_fdt(e["data"]))
            metas.append(dict(id=e["id"], rev=e["rev"], size=e["size"]))
        if not trees:
            raise ValueError("镜像里没有 DTB 条目")
        self.path = path
        self.info = info
        self.baseline = trees
        self.entries_meta = metas
        self._detect_fragment()
        self._detect_device()
        self._scan_missing()
        return self

    def _detect_fragment(self):
        """每个 DTB 单独探测自己的片段；self.prefix 取 DTB #0 的（展示用）。"""
        self.frags = []
        for tree in self.baseline:
            info = detect_fragment(tree)
            self.frags.append(info["prefix"])
        self.frag = detect_fragment(self.baseline[0])
        self.prefix = self.frag["prefix"]

    def _scan_missing(self):
        """载入时静态清点：哪些参数在所有 DTB 里都不存在。"""
        gone = []
        for spec in SPECS:
            hit = any(self._cur_of(tree, spec) is not None
                      for tree in self.baseline)
            spec.missing = not hit
            if spec.missing:
                gone.append(spec)
        self.missing = gone

    def frag_desc(self):
        """一句话描述片段探测结果，给 CLI / GUI 用。"""
        if self.prefix is None:
            return "未找到充电 overlay 片段（%d 个参数全部缺失）" % len(self.missing)
        idx = self.frag.get("index")
        extra = ""
        if self.prefix != NODE_PREFIX:
            extra = "（原写死的 fragment@33 不适用，已自动重定向）"
        return "fragment@%s%s" % (idx, extra)

    def missing_report(self):
        return [(s.node, [s.prop]) for s in self.missing]

    def _detect_device(self):
        root = self.baseline[0]
        model = ""
        mv = root.get("model")
        if mv:
            model = mv.split(b"\x00")[0].decode("utf-8", "replace")
        bid = tuple(u32s(root.get("qcom,board-id") or b"")[:2])
        mid = tuple(u32s(root.get("xiaomi,miboard-id") or b"")[:2])
        self.device_name = model
        self.device_ok = (STOCK_MODEL_HINT in model.lower()
                          and bid == STOCK_BOARD_ID and mid == STOCK_MIBOARD_ID)
        self.fingerprint = {"model": model, "board_id": bid, "miboard_id": mid}

    # -------------------------------------------------- 读取当前值
    def _cur_of(self, tree, spec, i=0):
        """从指定 DTB 树里读该参数当前值（找不到返回 None）。"""
        node = tree.find(self._p(spec.node, i))
        if node is None:
            return None
        val = node.get(spec.prop)
        if val is None:
            return None
        return _read_kind(spec, val)

    def read_value(self, spec):
        return self._cur_of(self.baseline[0], spec, 0)

    # -------------------------------------------------- 生成
    def build(self, values, minsize=0):
        """values: {key: number}；返回 (bytes, changes)

        任何"要改却找不到节点"的情况都抛 MissingNodeError —— 除非显式设了
        self.allow_missing，否则绝不产出静默无效的镜像。
        """
        index = {s.key: s for s in SPECS}
        changes = []
        entries = []
        missing = []                 # [(路径, [属性...])]
        touched = set()              # 真正写过值的 key

        for i, base in enumerate(self.baseline):
            tree = base.clone()
            for key, num in values.items():
                spec = index.get(key)
                if spec is None:
                    continue
                node = tree.find(self._p(spec.node, i))
                if node is None or node.get(spec.prop) is None:
                    # 节点不存在：只有"确实要求改这个值"才算缺失。
                    # 值等于 spec.stock 意味着它只是界面默认显示（缺失时界面
                    # 本来就显示 stock），不是用户真的想写。
                    cur = self._cur_of(base, spec, i)
                    requested = (cur is not None and int(cur) != int(num)) or \
                                int(num) != int(spec.stock)
                    if requested:
                        pair = (self._p(spec.node, i), spec.prop)
                        if pair not in [(n, p) for n, ps in missing for p in ps]:
                            missing.append((pair[0], [pair[1]]))
                    continue
                if _read_kind(spec, node.get(spec.prop)) == int(num):
                    continue          # 这个 DTB 里已经是目标值，不用动
                _write_kind(spec, node, int(num))
                if i == 0:
                    touched.add(key)
                    changes.append((spec, int(num)))
            entries.append(dict(id=self.entries_meta[i]["id"],
                                rev=self.entries_meta[i]["rev"],
                                data=serialize_fdt(tree)))

        if missing and not self.allow_missing:
            raise MissingNodeError(missing, self.prefix)

        blob = build_dtbo(entries, page=self.info["page"], minsize=minsize)
        self.changes = changes
        self.touched = touched
        return blob, changes

    def verify(self, blob, values):
        """导出后自检：重新解析，确认每个参数都落到位

        只校验"这次真的动过手"的参数。找不到节点/属性/解析失败一律算失败——
        以前是 continue，等于把写失败当成功放过去。
        """
        info = read_dtbo_bytes(blob)
        tree = parse_fdt(info["entries"][0]["data"])
        bad = []
        for key, num in values.items():
            spec = SPEC_BY_KEY.get(key)
            if spec is None or key not in getattr(self, "touched", set()):
                continue
            node = tree.find(self._p(spec.node, 0))
            if node is None:
                bad.append((spec.label, num, "<节点缺失>"))
                continue
            val = node.get(spec.prop)
            if val is None:
                bad.append((spec.label, num, "<属性缺失>"))
                continue
            got = _read_kind(spec, val)
            if got is None:
                bad.append((spec.label, num, "<无法解析>"))
                continue
            if spec.kind == "thermal_cap":      # 上限语义：实际值 ≤ 设定值即通过
                if int(got) > int(num):
                    bad.append((spec.label, num, got))
            elif abs(int(got) - int(num)) > 1:
                bad.append((spec.label, num, got))
        return bad


def read_dtbo_bytes(d):
    magic, total, hsz, esz, cnt, eoff, psz, ver = struct.unpack_from(">8I", d, 0)
    if magic != DTBO_MAGIC:
        raise ValueError("导出镜像 magic 异常")
    entries = []
    for i in range(cnt):
        size, off, tid, rev = struct.unpack_from(">4I", d, eoff + i * esz)
        entries.append(dict(size=size, offset=off, id=tid, rev=rev,
                            data=d[off:off + size]))
    return dict(total=total, page=psz or 4096, entries=entries)


def _groups(val, width):
    items = strtab(val)
    if not items or len(items) % width:
        return None, None
    return [items[i:i + width] for i in range(0, len(items), width)], width


def _read_kind(spec, val):
    kind = spec.kind
    if kind == "u32":
        v = u32s(val)
        return v[0] if v else None
    if kind.startswith("u32["):
        idx = int(kind[4:-1])
        v = u32s(val)
        return v[idx] if idx < len(v) else None
    if kind.startswith("jeita|") or kind.startswith("atl|"):
        _, field_s, temp_s = kind.split("|")
        field, temp = int(field_s), int(temp_s)
        groups, _ = _groups(val, 8)
        if not groups:
            return None
        pick = [g for g in groups
                if _num(g[0]) is not None and _num(g[0]) >= temp]
        if not pick:
            return None
        vals = [_num(g[field]) for g in pick if _num(g[field]) is not None]
        return max(vals) if vals else None
    if kind == "thermal_cap":
        v = u32s(val)
        return max(v) if v else None
    return None


def _num(s):
    try:
        return int(s)
    except Exception:
        return None


def _write_kind(spec, node, value):
    kind = spec.kind
    if kind == "u32":
        node.put(spec.prop, pack_u32s([value]))
        return
    if kind.startswith("u32["):
        idx = int(kind[4:-1])
        arr = u32s(node.get(spec.prop))
        while len(arr) <= idx:
            arr.append(0)
        arr[idx] = value
        node.put(spec.prop, pack_u32s(arr))
        return
    if kind.startswith("jeita|") or kind.startswith("atl|"):
        _, field_s, temp_s = kind.split("|")
        field, temp = int(field_s), int(temp_s)
        raw = node.get(spec.prop)
        groups, width = _groups(raw, 8)
        if not groups:
            return
        flat = []
        for g in groups:
            g = list(g)
            if _num(g[0]) is not None and _num(g[0]) >= temp:
                if _num(g[field]) is not None:
                    g[field] = str(value)
            flat.extend(g)
        node.put(spec.prop, pack_strtab(flat))
        return
    if kind == "thermal_cap":
        arr = u32s(node.get(spec.prop))
        if not arr:
            return
        node.put(spec.prop, pack_u32s([min(v, value) for v in arr]))
        return


# ================================================================ 差异对比
def _flatten(node, path=""):
    p = (path.rstrip("/") + "/" + node.name) if node.name else (path or "/")
    out = {}
    for k, v in node.props:
        out[p + "::" + k] = v
    for c in node.children:
        out.update(_flatten(c, p))
    return out


def _fmt_raw(val):
    if val is None:
        return "<无>"
    if not val:
        return "<空>"
    u = u32s(val)
    if u and len(u) * 4 == len(val):
        return "<" + " ".join(str(x) for x in u) + ">"
    txt = strtab(val)
    if len(txt) > 2:
        return " / ".join(txt[:24]) + (" …(共%d项)" % len(txt) if len(txt) > 24 else "")
    try:
        return val.split(b"\x00")[0].decode("utf-8")
    except Exception:
        return val.hex()


def run_diff(argv):
    ap = argparse.ArgumentParser(prog="dtbo_tuner diff")
    ap.add_argument("base", help="基准镜像（原厂）")
    ap.add_argument("other", help="对比镜像（改过的）")
    ap.add_argument("--all", action="store_true", help="连同非充电节点一起列出")
    ap.add_argument("--only", default="", help="只显示路径包含该子串的项")
    a = ap.parse_args(argv)

    da, db = read_dtbo(a.base), read_dtbo(a.other)
    ea = sorted(da["entries"], key=lambda e: e["size"])
    eb = sorted(db["entries"], key=lambda e: e["size"])
    print("基准: %s  %d 个 DTB" % (a.base, len(ea)))
    print("对比: %s  %d 个 DTB" % (a.other, len(eb)))
    total = 0
    for i in range(min(len(ea), len(eb))):
        A = _flatten(parse_fdt(ea[i]["data"]))
        B = _flatten(parse_fdt(eb[i]["data"]))
        diffs = []
        for k in sorted(set(A) | set(B)):
            if A.get(k) == B.get(k):
                continue
            if a.only and a.only not in k:
                continue
            if not a.all and "/__overlay__/" not in k:
                continue
            diffs.append((k, A.get(k), B.get(k)))
        total += len(diffs)
        print("\n===== DTB #%d（%d 处差异）=====" % (i, len(diffs)))
        for k, va, vb in diffs:
            print("  " + k)
            print("      基准: " + _fmt_raw(va))
            print("      对比: " + _fmt_raw(vb))
    print("\n合计 %d 处差异" % total)
    return 0


# ================================================================ fastboot 辅助
def _find_tool(name):
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.getcwd()):
        p = os.path.join(base, "tools", "platform-tools", name)
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which(name) or shutil.which(os.path.splitext(name)[0])


def fb_exe():
    return _find_tool("fastboot.exe")


def adb_exe():
    return _find_tool("adb.exe")


def run_cmd(args, timeout=30):
    """执行外部命令，返回 (ok, 合并后的输出文本)"""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=flags)
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode == 0, out.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return False, "找不到 %s（未安装或不在 PATH）" % args[0]
    except subprocess.TimeoutExpired:
        return False, "命令超时"


def fb_devices():
    """返回 (设备序列号列表, 原始输出)；fastboot 不可用时列表为 None"""
    exe = fb_exe()
    if not exe:
        return None, "找不到 fastboot"
    ok, out = run_cmd([exe, "devices"])
    if not ok and not out:
        return None, out
    devs = [l.split("\t", 1)[0].strip() for l in out.splitlines()
            if "\t" in l and l.split("\t", 1)[1].strip() == "fastboot"]
    return devs, out


def fb_current_slot():
    exe = fb_exe()
    if not exe:
        return None
    ok, out = run_cmd([exe, "getvar", "current-slot"], timeout=10)
    for line in out.splitlines():
        if "current-slot" in line and ":" in line:
            v = line.split(":", 1)[1].strip().lower()
            if v in ("a", "b"):
                return v
    return None


def adb_devices():
    """返回 (序列号列表, 原始输出)；adb 不可用时列表为 None"""
    exe = adb_exe()
    if not exe:
        return None, "找不到 adb"
    ok, out = run_cmd([exe, "devices"])
    devs = []
    for line in (out or "").splitlines():
        line = line.strip()
        if "\t" in line and line.endswith("device"):
            devs.append(line.split("\t", 1)[0].strip())
    return devs, out


def adb_reboot_bootloader():
    exe = adb_exe()
    if not exe:
        return False, "找不到 adb"
    return run_cmd([exe, "reboot", "bootloader"], timeout=20)


def fb_flash(img, slot):
    part = "dtbo_%s" % slot
    exe = fb_exe()
    if not exe:
        return False, part, "找不到 fastboot"
    ok, out = run_cmd([exe, "flash", part, img], timeout=180)
    return ok, part, out


def run_flash(argv):
    ap = argparse.ArgumentParser(prog="dtbo_tuner flash")
    ap.add_argument("image", help="要刷入的 dtbo 镜像")
    ap.add_argument("--slot", default="auto", choices=["auto", "a", "b"],
                    help="目标槽位（默认自动取当前槽位）")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.image):
        print("镜像不存在: %s" % a.image)
        return 2
    devs, _ = fb_devices()
    if not devs:
        adevs, _ = adb_devices()
        if adevs:
            print("设备在线（系统模式）：%s" % ", ".join(adevs))
            print("请先执行: %s reboot bootloader" % (adb_exe() or "adb"))
        else:
            print("未检测到 fastboot 设备（需要先进入 Bootloader 模式）")
        return 2
    slot = a.slot
    if slot == "auto":
        slot = fb_current_slot()
        if slot is None:
            print("无法读取当前槽位，请用 --slot a/b 明确指定")
            return 2
    print("目标分区: dtbo_%s" % slot)
    ok, part, out = fb_flash(a.image, slot)
    print(out)
    print("刷入 %s → %s：%s" % (a.image, part, "成功" if ok else "失败"))
    return 0 if ok else 3


# ================================================================ CLI 模式
def run_cli(argv):
    try:                                    # 控制台统一输出 UTF-8，避免中文乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if argv and argv[0] == "diff":
        return run_diff(argv[1:])
    if argv and argv[0] == "flash":
        return run_flash(argv[1:])

    ap = argparse.ArgumentParser(prog="dtbo_tuner")
    ap.add_argument("input", help="原厂或现有 dtbo 镜像")
    ap.add_argument("output", nargs="?", help="导出文件（--list 时可省略）")
    ap.add_argument("--preset", default="原厂", choices=list(PRESETS))
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--list", action="store_true", help="列出所有可调参数")
    ap.add_argument("--verify", action="store_true", help="导出后自检")
    ap.add_argument("--allow-missing", action="store_true",
                    help="允许缺失节点时继续导出（默认直接失败）")
    a = ap.parse_args(argv)

    t = Tuner().load(a.input)
    t.allow_missing = a.allow_missing
    print("镜像: %s" % a.input)
    print("设备: %s  board-id=%s miboard-id=%s  匹配17ProMax=%s"
          % (t.fingerprint["model"], t.fingerprint["board_id"],
             t.fingerprint["miboard_id"], t.device_ok))
    print("条目: %d 个 DTB   充电片段: %s" % (len(t.baseline), t.frag_desc()))
    if t.missing:
        print("缺失参数: %d / %d 项（该镜像里没有对应节点）"
              % (len(t.missing), len(SPECS)))

    values = {}
    for spec in SPECS:
        cur = t.read_value(spec)
        values[spec.key] = spec.stock if cur is None else cur
    if a.list:
        last = None
        for s in SPECS:
            if s.group != last:
                last = s.group
                print("\n[%s]" % s.group)
            cur = values[s.key]
            flag = "  " if cur == s.stock else " *"
            print("  %s%-22s %-26s 当前=%-8s 原厂=%s%s"
                  % (flag, s.key, s.label, cur, s.stock, s.unit and (" " + s.unit) or ""))
        print("\n  带 * 表示当前值与原厂不同；导出时用 --set KEY=VALUE 覆盖")
        return 0

    if not a.output:
        print("缺少输出文件参数：dtbo_tuner <输入镜像> <输出镜像>")
        return 2

    # 只有"用户明确要求改"的参数才参与写入；其余保持镜像原值
    wanted = dict(PRESETS[a.preset])
    for item in a.set:
        k, _, v = item.partition("=")
        if k not in SPEC_BY_KEY:
            print("未知参数: %s" % k)
            return 2
        if SPEC_BY_KEY[k].missing:
            print("警告: 参数 %s（%s）在本镜像里不存在，将被忽略"
                  % (k, SPEC_BY_KEY[k].label))
        wanted[k] = int(v)

    values = {}
    for spec in SPECS:
        if spec.key in wanted:
            values[spec.key] = wanted[spec.key]
        else:
            cur = t.read_value(spec)
            values[spec.key] = spec.stock if cur is None else cur

    minsize = t.info["filesize"] if t.info["filesize"] > 1024 * 1024 else 0
    try:
        blob, changes = t.build(values, minsize=minsize)
    except MissingNodeError as ex:
        print(str(ex))
        return 4
    open(a.output, "wb").write(blob)
    print("已导出: %s (%d 字节)" % (a.output, len(blob)))
    print("相对原厂改动的参数: %d 项" % len(changes))
    for spec, v in changes:
        print("   %-30s %s → %s" % (spec.label, spec.show(spec.stock), spec.show(v)))
    if a.verify:
        bad = t.verify(blob, values)
        if bad:
            print("自检失败:")
            for lbl, want, got in bad:
                print("   %-30s 期望=%s 实际=%s" % (lbl, want, got))
            return 3
        print("自检通过 ✓")
    return 0


# ================================================================ GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # ---- 高 DPI：per-monitor v2，避免 125%~200% 缩放下模糊/错位
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # PerMonitorV2 context（Win10 1703+）
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if not user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v1
            except Exception:
                pass
    except Exception:
        user32 = None

    # Tk 字体按系统缩放补偿（tk scaling = 1pt 对应的像素数）
    dpi_v = 96
    try:
        try:
            dpi_v = user32.GetDpiForSystem()            # 96 基准
        except Exception:
            hdc = user32.GetDC(0)
            dpi_v = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            user32.ReleaseDC(0, hdc)
        root.tk.call("tk", "scaling", dpi_v / 72.0)
    except Exception:
        pass
    dpi_v = [dpi_v]                      # 列表包装，避免 tkinter 属性代理

    def P(n):                        # 像素值按 DPI 缩放
        return round(n * dpi_v[0] / 96)

    BG = "#0F1115"
    CARD = "#161A20"
    CARD2 = "#1B2029"
    BORDER = "#252C36"
    TEXT = "#E6EAF0"
    MUTED = "#8A94A6"
    DIM = "#5A6373"
    ACCENT = "#3B82F6"
    OK = "#22C55E"
    WARN = "#F59E0B"
    DANGER = "#EF4444"

    FN = "Microsoft YaHei UI"

    root = tk.Tk()
    root.title("%s  v%s" % (APP_TITLE, APP_VER))
    root.geometry("%dx%d" % (P(1180), P(840)))
    root.minsize(P(960), P(680))
    root.configure(bg=BG)

    tuner = Tuner()
    state = {
        "values": {},
        "rows": {},
        "loaded": False,
        "force": False,
    }

    # ------------------------------------------------ 样式
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TButton", background=CARD2, foreground=TEXT, borderwidth=0,
                    focusthickness=0, padding=(14, 8), font=(FN, 10))
    style.map("TButton",
              background=[("active", "#242B36"), ("pressed", "#2C3441")],
              foreground=[("disabled", DIM)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                    font=(FN, 10, "bold"))
    style.map("Accent.TButton",
              background=[("active", "#2F6FD0"), ("pressed", "#275FB4"),
                          ("disabled", "#233043")],
              foreground=[("disabled", "#6B7A8F")])
    style.configure("Ghost.TButton", background=BG, foreground=MUTED)
    style.map("Ghost.TButton", background=[("active", CARD2)])

    # ------------------------------------------------ 顶部
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=22, pady=(18, 6))
    tk.Label(header, text="DTBO Tuner", bg=BG, fg=TEXT,
             font=(FN, 17, "bold")).pack(side="left")
    tk.Label(header, text="  小米17 Pro Max · 充电参数调节", bg=BG, fg=DIM,
             font=(FN, 10)).pack(side="left", pady=(6, 0))
    status_lbl = tk.Label(header, text="等待文件", bg=BG, fg=MUTED, font=(FN, 9))
    status_lbl.pack(side="right", pady=(6, 0))

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=18, pady=(4, 0))

    # ------------------------------------------------ 左栏
    left = tk.Frame(body, bg=BG, width=P(318))
    left.pack(side="left", fill="y", padx=(4, 14))
    left.pack_propagate(False)

    drop = tk.Frame(left, bg=CARD, highlightbackground=BORDER,
                    highlightthickness=1)
    drop.pack(fill="x")
    drop_inner = tk.Frame(drop, bg=CARD)
    drop_inner.pack(fill="x", padx=18, pady=22)
    drop_icon = tk.Label(drop_inner, text="⤓", bg=CARD, fg=DIM, font=(FN, 30))
    drop_icon.pack()
    drop_title = tk.Label(drop_inner, text="拖入 dtbo 镜像", bg=CARD, fg=TEXT,
                          font=(FN, 11, "bold"))
    drop_title.pack(pady=(4, 2))
    drop_hint = tk.Label(drop_inner, text="或点击选择文件", bg=CARD, fg=DIM,
                         font=(FN, 9))
    drop_hint.pack()

    def pick_file(_=None):
        p = filedialog.askopenfilename(
            title="选择 dtbo 镜像",
            filetypes=[("DTBO 镜像", "*.img"), ("所有文件", "*.*")])
        if p:
            load_file(p)

    for w in (drop, drop_inner, drop_icon, drop_title, drop_hint):
        w.bind("<Button-1>", pick_file)

    info_card = tk.Frame(left, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
    info_card.pack(fill="x", pady=(12, 0))
    info_box = tk.Frame(info_card, bg=CARD)
    info_box.pack(fill="x", padx=16, pady=14)

    info_file = tk.Label(info_box, text="未载入", bg=CARD, fg=DIM, font=(FN, 9),
                         anchor="w", justify="left", wraplength=285)
    info_file.pack(fill="x")
    info_dev = tk.Label(info_box, text="", bg=CARD, fg=MUTED, font=(FN, 9),
                        anchor="w", justify="left", wraplength=285)
    info_dev.pack(fill="x", pady=(8, 0))
    info_frag = tk.Label(info_box, text="", bg=CARD, fg=MUTED, font=(FN, 9),
                         anchor="w", justify="left", wraplength=285)
    info_frag.pack(fill="x", pady=(8, 0))

    legend = tk.Frame(left, bg=BG)
    legend.pack(fill="x", pady=(14, 0))
    for color, txt in ((OK, "原厂值"), (WARN, "已改动"), (DANGER, "保护性参数")):
        row = tk.Frame(legend, bg=BG)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="●", bg=BG, fg=color, font=(FN, 9)).pack(side="left")
        tk.Label(row, text=" " + txt, bg=BG, fg=DIM, font=(FN, 9)).pack(side="left")

    # ------------------------------------------------ 刷入卡片
    flash_card = tk.Frame(left, bg=CARD, highlightbackground=BORDER,
                          highlightthickness=1)
    flash_card.pack(fill="x", pady=(12, 0))
    fbox = tk.Frame(flash_card, bg=CARD)
    fbox.pack(fill="x", padx=16, pady=12)
    tk.Label(fbox, text="刷入", bg=CARD, fg=TEXT,
             font=(FN, 10, "bold")).pack(anchor="w")
    slot_var = tk.StringVar(value="auto")
    slot_row = tk.Frame(fbox, bg=CARD)
    slot_row.pack(fill="x", pady=(8, 0))
    tk.Label(slot_row, text="槽位", bg=CARD, fg=MUTED,
             font=(FN, 9)).pack(side="left")
    for val, lab in (("auto", "自动"), ("a", "A"), ("b", "B")):
        tk.Radiobutton(slot_row, text=lab, value=val, variable=slot_var,
                       bg=CARD, fg=TEXT, selectcolor=CARD2, relief="flat",
                       activebackground=CARD, highlightthickness=0,
                       font=(FN, 9)).pack(side="left", padx=(10, 0))
    flash_status = tk.Label(fbox, text="", bg=CARD, fg=DIM, font=(FN, 8),
                            anchor="w", justify="left", wraplength=285)
    flash_status.pack(fill="x", pady=(8, 0))

    def flash_btn_action():
        if not state["loaded"]:
            messagebox.showinfo("提示", "请先载入并导出镜像")
            return
        devs, _ = fb_devices()
        if not devs:
            messagebox.showwarning(
                "未检测到设备",
                "没有处于 Bootloader 模式的设备。\n\n"
                "先在手机上执行：\n  adb reboot bootloader\n\n"
                "进入 Bootloader 后再点「检测设备」重试。")
            return
        slot = slot_var.get()
        if slot == "auto":
            slot = fb_current_slot()
            if slot is None:
                messagebox.showwarning("槽位未知",
                                       "无法读取当前槽位，请手动选择 A 或 B")
                return
        img = getattr(tuner, "last_export", None)
        if not img or not os.path.isfile(img):
            messagebox.showinfo("提示", "请先导出镜像，再刷入")
            return
        if not messagebox.askyesno(
                "确认刷入",
                "将把 %s 刷入 dtbo_%s 分区。\n\n"
                "刷入后建议重启系统再观察充电行为。" % (os.path.basename(img), slot)):
            return
        ok, part, out = fb_flash(img, slot)
        flash_status.configure(text=out[-400:] or ("已写入 %s" % part),
                               fg=OK if ok else DANGER)
        if ok:
            status_lbl.configure(text="已刷入 %s" % part, fg=OK)
            if messagebox.askyesno("完成", "刷入成功，现在重启系统吗？"):
                exe = fb_exe()
                if exe:
                    run_cmd([exe, "reboot"], timeout=20)

    def detect_btn_action():
        devs, raw = fb_devices()
        if devs is None:
            flash_status.configure(text="未找到 fastboot 工具", fg=DANGER)
            return
        if not devs:
            flash_status.configure(text="未检测到 Bootloader 设备", fg=DANGER)
            return
        slot = fb_current_slot()
        flash_status.configure(
            text="设备: %s  槽位: %s" % (", ".join(devs[:2]), slot or "?"),
            fg=OK)

    def reboot_bl_btn_action():
        devs, _ = adb_devices()
        if not devs:
            messagebox.showwarning("未检测到设备",
                                   "没有通过 adb 连接的设备（需要打开 USB 调试）")
            return
        if not messagebox.askyesno(
                "重启到 Bootloader",
                "设备 %s 将重启进入 Bootloader。\n继续吗？" % devs[0]):
            return
        ok, out = adb_reboot_bootloader()
        flash_status.configure(text=out[-300:] or "已发送重启命令",
                               fg=OK if ok else DANGER)

    fbtn = tk.Frame(fbox, bg=CARD)
    fbtn.pack(fill="x", pady=(8, 0))
    ttk.Button(fbtn, text="检测设备", style="TButton",
               command=detect_btn_action).pack(side="left")
    ttk.Button(fbtn, text="重启BL", style="TButton",
               command=reboot_bl_btn_action).pack(side="left", padx=(8, 0))
    ttk.Button(fbtn, text="刷入镜像", style="Accent.TButton",
               command=flash_btn_action).pack(side="left", padx=(8, 0))

    # ------------------------------------------------ 右栏：滚动参数
    right = tk.Frame(body, bg=BG)
    right.pack(side="left", fill="both", expand=True)

    canvas = tk.Canvas(right, bg=BG, highlightthickness=0, bd=0)
    vbar = ttk.Scrollbar(right, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=BG)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    vbar.pack(side="right", fill="y")

    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfigure(win, width=e.width))

    def wheel(e):
        if e.delta:
            canvas.yview_scroll(int(-e.delta / 120) * 3, "units")

    canvas.bind_all("<MouseWheel>", wheel)

    # ------------------------------------------------ 自定义滑块
    class SliderRow(tk.Frame):
        H = 46

        def __init__(self, master, spec, on_change):
            tk.Frame.__init__(self, master, bg=CARD)
            self.spec = spec
            self.on_change = on_change
            self.value = spec.stock
            self.enabled = True

            top = tk.Frame(self, bg=CARD)
            top.pack(fill="x", padx=16, pady=(12, 0))
            self.dot = tk.Label(top, text="●", bg=CARD, fg=OK, font=(FN, 8))
            self.dot.pack(side="left")
            tk.Label(top, text=" " + spec.label, bg=CARD, fg=TEXT,
                     font=(FN, 10)).pack(side="left")
            self.val = tk.Label(top, text=spec.show(spec.stock), bg=CARD,
                                fg=MUTED, font=(FN, 10, "bold"))
            self.val.pack(side="right")

            self.cv = tk.Canvas(self, bg=CARD, height=self.H,
                                highlightthickness=0, bd=0)
            self.cv.pack(fill="x", padx=16, pady=(6, 0))
            self.cv.bind("<Configure>", lambda e: self._draw())
            self.cv.bind("<Button-1>", self._press)
            self.cv.bind("<B1-Motion>", self._drag)
            self.cv.bind("<Double-Button-1>", self._reset)

            desc = tk.Label(self, text=spec.desc, bg=CARD, fg=DIM, font=(FN, 8),
                            anchor="w", justify="left",
                            wraplength=690)
            desc.pack(fill="x", padx=16, pady=(0, 10))
            self.desc = desc

        # -- 绘制
        def _geom(self):
            w = self.cv.winfo_width() or 600
            pad = 9
            return pad, w - pad, 24

        def _draw(self):
            cv, spec = self.cv, self.spec
            cv.delete("all")
            x0, x1, y = self._geom()
            cv.create_line(x0 + 6, y, x1 - 6, y, width=6, capstyle="round",
                           fill="#2A3140")
            span = spec.mx - spec.mn or 1
            frac = (self.value - spec.mn) / span
            frac = max(0.0, min(1.0, frac))
            kx = x0 + 6 + frac * (x1 - x0 - 12)
            color = ACCENT
            if spec.danger is not None:
                bad = (self.value >= spec.danger if spec.stock < spec.danger
                       else self.value <= spec.danger)
                if bad:
                    color = DANGER
                elif abs(self.value - spec.stock) > 1e-9:
                    color = WARN
            elif abs(self.value - spec.stock) > 1e-9:
                color = WARN
            cv.create_line(x0 + 6, y, kx, y, width=6, capstyle="round", fill=color)
            # 原厂位置标记
            sfrac = (spec.stock - spec.mn) / span
            sfrac = max(0.0, min(1.0, sfrac))
            sx = x0 + 6 + sfrac * (x1 - x0 - 12)
            cv.create_line(sx, y - 9, sx, y + 9, width=1, fill=DIM)
            cv.create_oval(kx - 8, y - 8, kx + 8, y + 8, fill=color,
                           outline=CARD, width=3)
            cv.create_text(x0, y + 20, text=str(spec.mn), anchor="w",
                           fill=DIM, font=(FN, 7))
            cv.create_text(x1, y + 20, text=str(spec.mx), anchor="e",
                           fill=DIM, font=(FN, 7))

        # -- 交互
        def _apply_x(self, x):
            x0, x1, _ = self._geom()
            frac = (x - x0 - 6) / max(1, (x1 - x0 - 12))
            frac = max(0.0, min(1.0, frac))
            v = self.spec.mn + frac * (self.spec.mx - self.spec.mn)
            step = self.spec.step
            v = round(v / step) * step
            v = max(self.spec.mn, min(self.spec.mx, int(v)))
            if v != self.value:
                self.set(v, notify=True)

        def _press(self, e):
            self._apply_x(e.x)

        def _drag(self, e):
            self._apply_x(e.x)

        def _reset(self, _=None):
            self.set(self.spec.stock, notify=True)

        def set(self, v, notify=False):
            self.value = int(v)
            self.val.configure(text=self.spec.show(self.value))
            if self.spec.danger is not None:
                bad = (self.value >= self.spec.danger if self.spec.stock < self.spec.danger
                       else self.value <= self.spec.danger)
                col = DANGER if bad else (WARN if self.value != self.spec.stock else OK)
            else:
                col = WARN if self.value != self.spec.stock else OK
            self.dot.configure(fg=col)
            self._draw()
            if notify:
                self.on_change(self.spec.key, self.value)

        def set_enabled(self, flag):
            self.enabled = flag
            st = "normal" if flag else "disabled"
            self.cv.configure(cursor="" if flag else "arrow")
            self.desc.configure(fg=DIM if flag else "#3C434F")

        def mark_missing(self, flag):
            """该参数在这个镜像里不存在：禁用滑条并说明原因。"""
            if flag:
                self.desc.configure(fg=DIM, text=self.spec.desc +
                                    "  ·  本镜像里没有这个节点，无法写入")
                self.set_enabled(False)
            else:
                self.desc.configure(fg=MUTED, text=self.spec.desc)
                self.set_enabled(True)

    def make_section(title, sub, danger=False):
        card = tk.Frame(inner, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", pady=(0, 12), padx=(0, 6))
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(head, text=title, bg=CARD,
                 fg=DANGER if danger else TEXT,
                 font=(FN, 11, "bold")).pack(side="left")
        tk.Label(head, text="   " + sub, bg=CARD, fg=DIM,
                 font=(FN, 8)).pack(side="left")
        return card

    sections = {}
    for gi, (gname, gsub, gdanger) in enumerate([
        (G_WIRED, "电池电流 / 电压 / 协议限流", False),
        (G_WIRELESS, "无线充电电流与电压", False),
        (G_SAFE, "⚠ 这些是保护机制，放宽会增加过热与损坏风险", True),
    ]):
        sections[gname] = make_section(gname, gsub, gdanger)

    def on_slider(key, value):
        state["values"][key] = value
        refresh_summary()

    for spec in SPECS:
        row = SliderRow(sections[spec.group], spec, on_slider)
        row.pack(fill="x")
        state["rows"][spec.key] = row
        sep = tk.Frame(sections[spec.group], bg=BORDER, height=1)
        sep.pack(fill="x", padx=16)

    # ------------------------------------------------ 底部操作栏
    bottom = tk.Frame(root, bg=CARD, highlightbackground=BORDER,
                      highlightthickness=1)
    bottom.pack(fill="x", side="bottom", pady=(10, 0))

    bwrap = tk.Frame(bottom, bg=CARD)
    bwrap.pack(fill="x", padx=18, pady=12)

    summary = tk.Label(bwrap, text="未载入镜像", bg=CARD, fg=DIM, font=(FN, 9))
    summary.pack(side="left")

    btn_wrap = tk.Frame(bwrap, bg=CARD)
    btn_wrap.pack(side="right")

    allow_var = tk.BooleanVar(value=False)
    tk.Checkbutton(bwrap, text="允许缺失节点（不推荐）", variable=allow_var,
                   bg=CARD, fg=MUTED, selectcolor=CARD2, relief="flat",
                   activebackground=CARD, highlightthickness=0,
                   font=(FN, 9)).pack(side="left", padx=(12, 0))

    def refresh_summary():
        if not state["loaded"]:
            summary.configure(text="未载入镜像", fg=DIM)
            return
        n = sum(1 for k, v in state["values"].items()
                if abs(v - SPEC_BY_KEY[k].stock) > 1e-9)
        risky = 0
        for k, v in state["values"].items():
            s = SPEC_BY_KEY[k]
            if s.danger is None:
                continue
            bad = (v >= s.danger if s.stock < s.danger else v <= s.danger)
            if bad:
                risky += 1
        txt = "已改动 %d 项" % n
        if risky:
            txt += "  ·  %d 项保护参数已放宽" % risky
            summary.configure(text=txt, fg=WARN)
        else:
            summary.configure(text=txt, fg=DIM)

    def apply_preset(name):
        if not state["loaded"]:
            messagebox.showinfo("提示", "请先拖入 dtbo 镜像")
            return
        vals = PRESETS[name]
        for spec in SPECS:
            v = vals.get(spec.key, spec.stock)
            state["rows"][spec.key].set(v)
        state["values"] = {s.key: state["rows"][s.key].value for s in SPECS}
        refresh_summary()
        status_lbl.configure(text="已套用预设：%s" % name, fg=MUTED)

    def export():
        if not state["loaded"]:
            messagebox.showinfo("提示", "请先拖入 dtbo 镜像")
            return
        if not tuner.device_ok and not state["force"]:
            if not messagebox.askyesno(
                    "设备不匹配",
                    "这个镜像的设备指纹与小米17 Pro Max 不符：\n\n"
                    "model: %s\nboard-id: %s\n\n"
                    "参数表（原厂值与安全上下限）是按 17 Pro Max 定义的，"
                    "换机型后这些数值不一定适用。\n继续导出可能做出无法开机的镜像。\n仍要继续吗？"
                    % (tuner.fingerprint["model"], tuner.fingerprint["board_id"])):
                return
            state["force"] = True
        base = os.path.splitext(os.path.basename(tuner.path))[0]
        out = filedialog.asksaveasfilename(
            title="导出镜像", defaultextension=".img",
            initialfile="%s_tuned.img" % base,
            initialdir=os.path.dirname(tuner.path),
            filetypes=[("DTBO 镜像", "*.img")])
        if not out:
            return
        try:
            minsize = tuner.info["filesize"] if tuner.info["filesize"] > 1024 * 1024 else 0
            tuner.allow_missing = bool(allow_var.get())
            blob, changes = tuner.build(state["values"], minsize=minsize)
            bad = tuner.verify(blob, state["values"])
            if bad:
                messagebox.showerror("自检失败", "以下参数没有正确写入：\n\n"
                                     + "\n".join("%s 期望%s 实际%s" % b for b in bad[:8]))
                return
            open(out, "wb").write(blob)
        except MissingNodeError as ex:
            messagebox.showerror("节点缺失（已阻止导出）", str(ex))
            return
        except Exception as ex:
            messagebox.showerror("导出失败", str(ex))
            return
        tuner.last_export = out
        status_lbl.configure(text="已导出 %s" % os.path.basename(out), fg=OK)
        lines = ["已写入 %d 字节 → %s" % (len(blob), out), "",
                 "改动 %d 项（相对原厂）：" % len(changes)]
        for spec, v in changes[:24]:
            lines.append("  %-26s %s → %s" % (spec.label, spec.show(spec.stock), spec.show(v)))
        if len(changes) > 24:
            lines.append("  … 还有 %d 项" % (len(changes) - 24))
        lines.append("")
        lines.append("刷入命令：")
        lines.append('  adb reboot bootloader')
        lines.append('  fastboot flash dtbo_b "%s"' % os.path.basename(out))
        lines.append('  fastboot reboot')
        messagebox.showinfo("导出成功（已通过自检）", "\n".join(lines))

    def restore():
        apply_preset("原厂")

    ttk.Button(btn_wrap, text="恢复原厂", style="Ghost.TButton",
               command=restore).pack(side="left", padx=(0, 6))
    ttk.Button(btn_wrap, text="导出镜像", style="Accent.TButton",
               command=export).pack(side="left")

    preset_bar = tk.Frame(root, bg=BG)
    preset_bar.pack(fill="x", padx=18, pady=(6, 0), side="bottom")
    tk.Label(preset_bar, text="预设", bg=BG, fg=DIM, font=(FN, 9)).pack(side="left")
    for name in PRESETS:
        ttk.Button(preset_bar, text=name, style="TButton",
                   command=lambda n=name: apply_preset(n)).pack(side="left", padx=(8, 0))

    # ------------------------------------------------ 载入逻辑
    def load_file(path):
        try:
            tuner.load(path)
        except Exception as ex:
            messagebox.showerror("无法解析", "%s\n\n%s" % (os.path.basename(path), ex))
            return
        state["loaded"] = True
        state["force"] = False
        fp = tuner.fingerprint
        info_file.configure(text="%s\n%.1f KB · %d 个 DTB"
                            % (os.path.basename(path),
                               tuner.info["filesize"] / 1024.0,
                               len(tuner.baseline)),
                            fg=TEXT)
        if tuner.device_ok:
            info_dev.configure(text="✓ 小米 17 Pro Max (popsicle)\n%s" % fp["model"][:42],
                               fg=OK)
            status_lbl.configure(text="已载入", fg=OK)
        else:
            info_dev.configure(
                text="✗ 设备指纹不匹配\nmodel: %s\nboard-id: %s" %
                     (fp["model"][:38], list(fp["board_id"])), fg=DANGER)
            status_lbl.configure(text="设备不匹配", fg=DANGER)

        if tuner.frag["index"] is None:
            info_frag.configure(
                text="✗ 未找到充电片段\n该镜像里没有 %s 这类节点"
                     % "、".join(ANCHOR_NODES[:3]), fg=DANGER)
        else:
            info_frag.configure(
                text="充电片段: fragment@%d（命中 %d 个锚点）"
                     % (tuner.frag["index"], len(tuner.frag["hits"])), fg=MUTED)

        # 用文件里的实际值作为初始值
        for spec in SPECS:
            cur = tuner.read_value(spec)
            state["rows"][spec.key].mark_missing(spec.missing)
            if cur is None:
                state["rows"][spec.key].set(spec.stock)
                state["values"][spec.key] = spec.stock
            else:
                state["values"][spec.key] = cur
                state["rows"][spec.key].set(cur)
        refresh_summary()

    # ------------------------------------------------ 原生拖放（纯 ctypes）
    def enable_dnd():
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            WM_DROPFILES = 0x0233
            GWL_WNDPROC = -4
            LR = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LR, wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM)

            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                               wintypes.UINT, wintypes.WPARAM,
                                               wintypes.LPARAM]
            user32.CallWindowProcW.restype = LR
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                               wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
            shell32.DragAcceptFiles.restype = None
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragFinish.restype = None

            hwnd = ctypes.c_void_p(int(root.frame(), 16))
            shell32.DragAcceptFiles(hwnd, True)

            # 原生缓冲 + 就绪标志：回调里只做原生读取，绝不回调 Python/Tk。
            # 否则 tkinter 主循环用 Py_BEGIN_ALLOW_THREADS 释放了 GIL，回调里
            # 再进入 Python/Tk 会引发致命 GIL 崩溃（进程直接退出，表现就是“卡退”）。
            g_buf = ctypes.create_unicode_buffer(32768)
            g_ready = ctypes.c_int(0)

            def forward(h, msg, wp, lp):
                # 转发前先还原原 WndProc，避免 Tk 重入消息循环时再次进入本回调
                setter(hwnd, GWL_WNDPROC, old)
                try:
                    return user32.CallWindowProcW(old, h, msg, wp, lp)
                finally:
                    setter(hwnd, GWL_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))

            def handler(h, msg, wp, lp):
                try:
                    if msg == WM_DROPFILES:
                        hdrop = ctypes.c_void_p(wp)
                        n = shell32.DragQueryFileW(hdrop, 0, None, 0)
                        if n == 0 or n >= len(g_buf):
                            shell32.DragFinish(hdrop)
                            return 0
                        shell32.DragQueryFileW(hdrop, 0, g_buf, n + 1)
                        shell32.DragFinish(hdrop)
                        g_ready.value = 1
                        return 0
                    return forward(h, msg, wp, lp)
                except Exception:
                    try:
                        return forward(h, msg, wp, lp)
                    except Exception:
                        return 0

            proc = WNDPROC(handler)
            setter = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            setter.restype = ctypes.c_void_p
            old = setter(hwnd, GWL_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
            root._dnd_proc = proc
            root._dnd_old = old

            # 由 Tk 自身的 after 轮询（Python 线程，GIL 正常）取路径并加载
            def poll_dnd():
                try:
                    if g_ready.value == 1:
                        g_ready.value = 0
                        p = g_buf.value
                        if p:
                            load_file(p)
                except Exception:
                    pass
                root.after(150, poll_dnd)

            root.after(150, poll_dnd)
            return True
        except Exception:
            return False

    root.update_idletasks()
    dnd_ok = enable_dnd()
    if not dnd_ok:
        drop_hint.configure(text="拖放不可用，请点击选择文件", fg=WARN)

    # 启动时把滑块置为原厂值（未载入文件也能看）
    for spec in SPECS:
        state["rows"][spec.key].set(spec.stock)

    def on_close():
        try:
            canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main():
    args = sys.argv[1:]
    # 无参数 / --gui → 图形界面；带任意参数（--list、--set、镜像路径…）→ 命令行
    if not args or args[0] in ("--gui", "-g"):
        run_gui()
        return
    if args[0] in ("--cli", "-c"):
        args = args[1:]
    # 打包成 windowed exe 时没有 stdout，这时才挂到父控制台
    if sys.stdout is None or sys.stderr is None:
        try:
            import ctypes
            ctypes.windll.kernel32.AttachConsole(-1)
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
        except Exception:
            pass
    sys.exit(run_cli(args))


if __name__ == "__main__":
    main()
