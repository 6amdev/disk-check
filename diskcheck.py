#!/usr/bin/env python3
"""diskcheck — ตรวจสุขภาพฮาร์ดดิสก์ / SSD บน Linux แล้วสรุปเป็นรายงาน HTML

ใช้แค่ Python มาตรฐาน ไม่ต้องติดตั้งไลบรารีเพิ่ม (ค่า SMART ต้องมี smartmontools)

  sudo ./run.sh                    เปิดเมนู เลือกดิสก์และระดับการตรวจ
  sudo ./run.sh --disk sdb         ตรวจเฉพาะ /dev/sdb แบบมาตรฐาน
  sudo ./run.sh --disk sdb --full  + สแกนผิวจานเต็มทั้งลูก (นาน)
  sudo ./run.sh --disk sdb --selftest long   สั่ง self-test ในตัวดิสก์
  python3 diskcheck.py --quick     ตรวจเร็วแบบไม่ใช้ sudo (ข้อมูลไม่ครบ)

ทุกการทดสอบเป็นแบบ "อ่านอย่างเดียว" ไม่ลบหรือเขียนทับข้อมูลในดิสก์
"""

import argparse
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n import _, using, get_lang, set_default, LANGS  # noqa: E402
import i18n  # noqa: E402


def N_(text):
    """ทำเครื่องหมายข้อความที่ต้องแปลตอนแสดงผล แต่ค่าที่เก็บยังเป็นภาษาไทย (เก็บลงฐานข้อมูล/ใช้เทียบในโค้ด)
    Mark a Thai data value for translation at display time — the stored value itself stays Thai."""
    return text

VERSION = "1.0"
HERE = Path(__file__).resolve().parent
REPORT_DIR = HERE / "reports"

# ---------- เกณฑ์ ----------
USE_WARN, USE_CRIT = 80, 90            # % พื้นที่ใช้ไป
SSD_WEAR_WARN, SSD_WEAR_CRIT = 70, 90  # % อายุ SSD ที่ใช้ไป
HOURS_B, HOURS_WARN = 20_000, 40_000   # ชั่วโมงใช้งาน: เกิน B = เกรด B, เกิน WARN = มีข้อสังเกต
REAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "f2fs", "zfs"}
IS_ROOT = os.geteuid() == 0

# ---------- สีใน terminal ----------
TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else str(text)


OK = lambda t: c(t, "32")
WARN = lambda t: c(t, "33")
BAD = lambda t: c(t, "31;1")
DIM = lambda t: c(t, "2")
BOLD = lambda t: c(t, "1")
CYAN = lambda t: c(t, "36")
LEVEL_COLOR = {"ok": OK, "warn": WARN, "crit": BAD, "info": CYAN}


def width(t):
    """ความกว้างบนจอ — สระ/วรรณยุกต์ไทยที่อยู่บน-ล่างไม่กินช่อง"""
    import unicodedata
    return sum(0 if unicodedata.category(ch) in ("Mn", "Me", "Cf") else 1 for ch in str(t))


def pad(t, w):
    t = str(t)
    return t + " " * max(0, w - width(t))


def header(title):
    print()
    print(BOLD(CYAN(f"━━ {title} ")) + CYAN("━" * max(4, 62 - width(title))))


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def tbar(pct, width=20):
    filled = int(round(pct / 100 * width))
    color = OK if pct < USE_WARN else WARN if pct < USE_CRIT else BAD
    return color("█" * filled) + DIM("░" * (width - filled))


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _targs(kw):
    """ค่าที่แทนลงในข้อความ ถ้าเป็นข้อความไทย (เช่น ชื่อค่า SMART) ก็แปลด้วย / translate Thai string arguments too"""
    return {k: _(v) if isinstance(v, str) else v for k, v in kw.items()}


def add(target, level, msg, **kw):
    """บันทึกข้อสังเกต — msg เป็นข้อความไทยต้นฉบับ (ใช้ {ชื่อ} แทนค่า) เก็บ key+args ไว้แปลใหม่ได้ทีหลัง
    msg is the Thai source template; key+args are stored so reports can be re-rendered in any language."""
    target.setdefault("findings", []).append({"level": level, "msg": _(msg, **_targs(kw)), "key": msg, "args": kw})


def fmsg(f):
    """ข้อความของข้อสังเกตในภาษาปัจจุบัน (ผลตรวจเก่าที่ไม่มี key จะเทียบกับแม่แบบเพื่อแปล)"""
    if f.get("key"):
        return _(f["key"], **_targs(f.get("args") or {}))
    return legacy_msg(f.get("msg", ""))


_LEGACY = {}


def legacy_msg(msg):
    """ผลตรวจเก่า (ก่อนมีระบบหลายภาษา) เก็บแค่ข้อความไทยที่แทนค่าแล้ว — เทียบกับแม่แบบใน locales/<lang>_diskcheck.json
    แล้วแปลพร้อมค่าเดิม / Old findings stored only the filled-in Thai text: match it against the known templates."""
    lang = get_lang()
    if lang == i18n.SOURCE or not msg or i18n.has(msg, lang):
        return _(msg)
    pats = _LEGACY.get(lang)
    if pats is None:
        pats = []
        try:
            cat = json.loads((HERE / "locales" / f"{lang}_diskcheck.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cat = {}
        for key, tr in cat.items():
            if "{" not in key or "{{" in key:
                continue
            rx, seen, pos = "", set(), 0
            for m in re.finditer(r"\{(\w+)(?:[:!][^{}]*)?\}", key):
                rx += re.escape(key[pos:m.start()])
                rx += f"(?P={m.group(1)})" if m.group(1) in seen else f"(?P<{m.group(1)}>.+?)"
                seen.add(m.group(1))
                pos = m.end()
            rx += re.escape(key[pos:])
            literal = len(re.sub(r"\{[^{}]*\}", "", key).strip())
            if literal >= 3:
                pats.append((literal, re.compile(rx, re.S), re.sub(r"\{(\w+)[:!][^{}]*\}", r"{\1}", tr)))
        pats.sort(key=lambda p: -p[0])
        _LEGACY[lang] = pats
    for _n, rx, tpl in pats:
        m = rx.fullmatch(msg)
        if m:
            try:
                return tpl.format(**_targs(m.groupdict()))
            except (KeyError, IndexError, ValueError):
                break
    return msg


# =====================================================================
#  เก็บข้อมูล
# =====================================================================
def list_disks():
    rc, out, _err = run(["lsblk", "-J", "-b", "-o",
                      "NAME,TYPE,SIZE,MODEL,SERIAL,ROTA,TRAN,RM,FSTYPE,LABEL,MOUNTPOINTS,VENDOR"])
    if rc != 0:
        return []
    root_src = ""
    with open("/proc/mounts") as f:
        for line in f:
            p = line.split()
            if p[1] == "/":
                root_src = Path(p[0]).name
    disks = []
    for d in json.loads(out)["blockdevices"]:
        if d["type"] != "disk" or d["name"].startswith(("zram", "loop", "ram")) or not d.get("size"):
            continue  # size 0 = ช่องเสียบการ์ดที่ไม่มีการ์ด
        name = d["name"]
        vendor = (d.get("vendor") or "").strip()
        model = (d.get("model") or "").strip()
        if vendor.upper() in ("ATA", "") or model.upper().startswith(vendor.upper()):
            vendor = ""
        if name.startswith("nvme"):
            kind = "NVMe SSD"
        elif d.get("rota") in (True, "1", 1):
            kind = "HDD"
        else:
            kind = "SSD"
        parts = []
        for ch in d.get("children") or []:
            mps = [m for m in (ch.get("mountpoints") or []) if m]
            parts.append({"name": ch["name"], "size": ch["size"], "fstype": ch.get("fstype") or "",
                          "label": ch.get("label") or "", "mount": ", ".join(mps)})
        disks.append({
            "name": name, "dev": f"/dev/{name}", "kind": kind, "size": d["size"],
            "model": " ".join(filter(None, [vendor, model])) or N_("ไม่ทราบรุ่น"),
            "serial": (d.get("serial") or "").strip() or "-",
            "tran": d.get("tran") or "-",
            "usb": d.get("tran") == "usb" or d.get("rm") in (True, "1", 1),
            "fw": read(f"/sys/block/{name}/device/firmware_rev") or read(f"/sys/block/{name}/device/rev") or "-",
            "system": any(p["name"] == root_src for p in parts) or name == root_src,
            "partitions": parts, "findings": [],
        })
        identify(disks[-1])
        b = disks[-1]["bridge"] = bridge_info(name) if disks[-1]["usb"] else None
        disks[-1]["external"] = detect_external(b, disks[-1]["model"])
        if b and f"{b['vid']}:{b['pid']}" in NVME_BRIDGES:
            disks[-1]["kind"] = "NVMe SSD"  # กล่อง M.2 NVMe → USB
        if b and b["usb"] in ("USB 2.0", "USB 1.1", "USB 1.0"):
            add(disks[-1], "warn", "ต่ออยู่ที่ {usb} — ความเร็วจะตัน ลองเสียบพอร์ต USB 3.0 (สีฟ้า)", usb=b["usb"])
    return disks


# ---------- ฮาร์ดดิสก์/SSD ภายนอกสำเร็จรูป (My Book, Expansion, ...) ----------
EXTERNAL_LINES = [  # (คำที่เจอในชื่อ USB/รุ่น, ชื่อรุ่นภายนอก) — เรียงจากเฉพาะเจาะจงไปทั่วไป
    (r"my\s*book\s*duo", "WD My Book Duo"), (r"my\s*book", "WD My Book"), (r"my\s*passport", "WD My Passport"),
    (r"easystore", "WD easystore"), (r"wd_?black|\b[pd]10\b|\bp40\b|\bp50\b", "WD_BLACK"),
    (r"elements", "WD Elements"), (r"wd\s*blue\s*sn|my\s*cloud", "WD"),
    (r"expansion", "Seagate Expansion"), (r"backup\s*\+|backup\s*plus", "Seagate Backup Plus"),
    (r"one\s*touch", "Seagate One Touch"), (r"ultra\s*touch", "Seagate Ultra Touch"),
    (r"game\s*drive|firecuda\s*gaming", "Seagate Game Drive"), (r"firecuda", "Seagate FireCuda (External)"), (r"barracuda\s*fast", "Seagate BarraCuda Fast SSD"),
    (r"seagate\s*basic", "Seagate Basic"), (r"desktop\s*(drive|hdd)", "Seagate Desktop"),
    (r"canvio|toshiba\s*external", "Toshiba Canvio"), (r"(?:portable\s*ssd|pssd)\s*(t\d+)", "Samsung Portable SSD {0}"),
    (r"extreme\s*pro", "SanDisk Extreme PRO"), (r"extreme", "SanDisk Extreme"), (r"sandisk.*(portable|ssd)", "SanDisk Portable SSD"),
    (r"rugged", "LaCie Rugged"), (r"lacie|d2\s*professional", "LaCie"), (r"storejet", "Transcend StoreJet"),
    (r"esd\d+", "Transcend ESD"), (r"\bhd\s*\d{3}\b|\bsd\s*\d{3}\b", "ADATA"), (r"g-?drive", "G-Technology G-DRIVE"),
    (r"buffalo|ministation|hd-pc", "Buffalo"), (r"crucial\s*x\d+", "Crucial X"),
    (r"kingston\s*xs|\bxs[12]000\b", "Kingston XS"), (r"silicon\s*power|\barmor\b", "Silicon Power"),
]
EXTERNAL_VID = {  # USB vendor id ของแบรนด์ที่ขายเป็นดิสก์ภายนอกสำเร็จรูป (ไม่ใช่กล่องเปล่า)
    "1058": "WD", "0bc2": "Seagate", "0480": "Toshiba", "04e8": "Samsung", "0781": "SanDisk", "059f": "LaCie",
    "2537": None, "125f": "ADATA", "1f75": None, "0411": "Buffalo", "634e": "Crucial", "0634": "Crucial",
    "8564": "Transcend", "2770": None, "13fe": "Kingston", "0951": "Kingston",
}


def detect_external(bridge, name):
    """ดิสก์ภายนอกสำเร็จรูป → ชื่อรุ่น เช่น 'WD My Book' / None = กล่องทั่วไปหรือต่อตรง"""
    if not bridge:
        return None
    text = f"{bridge.get('name', '')} {name or ''}".lower()
    for pat, label in EXTERNAL_LINES:
        m = re.search(pat, text)
        if m:
            return label.format(*(g.upper() for g in m.groups())) if m.groups() else label
    brand = EXTERNAL_VID.get((bridge.get("vid") or "").lower())
    if brand:  # แบรนด์ภายนอกแต่ไม่รู้จักชื่อรุ่น → ใช้ชื่อสินค้าจาก USB
        prod = re.sub(r"\s+", " ", bridge.get("name") or "").strip()
        return prod if prod.lower().startswith(brand.lower()) else f"{brand} {prod}".strip()
    return None


def ext_kind(kind):
    return "External SSD" if "SSD" in (kind or "") else "External HDD"


def product_name(brand, series, cap, external=None):
    """ชื่อขาย: ดิสก์ภายนอกใช้ชื่อรุ่นภายนอก (WD My Book 8 TB) ไม่งั้นใช้ ยี่ห้อ + ซีรีส์ + ความจุ"""
    if external:
        return " ".join(filter(None, [external, cap]))
    brand = brand if brand not in (None, "ไม่ทราบ") else ""
    return " ".join(filter(None, [brand, (series or "").split(" (")[0], cap]))


# กล่อง/อะแดปเตอร์ USB ที่รองรับเฉพาะ M.2 NVMe (รุ่นที่รับทั้ง SATA/NVMe เช่น RTL9210B จะรู้จากค่า SMART แทน)
NVME_BRIDGES = {"152d:0583", "152d:0586", "174c:2362", "174c:2364", "2109:0715"}


def form_label(kind, form=None, model=""):
    """แบบ/ขนาดของดิสก์ที่คนซื้อเข้าใจ: M.2 NVMe, M.2 SATA, mSATA, 2.5 นิ้ว, 3.5 นิ้ว"""
    f = (form or "").lower()
    m = (model or "").lower()
    if "nvme" in (kind or "").lower():
        return "M.2 NVMe" if not f or "m.2" in f else f"{form} NVMe"
    if "m.2" in f or "m.2" in m or "m2 " in m + " " or "2280" in m:
        return "M.2 SATA"
    if "msata" in f or "msata" in m:
        return "mSATA"
    for size in ("3.5", "2.5", "1.8"):
        if f.startswith(size):
            return f'{size}" SATA'
    return {"HDD": "HDD", "SSD": "SSD"}.get(kind, kind or "?")


def form_extra(d, s):
    """แบบของดิสก์สำหรับต่อท้ายสเปก — ว่างถ้าไม่ได้บอกอะไรเพิ่มจากชนิด"""
    x = form_label(d["kind"], s.get("form"), d["model"])
    return None if x == d["kind"] else x


# ---------- ยี่ห้อ / ซีรีส์ ----------
BRAND_PREFIX = [  # (คำขึ้นต้นของชื่อรุ่น, ยี่ห้อ) — เรียงจากเฉพาะเจาะจงไปทั่วไป
    ("WDC ", "Western Digital"), ("WD ", "Western Digital"), ("WDS", "Western Digital"), ("WD", "Western Digital"),
    ("SAMSUNG", "Samsung"), ("MZ-", "Samsung"), ("MZV", "Samsung"), ("HGST", "HGST (Hitachi)"),
    ("HITACHI", "Hitachi"), ("HDS", "Hitachi"), ("HTS", "Hitachi"), ("HUA", "HGST (Hitachi)"), ("HUS", "HGST (Hitachi)"),
    ("TOSHIBA", "Toshiba"), ("KIOXIA", "Kioxia"), ("HDWD", "Toshiba"), ("HDWG", "Toshiba"), ("HDWT", "Toshiba"),
    ("DT01", "Toshiba"), ("MQ0", "Toshiba"), ("MG0", "Toshiba"), ("MD0", "Toshiba"),
    ("ST", "Seagate"), ("MAXTOR", "Maxtor (Seagate)"), ("CT", "Crucial"), ("CRUCIAL", "Crucial"),
    ("MTFD", "Micron"), ("MICRON", "Micron"), ("KINGSTON", "Kingston"), ("SA400", "Kingston"), ("SNV", "Kingston"),
    ("SANDISK", "SanDisk"), ("SDSSD", "SanDisk"), ("INTEL", "Intel"), ("SSDPE", "Intel"), ("SSDSC", "Intel"),
    ("SK HYNIX", "SK hynix"), ("SKHYNIX", "SK hynix"), ("HFS", "SK hynix"), ("ADATA", "ADATA"), ("XPG", "ADATA"),
    ("LEXAR", "Lexar"), ("TRANSCEND", "Transcend"), ("TS", "Transcend"), ("FORCE MP", "Corsair"),
    ("CORSAIR", "Corsair"), ("PNY", "PNY"), ("TEAM", "TeamGroup"), ("T-FORCE", "TeamGroup"), ("APACER", "Apacer"),
    ("HP ", "HP"), ("PATRIOT", "Patriot"), ("GIGABYTE", "Gigabyte"), ("NETAC", "Netac"), ("FUJITSU", "Fujitsu"),
    ("MHV", "Fujitsu"), ("MHZ", "Fujitsu"), ("SPCC", "Silicon Power"), ("PLEXTOR", "Plextor"), ("LITEON", "Lite-On"),
    ("SABRENT", "Sabrent"), ("ORICO", "Orico"), ("KLEVV", "KLEVV"), ("HIKVISION", "Hikvision"), ("HS-SSD", "Hikvision"),
]
PCI_VENDOR = {"144d": "Samsung", "15b7": "SanDisk / WD", "1c5c": "SK hynix", "1344": "Micron / Crucial",
              "c0a9": "Micron / Crucial", "8086": "Intel", "1e0f": "Kioxia", "1179": "Toshiba", "2646": "Kingston",
              "1b1c": "Corsair", "1cc1": "ADATA", "1d97": "Lexar / Longsys", "1987": "Phison", "126f": "Silicon Motion",
              "1e4b": "Maxio", "1b4b": "Marvell", "10ec": "Realtek", "1dbe": "INNOGRIT", "1e49": "YMTC", "1f40": "Netac"}
SERIES = [  # (ยี่ห้อ, regex ของรุ่น, ซีรีส์)
    ("Seagate", r"ST\d+VN", "IronWolf (NAS)"), ("Seagate", r"ST\d+NE", "IronWolf Pro (NAS)"),
    ("Seagate", r"ST\d+NM", "Exos (Enterprise)"), ("Seagate", r"ST\d+DM", "BarraCuda (Desktop)"),
    ("Seagate", r"ST\d+LM", "BarraCuda (Laptop)"), ("Seagate", r"ST\d+VX", N_("SkyHawk (กล้องวงจรปิด)")),
    ("Seagate", r"ST\d+VM", "Video / SkyHawk"), ("Seagate", r"ST\d+(LX|DX)", "FireCuda"),
    ("Western Digital", r"WD\d+EF", "WD Red (NAS)"), ("Western Digital", r"WD\d+FFW", "WD Red Pro (NAS)"),
    ("Western Digital", r"WD\d+EZ", "WD Blue (Desktop)"), ("Western Digital", r"WD\d+EZRX", "WD Green"),
    ("Western Digital", r"WD\d+(FZ|FA)", "WD Black (Performance)"), ("Western Digital", r"WD\d+PUR", N_("WD Purple (กล้องวงจรปิด)")),
    ("Western Digital", r"WD\d+F[RY]YZ", "WD Gold (Enterprise)"), ("Western Digital", r"WD\d+(LP|SP|JP)", "WD Blue/Black (Laptop)"),
    ("Western Digital", r"WD\d+EMAZ|WD\d+EDAZ", N_("WD Elements/My Book (ถอดจากกล่อง)")),
    ("Toshiba", r"HDWD", "P300 (Desktop)"), ("Toshiba", r"HDWG|MN0", "N300 (NAS)"), ("Toshiba", r"HDWT|MD0", "Surveillance"),
    ("Toshiba", r"MG0", "Enterprise Capacity"), ("Toshiba", r"MQ0", "Laptop"),
]


def bridge_info(name):
    """หา USB bridge / กล่องที่ดิสก์ต่ออยู่ จาก sysfs"""
    try:
        p = Path(f"/sys/block/{name}/device").resolve()
    except OSError:
        return None
    for parent in [p, *p.parents]:
        if (parent / "idVendor").exists():
            vid, pid = read(parent / "idVendor", ""), read(parent / "idProduct", "")
            mfr, prod = read(parent / "manufacturer", ""), read(parent / "product", "")
            speed = read(parent / "speed", "")
            usb = {"1.5": "USB 1.0", "12": "USB 1.1", "480": "USB 2.0", "5000": "USB 3.0",
                   "10000": "USB 3.1/3.2 Gen 2", "20000": "USB 3.2 Gen 2x2"}.get(speed, "")
            return {"vid": vid, "pid": pid, "name": (prod if mfr and prod.lower().startswith(mfr.lower()) else " ".join(filter(None, [mfr, prod]))) or f"{vid}:{pid}", "usb": usb}
    return None


def identify(d, family=None):
    model = d["model"].upper()
    brand = next((b for pre, b in BRAND_PREFIX if model.startswith(pre)), None)
    if not brand and d["name"].startswith("nvme"):
        ctrl = Path(f"/sys/block/{d['name']}/device")
        for f in ("device/subsystem_vendor", "device/vendor"):
            v = (read(ctrl / f, "") or "").replace("0x", "")
            if v in PCI_VENDOR:
                brand = PCI_VENDOR[v]
                break
    series = None
    if family:  # model_family จากฐานข้อมูลของ smartctl แม่นที่สุด
        series = family
        fam_brand = next((b for pre, b in BRAND_PREFIX if family.upper().startswith(pre)), None)
        brand = brand or fam_brand
        first = (brand or "").split()[0] if brand else ""
        if first and series.lower().startswith(first.lower()):
            series = series[len(first):].strip(" -") or None
    else:
        for b, pat, sname in SERIES:
            if b == brand and re.match(pat, model):
                series = sname
    d["brand"] = brand or N_("ไม่ทราบ")
    d["series"] = series


def smartctl_path():
    return shutil.which("smartctl") or next((p for p in ("/usr/sbin/smartctl", "/sbin/smartctl")
                                             if os.access(p, os.X_OK)), None)


SMARTCTL = smartctl_path()


def smart_read(name, dtype_hint=None):
    """อ่าน SMART — ถ้าผ่านกล่อง USB ไม่ได้ ลองบอกชนิด bridge ให้ smartctl"""
    tries = [dtype_hint] if dtype_hint else \
        [None, "sat", "sat,12", "sntjmicron", "sntrealtek", "sntasmedia", "usbjmicron", "usbcypress", "usbsunplus"]
    for dtype in tries:
        cmd = [SMARTCTL, "-j", "-a", f"/dev/{name}"]
        if dtype:
            cmd[2:2] = ["-d", dtype]
        rc, out, _err = run(cmd, timeout=60)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        if rc & 0b11:  # bit0 = argument ผิด, bit1 = เปิดอุปกรณ์ไม่ได้
            continue
        if any(k in data for k in ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log")):
            data["_dtype"] = dtype
            return data
    return None


ATA_WATCH = {  # ค่าที่ควรเป็น 0 — ถ้าไม่ใช่ แปลว่าดิสก์เริ่มเสีย
    5: N_("เซกเตอร์เสียที่ถูกย้ายแล้ว (Reallocated)"),
    10: N_("หมุนจานไม่ขึ้น (Spin Retry)"),
    187: N_("อ่านผิดพลาดแก้ไม่ได้ (Uncorrectable)"),
    196: N_("เหตุการณ์ย้ายเซกเตอร์ (Realloc Event)"),
    197: N_("เซกเตอร์รอย้าย (Pending)"),
    198: N_("เซกเตอร์อ่านไม่ได้ (Offline Unc.)"),
    199: N_("CRC error สาย/กล่อง (UDMA CRC)"),
}


def collect_smart(d):
    s = {"available": False}
    d["smart"] = s
    if not SMARTCTL:
        s["reason"] = N_("ยังไม่ได้ติดตั้ง smartmontools (sudo apt install smartmontools)")  # เก็บไทย แปลตอนแสดง
        add(d, "info", "ยังไม่ได้อ่านค่า SMART — ติดตั้ง smartmontools ก่อน")
        return
    if not IS_ROOT:
        s["reason"] = N_("ต้องรันด้วย sudo")
        add(d, "info", "ยังไม่ได้อ่านค่า SMART — รันด้วย sudo")
        return
    data = smart_read(d["name"])
    if not data:
        s["reason"] = N_("อ่านไม่ได้ — กล่อง/อะแดปเตอร์ USB บางรุ่นไม่ส่งค่า SMART")
        add(d, "warn", "อ่านค่า SMART ไม่ได้ — ลองเปลี่ยนกล่อง/สาย USB หรือต่อ SATA ตรง")
        return
    # ดิสก์บางลูกปิด SMART ไว้ — เปิดให้ก่อน (ไม่กระทบข้อมูล)
    if data.get("smart_support", {}).get("available") and not data.get("smart_support", {}).get("enabled"):
        cmd = [SMARTCTL, "-s", "on", d["dev"]]
        if data["_dtype"]:
            cmd[1:1] = ["-d", data["_dtype"]]
        run(cmd)
        data = smart_read(d["name"], data["_dtype"]) or data
        add(d, "info", "ดิสก์ปิด SMART ไว้ — เปิดให้แล้ว")
    if data.get("device", {}).get("protocol") == "NVMe" or "nvme_smart_health_information_log" in data:
        d["kind"] = "NVMe SSD"  # M.2 NVMe ที่ต่อผ่านกล่อง USB lsblk จะเห็นเป็น SSD ธรรมดา
    s["available"] = True
    s["dtype"] = data["_dtype"]
    s["passed"] = data.get("smart_status", {}).get("passed")
    s["hours"] = data.get("power_on_time", {}).get("hours")
    s["power_cycles"] = data.get("power_cycle_count")
    s["temp"] = data.get("temperature", {}).get("current")
    s["rpm"] = data.get("rotation_rate")
    s["form"] = data.get("form_factor", {}).get("name")
    s["interface"] = data.get("interface_speed", {}).get("current", {}).get("string")
    if data.get("serial_number"):
        d["serial"] = data["serial_number"]
    if data.get("model_name"):
        d["model"] = data["model_name"]
    if data.get("firmware_version"):
        d["fw"] = data["firmware_version"]
    identify(d, data.get("model_family"))
    s["attrs"] = []

    if s["passed"] is False:
        add(d, "crit", "SMART ประเมินว่า \"ไม่ผ่าน\" — ดิสก์ใกล้เสีย")
    elif s["passed"]:
        add(d, "ok", "SMART ประเมินรวม: ผ่าน")

    # error ที่ดิสก์บันทึกไว้เอง (ATA error log)
    errlog = data.get("ata_smart_error_log", {}).get("summary", {}).get("count")
    if errlog is not None:
        s["error_log"] = errlog
        if errlog:
            add(d, "warn", "ดิสก์บันทึก error ไว้ {n} ครั้ง (ATA error log)", n=errlog)
    # อายุ SSD แบบ SATA (smartctl อ่านจาก Device Statistics)
    if data.get("endurance_used", {}).get("current_percent") is not None and d["kind"] == "SSD":
        s["wear"] = data["endurance_used"]["current_percent"]
    # SCSI / USB-SCSI: grown defect list
    gdl = data.get("scsi_grown_defect_list")
    if gdl is not None:
        s["grown_defects"] = gdl
        if gdl:
            add(d, "crit", "มีเซกเตอร์เสียเพิ่มขึ้น (grown defects) {n} จุด", n=gdl)

    nv = data.get("nvme_smart_health_information_log")
    if nv:
        s["wear"] = nv.get("percentage_used", 0)
        s["spare"] = nv.get("available_spare")
        s["media_errors"] = nv.get("media_errors", 0)
        s["unsafe"] = nv.get("unsafe_shutdowns", 0)
        s["written"] = nv.get("data_units_written", 0) * 512_000
        s["read"] = nv.get("data_units_read", 0) * 512_000
        s["attrs"] = [
            {"label": N_("อายุ SSD ที่ใช้ไป"), "value": f"{s['wear']}%", "bad": s["wear"] >= SSD_WEAR_WARN},
            {"label": N_("พื้นที่สำรองคงเหลือ"), "value": f"{s['spare']}%", "bad": (s["spare"] or 100) < 20},
            {"label": "Media error", "value": s["media_errors"], "bad": s["media_errors"] > 0},
            {"label": N_("ปิดเครื่องไม่ปกติ"), "value": f"{s['unsafe']:,}", "bad": False},
            {"label": N_("เขียนข้อมูลไปแล้ว"), "value": human(s["written"]), "bad": False},
            {"label": N_("อ่านข้อมูลไปแล้ว"), "value": human(s["read"]), "bad": False},
        ]
        if s["wear"] >= SSD_WEAR_CRIT:
            add(d, "crit", "SSD ใช้ไปแล้ว {p}% ของอายุ", p=s["wear"])
        elif s["wear"] >= SSD_WEAR_WARN:
            add(d, "warn", "SSD ใช้ไปแล้ว {p}% ของอายุ", p=s["wear"])
        else:
            add(d, "ok", "SSD เหลืออายุประมาณ {p}%", p=100 - s["wear"])
        if s["media_errors"]:
            add(d, "crit", "มี media error {n} ครั้ง", n=s["media_errors"])
        cw = nv.get("critical_warning", 0)
        if cw:
            add(d, "crit", "NVMe แจ้งเตือนวิกฤต (critical warning = {cw:#x})", cw=cw)
        errs = nv.get("num_err_log_entries", 0)
        if errs:
            s["attrs"].append({"label": N_("รายการใน error log"), "value": f"{errs:,}", "bad": False})

    table = {a["id"]: a for a in data.get("ata_smart_attributes", {}).get("table", [])}
    if 4 in table:
        s["start_stop"] = table[4]["raw"]["value"]
    for aid, label in ATA_WATCH.items():
        if aid not in table:
            continue
        raw = table[aid]["raw"]["value"] & 0xFFFFFFFF  # บางยี่ห้อเก็บค่าอื่นไว้ในบิตสูง
        s["attrs"].append({"id": aid, "label": label, "value": raw, "bad": raw > 0})
        if raw and aid in (5, 196) and raw < 10:
            add(d, "warn", "{label} = {raw} — มีเซกเตอร์เสียเล็กน้อย ควรเฝ้าดูว่าเพิ่มขึ้นหรือไม่", label=label, raw=raw)
        elif raw and aid != 199:
            add(d, "crit", "{label} = {raw}", label=label, raw=raw)
        elif raw:
            add(d, "warn", "CRC error = {raw} — มักเกิดจากสาย/กล่อง USB ไม่ใช่ตัวดิสก์", raw=raw)
    if table and not any(a["bad"] for a in s["attrs"]):
        add(d, "ok", "ไม่มีเซกเตอร์เสียหรือรอย้าย")
    if s.get("wear") is not None and not nv:  # SATA SSD
        s["attrs"].insert(0, {"label": N_("อายุ SSD ที่ใช้ไป"), "value": f"{s['wear']}%", "bad": s["wear"] >= SSD_WEAR_WARN})
        if s["wear"] >= SSD_WEAR_CRIT:
            add(d, "crit", "SSD ใช้ไปแล้ว {p}% ของอายุ", p=s["wear"])
        elif s["wear"] >= SSD_WEAR_WARN:
            add(d, "warn", "SSD ใช้ไปแล้ว {p}% ของอายุ", p=s["wear"])
    if s.get("error_log") is not None:
        s["attrs"].append({"label": N_("Error ที่ดิสก์บันทึกไว้ (ATA log)"), "value": s["error_log"], "bad": s["error_log"] > 0})
    # ค่าที่ต่ำกว่าเกณฑ์ผู้ผลิตตอนนี้ (FAILING_NOW)
    failing = [a["name"] for a in table.values() if a.get("when_failed") == "now"]
    if failing:
        add(d, "crit", "ค่า SMART ต่ำกว่าเกณฑ์ผู้ผลิต: {names}", names=", ".join(failing))
    past = [a["name"] for a in table.values() if a.get("when_failed") == "past"]
    if past:
        add(d, "warn", "ค่า SMART เคยต่ำกว่าเกณฑ์ในอดีต: {names}", names=", ".join(past))
    # ค่าทั้งหมด (ไว้แสดงในตารางละเอียด)
    s["all_attrs"] = [{"id": a["id"], "name": a["name"], "value": a.get("value"), "worst": a.get("worst"),
                       "thresh": a.get("thresh"), "raw": a["raw"].get("string", a["raw"]["value"]),
                       "failing": a.get("when_failed", "") not in ("", None)}
                      for a in table.values()]

    h = s["hours"]
    if h is not None:
        if h >= HOURS_WARN:
            add(d, "warn", "ใช้งานมาแล้ว {h:,} ชม. (~{y:.1f} ปี) ถือว่าเยอะ", h=h, y=h / 8760)
        else:
            add(d, "ok", "ใช้งานมาแล้ว {h:,} ชม. (~{y:.1f} ปี)", h=h, y=h / 8760)
    if s["temp"] is not None:
        hot = 70 if d["kind"] == "NVMe SSD" else 55
        if s["temp"] >= hot:
            add(d, "warn", "อุณหภูมิสูง {t}°C", t=s["temp"])

    log = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", []) \
        or data.get("nvme_self_test_log", {}).get("table", [])
    if log:
        last = log[0]
        st = last.get("status", {}) if isinstance(last.get("status"), dict) else {}
        res = last.get("self_test_result", {}) if isinstance(last.get("self_test_result"), dict) else {}
        txt = st.get("string") or res.get("string") or "?"
        passed = st.get("passed", res.get("value", 0) == 0)
        s["selftest"] = {"type": (last.get("type") or last.get("self_test_code") or {}).get("string", "?"),
                         "status": txt, "passed": passed,
                         "hours": last.get("lifetime_hours", last.get("power_on_hours"))}
        if not passed:
            add(d, "crit", "self-test ล่าสุดไม่ผ่าน ({status})", status=txt)
    running = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    if running.get("remaining_percent"):
        s["selftest_running"] = running["remaining_percent"]
    s["selftest_minutes"] = data.get("ata_smart_data", {}).get("self_test", {}).get("polling_minutes", {})


def collect_sensor_temp(d):
    """อุณหภูมิจาก hwmon (NVMe / drivetemp) — ใช้ได้แม้ไม่มี sudo"""
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        if read(hw / "name") not in ("nvme", "drivetemp"):
            continue
        try:
            devpath = str((hw / "device").resolve())
        except OSError:
            continue
        blk = Path(f"/sys/block/{d['name']}/device").resolve()
        if devpath == str(blk) or devpath.startswith(str(blk)) or str(blk).startswith(devpath):
            v = read(hw / "temp1_input")
            if v:
                return int(v) / 1000
    return None


def collect_io(d):
    stat = read(f"/sys/block/{d['name']}/stat")
    if stat:
        f = [int(x) for x in stat.split()]
        d["io"] = {"read": f[2] * 512, "written": f[6] * 512}


def collect_partitions(report):
    rows, seen = [], set()
    with open("/proc/mounts") as f:
        mounts = [line.split()[:4] for line in f]
    for dev, mnt, fstype, opts in mounts:
        if fstype not in REAL_FS or dev in seen or not dev.startswith("/dev/"):
            continue
        mnt = mnt.replace("\\040", " ")
        seen.add(dev)
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        pct = used / (used + free) * 100 if (used + free) else 0
        ipct = (st.f_files - st.f_ffree) / st.f_files * 100 if st.f_files else 0
        rows.append({"mount": mnt, "dev": dev, "fstype": fstype, "total": total, "used": used,
                     "free": free, "pct": pct, "ipct": ipct, "ro": "ro" in opts.split(",")})
        system_mnt = mnt in ("/", "/home", "/var", "/boot", "/boot/efi", "/tmp")
        if pct >= USE_CRIT:
            add(report, "crit" if system_mnt else "warn", "{mnt} ใช้พื้นที่ไป {pct:.0f}% — ใกล้เต็มมาก", mnt=mnt, pct=pct)
        elif pct >= USE_WARN:
            add(report, "warn", "{mnt} ใช้พื้นที่ไป {pct:.0f}%", mnt=mnt, pct=pct)
        if ipct >= 80:
            add(report, "warn", "{mnt} ใช้ inode ไป {pct:.0f}% — สร้างไฟล์ใหม่อาจไม่ได้", mnt=mnt, pct=ipct)
        if rows[-1]["ro"] and mnt in ("/", "/home"):
            add(report, "crit", "{mnt} ถูก mount แบบอ่านอย่างเดียว — ระบบไฟล์อาจมีปัญหา", mnt=mnt)
    report["partitions"] = rows


def collect_log_errors(report, names):
    rc, out, _err = run(["journalctl", "-k", "-b", "--no-pager", "-o", "short-iso"], timeout=30)
    if rc != 0 or not out:
        rc, out, _err = run(["dmesg"])
    if rc != 0 or not out:
        report["log"] = None
        return
    pat = re.compile(r"i/o error|medium error|bad sector|ext4-fs error|btrfs.*error|xfs.*error|"
                     r"nvme.*timeout|reset controller|uncorrect|remounting filesystem read-only|"
                     r"critical medium|failed command|sense key", re.I)
    # เอาเฉพาะบรรทัดที่เกี่ยวกับดิสก์ที่ตรวจ (หรือพอร์ต ataN ที่ไม่ระบุชื่อดิสก์)
    dev_pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r"|ata\d+)", re.I) if names else None
    hits = [l for l in out.splitlines() if pat.search(l) and (not dev_pat or dev_pat.search(l))]
    report["log"] = hits[-30:]
    report["log_total"] = len(hits)
    if hits:
        add(report, "warn", "พบ error เกี่ยวกับดิสก์ใน log ระบบ {n} บรรทัด", n=len(hits))
    else:
        add(report, "ok", "ไม่พบ error ของดิสก์ใน log ระบบ")


# ---------- ทดสอบความเร็วอ่าน (ต้น/กลาง/ท้ายจาน) ----------
def read_speed(d, mb=256):
    size = int(read(f"/sys/block/{d['name']}/size", "0")) * 512
    chunk = 4 * 1024 * 1024
    total = min(mb * 1024 * 1024, size // 4 // chunk * chunk)
    if total < chunk:
        return
    zones = [(N_("ต้นจาน"), 0), (N_("กลางจาน"), size // 2), (N_("ท้ายจาน"), max(0, size - total - chunk))]  # เก็บไทย
    res = []
    fd = os.open(d["dev"], os.O_RDONLY)
    try:
        for label, start in zones:
            start -= start % chunk
            os.posix_fadvise(fd, start, total, os.POSIX_FADV_DONTNEED)
            t0, done = time.perf_counter(), 0
            try:
                while done < total:
                    done += len(os.pread(fd, chunk, start + done))
            except OSError:
                res.append({"zone": label, "mbps": None})
                continue
            res.append({"zone": label, "mbps": done / (time.perf_counter() - t0) / 1e6})
            sys.stdout.write(f"\r  {_(label)}: {res[-1]['mbps']:,.0f} MB/s   ")
            sys.stdout.flush()
    finally:
        os.close(fd)
    print()
    d["speed"] = res
    speeds = [r["mbps"] for r in res if r["mbps"]]
    if speeds:
        low = {"HDD": 40, "SSD": 150, "NVMe SSD": 400}.get(d["kind"], 40)
        if d["usb"] and (d.get("bridge") or {}).get("usb") == "USB 3.0":
            low = min(low, 250)  # USB 3.0 (5Gbps) อ่านได้สูงสุดราว 400 MB/s
        if min(speeds) < low:
            if d["usb"]:
                add(d, "warn", "ความเร็วอ่านต่ำผิดปกติ ({v:.0f} MB/s) — เช็คว่าเสียบพอร์ต USB 3.0 (สีฟ้า)", v=min(speeds))
            else:
                add(d, "warn", "ความเร็วอ่านต่ำผิดปกติ ({v:.0f} MB/s)", v=min(speeds))
        else:
            add(d, "ok", "ความเร็วอ่านเฉลี่ย {v:,.0f} MB/s", v=sum(speeds) / len(speeds))


# ---------- สแกนผิวจาน (อ่านอย่างเดียว) ----------
def surface_scan(d, quick):
    size = int(read(f"/sys/block/{d['name']}/size", "0")) * 512
    chunk = 1024 * 1024
    n = size // chunk
    if n < 2:
        return
    if quick:
        rnd = random.Random(int.from_bytes(os.urandom(4), "little"))
        positions = sorted({0, n - 1, *(rnd.randrange(n) for _i in range(2000))})
    else:
        positions = range(n)
    bad, slow, done = [], 0, 0
    t_start = time.perf_counter()
    last = 0
    interrupted = False
    fd = os.open(d["dev"], os.O_RDONLY)
    try:
        for i, pos in enumerate(positions):
            t0 = time.perf_counter()
            try:
                os.posix_fadvise(fd, pos * chunk, chunk, os.POSIX_FADV_DONTNEED)
                os.pread(fd, chunk, pos * chunk)
            except OSError:
                bad.append(pos)
            if time.perf_counter() - t0 > 0.5:
                slow += 1
            done = i + 1
            now = time.perf_counter()
            if now - last > 0.5 or done == len(positions):
                last = now
                frac = done / len(positions)
                el = now - t_start
                eta = el / frac - el
                w = 24
                barstr = "█" * int(frac * w) + "░" * (w - int(frac * w))
                sys.stdout.write(f"\r  {CYAN(barstr)} {frac * 100:5.1f}%  "
                                 + _("เสีย {bad}  ช้า {slow}  เหลือ ~{eta:.0f} นาที", bad=BAD(len(bad)) if bad else OK(0),
                                     slow=WARN(slow) if slow else 0, eta=eta / 60) + "   ")
                sys.stdout.flush()
    except KeyboardInterrupt:
        interrupted = True
        print(WARN("\n  " + _("หยุดสแกนกลางทาง — รายงานจะแสดงผลเท่าที่สแกนได้")))
    finally:
        os.close(fd)
    print()
    secs = time.perf_counter() - t_start
    d["scan"] = {"mode": "quick" if quick else "full", "checked": done, "total": len(positions),
                 "bad": len(bad), "bad_pos": bad[:50], "slow": slow, "secs": secs,
                 "interrupted": interrupted}
    label = N_("สุ่มอ่าน 2,000 จุด") if quick else N_("สแกนเต็มทั้งจาน")
    if bad:
        add(d, "crit", "{label}: พบจุดอ่านไม่ได้ {n} จุด", label=label, n=len(bad))
    elif interrupted:
        add(d, "ok", "{label}: ไม่พบจุดอ่านไม่ได้ (ยังสแกนไม่ครบ)", label=label)
    else:
        add(d, "ok", "{label}: ไม่พบจุดอ่านไม่ได้", label=label)
    if slow > (5 if quick else 50):
        add(d, "warn", "มีจุดที่อ่านช้าผิดปกติ {n} จุด — อาจเริ่มเสื่อม", n=slow)


def selftest_progress(data):
    """คืน % ที่เหลือ ถ้ายังทำ self-test อยู่ / None ถ้าไม่ได้ทำ"""
    st = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    if st.get("remaining_percent") is not None:
        return st["remaining_percent"]
    nv = data.get("nvme_self_test_log", {})
    if nv.get("current_self_test_operation", {}).get("value"):
        return 100 - nv.get("current_self_test_completion_percent", 0)
    return None


def run_selftest(d, kind):
    """สั่ง self-test ในตัวดิสก์ แล้วรอจนเสร็จ (กด Ctrl+C เพื่อข้ามการรอ)"""
    data = smart_read(d["name"])
    if not data:
        return
    dtype = data["_dtype"]
    mins = data.get("ata_smart_data", {}).get("self_test", {}).get("polling_minutes", {}) \
        .get("extended" if kind == "long" else "short")
    if d["kind"] == "NVMe SSD" and not mins:
        mins = 2 if kind == "short" else 20
    cmd = [SMARTCTL, "-t", kind, d["dev"]]
    if dtype:
        cmd[1:1] = ["-d", dtype]
    rc, out, err = run(cmd)
    if rc & 0b111 or "not supported" in (out + err).lower():
        print(WARN("  " + _("ดิสก์/กล่องนี้ไม่รองรับ self-test — ข้ามไป")))
        add(d, "info", "ไม่ได้ทำ self-test (ดิสก์หรือกล่อง USB ไม่รองรับ)")
        return
    label = N_("แบบสั้น") if kind == "short" else N_("แบบยาว (ตรวจทั้งจาน)")
    print(f"  self-test {_(label)} " + (DIM(_("~{m} นาที", m=mins)) if mins else "")
          + DIM(" · " + _("ห้ามถอดสาย · Ctrl+C = ข้ามการรอ")))
    limit = time.time() + ((mins or 10) * 1.5 + 5) * 60
    t0 = time.time()
    time.sleep(5)
    try:
        while time.time() < limit:
            data = smart_read(d["name"], dtype) or {}
            left = selftest_progress(data)
            if left is None:
                break
            frac = (100 - left) / 100
            w = 24
            el = (time.time() - t0) / 60
            sys.stdout.write(f"\r  {CYAN('█' * int(frac * w) + '░' * (w - int(frac * w)))} {100 - left:3d}%  "
                             + _("ผ่านไป {m:.0f} นาที", m=el) + "   ")
            sys.stdout.flush()
            time.sleep(15)
        print()
    except KeyboardInterrupt:
        print(WARN("\n  " + _("ข้ามการรอ — self-test ยังทำต่อในตัวดิสก์ รันโปรแกรมใหม่ภายหลังเพื่อดูผล")))
        add(d, "info", "สั่ง self-test {label} แล้ว แต่ไม่ได้รอผล", label=label)
        return
    d["selftest_done"] = kind


def chown_user(*paths):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if IS_ROOT and uid:  # ให้ไฟล์เป็นของผู้ใช้ ไม่ใช่ root
        for p in paths:
            try:
                os.chown(p, int(uid), int(gid))
            except OSError:
                pass


def big_dirs(report, root):
    rc, out, _err = run(["du", "-x", "-b", "-d", "2", str(root)], timeout=600)
    rows = []
    for line in out.splitlines():
        try:
            size, p = line.split("\t", 1)
        except ValueError:
            continue
        if p != str(root):
            rows.append({"size": int(size), "path": p})
    rows.sort(key=lambda r: -r["size"])
    report["big"] = {"root": str(root), "rows": rows[:20]}


def grade(d):
    """คืน (ตัวอักษร, ชื่อเกรด, คำอธิบาย) เป็นภาษาไทยเสมอ (เก็บลงฐานข้อมูล) — แปลตอนแสดงผลด้วย _()"""
    lv = {f["level"] for f in d["findings"]}
    s = d.get("smart", {})
    if "crit" in lv:
        return "F", N_("มีปัญหา"), N_("พบสัญญาณดิสก์เสีย ไม่ควรเก็บข้อมูลสำคัญ")
    if "warn" in lv:
        return "C", N_("พอใช้"), N_("ใช้งานได้ แต่มีข้อสังเกต")
    if not s.get("available"):
        return "?", N_("ข้อมูลไม่พอ"), s.get("reason") or N_("ยังไม่ได้อ่านค่า SMART")
    if s.get("passed") is None and not s.get("attrs"):
        return "?", N_("ข้อมูลไม่พอ"), N_("ดิสก์ส่งค่า SMART มาไม่ครบ")
    h = s.get("hours") or 0
    if h >= HOURS_B or (s.get("wear") or 0) >= 30:
        return "B", N_("ดี"), N_("ไม่พบปัญหา ผ่านการใช้งานมาพอสมควร")
    return "A", N_("ดีมาก"), N_("ไม่พบปัญหา สภาพดี")


# =====================================================================
#  แสดงผลใน terminal
# =====================================================================
BANNER = r"""
   ____  _     _       ____ _               _
  |  _ \(_)___| | __  / ___| |__   ___  ___| | __
  | | | | / __| |/ / | |   | '_ \ / _ \/ __| |/ /
  | |_| | \__ \   <  | |___| | | |  __/ (__|   <
  |____/|_|___/_|\_\  \____|_| |_|\___|\___|_|\_\ """


def print_disk_list(disks):
    print(DIM(f"   #  {pad(_('อุปกรณ์'), 14)}{pad(_('รุ่น'), 30)}{'':>4}{pad(_('ขนาด'), 6)}  {pad(_('ชนิด'), 9)} {_('ต่อผ่าน')}"))
    for i, d in enumerate(disks, 1):
        tag = WARN(" [" + _("ดิสก์ระบบ") + "]") if d["system"] else CYAN(" [USB]") if d["usb"] else ""
        print(f"  {BOLD(f'{i:>2}')}  {d['dev']:<14}{_(d['model'])[:29]:<30}{human(d['size']):>10}  "
              f"{d['kind']:<9} {d['tran']:<6}{tag}")


def print_disk(d):
    s = d.get("smart", {})
    g, gname, _gdesc = d["grade"]
    gcol = {"A": OK, "B": OK, "C": WARN, "F": BAD}.get(g, CYAN)
    header(f"{d['dev']}  {_(d['model'])}")
    print("  " + _("เกรด {g} {name}", g=gcol(BOLD(f" {g} ")), name=gcol(_(gname))))
    print("  " + _("ยี่ห้อ {brand}", brand=BOLD(_(d["brand"])))
          + (" · " + _("ซีรีส์ {series}", series=_(d["series"])) if d.get("series") else ""))
    print(DIM(f"  S/N {d['serial']} · firmware {d['fw']} · {human(d['size'])} · {d['kind']} · {d['tran']}"))
    if d.get("bridge"):
        b = d["bridge"]
        print(DIM("  " + _("ต่อผ่านกล่อง/อะแดปเตอร์: {name}", name=f"{b['name']} [{b['vid']}:{b['pid']}]")
                  + (f" · {b['usb']}" if b["usb"] else "")))
    if d.get("external"):
        print("  " + _("ดิสก์ภายนอก: {ext} (ข้างในเป็น {model})", ext=CYAN(d["external"]), model=d["model"]))

    if s.get("available"):
        if s.get("hours") is not None:
            print(f"  {pad(_('ชั่วโมงใช้งาน'), 16)}" + _("{h:,} ชม. (~{y:.1f} ปี)", h=s["hours"], y=s["hours"] / 8760))
        if s.get("temp") is not None:
            print(f"  {pad(_('อุณหภูมิ'), 16)}{s['temp']}°C")
        for a in s.get("attrs", []):
            v = BAD(a["value"]) if a["bad"] else OK(a["value"])
            print(f"  {pad(_(a['label']), 40)} {v}")
        if s.get("selftest"):
            st = s["selftest"]
            print(f"  {pad(_('self-test ล่าสุด'), 16)}{st['type']}: {OK(st['status']) if st['passed'] else BAD(st['status'])}")
        if s.get("selftest_running"):
            print(WARN("  " + _("กำลังทำ self-test อยู่ เหลือ {p}%", p=s["selftest_running"])))
    else:
        if d.get("sensor_temp") is not None:
            print(f"  {pad(_('อุณหภูมิ'), 16)}{d['sensor_temp']:.0f}°C")
        print(WARN(f"  SMART: {_(s.get('reason', '-'))}"))
    for f in d["findings"]:
        print(f"  {LEVEL_COLOR[f['level']]('●')} {fmsg(f)}")


# =====================================================================
#  รายงาน HTML
# =====================================================================
CSS = """
:root{--bg:#0b0f17;--card:#121826;--card2:#182033;--line:#243049;--fg:#e6ebf5;--mut:#8a97b1;
--ok:#22c55e;--warn:#f59e0b;--crit:#ef4444;--info:#38bdf8;--acc:#7c5cff;}
@media (prefers-color-scheme: light){:root:not([data-theme=dark]){--bg:#f4f6fb;--card:#fff;--card2:#f1f4fa;
--line:#dfe4ee;--fg:#141a29;--mut:#5d6880;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 "Noto Sans Thai","Sarabun",system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
.hero{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:space-between;
padding:26px;border-radius:20px;background:linear-gradient(135deg,#1c1640,#0f2a44 60%,#0b3b3a);color:#fff;
border:1px solid #2b3a5c;box-shadow:0 20px 60px -30px #7c5cff88}
.hero h1{margin:0;font-size:28px;letter-spacing:.3px}.hero p{margin:4px 0 0;color:#b9c4dd}
.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{padding:6px 12px;border-radius:99px;background:#ffffff14;
border:1px solid #ffffff22;font-size:13px}
h2{font-size:18px;margin:34px 0 12px;display:flex;align-items:center;gap:10px}
h2:before{content:"";width:6px;height:20px;border-radius:3px;background:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;margin-bottom:18px}
.disk-top{display:flex;gap:22px;align-items:center;flex-wrap:wrap}
.grade{width:108px;height:108px;border-radius:50%;display:grid;place-items:center;flex:none;
font-size:52px;font-weight:800;color:#fff;position:relative}
.grade small{position:absolute;bottom:-26px;font-size:13px;font-weight:600;white-space:nowrap}
.gA,.gB{background:radial-gradient(circle at 30% 30%,#4ade80,#15803d);box-shadow:0 0 40px -6px #22c55e}
.gC{background:radial-gradient(circle at 30% 30%,#fbbf24,#b45309);box-shadow:0 0 40px -6px #f59e0b}
.gF{background:radial-gradient(circle at 30% 30%,#f87171,#b91c1c);box-shadow:0 0 40px -6px #ef4444}
.gQ{background:radial-gradient(circle at 30% 30%,#7dd3fc,#0369a1);box-shadow:0 0 40px -6px #38bdf8}
.disk-title{flex:1;min-width:240px}.brand{font-size:12.5px;font-weight:700;letter-spacing:1.2px;
text-transform:uppercase;color:var(--acc);margin-bottom:2px}.disk-title h3{margin:0 0 4px;font-size:21px}
.mut{color:var(--mut)}.mono{font-family:ui-monospace,"JetBrains Mono",monospace;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:22px}
.stat{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.stat b{display:block;font-size:20px}.stat span{font-size:12.5px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px 10px;text-align:left;
border-bottom:1px solid var(--line)}th{color:var(--mut);font-weight:600;font-size:12.5px}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12.5px;font-weight:600}
.p-ok{background:#22c55e22;color:var(--ok)}.p-warn{background:#f59e0b22;color:var(--warn)}
.p-crit{background:#ef444422;color:var(--crit)}.p-info{background:#38bdf822;color:var(--info)}
.find{display:flex;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line)}.find:last-child{border:0}
.dot{width:10px;height:10px;border-radius:50%;margin-top:7px;flex:none}
.d-ok{background:var(--ok)}.d-warn{background:var(--warn)}.d-crit{background:var(--crit)}.d-info{background:var(--info)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:18px}
@media(max-width:760px){.cols{grid-template-columns:1fr}.grade{width:84px;height:84px;font-size:40px}
.disk-top .grade{margin-bottom:26px}.stats{grid-template-columns:1fr 1fr}.stat b{font-size:17px}}
.sub{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.sub h4{margin:0 0 8px;font-size:14px;color:var(--mut);font-weight:600}
.bar{height:10px;border-radius:99px;background:var(--line);overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px}
.spd{display:grid;grid-template-columns:70px 1fr 90px;gap:10px;align-items:center;margin:8px 0}
details{margin-top:14px}summary{cursor:pointer;color:var(--mut)}
pre{white-space:pre-wrap;background:var(--card2);padding:12px;border-radius:12px;border:1px solid var(--line);
font-size:12px;max-height:340px;overflow:auto}
.foot{margin-top:40px;color:var(--mut);font-size:13px;text-align:center}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--mut)}
@media print{body{background:#fff;color:#000}.hero{color:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.card{break-inside:avoid}}
"""

E = html.escape


def pill(level, text):
    return f'<span class="pill p-{level}">{E(str(text))}</span>'


def barhtml(pct, color=None):
    color = color or ("var(--ok)" if pct < USE_WARN else "var(--warn)" if pct < USE_CRIT else "var(--crit)")
    return f'<div class="bar"><i style="width:{max(1, min(100, pct)):.1f}%;background:{color}"></i></div>'


def disk_html(d, public=False):
    """public=True = ฉบับแนบให้คนอื่นดู: ไม่มีข้อมูลเครื่องที่ใช้ตรวจ (ชื่อพาร์ทิชัน, กล่อง USB, /dev)"""
    s = d.get("smart", {})
    g, gname, gdesc = d["grade"]
    gcls = {"?": "gQ"}.get(g, f"g{g}")
    stats = []

    def stat(v, label):
        stats.append(f'<div class="stat"><b>{E(str(v))}</b><span>{E(label)}</span></div>')

    stat(human(d["size"]), _("ความจุ"))
    if s.get("hours") is not None:
        stat(f"{s['hours']:,}", _("ชั่วโมงใช้งาน (~{y:.1f} ปี)", y=s["hours"] / 8760))
    if s.get("power_cycles") is not None:
        stat(f"{s['power_cycles']:,}", _("ครั้งที่เปิดเครื่อง"))
    temp = s.get("temp") if s.get("temp") is not None else d.get("sensor_temp")
    if temp is not None:
        stat(f"{temp:.0f}°C", _("อุณหภูมิตอนตรวจ"))
    if s.get("wear") is not None:
        stat(f"{100 - s['wear']}%", _("อายุ SSD คงเหลือ"))
    if s.get("rpm"):
        stat(f"{s['rpm']:,}", _("รอบต่อนาที (RPM)"))
    if d.get("speed"):
        sp = [r["mbps"] for r in d["speed"] if r["mbps"]]
        if sp:
            stat(f"{max(sp):,.0f} MB/s", _("ความเร็วอ่านสูงสุด"))

    # findings
    order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
    finds = "".join(f'<div class="find"><span class="dot d-{f["level"]}"></span><div>{E(fmsg(f))}</div></div>'
                    for f in sorted(d["findings"], key=lambda f: order[f["level"]])
                    if not (public and f["level"] == "info")) or '<p class="mut">-</p>'

    # SMART key attrs
    if s.get("available"):
        rows = "".join(f'<tr><td>{E(_(a["label"]))}</td><td class="num">'
                       f'{pill("crit" if a["bad"] else "ok", a["value"])}</td></tr>' for a in s.get("attrs", []))
        st = s.get("selftest")
        if st:
            rows += (f'<tr><td>{E(_("Self-test ล่าสุด ({type})", type=str(st["type"])))}</td><td class="num">'
                     f'{pill("ok" if st["passed"] else "crit", st["status"])}</td></tr>')
        if s.get("selftest_running"):
            left = s["selftest_running"]
            rows += (f'<tr><td>{E(_("กำลังทำ self-test"))}</td>'
                     f'<td class="num">{pill("info", _("เหลือ {p}%", p=left))}</td></tr>')
        verdict = {True: pill("ok", _("ผ่าน")), False: pill("crit", _("ไม่ผ่าน"))}.get(s.get("passed"), pill("info", _("ไม่ทราบ")))
        smart_box = (f'<div class="sub"><h4>{_("ค่า SMART สำคัญ · ประเมินรวม {verdict}", verdict=verdict)}</h4>'
                     f'<table>{rows}</table></div>')
    else:
        smart_box = (f'<div class="sub"><h4>{_("ค่า SMART")}</h4><p>{pill("info", _("ไม่มีข้อมูล"))} '
                     f'{E(_(s.get("reason", "")))}</p></div>')

    # speed + scan
    right = ""
    if d.get("speed"):
        mx = max([r["mbps"] or 0 for r in d["speed"]] + [1])
        right += f'<div class="sub"><h4>{_("ความเร็วอ่าน (ต้น · กลาง · ท้ายจาน)")}</h4>'
        for r in d["speed"]:
            v = r["mbps"]
            right += (f'<div class="spd"><span>{E(_(r["zone"]))}</span>'
                      f'{barhtml((v or 0) / mx * 100, "linear-gradient(90deg,#7c5cff,#38bdf8)")}'
                      f'<b class="num">{f"{v:,.0f} MB/s" if v else _("อ่านไม่ได้")}</b></div>')
        right += '</div>'
    if d.get("scan"):
        sc = d["scan"]
        mode = _("สุ่มอ่าน 2,000 จุดทั่วจาน") if sc["mode"] == "quick" else _("อ่านเต็มทุกเซกเตอร์")
        pct = sc["checked"] / sc["total"] * 100 if sc["total"] else 0
        lvl = "crit" if sc["bad"] else "warn" if sc["interrupted"] else "ok"
        line = _("ตรวจแล้ว {pct:.1f}% · จุดอ่านไม่ได้ {bad} · จุดอ่านช้า {slow} · ใช้เวลา {m:.1f} นาที", pct=pct,
                 bad=pill("crit" if sc["bad"] else "ok", sc["bad"]), slow=pill("warn" if sc["slow"] else "ok", sc["slow"]),
                 m=sc["secs"] / 60)
        right += (f'<div class="sub" style="margin-top:12px"><h4>{_("สแกนผิวจาน · {mode}", mode=mode)}</h4>'
                  f'{barhtml(pct, "var(--" + lvl + ")")}'
                  f'<p style="margin:10px 0 0">{line}</p></div>')
    if not right:
        right = (f'<div class="sub"><h4>{_("ทดสอบการอ่าน")}</h4>'
                 f'<p class="mut">{_("ไม่ได้ทดสอบ (ต้องรันด้วย sudo)")}</p></div>')

    parts = "".join(f'<tr><td class="mono">{E(p["name"])}</td><td>{E(p["fstype"] or "-")}</td><td>{E(p["label"] or "-")}</td>'
                    f'<td class="num">{human(p["size"])}</td><td>{E(p["mount"] or "-")}</td></tr>' for p in d.get("partitions", []))
    parts = (f'<details><summary>{_("พาร์ทิชันในดิสก์ ({n})", n=len(d.get("partitions", [])))}</summary><table><tr>'
             f'<th>{_("ชื่อ")}</th><th>{_("ชนิด")}</th><th>{_("ชื่อไดรฟ์")}</th><th>{_("ขนาด")}</th><th>mount</th></tr>'
             f'{parts}</table></details>') if d.get("partitions") else \
        f'<p class="mut" style="margin-top:14px">{_("ไม่มีพาร์ทิชัน (ดิสก์ว่าง)")}</p>'

    allattrs = ""
    if s.get("all_attrs"):
        r = "".join(f'<tr><td class="num">{a["id"]}</td><td class="mono">{E(a["name"])}</td><td class="num">{a["value"]}</td>'
                    f'<td class="num">{a["worst"]}</td><td class="num">{a["thresh"]}</td><td class="num mono">{E(str(a["raw"]))}</td></tr>'
                    for a in sorted(s["all_attrs"], key=lambda a: a["id"]))
        allattrs = (f'<details><summary>{_("ค่า SMART ทั้งหมด ({n} รายการ)", n=len(s["all_attrs"]))}</summary><table><tr><th>ID</th>'
                    f'<th>{_("ชื่อ")}</th><th>Value</th><th>Worst</th><th>Thresh</th><th>Raw</th></tr>{r}</table></details>')

    tags = " ".join(filter(None, [pill("warn", _("ดิสก์ระบบ")) if d.get("system") else "", pill("info", "USB") if d.get("usb") else ""]))
    detail = [f"S/N {E(d['serial'])}", f"Firmware {E(d['fw'])}", E(d["kind"])]
    if form_extra(d, s):
        detail.append(E(form_extra(d, s)))
    if s.get("interface"):
        detail.append(E(s["interface"]))
    if public:
        tags, parts = "", ""
    else:
        detail.insert(0, E(d["dev"]))
        detail.append(_("ต่อผ่าน {x}", x=E(d["tran"])))
        if d.get("bridge"):
            detail.append(_("กล่อง {name}", name=E(d["bridge"]["name"]))
                          + (f" ({E(d['bridge']['usb'])})" if d["bridge"]["usb"] else ""))
    return f"""
<div class="card">
  <div class="disk-top">
    <div class="grade {gcls}">{E(g)}<small>{E(_(gname))}</small></div>
    <div class="disk-title">
      <div class="brand">{E(_(d.get('brand', '')))}{(' · ' + E(_(d['series']))) if d.get('series') else ''}{(' · 📦 ' + E(d['external'])) if d.get('external') else ''}</div>
      <h3>{E(_(d['model']))} {tags}</h3>
      <div class="mut">{E(_(gdesc))}</div>
      <div class="mono mut" style="margin-top:6px">{' · '.join(detail)}</div>
    </div>
  </div>
  <div class="stats" style="margin-top:34px">{''.join(stats)}</div>
  <div class="cols">
    <div>{smart_box}<div class="sub" style="margin-top:12px"><h4>{_('ผลการตรวจ')}</h4>{finds}</div></div>
    <div>{right}</div>
  </div>
  {parts}{allattrs}
</div>"""


def hours_text(d):
    h = d.get("smart", {}).get("hours")
    return f"{h:,}" if h is not None else "-"


def scan_text(d):
    sc = d.get("scan")
    if not sc:
        return _("ไม่ได้สแกน")
    mode = _("สุ่ม") if sc["mode"] == "quick" else _("เต็มจาน")
    return (_("{mode}: เสีย {bad} · ช้า {slow}", mode=mode, bad=sc["bad"], slow=sc["slow"])
            + (_(" (ไม่ครบ)") if sc["interrupted"] else ""))


def render_html(report):
    disks = report["disks"]
    cards = "".join(disk_html(d) for d in disks) or f'<div class="card">{_("ไม่ได้เลือกดิสก์")}</div>'
    order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
    sysf = "".join(f'<div class="find"><span class="dot d-{f["level"]}"></span><div>{E(fmsg(f))}</div></div>'
                   for f in sorted(report.get("findings", []), key=lambda f: order[f["level"]]))
    prows = "".join(f'<tr><td>{E(p["mount"])}</td><td class="mono">{E(p["dev"])}</td><td>{E(p["fstype"])}</td>'
                    f'<td class="num">{human(p["total"])}</td><td class="num">{human(p["used"])}</td>'
                    f'<td class="num">{human(p["free"])}</td><td style="min-width:140px">{barhtml(p["pct"])}'
                    f'<span class="mut" style="font-size:12px">{p["pct"]:.0f}%</span></td></tr>'
                    for p in report.get("partitions", []))
    log = report.get("log")
    logbox = (f'<p class="mut">{_("อ่าน log ไม่ได้")}</p>' if log is None else
              f'<p>{pill("ok", _("ไม่พบ error"))}</p>' if not log else
              f'<p>{pill("warn", _("พบ {n} บรรทัด", n=report["log_total"]))} {_("(แสดงล่าสุด {n})", n=len(log))}</p>'
              f'<pre>{E(chr(10).join(log))}</pre>')
    big = ""
    if report.get("big"):
        mx = max([r["size"] for r in report["big"]["rows"]] + [1])
        big = (f'<h2>{_("โฟลเดอร์ที่ใหญ่ที่สุดใน {root}", root=E(report["big"]["root"]))}</h2><div class="card"><table>' +
               "".join(f'<tr><td class="mono">{E(r["path"])}</td><td style="width:35%">'
                       f'{barhtml(r["size"] / mx * 100, "var(--acc)")}</td><td class="num">{human(r["size"])}</td></tr>'
                       for r in report["big"]["rows"]) + '</table></div>')
    grades = " ".join(f'<span class="chip">{E(_("{name}: เกรด {g}", name=d["name"], g=d["grade"][0]))}</span>' for d in disks)
    glvl = {"A": "ok", "B": "ok", "C": "warn", "F": "crit"}
    summary = ""
    if len(disks) > 1:
        summary = (f'<h2>{_("สรุปทุกลูก")}</h2><div class="card" style="overflow-x:auto"><table><tr><th>{_("เกรด")}</th>'
                   f'<th>{_("อุปกรณ์")}</th><th>{_("ยี่ห้อ")}</th><th>{_("รุ่น")}</th><th>S/N</th><th>{_("ความจุ")}</th>'
                   f'<th>{_("ชั่วโมงใช้งาน")}</th><th>{_("ผลสแกน")}</th></tr>' +
                   "".join(f'<tr><td>{pill(glvl.get(d["grade"][0], "info"), d["grade"][0] + " " + _(d["grade"][1]))}</td>'
                           f'<td class="mono">{E(d["dev"])}</td><td>{E(_(d.get("brand", "")))}</td><td>{E(_(d["model"]))}</td><td class="mono">{E(d["serial"])}</td>'
                           f'<td class="num">{human(d["size"])}</td>'
                           f'<td class="num">{hours_text(d)}</td>'
                           f'<td>{scan_text(d)}</td></tr>' for d in disks) + '</table></div>')
    return f"""<!doctype html><html lang="{get_lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Disk Check Report</title><style>{CSS}</style></head><body><div class="wrap">
<div class="hero"><div><h1>🩺 {_('รายงานสุขภาพดิสก์')}</h1>
<p>{E(report['host'])} · {E(report['time'])} · {_('ระดับการตรวจ: {level}', level=E(_(report['level'])))}</p></div>
<div class="chips">{grades}</div></div>
{summary}
<h2>{_('ดิสก์ที่ตรวจ ({n})', n=len(disks))}</h2>
<div class="legend"><span>{pill('ok', 'A')} {_('ดีมาก')}</span><span>{pill('ok', 'B')} {_('ดี ใช้มาพอสมควร')}</span>
<span>{pill('warn', 'C')} {_('พอใช้ มีข้อสังเกต')}</span><span>{pill('crit', 'F')} {_('มีปัญหา')}</span>
<span>{pill('info', '?')} {_('ข้อมูลไม่พอ')}</span></div><div style="height:12px"></div>
{cards}
<h2>{_('พื้นที่ของพาร์ทิชันที่ mount อยู่')}</h2>
<div class="card"><table><tr><th>{_('จุด mount')}</th><th>{_('อุปกรณ์')}</th><th>{_('ชนิด')}</th><th>{_('ขนาด')}</th><th>{_('ใช้ไป')}</th><th>{_('ว่าง')}</th><th>%</th></tr>
{prows}</table>{('<div style="margin-top:14px">' + sysf + '</div>') if sysf else ''}</div>
<h2>{_('Error ของดิสก์ใน log ระบบ (ตั้งแต่เปิดเครื่อง)')}</h2><div class="card">{logbox}</div>
{big}
<div class="foot">{_('สร้างโดย diskcheck v{v} · การทดสอบทั้งหมดเป็นแบบอ่านอย่างเดียว ไม่แก้ไขข้อมูลในดิสก์', v=VERSION)}<br>
{_('คำสั่ง:')} <span class="mono">{E(report['cmd'])}</span></div>
</div></body></html>"""


# =====================================================================
#  แพ็กเกจผลตรวจต่อดิสก์ (เอาไปแนบเว็บ/โพสต์ได้เลย)
# =====================================================================
def load_config():
    cfg = {"shop": "", "note": ""}
    try:
        cfg.update(json.loads((HERE / "config.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def disk_data(d, report):
    """ข้อมูลสะอาดๆ สำหรับให้เว็บดึงไปแสดง"""
    s = d.get("smart", {})
    attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
    sp = {r["zone"]: round(r["mbps"]) if r["mbps"] else None for r in d.get("speed", [])}
    sc = d.get("scan")
    return {
        "tested_at": report["time"], "tool": f"diskcheck v{VERSION}", "level": report["level"],
        "brand": d.get("brand"), "series": d.get("series"), "model": d["model"], "serial": d["serial"],
        "external_product": d.get("external"),  # ดิสก์ภายนอกสำเร็จรูป เช่น "WD My Book" (model = ไดรฟ์ข้างใน) / null
        "product_name": product_name(d.get("brand"), d.get("series"), human(d["size"]).replace(".0 ", " "), d.get("external")),
        "firmware": d["fw"], "type": d["kind"], "form_factor": s.get("form"), "rpm": s.get("rpm"),
        "interface": s.get("interface"), "capacity_bytes": d["size"], "capacity": human(d["size"]),
        "grade": d["grade"][0], "grade_label": d["grade"][1], "grade_detail": d["grade"][2],
        "smart": {
            "read_ok": bool(s.get("available")), "overall_passed": s.get("passed"),
            "power_on_hours": s.get("hours"), "power_cycles": s.get("power_cycles"),
            "temperature_c": s.get("temp") if s.get("temp") is not None else d.get("sensor_temp"),
            "ssd_wear_used_pct": s.get("wear"), "reallocated_sectors": attr.get(5),
            "pending_sectors": attr.get(197), "offline_uncorrectable": attr.get(198),
            "reported_uncorrectable": attr.get(187), "crc_errors": attr.get(199),
            "media_errors": s.get("media_errors"), "error_log_count": s.get("error_log"),
            "self_test": s.get("selftest"),
        },
        "read_speed_mbps": sp or None,
        "surface_scan": {"mode": sc["mode"], "checked_pct": round(sc["checked"] / sc["total"] * 100, 1),
                         "unreadable": sc["bad"], "slow": sc["slow"], "minutes": round(sc["secs"] / 60, 1)}
        if sc else None,
        "findings": [f for f in d["findings"] if f["level"] != "info"],
    }


def listing_text(d, report, cfg):
    """ข้อความสรุป คัดลอกไปวางในหน้าเว็บ/โพสต์ได้เลย"""
    s = d.get("smart", {})
    attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
    title = product_name(d.get("brand"), d.get("series"), human(d["size"]).replace(".0 ", " "), d.get("external"))
    L = [(f"[#{d['disk_no']}] " if d.get("disk_no") else "") + f"{title} ({d['model']})", ""]
    L.append(_("ผลตรวจสุขภาพ: เกรด {g} ({label}) — {desc}", g=d["grade"][0], label=_(d["grade"][1]), desc=_(d["grade"][2])))
    spec = [ext_kind(d["kind"]) if d.get("external") else d["kind"], form_extra(d, s),
            f"{s['rpm']:,} RPM" if s.get("rpm") else None, s.get("interface")]
    L.append("• " + _("สเปก: {spec}", spec=" · ".join(filter(None, spec))))
    if s.get("available"):
        L.append("• " + _("SMART ประเมินรวม: {v}", v={True: _("ผ่าน") + " ✅", False: _("ไม่ผ่าน") + " ❌"}.get(s.get("passed"), _("ไม่ทราบ"))))
        if s.get("hours") is not None:
            L.append("• " + _("ชั่วโมงใช้งาน: {h:,} ชม. (~{y:.1f} ปี)", h=s["hours"], y=s["hours"] / 8760))
        if s.get("wear") is not None:
            L.append("• " + _("อายุ SSD คงเหลือ: {p}%", p=100 - s["wear"]))
        if attr:
            L.append("• " + _("เซกเตอร์เสีย: {bad} · รอย้าย: {pending} · อ่านไม่ได้: {unc}",
                              bad=attr.get(5, "-"), pending=attr.get(197, "-"), unc=attr.get(198, "-")))
        if s.get("media_errors") is not None:
            L.append(f"• Media error: {s['media_errors']}")
        if s.get("selftest"):
            st = s["selftest"]
            L.append(f"• Self-test ({st['type']}): " + (_("ผ่าน") + " ✅" if st["passed"] else _("ไม่ผ่าน") + " ❌"))
    sp = [r["mbps"] for r in d.get("speed", []) if r["mbps"]]
    if sp:
        L.append("• " + _("ความเร็วอ่าน: {lo:,.0f}–{hi:,.0f} MB/s", lo=min(sp), hi=max(sp)))
    if d.get("scan"):
        sc = d["scan"]
        mode = _("สุ่มอ่าน 2,000 จุดทั่วจาน") if sc["mode"] == "quick" else _("อ่านเต็มทุกเซกเตอร์")
        L.append("• " + (_("สแกนผิวจาน ({mode}): พบจุดอ่านไม่ได้ {n} จุด", mode=mode, n=sc["bad"]) if sc["bad"]
                         else _("สแกนผิวจาน ({mode}): ไม่พบจุดเสีย", mode=mode) + " ✅"))
    L.append(f"• S/N: {d['serial']}")
    sale = d.get("sale") or {}
    if sale:
        L.append("")
        if sale.get("price"):
            tag = status_tag(sale.get("status"))
            L.append(_("ราคา: {price}", price=fmt_price(sale["price"])) + (f" ({_(tag)})" if tag else ""))
        for k, lbl, _kind, _ex in SALE_FIELDS:
            if k not in ("status", "price") and sale.get(k):
                L.append(f"{_(lbl)}: {sale[k]}")
    L += ["", _("ตรวจเมื่อ {time} ด้วย diskcheck v{v}", time=report["time"], v=VERSION) + (f" · {cfg['shop']}" if cfg.get("shop") else "")]
    if cfg.get("note"):
        L.append(cfg["note"])
    return "\n".join(L) + "\n"


def public_report_html(d, report, cfg):
    shop = E(cfg.get("shop") or "")
    return f"""<!doctype html><html lang="{get_lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(d['model'])} · Disk Health Report</title><style>{CSS}</style></head><body><div class="wrap">
<div class="hero"><div><h1>🩺 {_('รายงานตรวจสุขภาพดิสก์')}</h1>
<p>{E(_(d.get('brand', '')))} {E(d['model'])} · {human(d['size'])} · S/N {E(d['serial'])}</p></div>
<div class="chips"><span class="chip">{_('ตรวจเมื่อ {time}', time=E(report['time']))}</span><span class="chip">{_('ระดับ: {level}', level=E(_(report['level'])))}</span>
{f'<span class="chip">{shop}</span>' if shop else ''}</div></div>
<div style="height:18px"></div>
<div class="legend"><span>{pill('ok', 'A')} {_('ดีมาก')}</span><span>{pill('ok', 'B')} {_('ดี')}</span>
<span>{pill('warn', 'C')} {_('พอใช้ มีข้อสังเกต')}</span><span>{pill('crit', 'F')} {_('มีปัญหา')}</span></div>
<div style="height:12px"></div>
{disk_html(d, public=True)}
<div class="foot">{_('ตรวจด้วย diskcheck v{v}', v=VERSION)}{' · ' + shop if shop else ''} · {_('การทดสอบทั้งหมดเป็นแบบอ่านอย่างเดียว ไม่แก้ไขข้อมูลในดิสก์')}</div>
</div></body></html>"""


CARD_CSS = """
*{box-sizing:border-box;margin:0}html,body{width:1080px;height:1080px;overflow:hidden}
body{font-family:"Noto Sans Thai","Sarabun","Noto Sans",sans-serif;color:#fff;
background:radial-gradient(1200px 700px at 85% -10%,#3b2a8f 0,transparent 60%),
radial-gradient(900px 600px at -10% 110%,#0d5b57 0,transparent 60%),#0b0f17;padding:72px;display:flex;flex-direction:column}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:30px}
.brand{font-size:30px;letter-spacing:6px;text-transform:uppercase;color:#a78bfa;font-weight:700}
.model{font-size:40px;font-weight:700;margin-top:8px;color:#dbe3f5}
.cap{font-size:150px;font-weight:800;line-height:1;margin-top:26px;letter-spacing:-3px}
.kind{font-size:30px;color:#8a97b1;margin-top:10px}
.grade{width:230px;height:230px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;flex:none}
.grade b{font-size:120px;line-height:1}.grade span{font-size:28px;font-weight:700;margin-top:4px}
.gA,.gB{background:radial-gradient(circle at 30% 30%,#4ade80,#15803d);box-shadow:0 0 90px -10px #22c55e}
.gC{background:radial-gradient(circle at 30% 30%,#fbbf24,#b45309);box-shadow:0 0 90px -10px #f59e0b}
.gF{background:radial-gradient(circle at 30% 30%,#f87171,#b91c1c);box-shadow:0 0 90px -10px #ef4444}
.gQ{background:radial-gradient(circle at 30% 30%,#7dd3fc,#0369a1);box-shadow:0 0 90px -10px #38bdf8}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:auto}
.st{background:#ffffff0d;border:1px solid #ffffff1f;border-radius:26px;padding:24px 28px}
.st b{display:block;font-size:44px}.st span{font-size:25px;color:#a5b1c9}
.ok{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}
.foot{display:flex;justify-content:space-between;margin-top:30px;font-size:24px;color:#8a97b1}
"""


def card_html(d, report, cfg):
    s = d.get("smart", {})
    g, gname, _gdesc = d["grade"]
    gcls = {"?": "gQ"}.get(g, f"g{g}")
    attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
    tiles = []

    def tile(v, label, cls=""):
        tiles.append(f'<div class="st"><b class="{cls}">{E(str(v))}</b><span>{E(label)}</span></div>')

    if s.get("available"):
        tile({True: _("ผ่าน") + " ✓", False: _("ไม่ผ่าน") + " ✗"}.get(s.get("passed"), _("ไม่ทราบ")), _("SMART ประเมินรวม"),
             {True: "ok", False: "bad"}.get(s.get("passed"), ""))
    if s.get("hours") is not None:
        tile(_("{h:,} ชม.", h=s["hours"]), _("ชั่วโมงใช้งาน (~{y:.1f} ปี)", y=s["hours"] / 8760))
    if s.get("wear") is not None:
        tile(f"{100 - s['wear']}%", _("อายุ SSD คงเหลือ"), "ok" if s["wear"] < SSD_WEAR_WARN else "warn")
    if attr:
        bad = (attr.get(5) or 0) + (attr.get(197) or 0) + (attr.get(198) or 0)
        tile(f"{bad:,}", _("เซกเตอร์เสีย / รอย้าย"), "ok" if bad == 0 else "warn" if bad < 10 else "bad")
    sp = [r["mbps"] for r in d.get("speed", []) if r["mbps"]]
    if sp:
        tile(f"{max(sp):,.0f} MB/s", _("ความเร็วอ่านสูงสุด"))
    if d.get("scan"):
        sc = d["scan"]
        tile(_("ไม่พบจุดเสีย") if not sc["bad"] else _("เสีย {n} จุด", n=sc["bad"]), _("สแกนผิวจาน"),
             "ok" if not sc["bad"] else "bad")
    temp = s.get("temp") if s.get("temp") is not None else d.get("sensor_temp")
    if temp is not None and len(tiles) < 6:
        tile(f"{temp:.0f}°C", _("อุณหภูมิตอนตรวจ"))
    tiles = tiles[:6]
    spec = " · ".join(filter(None, [_(d.get("series")), d["kind"], form_extra(d, s), f"{s['rpm']:,} RPM" if s.get("rpm") else None]))
    inner = _("ข้างใน {model}", model=d["model"]) if d.get("external") else _(d["model"])
    return f"""<!doctype html><html lang="{get_lang()}"><head><meta charset="utf-8"><style>{CARD_CSS}</style></head><body>
<div class="top"><div><div class="brand">{E(d.get('external') or _(d.get('brand')) or '')}</div><div class="model">{E(inner)}</div>
<div class="cap">{E(human(d['size']).replace('.0 ', ' '))}</div><div class="kind">{E(spec)}</div></div>
<div class="grade {gcls}"><b>{E(g)}</b><span>{E(_(gname))}</span></div></div>
<div class="grid">{''.join(tiles)}</div>
<div class="foot"><span>{('#' + str(d['disk_no']) + ' · ') if d.get('disk_no') else ''}S/N {E(d['serial'])}{(' · <b style="color:#fff">' + E(fmt_price(d['sale']['price'])) + '</b>') if (d.get('sale') or {}).get('price') else ''}</span><span>{_('ตรวจ {date}', date=E(report['time'][:10]))}{' · ' + E(cfg['shop']) if cfg.get('shop') else ''}</span></div>
</body></html>"""


def find_chrome():
    return next((shutil.which(c) for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                 if shutil.which(c)), None)


def render_png(html_path, png_path):
    """แปลง card.html เป็นรูป PNG ด้วย Chrome แบบไม่เปิดหน้าต่าง"""
    chrome = find_chrome()
    if not chrome:
        return False
    import tempfile
    prof = tempfile.mkdtemp(prefix="diskcheck-chrome-")
    chown_user(prof)
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--no-default-browser-check", f"--user-data-dir={prof}", "--window-size=1080,1080",
           "--virtual-time-budget=2000", f"--screenshot={png_path}", f"file://{html_path}"]
    if IS_ROOT and os.environ.get("SUDO_USER"):
        cmd = ["sudo", "-u", os.environ["SUDO_USER"], "env", f"HOME=/home/{os.environ['SUDO_USER']}", *cmd]
    run(cmd, timeout=90)
    shutil.rmtree(prof, ignore_errors=True)
    return Path(png_path).exists() and Path(png_path).stat().st_size > 0


# =====================================================================
#  ฐานข้อมูลในตัว (SQLite) — ใช้ Serial Number เป็นกุญแจ
# =====================================================================
import base64
import sqlite3

DATA_DIR = HERE / "data"
DB_PATH = DATA_DIR / "diskcheck.db"
DISKS_DIR = HERE / "disks"
PHOTO_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
             ".gif": "image/gif", ".heic": None, ".heif": None}  # None = เก็บไว้ แต่เบราว์เซอร์แสดงไม่ได้

SCHEMA = """
CREATE TABLE IF NOT EXISTS disks(
  no               INTEGER PRIMARY KEY AUTOINCREMENT,  -- เลขประจำดิสก์ 1, 2, 3, ... ไม่ซ้ำ
  serial           TEXT UNIQUE NOT NULL,               -- Serial Number (กุญแจหลักที่ใช้ค้นหา)
  brand TEXT, series TEXT, model TEXT, capacity_bytes INTEGER, type TEXT,
  form_factor TEXT, rpm INTEGER, firmware TEXT,
  first_seen TEXT, last_tested TEXT, last_grade TEXT, last_grade_label TEXT,
  tests_count INTEGER DEFAULT 0,
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tests(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- เลขที่การตรวจ ไม่ซ้ำ
  disk_no          INTEGER NOT NULL REFERENCES disks(no),
  serial           TEXT NOT NULL,
  tested_at TEXT, level TEXT, grade TEXT, grade_label TEXT,
  smart_passed INTEGER, power_on_hours INTEGER, temperature_c REAL,
  reallocated INTEGER, pending INTEGER, offline_unc INTEGER, crc INTEGER,
  read_mbps_avg REAL, scan_mode TEXT, scan_unreadable INTEGER, scan_slow INTEGER,
  detail_json TEXT NOT NULL                            -- ผลตรวจละเอียดทั้งหมด
);
CREATE INDEX IF NOT EXISTS idx_tests_serial ON tests(serial);
"""


def db():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(disks)")}
    for col in ("sale_json", "fb_json"):  # อัปเกรดฐานข้อมูลเก่า
        if col not in cols:
            con.execute(f"ALTER TABLE disks ADD COLUMN {col} TEXT DEFAULT '{{}}'")
    if "external" not in cols:  # ชื่อรุ่นดิสก์ภายนอก เช่น WD My Book
        con.execute("ALTER TABLE disks ADD COLUMN external TEXT")
    con.commit()
    chown_user(DATA_DIR, DB_PATH)
    return con


# ---------- ข้อมูลการขาย ----------
# ป้าย/ตัวอย่าง/ชื่อสถานะเป็นภาษาไทย (เก็บลงฐานข้อมูล/ใช้เทียบในโค้ด) — แปลตอนแสดงผลด้วย _(lbl), _(status)
SALE_FIELDS = [  # (key, ป้าย, ชนิด, ตัวอย่าง)
    ("status", N_("สถานะ"), "select", ""),
    ("price", N_("ราคาขาย (บาท)"), "number", N_("เช่น 2500")),
    ("condition", N_("สภาพภายนอก"), "text", N_("เช่น สวย ไม่มีรอยบุบ")),
    ("warranty", N_("ประกัน"), "text", N_("เช่น รับประกันร้าน 30 วัน")),
    ("includes", N_("สิ่งที่ได้รับ"), "text", N_("เช่น ตัวดิสก์ + ห่อกันกระแทก")),
    ("shipping", N_("การจัดส่ง"), "text", N_("เช่น Kerry/Flash 50 บาท · นัดรับได้")),
    ("payment", N_("การชำระเงิน"), "text", N_("เช่น โอน / เก็บเงินปลายทาง")),
    ("contact", N_("ติดต่อ"), "text", N_("เช่น Line: 6amdev · โทร 08x-xxx-xxxx")),
    ("details", N_("รายละเอียดเพิ่มเติม"), "textarea", N_("ข้อมูลอื่นๆ ที่ลูกค้าอยากรู้")),
]
SALE_PRIVATE_FIELDS = [  # เก็บในฐานข้อมูลอย่างเดียว ไม่ออกไปกับโพสต์/รายงาน
    ("buyer", N_("ผู้ซื้อ / ช่องทาง"), "text", N_("เช่น คุณเอ · FB Marketplace · Kerry TH123...")),
    ("sold_price", N_("ราคาที่ขายได้จริง (บาท)"), "number", N_("ถ้าต่างจากราคาตั้ง")),
    ("cost", N_("ต้นทุน (บาท)"), "number", N_("ราคาที่ซื้อมา")),
    ("sold_at", N_("วันที่ขาย (เริ่มนับประกัน)"), "date", ""),
]
# สถานะ: (ชื่อ, ไอคอน, ระดับสี, กลุ่ม, คำอธิบาย)
#   กลุ่ม: prep = ยังไม่ได้ลงขาย · sell = ขายอยู่ · hold = จอง · done = ปิดการขาย · off = ไม่ได้ขาย
SALE_STATUSES = [
    (N_("เตรียมขาย"), "🧰", "info", "prep", N_("ตรวจแล้ว ยังไม่ได้ถ่ายรูป/ตั้งราคา")),
    (N_("พร้อมขาย"), "✅", "ok", "sell", N_("ข้อมูลครบ พร้อมลงประกาศ")),
    (N_("กำลังขาย"), "📣", "ok", "sell", N_("ลงประกาศแล้ว รอคนซื้อ")),
    (N_("จองแล้ว"), "🔖", "warn", "hold", N_("มีคนจอง รอโอน/นัดรับ")),
    (N_("ขายแล้ว"), "💰", "crit", "done", N_("ได้เงินแล้ว รอส่งของ")),
    (N_("ส่งแล้ว"), "📦", "crit", "done", N_("ส่งของ/ส่งมอบแล้ว ปิดการขาย")),
    (N_("เคลม/ตีกลับ"), "↩️", "warn", "off", N_("ลูกค้าส่งคืน รอตรวจใหม่")),
    (N_("เก็บไว้ใช้"), "🏠", "info", "off", N_("ไม่ขาย เก็บไว้ใช้เอง")),
    (N_("ไม่ขาย"), "⛔", "info", "off", N_("เสีย/ไม่ผ่าน/ไม่นำมาขาย")),
]
SALE_STATUS = [s[0] for s in SALE_STATUSES]
STATUS_META = {s[0]: {"icon": s[1], "level": s[2], "group": s[3], "desc": s[4]} for s in SALE_STATUSES}
STATUS_DEFAULT = "พร้อมขาย"
PRIVATE_SALE = ("buyer", "sold_price", "cost", "sold_at", "status_at", "status_log", "claims")  # ไม่แสดงในโพสต์/รายงาน/ZIP


def status_meta(st):
    return STATUS_META.get(st) or {"icon": "•", "level": "info", "group": "off", "desc": ""}


def is_selling(st):
    """ยังเปิดขายอยู่ (ลงแคตตาล็อก/ตอบลูกค้าว่ายังอยู่)"""
    return status_meta(st)["group"] in ("prep", "sell")


def status_tag(st):
    """ป้ายสถานะในโพสต์ — แสดงเฉพาะสถานะที่ลูกค้าควรรู้"""
    g = status_meta(st)["group"]
    return {"hold": "จองแล้ว", "done": "ขายแล้ว"}.get(g, "")  # ค่าไทย (ใช้เทียบในโค้ด) — แสดงผลด้วย _(tag)


def sale_info(row, cfg, private=False):
    """ข้อมูลการขายของดิสก์ — ช่องที่ว่างใช้ค่าเริ่มต้นจาก config.json (sale_defaults)
    private=False ตัดข้อมูลส่วนตัว (ผู้ซื้อ ราคาที่ขายได้ ประวัติสถานะ) ออก — ใช้กับทุกอย่างที่ส่งให้ลูกค้า"""
    own = json.loads((row["sale_json"] if "sale_json" in row.keys() else None) or "{}")
    merged = {k: v for k, v in (cfg.get("sale_defaults") or {}).items() if v and k not in PRIVATE_SALE}
    merged.update({k: v for k, v in own.items() if v not in (None, "") and str(v).strip()})
    merged.setdefault("status", STATUS_DEFAULT)
    if not private:
        for k in PRIVATE_SALE:
            merged.pop(k, None)
    return merged


def fmt_price(v):
    try:
        return _("{v:,.0f} บาท", v=float(str(v).replace(",", "")))
    except ValueError:
        return str(v)


def fb_post_text(d, sale, cfg, no):
    """ข้อความโพสต์ Facebook จากผลตรวจละเอียด + ข้อมูลการขาย"""
    s = d.get("smart", {})
    attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
    cap = human(d["size"]).replace(".0 ", " ")
    brand = d.get("brand") if d.get("brand") not in (None, "ไม่ทราบ") else ""
    title = " ".join([product_name(brand, d.get("series"), cap, d.get("external")),
                      ext_kind(d["kind"]) if d.get("external") else d["kind"]])
    tag = _(status_tag(sale.get("status")))
    L = [f"🔥 {title}" + (f"  【{tag}】" if tag else "")]
    if sale.get("price"):
        L.append("💰 " + _("ราคา {price}", price=fmt_price(sale["price"])))
    L += ["", "✅ " + _("ตรวจสุขภาพละเอียดแล้ว — เกรด {g} ({label})", g=d["grade"][0], label=_(d["grade"][1]))]
    if s.get("passed") is not None:
        L.append("• " + _("SMART ประเมินรวม: {v}", v=_("ผ่าน") if s["passed"] else _("ไม่ผ่าน")))
    if s.get("hours") is not None:
        L.append("• " + _("ชั่วโมงใช้งาน {h:,} ชม. (~{y:.1f} ปี)", h=s["hours"], y=s["hours"] / 8760))
    if s.get("wear") is not None:
        L.append("• " + _("อายุ SSD คงเหลือ {p}%", p=100 - s["wear"]))
    if attr:
        L.append("• " + _("เซกเตอร์เสีย {bad} · รอย้าย {pending} · อ่านไม่ได้ {unc}",
                          bad=attr.get(5, 0), pending=attr.get(197, 0), unc=attr.get(198, 0)))
    if s.get("selftest"):
        L.append("• Self-test: " + (_("ผ่าน") if s["selftest"]["passed"] else _("ไม่ผ่าน")))
    sp = [r["mbps"] for r in d.get("speed") or [] if r["mbps"]]
    if sp:
        L.append("• " + _("ความเร็วอ่าน {lo:,.0f}–{hi:,.0f} MB/s", lo=min(sp), hi=max(sp)))
    if d.get("scan"):
        sc = d["scan"]
        mode = _("สุ่ม 2,000 จุด") if sc["mode"] == "quick" else _("เต็มทั้งจาน")
        L.append("• " + (_("สแกนผิวจาน ({mode}): พบจุดอ่านไม่ได้ {n}", mode=mode, n=sc["bad"]) if sc["bad"]
                         else _("สแกนผิวจาน ({mode}): ไม่พบจุดเสีย", mode=mode)))
    if s.get("temp") is not None:
        L.append("• " + _("อุณหภูมิขณะตรวจ {t}°C", t=s["temp"]))
    icons = {"condition": "📦 " + _("สภาพ"), "warranty": "🛡 " + _("ประกัน"), "includes": "🎁 " + _("ได้รับ"),
             "shipping": "🚚 " + _("จัดส่ง"), "payment": "💳 " + _("ชำระเงิน"), "contact": "📞 " + _("ติดต่อ")}
    extra = [f"{lbl}: {sale[k]}" for k, lbl in icons.items() if sale.get(k)]
    if extra:
        L += [""] + extra
    if sale.get("details"):
        L += ["", f"📝 {sale['details']}"]
    spec = " · ".join(filter(None, [d["model"], form_extra(d, s), f"{s['rpm']:,} RPM" if s.get("rpm") else None]))
    L += ["", ("📦 " + _("{ext} · ข้างในเป็น {spec}", ext=d["external"], spec=spec) if d.get("external")
               else _("รุ่น {spec}", spec=spec)),
          _("S/N {serial} · รหัสสินค้า #{no}", serial=d["serial"], no=no)]
    tags = [f"#{t}" for t in (brand, d.get("series", "").split(" (")[0] if d.get("series") else "", cap.replace(" ", ""))
            if t]
    tags = " ".join(dict.fromkeys(t.replace(" ", "") for t in tags))
    L += ["", " ".join(filter(None, [tags, cfg.get("fb_hashtags", "")]))]
    return tidy("\n".join(L))


def _facts(d, sale, cfg):
    """ข้อมูลที่ใช้ร่วมกันในข้อความทุกแบบ"""
    s = d.get("smart", {})
    attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
    cap = human(d["size"]).replace(".0 ", " ")
    brand = d.get("brand") if d.get("brand") not in (None, "ไม่ทราบ") else ""
    series = (d.get("series") or "").split(" (")[0]
    sp = [r["mbps"] for r in d.get("speed") or [] if r["mbps"]]
    bad = (attr.get(5) or 0) + (attr.get(197) or 0) + (attr.get(198) or 0) if attr else None
    tags = " ".join(dict.fromkeys(f"#{t.replace(' ', '')}" for t in (brand, d.get("external"), series, cap) if t))
    return {
        "s": s, "attr": attr, "cap": cap, "brand": brand, "series": series,
        "name": product_name(brand, series, cap, d.get("external")),
        "kind": ext_kind(d["kind"]) if d.get("external") else d["kind"], "external": d.get("external"), "grade": d["grade"][0], "glabel": _(d["grade"][1]),
        "hours": s.get("hours"), "years": (s["hours"] / 8760) if s.get("hours") is not None else None,
        "smart": {True: _("ผ่าน"), False: _("ไม่ผ่าน")}.get(s.get("passed")),
        "bad": bad, "wear": s.get("wear"), "speed": (min(sp), max(sp)) if sp else None,
        "scan_ok": (not d["scan"]["bad"]) if d.get("scan") else None,
        "selftest": (_("ผ่าน") if s["selftest"]["passed"] else _("ไม่ผ่าน")) if s.get("selftest") else None,
        "price": fmt_price(sale["price"]) if sale.get("price") else None,
        "status": sale.get("status", "พร้อมขาย"),  # ค่าไทย ใช้เทียบ
        "tags": " ".join(filter(None, [tags, cfg.get("fb_hashtags", "")])),
        "shop": cfg.get("shop") or "",
    }


def _health_line(f):
    """สรุปสุขภาพบรรทัดเดียว"""
    parts = [_("เกรด {g} ({label})", g=f["grade"], label=f["glabel"])]
    if f["smart"]:
        parts.append(f"SMART {f['smart']}")
    if f["hours"] is not None:
        parts.append(_("ใช้งาน {h:,} ชม.", h=f["hours"]))
    if f["wear"] is not None:
        parts.append(_("อายุคงเหลือ {p}%", p=100 - f["wear"]))
    if f["bad"] is not None:
        parts.append(_("ไม่มีเซกเตอร์เสีย") if f["bad"] == 0 else _("เซกเตอร์เสีย {n}", n=f["bad"]))
    return " · ".join(parts)


def post_variants(d, sale, cfg, no):
    """ข้อความสำเร็จรูปทุกแบบ — {key: (ชื่อ, ข้อความ)}"""
    f = _facts(d, sale, cfg)
    v = {}
    v["facebook"] = (_("โพสต์ Facebook (เต็ม)"), fb_post_text(d, sale, cfg, no))

    # Marketplace: หัวข้อสั้น + รายละเอียดไม่มีแฮชแท็ก
    title = _("{name} {kind} เกรด {g}", name=f["name"], kind=f["kind"], g=f["grade"])
    if f["hours"] is not None:
        title += " · " + _("{h:,} ชม.", h=f["hours"])
    v["mp_title"] = (_("Marketplace · หัวข้อสินค้า"), title[:99] + "\n")
    L = [_("{name} ({model}) ตรวจสุขภาพละเอียดแล้ว", name=f["name"], model=d["model"]), "",
         _("ผลตรวจ: {health}", health=_health_line(f))]
    if f["speed"]:
        L.append(_("ความเร็วอ่าน {lo:,.0f}–{hi:,.0f} MB/s", lo=f["speed"][0], hi=f["speed"][1]))
    if f["scan_ok"] is not None:
        L.append(_("สแกนผิวจาน: ไม่พบจุดเสีย") if f["scan_ok"] else _("สแกนผิวจาน: พบจุดอ่านไม่ได้"))
    if f["selftest"]:
        L.append(f"Self-test: {f['selftest']}")
    L.append("")
    for k, lbl in (("condition", _("สภาพ")), ("warranty", _("ประกัน")), ("includes", _("ได้รับ")), ("shipping", _("จัดส่ง")),
                   ("payment", _("ชำระเงิน")), ("contact", _("ติดต่อ"))):
        if sale.get(k):
            L.append(f"{lbl}: {sale[k]}")
    if sale.get("details"):
        L += ["", sale["details"]]
    L += ["", _("S/N {serial} · รหัส #{no}", serial=d["serial"], no=no) + (f" · {f['shop']}" if f["shop"] else "")]
    v["mp_desc"] = (_("Marketplace · รายละเอียด"), "\n".join(L).strip() + "\n")
    if f["price"]:
        v["mp_price"] = (_("Marketplace · ราคา (ตัวเลข)"), re.sub(r"[^0-9]", "", str(sale["price"]).split(".")[0]) + "\n")

    # โพสต์สั้น สำหรับกลุ่ม/คอมเมนต์
    tag = status_tag(f["status"])
    S = [f"🔥 {f['name']} {f['kind']}" + (f" · 💰 {f['price']}" if f["price"] else "")
         + (f" 【{_(tag)}】" if tag else ""),
         f"✅ {_health_line(f)}"]
    tail = " · ".join(filter(None, [f"🛡 {sale['warranty']}" if sale.get("warranty") else "",
                                    f"🚚 {sale['shipping']}" if sale.get("shipping") else ""]))
    if tail:
        S.append(tail)
    S.append("📩 " + _("สนใจทักแชท · รหัส #{no}", no=no))
    v["short"] = (_("โพสต์สั้น (กลุ่ม/คอมเมนต์)"), "\n".join(S) + "\n")

    # ตอบแชทลูกค้า
    if tag == "ขายแล้ว":
        R = [_("สวัสดีครับ ขอบคุณที่สนใจครับ 🙏 ตัว {name} (#{no}) ขายไปแล้วครับ", name=f["name"], no=no),
             _("ถ้าสนใจรุ่นอื่น บอกความจุที่ต้องการได้เลยครับ")]
    elif tag == "จองแล้ว":
        R = [_("สวัสดีครับ ตัว {name} (#{no}) ตอนนี้ติดจองอยู่ครับ", name=f["name"], no=no),
             _("ถ้าผู้จองยกเลิก จะรีบแจ้งกลับนะครับ 🙏")]
    else:
        R = [(_("สวัสดีครับ ตัว {name} (#{no}) ยังอยู่ครับ 😊 ราคา {price}", name=f["name"], no=no, price=f["price"])
              if f["price"] else _("สวัสดีครับ ตัว {name} (#{no}) ยังอยู่ครับ 😊", name=f["name"], no=no)),
             "", _("ผลตรวจสุขภาพ: {health}", health=_health_line(f))]
        if f["speed"]:
            R.append(_("ความเร็วอ่าน {lo:,.0f}–{hi:,.0f} MB/s", lo=f["speed"][0], hi=f["speed"][1]) +
                     (" · " + _("สแกนผิวจานไม่พบจุดเสีย") if f["scan_ok"] else ""))
        for k, lbl in (("condition", _("สภาพ")), ("warranty", _("ประกัน")), ("shipping", _("จัดส่ง")), ("payment", _("ชำระเงิน"))):
            if sale.get(k):
                R.append(f"{lbl}: {sale[k]}")
        R += ["", _("มีรายงานผลตรวจเต็มและรูปจริง ส่งให้ดูได้ครับ สนใจรับเลยไหมครับ")]
    v["reply"] = (_("ตอบแชทลูกค้า"), "\n".join(R) + "\n")

    # อัปเดตสถานะ (คอมเมนต์ใต้โพสต์/แก้โพสต์)
    v["status"] = (_("อัปเดตสถานะ"), "\n".join([
        _("【จองแล้ว】 {name} (#{no}) ติดจองแล้วครับ ขอบคุณครับ 🙏", name=f["name"], no=no),
        _("【ขายแล้ว】 {name} (#{no}) ขายแล้วครับ ขอบคุณลูกค้ามากครับ 🙏", name=f["name"], no=no),
        (_("【กลับมาว่าง】 {name} (#{no}) กลับมาพร้อมขายอีกครั้งครับ ราคา {price}", name=f["name"], no=no, price=f["price"])
         if f["price"] else _("【กลับมาว่าง】 {name} (#{no}) กลับมาพร้อมขายอีกครั้งครับ", name=f["name"], no=no)),
    ]) + "\n")
    v["tags"] = (_("แฮชแท็ก"), (f["tags"] or "-") + "\n")
    return {k: (t, tidy(x)) for k, (t, x) in v.items()}


def tidy(text):
    """ตัดบรรทัดว่างซ้อน/ช่องว่างท้ายบรรทัด"""
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def posts_txt(variants):
    """รวมข้อความทุกแบบไว้ในไฟล์เดียว"""
    return "\n".join(f"===== {title} =====\n{text}" for title, text in variants.values())


def catalog_text(con, cfg):
    """ข้อความรวมสินค้าทุกลูกที่พร้อมขาย — โพสต์รวมในกลุ่มได้"""
    rows = con.execute("SELECT * FROM disks ORDER BY no").fetchall()
    items = []
    for r in rows:
        sale = sale_info(r, cfg)
        if status_meta(sale.get("status"))["group"] not in ("prep", "sell", "hold"):
            continue
        last = con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id DESC LIMIT 1", (r["no"],)).fetchone()
        cap = human(r["capacity_bytes"]).replace(".0 ", " ")
        name = product_name(r["brand"], r["series"], cap, r["external"]) + " " + (ext_kind(r["type"]) if r["external"] else r["type"] or "")
        info = [_("เกรด {g}", g=r["last_grade"])]
        if last and last["power_on_hours"] is not None:
            info.append(_("{h:,} ชม.", h=last["power_on_hours"]))
        if last and last["reallocated"] is not None:
            bad = (last["reallocated"] or 0) + (last["pending"] or 0)
            info.append(_("ไม่มีเซกเตอร์เสีย") if not bad else _("เซกเตอร์เสีย {n}", n=bad))
        price = f" — {fmt_price(sale['price'])}" if sale.get("price") else ""
        mark = f" 【{_('จองแล้ว')}】" if status_tag(sale.get("status")) == "จองแล้ว" else ""
        items.append(f"#{r['no']} {name}{price}{mark}\n    {' · '.join(info)}")
    shop = cfg.get("shop") or ""
    head = ["📦 " + _("รายการฮาร์ดดิสก์พร้อมขาย{shop} ({n} ลูก)", shop=(" · " + shop) if shop else "", n=len(items)),
            _("ทุกลูกตรวจสุขภาพละเอียดแล้ว (SMART + สแกนผิวจาน + วัดความเร็ว) มีรายงานผลตรวจให้ดู"), ""]
    sd = cfg.get("sale_defaults") or {}
    foot = [""] + [f"{lbl}: {sd[k]}" for k, lbl in (("warranty", "🛡 " + _("ประกัน")), ("shipping", "🚚 " + _("จัดส่ง")),
                                                     ("payment", "💳 " + _("ชำระเงิน")), ("contact", "📞 " + _("ติดต่อ")))
                   if sd.get(k)]
    foot += ["", "📩 " + _("สนใจลูกไหน แจ้งรหัส # ทางแชทได้เลยครับ"), cfg.get("fb_hashtags", "")]
    return tidy("\n".join(head + (items or [_("(ยังไม่มีสินค้าพร้อมขาย)")]) + foot))


def num(v):
    """ตัวเลขมีคอมมา / '-' ถ้าไม่มีค่า"""
    return "-" if v is None else f"{round(v):,}"


def warranty_days(text):
    """'รับประกันร้าน 30 วัน' → 30 · '3 เดือน' → 90 · '1 ปี' → 365 · 'ไม่มีประกัน' → 0 · อ่านไม่ออก → None"""
    t = str(text or "").lower().strip()
    if not t:
        return None
    if re.search(r"ไม่มีประกัน|ไม่รับประกัน|no warranty", t):
        return 0
    m = re.search(r"(\d+)\s*(วัน|day|d\b|สัปดาห์|อาทิตย์|week|w\b|เดือน|month|m\b|ปี|year|y\b)", t)
    if not m:
        return None
    unit = m.group(2)
    mult = 7 if unit in ("สัปดาห์", "อาทิตย์", "week", "w") else 30 if unit in ("เดือน", "month", "m") \
        else 365 if unit in ("ปี", "year", "y") else 1
    return int(m.group(1)) * mult


def warranty_info(sale, today=None):
    """สถานะประกันจากวันที่ขาย + ข้อความประกัน → {days, sold, until, left, active}"""
    import datetime as dt
    days = warranty_days(sale.get("warranty"))
    sold = (sale.get("sold_at") or "")[:10]
    info = {"days": days, "sold": sold or None, "until": None, "left": None, "active": None}
    if days is None or not sold:
        return info
    try:
        until = dt.date.fromisoformat(sold) + dt.timedelta(days=days)
    except ValueError:
        return info
    left = (until - (today or dt.date.today())).days
    info.update(until=until.isoformat(), left=left, active=left >= 0)
    return info


def is_sold(sale):
    """นับเป็นยอดขาย: ขายแล้ว/ส่งแล้ว หรือกำลังเคลมของที่ขายไปแล้ว (คืนเงินแล้วให้เปลี่ยนสถานะเป็นอย่างอื่น)"""
    st = sale.get("status")
    return status_meta(st)["group"] == "done" or (st == "เคลม/ตีกลับ" and bool(sale.get("sold_at")))


def profit_of(sale):
    """กำไร = ราคาที่ขายได้ (หรือราคาตั้ง) − ทุน · ไม่มีทุน → None"""
    cost = num_or(sale.get("cost"))
    got = num_or(sale.get("sold_price")) or num_or(sale.get("price"))
    return None if cost is None or got is None else got - cost


def num_or(v):
    """แปลงข้อความเป็นตัวเลข (ตัดคอมมา/คำว่าบาท) — ไม่ได้คืน None"""
    try:
        return float(re.sub(r"[^0-9.]", "", str(v))) if v not in (None, "") else None
    except ValueError:
        return None


def safe_name(t):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(t)).strip("-") or "unknown"


def disk_folder(no, serial):
    return DISKS_DIR / f"{no:04d}_{safe_name(serial)}"


def find_disk(con, key):
    """ค้นจากเลขประจำดิสก์ (เช่น 1) หรือ Serial Number"""
    key = str(key).strip()
    row = None
    if key.lstrip("#").isdigit():
        row = con.execute("SELECT * FROM disks WHERE no=?", (int(key.lstrip("#")),)).fetchone()
    return row or con.execute("SELECT * FROM disks WHERE serial=? COLLATE NOCASE", (key,)).fetchone()


def record_test(con, d, report):
    """บันทึกผลตรวจลงฐานข้อมูล คืน (เลขดิสก์, เลขการตรวจ)"""
    s = d.get("smart", {})
    attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
    sp = [r["mbps"] for r in d.get("speed", []) if r["mbps"]]
    sc = d.get("scan") or {}
    g = d["grade"]
    con.execute("""INSERT INTO disks(serial, brand, series, model, capacity_bytes, type, form_factor, rpm, firmware,
                     first_seen, last_tested, last_grade, last_grade_label, tests_count, external)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(serial) DO UPDATE SET brand=excluded.brand, series=excluded.series,
                     model=excluded.model, capacity_bytes=excluded.capacity_bytes, type=excluded.type,
                     form_factor=excluded.form_factor, rpm=excluded.rpm, firmware=excluded.firmware,
                     last_tested=excluded.last_tested, last_grade=excluded.last_grade,
                     last_grade_label=excluded.last_grade_label, tests_count=tests_count+1,
                     external=excluded.external""",
                (d["serial"], d.get("brand"), d.get("series"), d["model"], d["size"], d["kind"], s.get("form"),
                 s.get("rpm"), d["fw"], report["time"], report["time"], g[0], g[1], d.get("external")))
    no = con.execute("SELECT no FROM disks WHERE serial=?", (d["serial"],)).fetchone()["no"]
    detail = {"report": {k: report[k] for k in ("time", "level") if k in report}, "tool": f"diskcheck v{VERSION}",
              "disk": {k: v for k, v in d.items() if k not in ("partitions", "system", "dev", "bridge", "tran", "usb", "io")}}
    cur = con.execute("""INSERT INTO tests(disk_no, serial, tested_at, level, grade, grade_label, smart_passed,
                           power_on_hours, temperature_c, reallocated, pending, offline_unc, crc, read_mbps_avg,
                           scan_mode, scan_unreadable, scan_slow, detail_json)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (no, d["serial"], report["time"], report["level"], g[0], g[1],
                       None if s.get("passed") is None else int(bool(s.get("passed"))), s.get("hours"),
                       s.get("temp") if s.get("temp") is not None else d.get("sensor_temp"),
                       attr.get(5), attr.get(197), attr.get(198), attr.get(199),
                       round(sum(sp) / len(sp), 1) if sp else None, sc.get("mode"), sc.get("bad"), sc.get("slow"),
                       json.dumps(detail, ensure_ascii=False, default=str)))
    con.commit()
    return no, cur.lastrowid


def organize_photos(folder, serial):
    """ตั้งชื่อรูปเป็น SERIAL_1.jpg, SERIAL_2.jpg, ... (รูปที่ตั้งชื่อถูกแล้วไม่แตะ)"""
    pdir = folder / "photos"
    pdir.mkdir(parents=True, exist_ok=True)
    pat = re.compile(rf"^{re.escape(safe_name(serial))}_(\d+)$", re.I)
    named, others = {}, []
    for f in pdir.iterdir():
        if not f.is_file() or f.suffix.lower() not in PHOTO_EXT:
            continue
        m = pat.match(f.stem)
        if m and int(m.group(1)) not in named:
            named[int(m.group(1))] = f
        else:
            others.append(f)
    n = max(named, default=0)
    for f in sorted(others, key=lambda f: (f.stat().st_mtime, f.name)):
        n += 1
        target = pdir / f"{safe_name(serial)}_{n}{f.suffix.lower()}"
        f.rename(target)
        named[n] = target
        print(DIM("     " + _("ตั้งชื่อรูป: {old} → {new}", old=f.name, new=target.name)))
    chown_user(pdir, *named.values())
    return [named[k] for k in sorted(named)]


def photo_gallery(photos):
    if not photos:
        return ""
    items, total = [], 0
    for p in photos:
        mime = PHOTO_EXT.get(p.suffix.lower())
        if not mime:
            items.append(f'<figure class="ph"><div class="nop">{E(p.suffix.upper())}<br>{_("แสดงในเบราว์เซอร์ไม่ได้")}</div>'
                         f'<figcaption>{E(p.name)}</figcaption></figure>')
            continue
        raw = p.read_bytes()
        total += len(raw)
        items.append(f'<figure class="ph"><a href="data:{mime};base64,{base64.b64encode(raw).decode()}" target="_blank">'
                     f'<img src="data:{mime};base64,{base64.b64encode(raw).decode()}" alt="{E(p.name)}" loading="lazy"></a>'
                     f'<figcaption>{E(p.name)}</figcaption></figure>')
    if total > 25e6:
        print(WARN("     " + _("รูปรวม {size} — ไฟล์ report.html จะใหญ่ ลองย่อรูปก่อนใส่", size=human(total))))
    return f'<h2>{_("รูปดิสก์ ({n})", n=len(photos))}</h2><div class="card"><div class="gallery">{"".join(items)}</div></div>'


GALLERY_CSS = """
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.ph{margin:0;background:var(--card2);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.ph img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;cursor:zoom-in}
.ph figcaption{padding:8px 12px;font-size:12.5px;color:var(--mut);font-family:ui-monospace,monospace}
.nop{aspect-ratio:4/3;display:grid;place-items:center;text-align:center;color:var(--mut)}
.no{display:inline-block;padding:4px 14px;border-radius:99px;background:#ffffff1f;font-weight:800;font-size:15px;margin-bottom:8px}
"""


def package_report_html(d, report, cfg, no, test_id, photos, history, note):
    shop = E(cfg.get("shop") or "")
    glvl = {"A": "ok", "B": "ok", "C": "warn", "F": "crit"}
    hist = "".join(
        f'<tr><td class="num">#{h["id"]}</td><td>{E(h["tested_at"])}</td><td>{E(_(h["level"]))}</td>'
        f'<td>{pill(glvl.get(h["grade"], "info"), h["grade"] + " " + _(h["grade_label"] or ""))}</td>'
        f'<td class="num">{num(h["power_on_hours"])}</td><td class="num">{num(h["reallocated"])}</td>'
        f'<td class="num">{num(h["pending"])}</td><td class="num">{num(h["read_mbps_avg"])}</td></tr>'
        for h in history)
    hist_box = (f'<h2>{_("ประวัติการตรวจ ({n} ครั้ง)", n=len(history))}</h2><div class="card" style="overflow-x:auto"><table>'
                f'<tr><th>{_("เลขที่")}</th><th>{_("วันที่")}</th><th>{_("ระดับ")}</th><th>{_("เกรด")}</th><th>{_("ชั่วโมงใช้งาน")}</th>'
                f'<th>{_("เซกเตอร์เสีย")}</th><th>{_("รอย้าย")}</th><th>{_("อ่านเฉลี่ย MB/s")}</th></tr>{hist}</table></div>') if history else ""
    note_box = f'<div class="card"><b>{_("บันทึก:")}</b> {E(note)}</div>' if note else ""
    return f"""<!doctype html><html lang="{get_lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>#{no} {E(d['model'])} · Disk Report</title><style>{CSS}{GALLERY_CSS}</style></head><body><div class="wrap">
<div class="hero"><div><span class="no">#{no}</span><h1>🩺 {_('รายงานตรวจสุขภาพดิสก์')}</h1>
<p>{E(_(d.get('brand', '')))} {E(d['model'])} · {human(d['size'])} · S/N {E(d['serial'])}</p></div>
<div class="chips"><span class="chip">{_('ตรวจครั้งที่ #{id}', id=test_id)}</span><span class="chip">{E(report['time'])}</span>
<span class="chip">{_('ระดับ: {level}', level=E(_(report['level'])))}</span>{f'<span class="chip">{shop}</span>' if shop else ''}</div></div>
<div style="height:18px"></div>
<div class="legend"><span>{pill('ok', 'A')} {_('ดีมาก')}</span><span>{pill('ok', 'B')} {_('ดี')}</span>
<span>{pill('warn', 'C')} {_('พอใช้ มีข้อสังเกต')}</span><span>{pill('crit', 'F')} {_('มีปัญหา')}</span></div>
<div style="height:12px"></div>
{note_box}
{sale_box(d.get("sale"))}
{disk_html(d, public=True)}
{photo_gallery(photos)}
{hist_box}
<div class="foot">{_('ดิสก์ #{no} · ตรวจด้วย diskcheck v{v}', no=no, v=VERSION)}{' · ' + shop if shop else ''} · {_('การทดสอบทั้งหมดเป็นแบบอ่านอย่างเดียว ไม่แก้ไขข้อมูลในดิสก์')}</div>
</div></body></html>"""


def sale_box(sale):
    if not sale or not any(sale.get(k) for k, *_rest in SALE_FIELDS if k != "status"):
        return ""
    tag = status_tag(sale.get("status")) or ("พร้อมขาย" if is_selling(sale.get("status")) else "")  # ค่าไทย ใช้เทียบ
    lvl = {"จองแล้ว": "warn", "ขายแล้ว": "crit", "พร้อมขาย": "ok"}.get(tag, "info")
    rows = "".join(f'<tr><td class="mut" style="width:34%">{E(_(lbl))}</td><td>{E(str(sale[k]))}</td></tr>'
                   for k, lbl, _kind, _ex in SALE_FIELDS if k not in ("status", "price") and sale.get(k))
    price = f'<div style="font-size:30px;font-weight:800">{E(fmt_price(sale["price"]))}</div>' if sale.get("price") else ""
    return (f'<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;'
            f'flex-wrap:wrap"><div><div class="mut">{_("ข้อมูลสินค้า")}</div>{price}</div>{pill(lvl, _(tag)) if tag else ""}</div>'
            f'<table style="margin-top:10px">{rows}</table></div>')


AI_README = ("Hard disk / SSD health check results from the diskcheck tool. disk_no is the unique disk number; serial is the "
             "primary key. grade: A = Excellent (no issues, < 20,000 power-on hours), B = Good (no issues, well used), "
             "C = Fair (has warnings), F = Faulty (SMART failed / reallocated sectors >= 10 / pending or uncorrectable sectors / "
             "unreadable spots found in the surface scan), ? = Not enough data. "
             "reallocated/pending/offline_uncorrectable/reported_uncorrectable should be 0; crc_errors usually come from the "
             "cable or USB enclosure, not the disk. Capacity uses base-10 units (1 TB = 10^12 bytes), speeds are MB/s, "
             "temperatures are °C. All tests are read-only. Text values such as grade_label, level, findings[].key and "
             "sale fields are stored in Thai (the source language); findings[].msg and post_texts are in the language "
             "the package was built in.")


def ai_json(row, d, report, test_id, history, photos):
    data = disk_data(d, report)
    data["smart"]["all_attributes"] = d.get("smart", {}).get("all_attrs")
    return {
        "_about": AI_README,
        "disk_no": row["no"], "serial": row["serial"], "note": row["note"] or None,
        "first_seen": row["first_seen"], "last_tested": row["last_tested"], "tests_count": row["tests_count"],
        "latest_test": {"test_id": test_id, **data},
        "test_history": [{k: h[k] for k in ("id", "tested_at", "level", "grade", "grade_label", "smart_passed",
                                            "power_on_hours", "temperature_c", "reallocated", "pending",
                                            "offline_unc", "crc", "read_mbps_avg", "scan_mode", "scan_unreadable",
                                            "scan_slow")} for h in history],
        "sale": d.get("sale"),
        "post_texts": {k: {"title": t, "text": x} for k, (t, x) in post_variants(d, d.get("sale") or {}, load_config(),
                                                                                 row["no"]).items()},
        "photos": [p.name for p in photos],
        "files": {"report": "report.html", "card_image": "card.png", "listing_text": "listing.txt",
                  "facebook_post": "facebook.txt", "all_post_texts": "posts.txt",
                  "photos_folder": "photos/", "per_test_snapshots": "tests/"},
    }


def output_lang(cfg):
    """ภาษาของไฟล์ที่สร้าง (รายงาน การ์ด โพสต์) — config: output_lang › lang › th"""
    for k in ("output_lang", "lang"):
        if (cfg or {}).get(k) in LANGS:
            return cfg[k]
    return "th"


def in_output_lang(fn):
    """ให้ฟังก์ชันที่สร้างไฟล์ทำงานในภาษาของไฟล์ (output_lang) / run in the output language"""
    import functools

    @functools.wraps(fn)
    def wrap(*args, **kw):
        cfg = kw.get("cfg") or next((a for a in args if isinstance(a, dict)), None)
        with using(output_lang(cfg)):
            return fn(*args, **kw)
    return wrap


@in_output_lang
def build_package(con, key, cfg, render_card=True):
    """สร้าง/อัปเดตโฟลเดอร์ของดิสก์จากผลตรวจล่าสุดในฐานข้อมูล (ไม่ต้องตรวจใหม่)"""
    row = find_disk(con, key)
    if not row:
        print(BAD("  " + _("ไม่พบดิสก์ {key} ในฐานข้อมูล", key=key)))
        return None
    history = con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id", (row["no"],)).fetchall()
    last = history[-1]
    detail = json.loads(last["detail_json"])
    d, report = detail["disk"], detail["report"]
    d["disk_no"] = row["no"]
    d["external"] = row["external"] if "external" in row.keys() else d.get("external")  # แก้เองได้จากเว็บ
    d["sale"] = sale_info(row, cfg)
    folder = disk_folder(row["no"], row["serial"])
    (folder / "tests").mkdir(parents=True, exist_ok=True)
    DISKS_DIR.mkdir(exist_ok=True)
    photos = organize_photos(folder, row["serial"])
    files = {
        "report.html": package_report_html(d, report, cfg, row["no"], last["id"], photos, history, row["note"]),
        "data.json": json.dumps(ai_json(row, d, report, last["id"], history, photos), ensure_ascii=False, indent=2,
                                default=str),
        "listing.txt": listing_text(d, report, cfg),
        "card.html": card_html(d, report, cfg),
        "facebook.txt": fb_post_text(d, d["sale"], cfg, row["no"]),
        "posts.txt": posts_txt(post_variants(d, d["sale"], cfg, row["no"])),
    }
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8")
    # สำเนาผลตรวจแต่ละครั้ง (ไม่เขียนทับของเดิม)
    snap = folder / "tests" / f"test-{last['id']:04d}_{last['tested_at'][:10]}.json"
    if not snap.exists():
        snap.write_text(json.dumps(detail, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    chown_user(DISKS_DIR, folder, folder / "tests", snap, *(folder / n for n in files))
    png = False
    if render_card:
        (folder / "card.png").unlink(missing_ok=True)
        png = render_png(folder / "card.html", folder / "card.png")
        if png:
            chown_user(folder / "card.png")
    return folder, png, row["no"], len(photos)


@in_output_lang
def update_index(con, cfg):
    """สร้างหน้ารวม disks/index.html (เปิดดูได้โดยไม่ต้องรันเว็บ)"""
    try:
        import webapp
        webapp.write_static_index(con, cfg)
    except Exception as e:  # หน้ารวมพังไม่ควรทำให้การตรวจล้ม
        print(WARN("  " + _("สร้างหน้ารวมไม่สำเร็จ: {e}", e=e)))


# ---------- คำสั่งจัดการฐานข้อมูล ----------
def cmd_list(con):
    rows = con.execute("""SELECT d.*, (SELECT COUNT(*) FROM tests t WHERE t.disk_no=d.no) n FROM disks d ORDER BY no""").fetchall()
    header(_("ดิสก์ในฐานข้อมูล ({n} ลูก)", n=len(rows)))
    if not rows:
        print(DIM("  " + _("ยังไม่มี — ตรวจดิสก์ด้วย sudo ./run.sh แล้วจะถูกบันทึกอัตโนมัติ")))
        return
    print(DIM(f"  {pad('#', 5)}{pad('Serial', 22)}{pad(_('ยี่ห้อ / รุ่น'), 34)}{pad(_('ความจุ'), 10)}{pad(_('เกรด'), 12)}"
              f"{pad(_('ตรวจล่าสุด'), 18)}{pad(_('รูป'), 7)}{_('หมายเหตุ')}"))
    for r in rows:
        g = r["last_grade"] or "?"
        gcol = {"A": OK, "B": OK, "C": WARN, "F": BAD}.get(g, CYAN)
        folder = disk_folder(r["no"], r["serial"]) / "photos"
        nph = sum(1 for f in folder.iterdir() if f.suffix.lower() in PHOTO_EXT) if folder.exists() else 0
        name = f"{_(r['brand'] or '')} {r['model'] or ''}".strip()[:32]
        print(f"  {BOLD(pad(r['no'], 5))}{pad(r['serial'], 22)}{pad(name, 34)}{pad(human(r['capacity_bytes']), 10)}"
              f"{gcol(pad(g + ' ' + _(r['last_grade_label'] or ''), 12))}{pad(r['last_tested'] or '-', 18)}"
              f"{pad(nph, 7)}{r['note'] or ''}")


def cmd_show(con, key):
    r = find_disk(con, key)
    if not r:
        print(BAD("  " + _("ไม่พบดิสก์ {key}", key=key)))
        return 2
    header(_("ดิสก์ #{no} · {name}", no=r["no"], name=f"{_(r['brand'] or '')} {r['model']}"))
    print(f"  {pad('Serial', 13)}{r['serial']}\n  {pad(_('ความจุ'), 13)}{human(r['capacity_bytes'])} · {r['type']}"
          f"\n  {pad(_('ตรวจครั้งแรก'), 13)}" + _("{first} · ล่าสุด {last}", first=r["first_seen"], last=r["last_tested"])
          + f"\n  {pad(_('หมายเหตุ'), 13)}{r['note'] or '-'}")
    print(f"  {pad(_('โฟลเดอร์'), 13)}{disk_folder(r['no'], r['serial'])}")
    print(DIM(f"\n  {pad(_('เลขที่'), 8)}{pad(_('วันที่'), 18)}{pad(_('เกรด'), 14)}{pad(_('ชม.ใช้งาน'), 11)}{pad(_('เสีย'), 6)}"
              f"{pad(_('รอย้าย'), 8)}{pad('MB/s', 6)}{_('ระดับ')}"))
    for t in con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id", (r["no"],)):
        print(f"  {pad('#' + str(t['id']), 8)}{pad(t['tested_at'], 18)}{pad((t['grade'] or '') + ' ' + _(t['grade_label'] or ''), 14)}"
              f"{pad(num(t['power_on_hours']), 11)}"
              f"{pad('-' if t['reallocated'] is None else t['reallocated'], 6)}{pad('-' if t['pending'] is None else t['pending'], 8)}"
              f"{pad('-' if t['read_mbps_avg'] is None else round(t['read_mbps_avg']), 6)}{_(t['level'])}")
    return 0


def cmd_note(con, key, text):
    r = find_disk(con, key)
    if not r:
        print(BAD("  " + _("ไม่พบดิสก์ {key}", key=key)))
        return 2
    con.execute("UPDATE disks SET note=? WHERE no=?", (text, r["no"]))
    con.commit()
    print(OK("  ✔ " + _("บันทึกของดิสก์ #{no}: {text}", no=r["no"], text=text)))
    build_package(con, r["no"], load_config(), render_card=False)
    return 0


def cmd_rebuild(con, key, cfg):
    keys = [r["no"] for r in con.execute("SELECT no FROM disks ORDER BY no")] if key == "all" else [key]
    for k in keys:
        res = build_package(con, k, cfg)
        if res:
            folder, png, no, nph = res
            print(OK(f"  ✔ #{no}") + f"  {folder}  " + DIM(_("รูป {n}", n=nph) + f" · card.png {'✔' if png else '✘'}"))
    return 0


def cmd_import(con, path, cfg):
    """นำผลตรวจเก่า (reports/diskcheck-*.json) เข้าฐานข้อมูล"""
    rep = json.loads(Path(path).read_text(encoding="utf-8"))
    for d in rep["disks"]:
        if not d.get("smart", {}).get("available") or d.get("serial") in (None, "", "-"):
            print(DIM("  " + _("ข้าม {dev} — ไม่มีค่า SMART หรือไม่มี S/N", dev=d.get("dev"))))
            continue
        if con.execute("SELECT 1 FROM tests WHERE serial=? AND tested_at=?", (d["serial"], rep["time"])).fetchone():
            print(DIM("  " + _("ข้าม {serial} — ผลตรวจเวลา {time} มีอยู่แล้ว", serial=d["serial"], time=rep["time"])))
            continue
        if not d.get("brand"):
            identify(d)
        no, tid = record_test(con, d, rep)
        folder, _png, _no, _nph = build_package(con, no, cfg)
        print(OK("  ✔ " + _("นำเข้า #{no} {serial} (การตรวจ #{tid})", no=no, serial=d["serial"], tid=tid)) + f"  {folder}")
    return 0


def save_and_open(report, open_browser):
    REPORT_DIR.mkdir(exist_ok=True)
    tag = report["disks"][0]["name"] if len(report["disks"]) == 1 else "all"
    model = re.sub(r"[^A-Za-z0-9]+", "-", report["disks"][0]["model"])[:30].strip("-") if len(report["disks"]) == 1 else ""
    path = REPORT_DIR / f"diskcheck-{time.strftime('%Y%m%d-%H%M%S')}-{tag}{'-' + model if model else ''}.html"
    path.write_text(render_html(report), encoding="utf-8")
    jpath = path.with_suffix(".json")
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    # ประวัติการตรวจทุกครั้ง — เปิดด้วย LibreOffice Calc / Excel ได้
    hist = REPORT_DIR / "history.csv"
    import csv
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow([_("เวลา"), _("อุปกรณ์"), _("ยี่ห้อ"), _("ซีรีส์"), _("รุ่น"), "S/N", _("ความจุ(GB)"), _("ชนิด"),
                        _("เกรด"), _("ผล"), _("ชั่วโมงใช้งาน"), _("อุณหภูมิ"), "SMART", _("เซกเตอร์เสีย(5)"),
                        _("รอย้าย(197)"), _("อ่านไม่ได้(198)"), "CRC(199)", _("ความเร็วอ่าน(MB/s)"), _("สแกน"), _("รายงาน")])
        for d in report["disks"]:
            s = d.get("smart", {})
            attr = {a.get("id"): a["value"] for a in s.get("attrs", []) if a.get("id")}
            sp = [r["mbps"] for r in d.get("speed", []) if r["mbps"]]
            w.writerow([report["time"], d["dev"], _(d.get("brand", "")), _(d.get("series") or ""), d["model"], d["serial"], f"{d['size'] / 1e9:.0f}", d["kind"],
                        d["grade"][0], _(d["grade"][1]), s.get("hours", ""),
                        s.get("temp") if s.get("temp") is not None else (round(d["sensor_temp"]) if d.get("sensor_temp") else ""),
                        {True: _("ผ่าน"), False: _("ไม่ผ่าน")}.get(s.get("passed"), "-"),
                        attr.get(5, ""), attr.get(197, ""), attr.get(198, ""), attr.get(199, ""),
                        f"{sum(sp) / len(sp):.0f}" if sp else "", scan_text(d), path.name])
    chown_user(REPORT_DIR, path, jpath, hist)
    uid = os.environ.get("SUDO_UID")
    print()
    print(BOLD("  📄 " + _("รายงาน HTML: ")) + CYAN(f"file://{path}"))
    if open_browser:
        cmd = ["xdg-open", str(path)]
        if IS_ROOT and os.environ.get("SUDO_USER"):
            rt = f"/run/user/{uid}"
            env = [f"DISPLAY={os.environ.get('DISPLAY', ':0')}", f"XDG_RUNTIME_DIR={rt}",
                   f"DBUS_SESSION_BUS_ADDRESS=unix:path={rt}/bus"]
            wl = os.environ.get("WAYLAND_DISPLAY") or ("wayland-0" if os.path.exists(f"{rt}/wayland-0") else "")
            if wl:
                env.append(f"WAYLAND_DISPLAY={wl}")
            xa = os.environ.get("XAUTHORITY") or next(iter(sorted(Path(rt).glob(".mutter-Xwaylandauth.*"))), None)
            if xa:
                env.append(f"XAUTHORITY={xa}")
            cmd = ["sudo", "-u", os.environ["SUDO_USER"], "env", *env, *cmd]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            print(DIM("     " + _("กำลังเปิดในเบราว์เซอร์...")))
        except FileNotFoundError:
            pass
    return path


# =====================================================================
#  เมนู / main
# =====================================================================
def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def menu(disks, a):
    header(_("เลือกดิสก์ที่จะตรวจ"))
    print_disk_list(disks)
    while True:
        pick = ask("\n  " + _("เลือกหมายเลข (เช่น 2 หรือ 2,3 · a = ทุกลูก · Enter = ทุกลูกที่ไม่ใช่ดิสก์ระบบ): "))
        if pick.lower() == "a":
            chosen = disks
        elif pick:
            idx = [int(x) for x in re.split(r"[,\s]+", pick) if x.isdigit()]
            chosen = [disks[i - 1] for i in dict.fromkeys(idx) if 1 <= i <= len(disks)]
        else:
            chosen = [d for d in disks if not d["system"]] or disks
        if chosen:
            break
        print(WARN("  " + _("ไม่มีหมายเลขนี้ — เลือก 1 ถึง {n}", n=len(disks))))
    print(OK("  ✔ " + _("เลือก: ")) + ", ".join(d["dev"] for d in chosen))
    header(_("เลือกระดับการตรวจ"))
    levels = [("1", _("เร็ว"), _("ข้อมูลดิสก์ + ค่า SMART"), _("~10 วินาที"), ""),
              ("2", _("มาตรฐาน"), _("+ self-test สั้น + ความเร็ว 3 โซน + สุ่มสแกน 2,000 จุด"), _("~3-5 นาที/ลูก"),
               "  " + OK(_("← แนะนำ"))),
              ("3", _("ละเอียด"), _("+ สแกนผิวจานเต็มทุกเซกเตอร์"), _("HDD 1TB ~2-3 ชม."), ""),
              ("4", _("ละเอียดสุด"), _("self-test แบบยาว + สแกนเต็มจาน"), _("HDD 4TB ~10+ ชม."), "")]
    for n, name, desc, took, extra in levels:
        print(f"  {BOLD(n)}  {pad(name, 11)}{pad(desc, 54)}{DIM(took)}{extra}")
    lv = ask("\n  " + _("เลือก [2]: "), "2")
    a.quick, a.full = lv == "1", lv in ("3", "4")
    if lv == "4":
        a.selftest = "long"
    return chosen


# ระดับการตรวจเก็บเป็นภาษาไทยในฐานข้อมูล (tests.level) — แปลตอนแสดงผลด้วย _(level)
LONG_LEVEL = {N_("เร็ว"): N_("เร็ว + self-test แบบยาว"), N_("มาตรฐาน"): N_("มาตรฐาน + self-test แบบยาว"),
              N_("ละเอียด (สแกนเต็มจาน)"): N_("ละเอียด (สแกนเต็มจาน) + self-test แบบยาว")}


def pick_lang(argv=None):
    """ภาษาของ terminal: --lang › DISKCHECK_LANG › config.json lang › th (ต้องรู้ก่อนสร้าง argparse เพราะ help แปลแล้ว)"""
    argv = sys.argv[1:] if argv is None else argv
    for i, x in enumerate(argv):
        if x == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if x.startswith("--lang="):
            return x.split("=", 1)[1]
    return os.environ.get("DISKCHECK_LANG") or load_config().get("lang") or "th"


def main():
    set_default(pick_lang())
    ap = argparse.ArgumentParser(description=_("ตรวจสุขภาพฮาร์ดดิสก์/SSD และสร้างรายงาน HTML"))
    ap.add_argument("--lang", choices=list(LANGS), help="th = ไทย · en = English")
    ap.add_argument("--disk", action="append", help=_("ชื่อดิสก์ เช่น sdb (ใส่ซ้ำได้) · ไม่ใส่ = เปิดเมนู"))
    ap.add_argument("--quick", action="store_true", help=_("ตรวจเร็ว: ข้อมูล + SMART เท่านั้น"))
    ap.add_argument("--full", action="store_true", help=_("สแกนผิวจานเต็มทั้งลูก (นาน)"))
    ap.add_argument("--selftest", choices=["short", "long", "none"],
                    help=_("self-test ในตัวดิสก์ แล้วรอจนเสร็จ (ค่าเริ่มต้น: short · --quick = none)"))
    ap.add_argument("--no-export", action="store_true", help=_("ไม่ต้องบันทึกฐานข้อมูล/สร้างโฟลเดอร์ดิสก์"))
    db_args = ap.add_argument_group(_("ฐานข้อมูล (ไม่ต้องใช้ sudo)"))
    db_args.add_argument("--list", action="store_true", help=_("แสดงดิสก์ทั้งหมดในฐานข้อมูล"))
    db_args.add_argument("--show", metavar=_("เลข|SERIAL"), help=_("ดูรายละเอียดและประวัติการตรวจ"))
    db_args.add_argument("--note", nargs=2, metavar=(_("เลข|SERIAL"), _("ข้อความ")), help=_("ใส่บันทึกให้ดิสก์"))
    db_args.add_argument("--rebuild", nargs="?", const="all", metavar=_("เลข|SERIAL"),
                         help=_("สร้างรายงาน/การ์ดใหม่จากผลล่าสุด (เช่น หลังเพิ่มรูป) · ไม่ใส่ = ทุกลูก"))
    db_args.add_argument("--web", action="store_true", help=_("เปิดเว็บ ดูผล/อัปโหลดรูปจากมือถือ"))
    db_args.add_argument("--port", type=int, default=8080, help=_("พอร์ตของเว็บ (ค่าเริ่มต้น 8080)"))
    db_args.add_argument("--import", dest="import_json", metavar=_("ไฟล์.json"), help=_("นำผลตรวจเก่าใน reports/ เข้าฐานข้อมูล"))
    ap.add_argument("--big", action="store_true", help=_("หาโฟลเดอร์ที่ใหญ่ที่สุดใน home"))
    ap.add_argument("--no-open", action="store_true", help=_("ไม่ต้องเปิดเบราว์เซอร์"))
    ap.add_argument("--yes", "-y", action="store_true", help=_("ไม่ต้องเปิดเมนู ตรวจทุกลูก"))
    a = ap.parse_args()

    if a.web:
        import webapp
        webapp.serve(a.port)
        return 0
    if a.list or a.show or a.note or a.rebuild or a.import_json:
        con, cfg = db(), load_config()
        if a.import_json:
            cmd_import(con, a.import_json, cfg)
        if a.note:
            cmd_note(con, *a.note)
        if a.rebuild:
            cmd_rebuild(con, a.rebuild, cfg)
        if a.rebuild or a.note or a.import_json:
            update_index(con, cfg)
        if a.show:
            return cmd_show(con, a.show)
        if a.list:
            cmd_list(con)
        return 0

    print(CYAN(BANNER) + DIM(f"  v{VERSION}"))
    print(DIM(f"  {os.uname().nodename} · {time.strftime('%Y-%m-%d %H:%M')} · "
              f"{'root ✔' if IS_ROOT else _('ไม่ได้ใช้ sudo (ข้อมูลจะไม่ครบ)')} · "
              f"smartctl {'✔' if SMARTCTL else '✘'}"))
    if not IS_ROOT:
        print(WARN("  ⚠  " + _("แนะนำให้รันด้วย sudo ./run.sh เพื่ออ่านค่า SMART และทดสอบการอ่านจริง")))
    if not SMARTCTL:
        print(WARN("  ⚠  " + _("ยังไม่ได้ติดตั้ง smartmontools → sudo apt install smartmontools")))

    if not a.disk and not a.yes and sys.stdin.isatty():
        header(_("เมนูหลัก"))
        print(f"  {BOLD('1')}  {_('ตรวจดิสก์')} {DIM(_('(ต้องใช้ sudo)'))}")
        print(f"  {BOLD('2')}  {_('ดูดิสก์ทั้งหมดในฐานข้อมูล')}")
        print(f"  {BOLD('3')}  {_('ดูประวัติการตรวจของดิสก์')}")
        print(f"  {BOLD('4')}  {_('สร้างรายงานใหม่ทุกลูก')} {DIM(_('(หลังใส่รูปใน photos/)'))}")
        print(f"  {BOLD('5')}  {_('ใส่บันทึกให้ดิสก์')}")
        print(f"  {BOLD('6')}  {_('เปิดเว็บ ดู/อัปโหลดรูปจากมือถือ')}")
        top = ask("\n  " + _("เลือก [1]: "), "1")
        if top != "1":
            con, cfg = db(), load_config()
            if top == "2":
                cmd_list(con)
            elif top == "3":
                cmd_list(con)
                return cmd_show(con, ask("\n  " + _("เลขดิสก์หรือ Serial: ")))
            elif top == "4":
                cmd_rebuild(con, "all", cfg)
                update_index(con, cfg)
            elif top == "6":
                import webapp
                webapp.serve(a.port)
            elif top == "5":
                cmd_list(con)
                return cmd_note(con, ask("\n  " + _("เลขดิสก์หรือ Serial: ")), ask("  " + _("ข้อความบันทึก: ")))
            return 0

    disks = list_disks()
    if not disks:
        print(BAD("  " + _("ไม่พบดิสก์ในเครื่อง")))
        return 2
    if a.disk:
        want = {x.replace("/dev/", "") for x in a.disk}
        chosen = [d for d in disks if d["name"] in want]
        missing = want - {d["name"] for d in chosen}
        if missing:
            print(BAD("  " + _("ไม่พบดิสก์: {names}", names=", ".join(sorted(missing)))))
            print_disk_list(disks)
            return 2
    elif sys.stdin.isatty() and not a.yes:
        chosen = menu(disks, a)
    else:
        chosen = disks

    if a.selftest is None:
        a.selftest = "none" if a.quick else "short"
    level = N_("เร็ว") if a.quick else N_("ละเอียด (สแกนเต็มจาน)") if a.full else N_("มาตรฐาน")  # เก็บไทยในฐานข้อมูล
    if a.selftest == "long":
        level = LONG_LEVEL[level]
    cfg = load_config()
    report = {"host": os.uname().nodename, "time": time.strftime("%Y-%m-%d %H:%M"), "level": level,
              "cmd": " ".join(["sudo" if IS_ROOT else "", *sys.argv]).strip(), "disks": chosen, "findings": []}

    for d in chosen:
        header(_("กำลังตรวจ {dev} · {model}", dev=d["dev"], model=_(d["model"])))
        if a.selftest != "none" and IS_ROOT and SMARTCTL:
            run_selftest(d, a.selftest)
        print(DIM("  " + _("อ่านค่า SMART...")))
        collect_smart(d)
        d["sensor_temp"] = collect_sensor_temp(d)
        collect_io(d)
        if not a.quick and IS_ROOT:
            print(DIM("  " + _("ทดสอบความเร็วอ่าน 3 โซน...")))
            try:
                read_speed(d)
            except OSError as e:
                add(d, "warn", "ทดสอบความเร็วไม่ได้: {e}", e=str(e))
            print(DIM("  " + (_("สแกนผิวจานเต็มทั้งลูก (กด Ctrl+C เพื่อหยุด)...") if a.full else _("สแกนผิวจานแบบสุ่ม 2,000 จุด..."))))
            try:
                surface_scan(d, quick=not a.full)
            except OSError as e:
                add(d, "warn", "สแกนผิวจานไม่ได้: {e}", e=str(e))
        d["grade"] = grade(d)
        print_disk(d)

    collect_partitions(report)
    collect_log_errors(report, [d["name"] for d in chosen])
    if a.big:
        print(DIM("\n  " + _("คำนวณขนาดโฟลเดอร์...")))
        big_dirs(report, Path(os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}")))

    header(_("สรุป"))
    worst = 0
    for d in chosen:
        g, gname, gdesc = d["grade"]
        gcol = {"A": OK, "B": OK, "C": WARN, "F": BAD}.get(g, CYAN)
        # web parses this line (webapp.py: r"^\s*\[([ABCF?])\]") — keep "  [grade]" at the start
        print(f"  {gcol(BOLD(f'[{g}]'))} {d['dev']:<12} {d['model'][:32]:<33} {gcol(_(gname))} — {_(gdesc)}")
        worst = max(worst, {"F": 2, "C": 1}.get(g, 0))
    for f in report["findings"]:
        if f["level"] in ("warn", "crit"):
            print(f"  {LEVEL_COLOR[f['level']]('●')} {fmsg(f)}")
    save_and_open(report, not a.no_open)
    if not a.no_export:
        con = db()
        print(BOLD("\n  📦 " + _("บันทึกลงฐานข้อมูล + โฟลเดอร์ของแต่ละดิสก์:")))
        for d in chosen:
            if not d.get("smart", {}).get("available"):
                print(WARN(f"     {d['dev']:<12} " + _("ข้าม — ยังไม่มีค่า SMART (รันด้วย sudo)")))
                continue
            if d["serial"] in ("", "-"):
                print(WARN(f"     {d['dev']:<12} " + _("ข้าม — ไม่มี Serial Number")))
                continue
            no, tid = record_test(con, d, report)
            folder, png, _no, nph = build_package(con, no, cfg)
            # web parses this line (webapp.py: r"#(\d+)\s+\S+\s+การตรวจ #(\d+)") — keep "การตรวจ #" in Thai, untranslated
            print(f"     {BOLD('#' + str(no)):<6} {d['serial']:<22} " + DIM(f"การตรวจ #{tid}") + "  " + CYAN(str(folder)))
            print(DIM("            report.html · data.json · listing.txt" + (" · card.png" if png else "")
                      + " · photos/ " + _("({n} รูป)", n=nph)))
        update_index(con, cfg)
        print(DIM("\n  " + _("หน้ารวม: {path}", path=DISKS_DIR / "index.html")))
        print(DIM("  " + _("ใส่รูป: วางไฟล์ใน photos/ แล้วรัน ./run.sh --rebuild  หรือเปิดเว็บ ./run.sh --web แล้วอัปโหลดจากมือถือ")))
    print(OK("\n  ✔ " + _("ตรวจเสร็จเรียบร้อย")))
    return worst


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  " + _("ยกเลิกแล้ว"))
        sys.exit(130)
