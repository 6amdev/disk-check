#!/usr/bin/env python3
"""diskcheck web — เปิดดูผลตรวจและอัปโหลดรูปจากมือถือ (ใช้ในวง Wi-Fi เดียวกัน)

  ./run.sh --web              เริ่มเว็บที่พอร์ต 8080
  ./run.sh --web --port 9000  เปลี่ยนพอร์ต

ใช้แค่ Python มาตรฐาน — ไม่ต้องติดตั้งอะไรเพิ่ม
"""

import html
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import time
import unicodedata
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diskcheck as dc  # noqa: E402
from i18n import _, get_lang, using, set_lang, set_default, norm as norm_lang, LANGS  # noqa: E402

E = html.escape
MAX_UPLOAD = 80 * 1024 * 1024  # ต่อครั้ง
SESSIONS = set()
GLVL = {"A": "ok", "B": "ok", "C": "warn", "F": "crit"}


def both(text):
    """ข้อความไทย + อังกฤษ (ไว้ใส่ในคำค้น ให้ค้นได้ทั้งสองภาษา) / Thai + English, for search haystacks"""
    if not text:
        return ""
    with using("th"):
        th = _(text)
    with using("en"):
        en = _(text)
    return th if en == th else f"{th} {en}"


# ---------------------------------------------------------------------
#  ตั้งค่า / PIN
# ---------------------------------------------------------------------
def load_cfg():
    cfg = dc.load_config()
    if not str(cfg.get("web_pin", "")).strip():
        # ครั้งแรก: สุ่ม PIN 4 หลักแล้วบันทึกลง config.json
        cfg["web_pin"] = f"{secrets.randbelow(10000):04d}"
        path = dc.HERE / "config.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        raw["web_pin"] = cfg["web_pin"]
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dc.chown_user(path)
    return cfg


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # ไม่ได้ส่งข้อมูลจริง แค่ถามว่าออกเน็ตทาง IP ไหน
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------
#  ข้อมูลจากฐานข้อมูล
# ---------------------------------------------------------------------
def all_disks(con):
    rows = con.execute("SELECT * FROM disks ORDER BY no DESC").fetchall()
    out = []
    for r in rows:
        folder = dc.disk_folder(r["no"], r["serial"])
        photos = sorted((p for p in (folder / "photos").glob("*") if p.suffix.lower() in dc.PHOTO_EXT),
                        key=lambda p: photo_num(p)) if (folder / "photos").exists() else []
        last = con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id DESC LIMIT 1", (r["no"],)).fetchone()
        out.append({"row": r, "folder": folder, "photos": photos, "last": last})
    return out


def photo_num(p):
    m = re.search(r"_(\d+)$", p.stem)
    return int(m.group(1)) if m else 10**9


def stats(disks):
    total = sum(d["row"]["capacity_bytes"] or 0 for d in disks)
    grades = {g: sum(1 for d in disks if (d["row"]["last_grade"] or "?") == g) for g in ("A", "B", "C", "F", "?")}
    return total, grades


# ---------------------------------------------------------------------
#  HTML
# ---------------------------------------------------------------------
APP_CSS = """
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:6px}
.top a{color:var(--fg);text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 16px;border-radius:12px;border:1px solid var(--line);
background:var(--card2);color:var(--fg);text-decoration:none;font:inherit;font-size:14px;cursor:pointer}
.btn.pri{background:linear-gradient(135deg,#7c5cff,#38bdf8);border:0;color:#fff;font-weight:700}
.btn.del{background:#ef444422;color:var(--crit);border-color:#ef444455;padding:6px 10px;font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.kpi b{display:block;font-size:26px}.kpi span{font-size:12.5px;color:var(--mut)}
.search{width:100%;padding:13px 16px;border-radius:14px;border:1px solid var(--line);background:var(--card);
color:var(--fg);font:inherit;margin-bottom:14px}
.list{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.item{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:18px;
padding:14px;text-decoration:none;color:var(--fg)}
.item:hover{border-color:var(--acc)}
.thumb{width:84px;height:84px;border-radius:14px;object-fit:cover;flex:none;background:var(--card2);
display:grid;place-items:center;color:var(--mut);font-size:30px}
.mini{width:46px;height:46px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:22px;color:#fff;flex:none}
.it{flex:1;min-width:0}.it b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.it small{color:var(--mut);display:block;font-size:12.5px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.ph{margin:0;background:var(--card2);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.ph img{display:block;width:100%;aspect-ratio:1;object-fit:cover}
.ph figcaption{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:8px 10px;font-size:11.5px;
color:var(--mut);font-family:ui-monospace,monospace}
.up{border:2px dashed var(--line);border-radius:18px;padding:22px;text-align:center}
.up input{display:none}.up .row{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:12px}
.prog{height:8px;border-radius:99px;background:var(--line);overflow:hidden;margin-top:14px;display:none}
.prog i{display:block;height:100%;width:0;background:linear-gradient(90deg,#7c5cff,#38bdf8);transition:width .2s}
textarea{width:100%;min-height:80px;border-radius:12px;border:1px solid var(--line);background:var(--card2);
color:var(--fg);font:inherit;padding:10px 12px}
.links{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.pin{max-width:340px;margin:12vh auto;text-align:center}
.pin input{font-size:32px;letter-spacing:12px;text-align:center;width:100%;padding:14px;border-radius:14px;
border:1px solid var(--line);background:var(--card);color:var(--fg);margin:18px 0}
.empty{text-align:center;color:var(--mut);padding:40px 10px}
.fl{display:block;margin-bottom:12px}.fl span{display:block;font-size:13px;color:var(--mut);margin-bottom:5px}
.fl input,.fl select{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:var(--card2);
color:var(--fg);font:inherit}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.post{white-space:pre-wrap;min-height:160px;font-size:14px}
.cps{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:8px}
.cp{display:flex;flex-direction:column;align-items:flex-start;gap:2px;text-align:left;position:relative;padding:10px 34px 10px 12px;
border-radius:12px;border:1px solid var(--line);background:var(--card2);color:var(--fg);font:inherit;cursor:pointer;min-width:0}
.cp span{font-size:11.5px;color:var(--mut)}.cp b{font-size:14.5px;word-break:break-all}
.cp i{position:absolute;right:10px;top:50%;transform:translateY(-50%);font-style:normal;opacity:.6}
.cp:active{border-color:var(--acc);background:#7c5cff22}.cp.wide{grid-column:1/-1}
.tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch}
.tab{flex:none;padding:9px 14px;border-radius:99px;border:1px solid var(--line);background:var(--card2);color:var(--fg);
font:inherit;font-size:13px;cursor:pointer;white-space:nowrap}.tab.on{background:var(--acc);border-color:var(--acc);color:#fff}
.steps{counter-reset:s;display:grid;gap:10px}.steps>div{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.steps>div:before{counter-increment:s;content:counter(s);width:28px;height:28px;border-radius:50%;background:var(--acc);
color:#fff;display:grid;place-items:center;font-weight:800;flex:none}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;margin-top:10px}
.imgs a{position:relative;display:block}.imgs img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;display:block}
.imgs span{position:absolute;right:6px;bottom:6px;background:#000a;color:#fff;border-radius:8px;padding:2px 6px;font-size:11px}
.price{font-weight:800;color:var(--ok)}
.chk{display:flex;gap:8px;flex-wrap:wrap}.lvl{flex:1;min-width:180px;padding:10px 12px;border-radius:12px;
border:1px solid var(--line);background:var(--card2);color:var(--fg);font:inherit;font-size:14px}
code{background:var(--card2);padding:2px 8px;border-radius:6px}
.sw{display:flex;align-items:center;gap:12px;cursor:pointer}.sw input{display:none}
.knob{width:50px;height:28px;border-radius:99px;background:var(--line);position:relative;transition:.2s;flex:none}
.knob:after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:.2s}
.sw input:checked+.knob{background:var(--ok)}.sw input:checked+.knob:after{left:25px}
.auto .lvl{width:100%}
.live{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ok);margin-left:6px;
box-shadow:0 0 0 0 #22c55e88;animation:pulse 1.6s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 9px #22c55e00}100%{box-shadow:0 0 0 0 #22c55e00}}
.item.busy{border-color:var(--info);box-shadow:0 0 30px -14px var(--info)}
.bar i.ind{animation:ind 1.2s infinite ease-in-out}@keyframes ind{0%{margin-left:-30%}100%{margin-left:100%}}
.stb{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:99px;font-size:11.5px;font-weight:700;
background:color-mix(in srgb,var(--c) 22%,transparent);color:var(--c);border:1px solid color-mix(in srgb,var(--c) 45%,transparent);white-space:nowrap}
.sts{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px}
.stbtn{padding:10px 8px;border-radius:12px;border:1px solid var(--line);background:var(--card2);color:var(--fg);font:inherit;
font-size:13px;cursor:pointer;text-align:center;line-height:1.3}.stbtn small{display:block;color:var(--mut);font-size:10.5px}
.stbtn.on{background:color-mix(in srgb,var(--c) 28%,transparent);border-color:var(--c);color:#fff;font-weight:700}
.filters{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}
.ghead{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 14px;margin-top:6px;
border-radius:14px;background:var(--card2);border:1px solid var(--line);cursor:pointer;user-select:none}
.ghead b{font-size:15px}.ghead span{color:var(--mut);font-size:12.5px;white-space:nowrap}.ghead i{font-style:normal;margin-right:8px;
display:inline-block;transition:.15s}.ghead.shut i{transform:rotate(-90deg)}
.gsum{display:flex;gap:8px;flex-wrap:wrap;margin:-2px 0 12px}.gsum button{font-size:12px;padding:6px 11px}
.filters select{width:100%;padding:10px 10px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;font-size:13px}
@media(max-width:620px){.filters{grid-template-columns:1fr 1fr}.filters #gb{grid-column:1/-1}}
.sbox{position:relative}.sbox .x{position:absolute;right:8px;top:7px;border:0;background:var(--card2);color:var(--mut);
width:32px;height:32px;border-radius:50%;font-size:16px;cursor:pointer;display:none}
.found{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;color:var(--mut);font-size:13px;margin:4px 2px 12px}
.tab .n{opacity:.7;margin-left:4px;font-size:11.5px}
.langsw{display:flex;justify-content:flex-end;gap:4px;margin:-6px 0 6px}.langsw a{padding:4px 10px;border-radius:99px;
font-size:12px;font-weight:700;color:var(--mut);text-decoration:none;border:1px solid var(--line)}
.langsw a.on{background:var(--acc);border-color:var(--acc);color:#fff}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(80px);background:#111827;color:#fff;
padding:12px 20px;border-radius:99px;transition:.25s;opacity:0;z-index:9}#toast.show{transform:translateX(-50%);opacity:1}
"""


def page(title, body):
    return f"""<!doctype html><html lang="{get_lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0f17"><title>{E(title)}</title>
<style>{dc.CSS}{APP_CSS}</style></head><body><div class="wrap">{lang_switch()}{body}</div></body></html>"""


def lang_switch():
    """ปุ่ม TH / EN มุมบนขวา (จำภาษาไว้ในคุกกี้ของมือถือเครื่องนั้น)"""
    cur = get_lang()
    links = "".join(f'<a href="/lang?to={k}" class="{"on" if k == cur else ""}" onclick="this.href+=\'&back=\'+'
                    f'encodeURIComponent(location.pathname+location.search+location.hash)">{k.upper()}</a>' for k in LANGS)
    return (f'<nav class="langsw" id="langsw">{links}</nav>'
            '<script>if(location.protocol==="file:")document.getElementById("langsw").remove();</script>')


def grade_mini(g):
    cls = {"?": "gQ"}.get(g or "?", f"g{g}")
    return f'<span class="mini grade {cls}" style="width:46px;height:46px;font-size:22px;box-shadow:none">{E(g or "?")}</span>'


STATUS_COLOR = {"เตรียมขาย": "#94a3b8", "พร้อมขาย": "#22c55e", "กำลังขาย": "#38bdf8", "จองแล้ว": "#f59e0b",
                "ขายแล้ว": "#a78bfa", "ส่งแล้ว": "#8b93a7", "เคลม/ตีกลับ": "#ef4444", "เก็บไว้ใช้": "#2dd4bf", "ไม่ขาย": "#64748b"}


def st_badge(st):
    return (f'<span class="stb" style="--c:{STATUS_COLOR.get(st, "#94a3b8")}">'
            f'{dc.status_meta(st)["icon"]} {E(_(st))}</span>')


def apply_status(own, new):
    """เปลี่ยนสถานะ + บันทึกเวลา/ประวัติ (เก็บใน sale_json)"""
    if new not in dc.SALE_STATUS:
        return own
    cur = own.get("status") or dc.STATUS_DEFAULT
    own["status"] = new
    if new == cur and own.get("status_at"):
        return own
    now = time.strftime("%Y-%m-%d %H:%M")
    own["status_at"] = now
    own["status_log"] = (own.get("status_log") or [])[-49:] + [{"status": new, "at": now}]
    if dc.status_meta(new)["group"] == "done":
        own.setdefault("sold_at", now)
    elif dc.is_selling(new):
        own.pop("sold_at", None)
    return own


def status_bar(r, sale, back=None):
    """ปุ่มเปลี่ยนสถานะแบบแตะครั้งเดียว"""
    cur = sale.get("status")
    btns = "".join(
        f'<button class="stbtn{" on" if st == cur else ""}" name="status" value="{E(st)}" style="--c:{STATUS_COLOR.get(st)}" '
        f'title="{E(_(dc.status_meta(st)["desc"]))}">{dc.status_meta(st)["icon"]} {E(_(st))}'
        f'<small>{E(_(dc.status_meta(st)["desc"]))}</small></button>' for st in dc.SALE_STATUS)
    since = _(" · ตั้งแต่ {t}", t=E(sale["status_at"])) if sale.get("status_at") else ""
    sold = _(" · ขายเมื่อ {t}", t=E(sale["sold_at"])) if sale.get("sold_at") else ""
    return (f'<form method="post" action="/disk/{r["no"]}/status">'
            f'<input type="hidden" name="back" value="{E(back or f"/disk/{r["no"]}")}">'
            f'<div class="mut" style="font-size:13px;margin-bottom:10px">{_("ตอนนี้:")} {st_badge(cur)}{since}{sold}</div>'
            f'<div class="sts">{btns}</div></form>')


def running_banner():
    with JOBS_LOCK:
        run = [j for j in JOBS.values() if j["status"] == "running"]
    if not run:
        return ""
    return ('<div class="card" style="margin-top:14px;border-color:var(--info)">⏳ ' + _("กำลังตรวจอยู่:") + ' ' +
            " · ".join(f'<a href="/job/{j["id"]}">{E(j["dev"])} {E(j["model"])}</a>' for j in run) + '</div>')


def index_html(con, cfg, web=True):
    """หน้ารวม — web=True ลิงก์ไปหน้าเว็บ / web=False ไฟล์ disks/index.html เปิดดูได้โดยไม่ต้องรันเว็บ"""
    disks = all_disks(con)
    total, grades = stats(disks)
    shop = cfg.get("shop") or ""
    items, st_count, types, caps, brands, forms, exts = [], {}, {}, {}, {}, {}, {}
    in_stock = sold_n = 0
    stock_value = sold_value = profit_sum = cost_sum = 0.0
    for d in disks:
        r = d["row"]
        rel = d["folder"].name
        href = f"/disk/{r['no']}" if web else f"{rel}/report.html"
        if d["photos"]:
            src = f"/files/{r['no']}/photos/{urllib.parse.quote(d['photos'][0].name)}" if web \
                else f"{rel}/photos/{urllib.parse.quote(d['photos'][0].name)}"
            thumb = f'<img class="thumb" src="{src}" alt="" loading="lazy">'
        else:
            thumb = '<div class="thumb">💽</div>'
        last = d["last"]
        hrs = last["power_on_hours"] if last else None
        sale = dc.sale_info(r, cfg, private=True)
        status = sale.get("status", dc.STATUS_DEFAULT)
        group = dc.status_meta(status)["group"]
        st_count[status] = st_count.get(status, 0) + 1
        price = float(dc.num_or(sale.get("price")) or 0)
        if group in ("prep", "sell", "hold"):
            in_stock += 1
            stock_value += price
        w = dc.warranty_info(sale)
        if dc.is_sold(sale):
            sold_n += 1
            sold_value += float(dc.num_or(sale.get("sold_price")) or price)
            profit_sum += dc.profit_of(sale) or 0
        if group in ("prep", "sell", "hold"):
            cost_sum += dc.num_or(sale.get("cost")) or 0
        cap = dc.human(r["capacity_bytes"]).replace(".0 ", " ")
        typ = r["type"] or "?"
        ext = r["external"] or ""
        form = dc.ext_kind(typ) if ext else dc.form_label(typ, r["form_factor"], r["model"])
        exts[ext or "—"] = exts.get(ext or "—", 0) + 1
        forms[form] = forms.get(form, 0) + 1
        types[typ] = types.get(typ, 0) + 1
        caps[cap] = r["capacity_bytes"] or 0
        bad = ((last["reallocated"] or 0) + (last["pending"] or 0)) if last else 0
        hay = [f"#{r['no']}", r["serial"], r["brand"], r["series"], r["model"], typ, cap, cap.replace(" ", ""),
               form, form.replace(".", ""), ext, ext.replace(" ", ""), "external ภายนอก" if ext else "", "sata" if "SATA" in form or typ in ("HDD", "SSD") else "",
               r["form_factor"], f"{r['rpm']}rpm" if r["rpm"] else "", r["firmware"],
               f"เกรด {r['last_grade']}", f"grade {r['last_grade']}", both(r["last_grade_label"]), both(status), r["note"],
               sale.get("price"), sale.get("condition"), sale.get("warranty"), sale.get("details"), sale.get("buyer") if web else "",
               f"{hrs}ชม {hrs}h" if hrs is not None else "",
               both("ในประกัน" if w["active"] else "หมดประกัน") if w["active"] is not None else "",
               both("เคลม") if sale.get("claims") else "", both("มีเซกเตอร์เสีย" if bad else "ไม่มีเซกเตอร์เสีย"),
               both("มีรูป" if d["photos"] else "ไม่มีรูป"), both("มีราคา" if sale.get("price") else "ไม่มีราคา")]
        search = " ".join(str(x) for x in hay if x)
        brand = r["brand"] if r["brand"] and r["brand"] != "ไม่ทราบ" else _("ไม่ทราบยี่ห้อ")
        series = (r["series"] or "").split(" (")[0]
        brands[brand] = brands.get(brand, 0) + 1
        price_html = f' · <span class="price">{E(dc.fmt_price(sale["price"]))}</span>' if sale.get("price") else ""
        items.append(
            f'<a class="item" href="{href}" data-s="{E(search.lower())}" data-st="{E(status)}" data-stl="{E(_(status))}" data-gr="{E(group)}" '
            f'data-g="{E(r["last_grade"] or "?")}" data-t="{E(typ)}" data-c="{E(cap)}" data-cb="{r["capacity_bytes"] or 0}" '
            f'data-p="{price or ""}" data-h="{hrs if hrs is not None else ""}" data-no="{r["no"]}" '
            f'data-d="{E(r["last_tested"] or "")}" data-b="{E(brand)}" data-se="{E(series)}" data-m="{E(r["model"] or "?")}" data-f="{E(form)}" data-x="{E(ext)}">'
            f'{thumb}<div class="it">'
            f'<small>#{r["no"]} · {E(_(r["brand"] or ""))}{" · " + E(r["series"]) if r["series"] else ""}{price_html}</small>'
            f'{"<small>📦 " + E(ext) + "</small>" if ext else ""}<b>{E(r["model"] or "")}</b>'
            f'<small>{cap} · {E(typ)}{" · " + E(form) if form != typ else ""} · S/N {E(r["serial"])}</small>'
            f'<small>{_("{h} ชม. · ตรวจ {d}", h=dc.num(hrs), d=E((r["last_tested"] or "")[:10]))} · 📷 {len(d["photos"])}'
            f'{" · 📝 " + E(r["note"]) if r["note"] else ""}</small>'
            f'<div style="margin-top:5px;display:flex;gap:6px;flex-wrap:wrap">{st_badge(status)}'
            f'{warranty_badge(w) if web and dc.is_sold(sale) else ""}</div></div>{grade_mini(r["last_grade"])}</a>')
    baht = lambda v: f"{v:,.0f}"
    kpis = (f'<div class="kpi"><b>{len(disks)}</b><span>{_("ดิสก์ทั้งหมด")} · {dc.human(total).replace(".0 ", " ")}</span></div>'
            f'<div class="kpi"><b>{in_stock}</b><span>{_("ยังมีของ · {v} บาท", v=baht(stock_value))}'
            f'{_(" · ทุน {v}", v=baht(cost_sum)) if cost_sum and web else ""}</span></div>'
            f'<{"a href=/sales" if web else "div"} class="kpi" style="color:inherit;text-decoration:none"><b>{sold_n}</b>'
            f'<span>{_("ขายแล้ว · {v} บาท", v=baht(sold_value))}{_(" · กำไร {v}", v=baht(profit_sum)) if profit_sum and web else ""}</span>'
            f'</{"a" if web else "div"}>' +
            "".join(f'<div class="kpi"><b>{grades[g]}</b><span>{dc.pill(GLVL.get(g, "info"), _("เกรด {g}", g=g))}</span></div>'
                    for g in ("A", "B", "C", "F", "?") if grades[g] or g in ("A", "F")))
    tabs = [("", _("ทั้งหมด"), len(disks)), ("@stock", "🟢 " + _("ยังมีของ"), in_stock)]
    tabs += [(st, f'{dc.status_meta(st)["icon"]} {_(st)}', st_count[st]) for st in dc.SALE_STATUS if st_count.get(st)]
    tabs += [(st, _(st), n) for st, n in st_count.items() if st not in dc.SALE_STATUS]
    tab_html = "".join(f'<button class="tab{" on" if k == "" else ""}" data-v="{E(k)}" onclick="stf(this)">{E(lbl)}'
                       f'<span class="n">{n}</span></button>' for k, lbl, n in tabs)
    opt = lambda pairs: "".join(f'<option value="{E(v)}">{E(t)}</option>' for v, t in pairs)
    g_opts = opt([("", _("ทุกเกรด"))] + [(g, _("เกรด {g} ({n})", g=g, n=grades[g])) for g in ("A", "B", "C", "F", "?") if grades[g]])
    t_opts = opt([("", _("ทุกชนิด"))] + [(t, f"{t} ({n})") for t, n in sorted(types.items())])
    c_opts = opt([("", _("ทุกความจุ"))] + [(c, c) for c, _cb in sorted(caps.items(), key=lambda x: x[1])])
    f_opts = opt([("", _("ทุกแบบ"))] + [(f, f"{f} ({n})") for f, n in sorted(forms.items())])
    b_opts = opt([("", _("ทุกยี่ห้อ"))] + [(b, f"{b} ({n})") for b, n in sorted(brands.items())])
    gb_opts = opt([("", _("ไม่จัดกลุ่ม")), ("b", _("กลุ่ม: ยี่ห้อ")), ("se", _("กลุ่ม: ยี่ห้อ › ซีรีส์")), ("m", _("กลุ่ม: รุ่น")),
                   ("f", _("กลุ่ม: แบบ (M.2 / 2.5\" / 3.5\" / External)")), ("x", _("กลุ่ม: รุ่นดิสก์ภายนอก")),
                   ("c", _("กลุ่ม: ความจุ")), ("t", _("กลุ่ม: ชนิด")), ("st", _("กลุ่ม: สถานะ")), ("g", _("กลุ่ม: เกรด"))])
    st_order = json.dumps(dc.SALE_STATUS, ensure_ascii=False)
    s_opts = opt([("new", _("ล่าสุดก่อน")), ("no", _("เลข #น้อย→มาก")), ("pa", _("ราคาต่ำ→สูง")), ("pd", _("ราคาสูง→ต่ำ")),
                  ("ca", _("ความจุน้อย→มาก")), ("cd", _("ความจุมาก→น้อย")), ("h", _("ชม.ใช้งานน้อยก่อน")), ("g", _("เกรดดีก่อน"))])
    lst = "".join(items) or (f'<div class="empty">{_("ยังไม่มีดิสก์ในฐานข้อมูล")}<br>'
                             f'{_("ตรวจดิสก์ด้วย {cmd} ก่อน", cmd="<code>sudo ./run.sh</code>")}</div>')
    tj = json.dumps({"pcs": _("{n} ลูก"), "baht": _(" · {v} บาท"), "found": _("พบ {n} จาก {t} ลูก"),
                     "sum": _("ราคารวม {v} บาท"), "noseries": _("(ไม่ระบุซีรีส์)"), "notext": _("ไม่ใช่ดิสก์ภายนอก"),
                     "grade": _("เกรด {g}")}, ensure_ascii=False).replace("</", "<\\/")
    return page("Disk Check", f"""
<div class="hero"><div><h1>🩺 Disk Check</h1><p>{_("รวมผลตรวจสุขภาพดิสก์")}{(' · ' + E(shop)) if shop else ''}</p></div>
<div class="chips"><span class="chip">{_("อัปเดต {t}", t=time.strftime('%Y-%m-%d %H:%M'))}</span>
{('<a class="chip" style="color:#fff;text-decoration:none" href="/settings">⚙️ ' + _("ตั้งค่า") + '</a>') if web else ''}</div></div>
{('<div class="links" style="margin-top:14px"><a class="btn pri" href="/devices">🔌 ' + _("ดิสก์ที่เสียบอยู่ / สั่งตรวจ") + '</a>'
   '<a class="btn" href="/catalog">📦 ' + _("ข้อความรวมสินค้า") + '</a><a class="btn" href="/sales">📊 ' + _("สรุปยอดขาย") + '</a>'
   '<a class="btn" href="/claim">🛡 ' + _("ประกัน / เคลม") + '</a></div>' + running_banner()) if web else ''}
<div class="kpis">{kpis}</div>
<div class="sbox"><input class="search" id="q" placeholder="{E(_("🔍 ค้นหา Serial / ยี่ห้อ / รุ่น / 4TB / M.2 / My Book / เกรด A ..."))}" autocomplete="off" enterkeyhint="search">
<button class="x" id="qx" onclick="clr()" aria-label="{E(_("ล้าง"))}">✕</button></div>
<div class="filters"><select id="fb">{b_opts}</select><select id="fg">{g_opts}</select><select id="ft">{t_opts}</select><select id="ff">{f_opts}</select>
<select id="fc">{c_opts}</select><select id="so">{s_opts}</select><select id="gb">{gb_opts}</select></div>
<div class="gsum" id="gctl" style="display:none"><button class="btn" onclick="fold(1)">▸ {_("ย่อทุกกลุ่ม")}</button><button class="btn" onclick="fold(0)">▾ {_("ขยายทุกกลุ่ม")}</button></div>
<div class="tabs" id="stf" style="margin-bottom:10px">{tab_html}</div>
<div class="found"><span id="cnt"></span><span id="sum"></span></div>
<div class="list" id="list">{lst}</div>
<div class="empty" id="none" style="display:none">{_("ไม่พบดิสก์ที่ตรงกับการค้นหา")}</div>
<script>
const T={tj};
let ST='';const $=i=>document.getElementById(i);const L=$('list');const ALL=[...L.querySelectorAll('.item')];
const GR={{A:0,B:1,C:2,F:3,'?':4}};
function norm(t){{return t.toLowerCase().replace(/\\s+/g,' ').trim();}}
function apply(save=true){{
  let qq=norm($('q').value),qg='';qq=qq.replace(/(?:เกรด|grade)\\s*([abcf])(?![a-z0-9])/,(m,x)=>{{qg=x.toUpperCase();return ' ';}});
  const words=qq.split(' ').filter(Boolean);const g=$('fg').value,t=$('ft').value,c=$('fc').value,fb=$('fb').value,gb=$('gb').value,ff=$('ff').value;
  let n=0,sum=0;
  ALL.forEach(a=>{{const s=a.dataset.s,sq=s.replace(/\\s+/g,'');
    const ok=words.every(w=>s.includes(w)||sq.includes(w))&&(!g||a.dataset.g===g)&&(!qg||a.dataset.g===qg)&&(!t||a.dataset.t===t)&&(!c||a.dataset.c===c)&&(!fb||a.dataset.b===fb)&&(!ff||a.dataset.f===ff)
      &&(!ST||(ST==='@stock'?['prep','sell','hold'].includes(a.dataset.gr):a.dataset.st===ST));
    a.style.display=ok?'':'none';if(ok){{n++;sum+=+a.dataset.p||0;}}}});
  const so=$('so').value,num=(a,k)=>a.dataset[k]===''?Infinity:+a.dataset[k];
  const cmp={{new:(a,b)=>b.dataset.d.localeCompare(a.dataset.d)||b.dataset.no-a.dataset.no,no:(a,b)=>a.dataset.no-b.dataset.no,
    pa:(a,b)=>num(a,'p')-num(b,'p'),pd:(a,b)=>(+b.dataset.p||-1)-(+a.dataset.p||-1),ca:(a,b)=>a.dataset.cb-b.dataset.cb,
    cd:(a,b)=>b.dataset.cb-a.dataset.cb,h:(a,b)=>num(a,'h')-num(b,'h'),g:(a,b)=>GR[a.dataset.g]-GR[b.dataset.g]||b.dataset.no-a.dataset.no}}[so];
  L.querySelectorAll('.ghead').forEach(h=>h.remove());
  const K=a=>gkey(a,gb),O=a=>gord(a,gb);
  const sorted=ALL.slice().sort((a,b)=>gb?(cmpo(O(a),O(b))||cmp(a,b)):cmp(a,b));
  let prev=null;const heads={{}};
  sorted.forEach(a=>{{if(gb&&a.style.display!=='none'){{const k=K(a);if(k!==prev){{const h=document.createElement('div');h.className='ghead';
      h.dataset.k=k;h.onclick=()=>{{SHUT.has(k)?SHUT.delete(k):SHUT.add(k);apply(false);}};L.appendChild(h);heads[k]={{h,n:0,p:0}};prev=k;}}
      heads[k].n++;heads[k].p+=+a.dataset.p||0;if(SHUT.has(k))a.style.display='none';}}L.appendChild(a);}});
  Object.entries(heads).forEach(([k,o])=>{{o.h.classList.toggle('shut',SHUT.has(k));
    o.h.innerHTML=`<b><i>▾</i>${{esc(k)}}</b><span>${{T.pcs.replace('{{n}}',o.n)}}${{o.p?T.baht.replace('{{v}}',o.p.toLocaleString()):''}}</span>`;}});
  $('gctl').style.display=gb?'flex':'none';
  $('cnt').textContent=T.found.replace('{{n}}',n).replace('{{t}}',ALL.length);$('sum').textContent=sum?T.sum.replace('{{v}}',sum.toLocaleString()):'';
  $('none').style.display=n||!ALL.length?'none':'';$('qx').style.display=$('q').value?'block':'none';
  if(save){{const u=new URLSearchParams();[['q',$('q').value],['g',g],['t',t],['c',c],['b',fb],['f',ff],['gb',gb],['s',so==='new'?'':so],['st',ST]].forEach(([k,v])=>v&&u.set(k,v));
    try{{history.replaceState(null,'',(u.toString()?'?'+u:location.pathname));}}catch(e){{}}}}
}}
const SHUT=new Set(),STO={st_order};
const esc=t=>t.replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
function gkey(a,gb){{const d=a.dataset;return {{b:d.b,se:d.b+' › '+(d.se||T.noseries),m:d.m.toLowerCase().startsWith(d.b.toLowerCase())?d.m:d.b+' '+d.m,f:d.f,x:d.x||T.notext,c:d.c,t:d.t,st:d.stl||d.st,g:T.grade.replace('{{g}}',d.g)}}[gb]||'';}}
function gord(a,gb){{const d=a.dataset;if(gb==='c')return +d.cb;if(gb==='g')return GR[d.g];if(gb==='st'){{const i=STO.indexOf(d.st);return i<0?99:i;}}
  if(gb==='x'&&!d.x)return '\uffff';return gkey(a,gb).toLowerCase();}}
function cmpo(x,y){{return typeof x==='number'?x-y:x.localeCompare(y,'th');}}
function fold(on){{SHUT.clear();if(on)document.querySelectorAll('#list .ghead').forEach(h=>SHUT.add(h.dataset.k));apply(false);}}
function stf(b){{ST=b.dataset.v;document.querySelectorAll('#stf .tab').forEach(x=>x.classList.toggle('on',x===b));apply();}}
function clr(){{$('q').value='';apply();$('q').focus();}}
(()=>{{const u=new URLSearchParams(location.search);$('q').value=u.get('q')||'';
  [['fg','g'],['ft','t'],['fc','c'],['fb','b'],['ff','f'],['gb','gb'],['so','s']].forEach(([i,k])=>{{if(u.get(k))$(i).value=u.get(k);}});
  const st=u.get('st')||'';const b=[...document.querySelectorAll('#stf .tab')].find(x=>x.dataset.v===st);
  if(b){{ST=st;document.querySelectorAll('#stf .tab').forEach(x=>x.classList.toggle('on',x===b));}}apply(false);}})();
$('q').addEventListener('input',()=>apply());['fg','ft','ff','fc','fb','so'].forEach(i=>$(i).addEventListener('change',()=>apply()));
$('gb').addEventListener('change',()=>{{SHUT.clear();apply();}});
</script>""")


def disk_page(con, cfg, no, msg=""):
    r = dc.find_disk(con, no)
    if not r:
        return None
    d = next(x for x in all_disks(con) if x["row"]["no"] == r["no"])
    t = d["last"]
    detail = json.loads(t["detail_json"])["disk"]
    g = r["last_grade"] or "?"
    gcls = {"?": "gQ"}.get(g, f"g{g}")
    photos = "".join(
        f'<figure class="ph"><a href="/files/{r["no"]}/photos/{urllib.parse.quote(p.name)}" target="_blank">'
        f'<img src="/files/{r["no"]}/photos/{urllib.parse.quote(p.name)}" loading="lazy" alt=""></a>'
        f'<figcaption><span>{E(p.name)}</span><form method="post" action="/disk/{r["no"]}/delete" '
        f'onsubmit="return confirm({E(json.dumps(_("ลบรูป {name} ?", name=p.name), ensure_ascii=False))})"><input type="hidden" name="name" value="{E(p.name)}">'
        f'<button class="btn del">{_("ลบ")}</button></form></figcaption></figure>' for p in d["photos"]) \
        or f'<p class="mut">{_("ยังไม่มีรูป")}</p>'
    hist = con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id DESC", (r["no"],)).fetchall()
    hrows = "".join(f'<tr><td>#{h["id"]}</td><td>{E(h["tested_at"])}</td>'
                    f'<td>{dc.pill(GLVL.get(h["grade"], "info"), (h["grade"] or "?") + " " + _(h["grade_label"] or ""))}</td>'
                    f'<td class="num">{dc.num(h["power_on_hours"])}</td><td class="num">{dc.num(h["reallocated"])}</td></tr>'
                    for h in hist)
    s = detail.get("smart", {})
    stat = lambda v, l: f'<div class="stat"><b>{E(str(v))}</b><span>{E(l)}</span></div>'
    sp = [z["mbps"] for z in detail.get("speed") or [] if z.get("mbps")]
    stats_html = "".join(filter(None, [
        stat(dc.human(r["capacity_bytes"]), _("ความจุ")),
        stat(dc.num(s.get("hours")), _("ชั่วโมงใช้งาน")) if s.get("hours") is not None else "",
        stat({True: _("ผ่าน"), False: _("ไม่ผ่าน")}.get(s.get("passed"), "-"), "SMART"),
        stat(dc.num(t["reallocated"]), _("เซกเตอร์เสีย")) if t["reallocated"] is not None else "",
        stat(f"{max(sp):,.0f} MB/s", _("อ่านสูงสุด")) if sp else "",
    ]))
    flash = f'<div class="card" style="border-color:var(--ok)">{E(msg)}</div>' if msg else ""
    own = json.loads(r["sale_json"] or "{}")
    defaults = cfg.get("sale_defaults") or {}
    sale = dc.sale_info(r, cfg)

    def field(k, lbl, typ, ph):
        ph = defaults.get(k) and _("ค่าเริ่มต้น: {v}", v=defaults[k]) or _(ph)
        lbl = _(lbl)
        v = E(str(own.get(k, "")))
        if typ == "select":
            opts = "".join(f'<option value="{E(o)}"{" selected" if sale.get("status") == o else ""}>{dc.status_meta(o)["icon"]} {E(_(o))}</option>'
                           for o in dc.SALE_STATUS)
            return f'<label class="fl"><span>{E(lbl)}</span><select name="{k}">{opts}</select></label>'
        if typ == "textarea":
            return f'<label class="fl"><span>{E(lbl)}</span><textarea name="{k}" placeholder="{E(ph)}">{v}</textarea></label>'
        if typ == "date":
            return f'<label class="fl"><span>{E(lbl)}</span><input type="date" name="{k}" value="{E(str(own.get(k, ""))[:10])}"></label>'
        mode = ' inputmode="numeric"' if typ == "number" else ""
        return f'<label class="fl"><span>{E(lbl)}</span><input name="{k}" value="{v}" placeholder="{E(ph)}"{mode}></label>'

    fl = {k: field(k, lbl, typ, ph) for k, lbl, typ, ph in dc.SALE_FIELDS}
    psale = dc.sale_info(r, cfg, private=True)
    pf = {k: field(k, lbl, typ, ph) for k, lbl, typ, ph in dc.SALE_PRIVATE_FIELDS}
    log = "".join(f'<div style="display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid var(--line)">'
                  f'{st_badge(x.get("status", ""))}<span class="mut mono" style="font-size:12px">{E(x.get("at", ""))}</span></div>'
                  for x in reversed(psale.get("status_log") or []))
    sale_form = (f'<div class="two">{fl["status"]}{fl["price"]}</div>' +
                 "".join(fl[k] for k in ("condition", "warranty", "includes", "shipping", "payment", "contact", "details")) +
                 f'<h4 style="margin:16px 0 8px">🔒 {_("ข้อมูลส่วนตัว")} <small class="mut" style="font-weight:400">{_("ไม่แสดงในโพสต์/รายงาน")}</small></h4>'
                 f'<div class="two">{pf["cost"]}{pf["sold_price"]}</div><div class="two">{pf["buyer"]}{pf["sold_at"]}</div>' +
                 (f'<p class="mut" style="margin:0 0 12px">{_("กำไร") if psale.get("sold_price") else _("กำไร (ถ้าขายได้ตามราคาตั้ง)")}: '
                  f'<b style="color:{"var(--ok)" if profit >= 0 else "var(--crit)"}">{_("{v:,.0f} บาท", v=profit)}</b></p>'
                  if (profit := dc.profit_of(psale)) is not None else ""))
    detail["disk_no"] = r["no"]
    variants = dc.post_variants(detail, sale, cfg, r["no"])
    texts_all = {}
    for lg in LANGS:  # ข้อความสำเร็จรูปทั้งสองภาษา สลับดูได้ / ready-made texts in every language
        with using(lg):
            texts_all[lg] = {k: x for k, (_t, x) in dc.post_variants(detail, sale, cfg, r["no"]).items()}
    plang = out_lang(cfg)
    lang_chips = "".join(f'<button class="tab{" on" if lg == plang else ""}" data-l="{lg}" onclick="plang(this)">{lbl}</button>'
                         for lg, lbl in LANGS.items())
    tabs = "".join(f'<button class="tab{" on" if i == 0 else ""}" data-k="{k}" onclick="pick(this)">{E(_(t))}</button>'
                   for i, (k, (t, _x)) in enumerate(variants.items()))
    texts_js = json.dumps(texts_all, ensure_ascii=False).replace("</", "<\\/")
    plugged = next((x for x in dc.list_disks() if x["serial"].lower() == r["serial"].lower()), None)
    recheck = ""
    if plugged:
        j = running_job(plugged["name"])
        if j:
            recheck = f'<div class="links"><a class="btn pri" href="/job/{j["id"]}">⏳ {_("กำลังตรวจอยู่ — ดูความคืบหน้า")}</a></div>'
        elif can_check():
            recheck = (f'<h4 style="margin:18px 0 8px;color:var(--mut)">🔁 {_("ตรวจใหม่ (เสียบอยู่ที่ {dev})", dev=E(plugged["dev"]))}</h4>'
                       f'<form method="post" action="/check" class="chk"><input type="hidden" name="name" value="{E(plugged["name"])}">'
                       f'{level_select()}<button class="btn pri">▶ {_("ตรวจใหม่")}</button></form>')
        else:
            recheck = (f'<p class="mut" style="margin-top:14px">'
                       f'{_("ดิสก์นี้เสียบอยู่ที่ {dev} · เปิดเว็บด้วย {cmd} เพื่อสั่งตรวจใหม่", dev=E(plugged["dev"]), cmd="<code>sudo ./web.sh</code>")}</p>')
    imgs = [("card.png", f"/files/{r['no']}/card.png")] if (d["folder"] / "card.png").exists() else []
    imgs += [(p.name, f"/files/{r['no']}/photos/{urllib.parse.quote(p.name)}") for p in d["photos"]]
    post_imgs = "".join(f'<a href="{u}" download="{E(n)}" target="_blank"><img src="{u}" loading="lazy" alt=""><span>{i}</span></a>'
                        for i, (n, u) in enumerate(imgs[:10], 1))
    tj = json.dumps({"shrink": _("กำลังย่อรูป..."), "uploading": _("กำลังอัปโหลด {p}%"), "done": _("เสร็จแล้ว ✔"),
                     "error": _("ผิดพลาด: "), "upfail": _("อัปโหลดไม่สำเร็จ")}, ensure_ascii=False).replace("</", "<\\/")
    return page(f"#{r['no']} {r['model']}", f"""
<div class="top"><a class="btn" href="/">← {_("ทั้งหมด")}</a><span class="mut">{_("ตรวจล่าสุด {t}", t=E(r['last_tested'] or ''))}</span></div>
{flash}
<div class="card"><div class="disk-top"><div class="grade {gcls}">{E(g)}<small>{E(_(r['last_grade_label'] or ''))}</small></div>
<div class="disk-title"><div class="brand">#{r['no']} · {E(_(r['brand'] or ''))}{(' · ' + E(r['series'])) if r['series'] else ''}{(' · 📦 ' + E(r['external'])) if r['external'] else ''}</div>
<h3>{E(r['model'] or '')}</h3><div class="mono mut">S/N {E(r['serial'])} · {E(r['type'] or '')}</div></div></div>
<div class="stats" style="margin-top:34px">{stats_html}</div>
<div class="links"><a class="btn pri" href="/files/{r['no']}/report.html" target="_blank">📄 {_("รายงานเต็ม")}</a>
<a class="btn" href="/files/{r['no']}/card.png" target="_blank">🖼 {_("การ์ด")}</a>
<a class="btn" href="/files/{r['no']}/data.json" target="_blank">{{ }} JSON</a>
<a class="btn" href="/disk/{r['no']}/zip">⬇ {_("ZIP ทั้งชุด")}</a>
<button class="btn" onclick="copyListing()">📋 {_("คัดลอกข้อความ")}</button></div>{recheck}</div>

<h2>{_("สถานะ")}</h2>
<div class="card">{status_bar(r, psale)}
{f'<details style="margin-top:12px"><summary class="mut" style="cursor:pointer">{_("ประวัติสถานะ")}</summary>{log}</details>' if log else ''}</div>

{warranty_box(con, r, psale)}

{label_box(r, detail, sale)}

<h2>{_("รูปดิสก์ ({n})", n=len(d['photos']))}</h2>
<div class="card">
<div class="up" id="drop"><div>📷 {_("ถ่ายรูปหรือเลือกจากเครื่อง")}<br><small class="mut">{_("ระบบย่อรูปให้อัตโนมัติ · ตั้งชื่อเป็น {serial}_ลำดับ", serial=E(r['serial']))}</small></div>
<div class="row"><label class="btn pri">📸 {_("ถ่ายรูป")}<input type="file" accept="image/*" capture="environment" multiple onchange="up(this.files)"></label>
<label class="btn">🖼 {_("เลือกรูป")}<input type="file" accept="image/*" multiple onchange="up(this.files)"></label></div>
<div class="prog" id="prog"><i id="bar"></i></div><div class="mut" id="st" style="margin-top:8px"></div></div>
<div style="height:16px"></div><div class="gallery">{photos}</div></div>

<h2>{_("ข้อมูลการขาย")}</h2>
<div class="card"><form method="post" action="/disk/{r['no']}/sale">{sale_form}
<button class="btn pri" style="width:100%;justify-content:center">💾 {_("บันทึกข้อมูลการขาย")}</button></form></div>

<h2>{_("ข้อความสำเร็จรูป")}</h2>
<div class="card"><div class="tabs" id="plangs" style="margin-bottom:8px">{lang_chips}</div><div class="tabs" id="tabs">{tabs}</div>
<textarea id="post" class="post" style="margin-top:12px"></textarea>
<div class="links" style="margin-top:10px"><button class="btn pri" onclick="copyText(document.getElementById('post').value)">📋 {_("คัดลอกข้อความนี้")}</button>
<a class="btn" href="/files/{r['no']}/posts.txt" target="_blank">📄 {_("posts.txt (ทุกแบบ)")}</a></div>
<p class="mut" style="font-size:12.5px">{_("แก้ข้อความในกล่องได้ก่อนคัดลอก (ไม่บันทึก) · ข้อความสร้างใหม่อัตโนมัติเมื่อตรวจใหม่หรือแก้ข้อมูลการขาย")}</p></div>

<h2>{_("โพสต์ลง Facebook / Marketplace")}</h2>
<div class="card"><div class="steps">
<div><span>{_("เลือกแบบข้อความด้านบน แล้วกด 📋 คัดลอก")}</span></div>
<div><span>{_("บันทึกรูปลงเครื่อง")} <small class="mut">{_("(แตะรูปค้างไว้ → บันทึกรูปภาพ)")}</small></span></div>
<div><a class="btn" href="https://m.facebook.com/" target="_blank">{_("เปิด Facebook")}</a>
<a class="btn" href="https://www.facebook.com/marketplace/create/item" target="_blank">{_("ลง Marketplace")}</a></div>
<div><span>{_("วางข้อความ แนบรูป แล้วโพสต์")}</span></div></div>
<div class="imgs">{post_imgs}</div></div>

<h2>{_("ดิสก์ภายนอก")}</h2>
<div class="card"><form method="post" action="/disk/{r['no']}/external">
<label class="fl"><span>{_("รุ่นภายนอก — ถ้าเป็นดิสก์สำเร็จรูป เช่น WD My Book (ปล่อยว่าง = ดิสก์เปล่า/ใส่กล่องทั่วไป)")}</span>
<input name="external" list="extlist" value="{E(r['external'] or '')}" placeholder="{E(_("ระบบตรวจให้เองตอนเสียบ แก้ได้ที่นี่"))}"></label>
<datalist id="extlist">{''.join(f'<option value="{E(x)}">' for x in sorted({lbl for _p, lbl in dc.EXTERNAL_LINES if '{' not in lbl}))}</datalist>
<button class="btn">{_("บันทึก")}</button> <span class="mut" style="font-size:12.5px">{E(_('ชื่อขายจะเป็น "รุ่นภายนอก + ความจุ" และบอกรุ่นดิสก์ข้างในให้ด้วย'))}</span></form></div>

<h2>{_("บันทึกส่วนตัว")}</h2>
<div class="card"><form method="post" action="/disk/{r['no']}/note">
<textarea name="note" placeholder="{E(_("เห็นเฉพาะคุณ เช่น ทุน, ที่เก็บ, ตำหนิที่ต้องจำ"))}">{E(r['note'] or '')}</textarea>
<div style="margin-top:10px"><button class="btn">{_("บันทึก")}</button></div></form></div>

<h2>{_("ประวัติการตรวจ ({n} ครั้ง)", n=len(hist))}</h2>
<div class="card" style="overflow-x:auto"><table><tr><th>{_("เลขที่")}</th><th>{_("วันที่")}</th><th>{_("เกรด")}</th><th>{_("ชม.ใช้งาน")}</th><th>{_("เสีย")}</th></tr>{hrows}</table></div>
<script>
const T={tj};
async function shrink(f){{  // ย่อรูปในมือถือก่อนส่ง (ด้านยาวสุด 1920px, JPEG 85%)
  try{{const b=await createImageBitmap(f,{{imageOrientation:'from-image'}});const m=1920,k=Math.min(1,m/Math.max(b.width,b.height));
    const c=document.createElement('canvas');c.width=Math.round(b.width*k);c.height=Math.round(b.height*k);
    c.getContext('2d').drawImage(b,0,0,c.width,c.height);
    const blob=await new Promise(r=>c.toBlob(r,'image/jpeg',0.85));
    return blob?new File([blob],f.name.replace(/\\.[^.]+$/,'')+'.jpg',{{type:'image/jpeg'}}):f;}}catch(e){{return f;}}
}}
async function up(files){{
  if(!files.length)return;const st=document.getElementById('st'),pg=document.getElementById('prog'),bar=document.getElementById('bar');
  pg.style.display='block';st.textContent=T.shrink;const fd=new FormData();
  for(const f of files)fd.append('photo',await shrink(f));
  const x=new XMLHttpRequest();x.open('POST','/disk/{r['no']}/upload');
  x.upload.onprogress=e=>{{if(e.lengthComputable){{bar.style.width=(e.loaded/e.total*100)+'%';st.textContent=T.uploading.replace('{{p}}',Math.round(e.loaded/e.total*100));}}}};
  x.onload=()=>{{st.textContent=x.status==200?T.done:T.error+x.responseText;if(x.status==200)location.reload();}};
  x.onerror=()=>st.textContent=T.upfail;x.send(fd);
}}
async function copyListing(){{copyText(await (await fetch('/files/{r['no']}/listing.txt')).text());}}
const TEXTS={texts_js};let PL='{plang}';
function plang(b){{PL=b.dataset.l;document.querySelectorAll('#plangs .tab').forEach(x=>x.classList.toggle('on',x===b));
  pick(document.querySelector('#tabs .tab.on')||document.querySelector('#tabs .tab'));}}
function pick(b){{document.querySelectorAll('#tabs .tab').forEach(x=>x.classList.toggle('on',x===b));
  const t=document.getElementById('post');t.value=TEXTS[PL][b.dataset.k];t.style.height='auto';t.style.height=(t.scrollHeight+4)+'px';}}
pick(document.querySelector('#tabs .tab'));
{copy_js()}
</script>""")


COPY_JS = """
function copyText(t){  // ใช้ได้ทั้ง http ในวง LAN (ไม่มี clipboard API) และ https
  if(navigator.clipboard&&window.isSecureContext){
    return navigator.clipboard.writeText(t).then(()=>toast(CT.copied)).catch(()=>copyOld(t));}
  copyOld(t);
}
function copyOld(t){
  const a=document.createElement('textarea');a.value=t;a.setAttribute('readonly','');a.style.position='fixed';a.style.opacity='0';
  document.body.appendChild(a);a.select();a.setSelectionRange(0,a.value.length);
  let ok=false;try{ok=document.execCommand('copy');}catch(e){}
  document.body.removeChild(a);ok?toast(CT.copied):prompt(CT.copythis,t);
}
function toast(m){let t=document.getElementById('toast');if(!t){t=document.createElement('div');t.id='toast';document.body.appendChild(t);}
  t.textContent=m;t.className='show';setTimeout(()=>t.className='',1600);}
"""


def copy_js():
    """COPY_JS + ข้อความที่แปลแล้ว / with translated toast texts"""
    ct = json.dumps({"copied": _("คัดลอกแล้ว ✔"), "copythis": _("คัดลอกข้อความนี้")}, ensure_ascii=False).replace("</", "<\\/")
    return "const CT=" + ct + ";" + COPY_JS


GRADE_RANK = {"A": 0, "B": 1, "C": 2, "F": 3}


def warranty_badge(w):
    if not w["sold"]:
        return ""
    if w["days"] is None:
        return f'<span class="stb" style="--c:#94a3b8">🛡 {_("ไม่ได้ระบุระยะประกัน")}</span>'
    if w["days"] == 0:
        return f'<span class="stb" style="--c:#94a3b8">{_("ไม่มีประกัน")}</span>'
    if w["active"]:
        return f'<span class="stb" style="--c:#22c55e">🛡 {_("ในประกัน · เหลือ {n} วัน", n=w["left"])}</span>'
    return f'<span class="stb" style="--c:#ef4444">{_("หมดประกันแล้ว ({d})", d=E(w["until"]))}</span>'


def test_at_sale(con, no, sold_at):
    """ผลตรวจล่าสุดก่อน/ตอนขาย (ไว้เทียบเวลาลูกค้าส่งเคลม)"""
    cut = sold_at if len(sold_at or "") > 10 else f"{sold_at} 23:59"
    return (con.execute("SELECT * FROM tests WHERE disk_no=? AND tested_at<=? ORDER BY id DESC LIMIT 1", (no, cut)).fetchone()
            or con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id LIMIT 1", (no,)).fetchone())


def compare_tests(a, b):
    """ตารางเทียบผลตรวจตอนขาย (a) กับล่าสุด (b) — แย่ลงขึ้นสีแดง"""
    fmt = lambda v: "-" if v is None else (f"{v:,.0f}" if isinstance(v, (int, float)) else E(str(v)))

    def cell(old, new, worse):
        bad = worse(old, new) if old is not None and new is not None else False
        style = ' style="color:var(--crit);font-weight:800"' if bad else ""
        return f"<td>{fmt(old)}</td><td{style}>{fmt(new)}{' ⚠' if bad else ''}</td>"
    higher = lambda o, n: (n or 0) > (o or 0)
    rows = [(_("วันที่ตรวจ"), "tested_at", lambda o, n: False),
            (_("เกรด"), "grade", lambda o, n: GRADE_RANK.get(n, 9) > GRADE_RANK.get(o, 9)),
            (_("ชั่วโมงใช้งาน"), "power_on_hours", lambda o, n: False),
            (_("เซกเตอร์เสีย (reallocated)"), "reallocated", higher), (_("รอย้าย (pending)"), "pending", higher),
            (_("อ่านไม่ได้ (uncorrectable)"), "offline_unc", higher), (_("CRC (สาย/กล่อง)"), "crc", higher),
            (_("สแกนเจอจุดอ่านไม่ได้"), "scan_unreadable", higher),
            (_("ความเร็วอ่านเฉลี่ย MB/s"), "read_mbps_avg", lambda o, n: n < o * 0.8)]
    body = "".join(f'<tr><td class="mut">{E(lbl)}</td>{cell(a[k], b[k], w)}</tr>' for lbl, k, w in rows)
    worse = any(w(a[k], b[k]) for _l, k, w in rows if a[k] is not None and b[k] is not None)
    head = dc.pill("crit", _("ผลตรวจแย่ลงจากตอนขาย")) if worse else dc.pill("ok", _("ไม่แย่ลงจากตอนขาย"))
    return (f'<div style="margin:10px 0">{head}</div><div style="overflow-x:auto"><table><tr><th></th>'
            f'<th>{_("ตอนขาย (#{id})", id=a["id"])}</th><th>{_("ล่าสุด (#{id})", id=b["id"])}</th></tr>{body}</table></div>')


def warranty_box(con, r, psale):
    claims = psale.get("claims") or []
    if not psale.get("sold_at") and not claims:
        return ""
    w = dc.warranty_info(psale)
    note = (_(" · ประกัน {w} (ถึง {d})", w=E(psale.get("warranty", "")), d=E(w["until"])) if w["until"] else
            E(_(' · กรอกช่อง "ประกัน" เช่น 30 วัน / 3 เดือน เพื่อให้นับวันให้')) if w["days"] is None else "")
    parts = [f'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">{warranty_badge(w)}</div>',
             f'<p class="mut" style="font-size:13px">{_("ขายเมื่อ {d}", d=E(w["sold"] or "-"))}{note}'
             + (_(" · ผู้ซื้อ {b}", b=E(psale["buyer"])) if psale.get("buyer") else "") + "</p>"]
    if w["sold"]:
        a = test_at_sale(con, r["no"], psale["sold_at"])
        b = con.execute("SELECT * FROM tests WHERE disk_no=? ORDER BY id DESC LIMIT 1", (r["no"],)).fetchone()
        if a and b and a["id"] != b["id"]:
            parts.append(f"<h4 style='margin:14px 0 4px'>{_('เทียบผลตรวจ ตอนขาย ↔ ล่าสุด')}</h4>" + compare_tests(a, b))
        elif a:
            parts.append(f'<p class="mut" style="font-size:13px">'
                         f'{_("ผลตรวจตอนขาย: ครั้งที่ #{id} ({t}) · ถ้าลูกค้าส่งเคลม เสียบตรวจใหม่ ระบบจะเทียบกับตอนขายให้", id=a["id"], t=E(a["tested_at"]))}</p>')
    if claims:
        parts.append(f"<h4 style='margin:14px 0 6px'>{_('ประวัติเคลม')}</h4>" + "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid var(--line)"><span class="mut mono" style="font-size:12px">'
            f'{E(c.get("at", ""))}</span><div style="white-space:pre-wrap">{E(c.get("text", ""))}</div></div>'
            for c in reversed(claims)))
    parts.append(
        f'<form method="post" action="/disk/{r["no"]}/claim" style="margin-top:12px">'
        f'<textarea name="text" placeholder="{E(_("บันทึกเคลม: อาการที่ลูกค้าแจ้ง / ผลตรวจ / การจัดการ (เปลี่ยน คืนเงิน ซ่อม)"))}"></textarea>'
        '<label style="display:flex;gap:8px;align-items:center;margin:8px 0;font-size:13.5px">'
        f'<input type="checkbox" name="set_status" value="1"> {_("เปลี่ยนสถานะเป็น ↩️ {st}", st=E(_("เคลม/ตีกลับ")))}</label>'
        f'<button class="btn">{_("บันทึกเคลม")}</button></form>')
    return f'<h2 id="warranty">🛡 {_("ประกัน / เคลม")}</h2><div class="card">{"".join(parts)}</div>'


def label_fields(r, detail, sale):
    """ข้อมูลสำหรับพิมพ์ฉลาก — [(ป้าย, ค่า)]"""
    s = detail.get("smart", {})
    cap = dc.human(r["capacity_bytes"]).replace(".0 ", " ")
    series = (r["series"] or "").split(" (")[0]
    name = dc.product_name(r["brand"], r["series"], cap, r["external"])
    hrs = s.get("hours")
    rows = [(_("รหัส"), f"#{r['no']}"), ("Serial", r["serial"]), (_("ยี่ห้อ"), _(r["brand"] or "")),
            (_("รุ่นภายนอก"), r["external"] or ""), (_("ซีรีส์"), series),
            (_("รุ่น"), r["model"] or ""), (_("ความจุ"), cap), (_("ชนิด"), r["type"] or ""),
            (_("แบบ"), dc.ext_kind(r["type"]) if r["external"] else (lambda f: f if f != r["type"] else "")(dc.form_label(r["type"], r["form_factor"], r["model"]))),
            (_("เกรด"), f"{r['last_grade']} ({_(r['last_grade_label'])})"),
            (_("ชั่วโมงใช้งาน"), _("{h:,} ชม.", h=hrs) if hrs is not None else ""),
            (_("ราคา"), dc.fmt_price(sale["price"]) if sale.get("price") else ""),
            (_("ตรวจเมื่อ"), (r["last_tested"] or "")[:10])]
    combo = [
        (_("ฉลาก 1 บรรทัด"), " | ".join(filter(None, [f"#{r['no']}", name, f"S/N {r['serial']}", _("เกรด {g}", g=r['last_grade']),
                                                         f"{hrs:,}h" if hrs is not None else ""]))),
        (_("ฉลาก 3 บรรทัด"), "\n".join([f"#{r['no']} {name}", f"S/N {r['serial']}",
                                          " · ".join(filter(None, [_("เกรด {g}", g=r['last_grade']), _("{h:,} ชม.", h=hrs) if hrs is not None else "",
                                                                   dc.fmt_price(sale["price"]) if sale.get("price") else ""]))])),
        (_("Serial + รุ่น"), f"{r['serial']} {r['model'] or ''}".strip()),
    ]
    return [(k, v) for k, v in rows if v], combo


def label_box(r, detail, sale):
    rows, combo = label_fields(r, detail, sale)
    cells = "".join(f'<button class="cp" data-v="{E(v)}" onclick="copyText(this.dataset.v)"><span>{E(k)}</span>'
                    f'<b>{E(v)}</b><i>📋</i></button>' for k, v in rows)
    combos = "".join(f'<button class="cp wide" data-v="{E(v)}" onclick="copyText(this.dataset.v)"><span>{E(k)}</span>'
                     f'<b style="white-space:pre-line">{E(v)}</b><i>📋</i></button>' for k, v in combo)
    return (f'<h2>{_("ข้อมูลสำหรับพิมพ์ฉลาก")}</h2><div class="card"><p class="mut" style="margin:0 0 10px;font-size:12.5px">'
            f'{_("แตะช่องไหนก็ได้เพื่อคัดลอก")}</p><div class="cps">{cells}</div><div class="cps" style="margin-top:10px">{combos}</div></div>')


def lang_opts(cur):
    return "".join(f'<option value="{k}"{" selected" if k == cur else ""}>{v}</option>' for k, v in LANGS.items())


def settings_page(cfg, msg=""):
    sd = cfg.get("sale_defaults") or {}
    fields = "".join(
        f'<label class="fl"><span>{E(_(lbl))}</span>'
        + (f'<textarea name="d_{k}" placeholder="{E(_(ph))}">{E(sd.get(k, ""))}</textarea>' if typ == "textarea" else
           f'<input name="d_{k}" value="{E(sd.get(k, ""))}" placeholder="{E(_(ph))}">') + '</label>'
        for k, lbl, typ, ph in dc.SALE_FIELDS if k in ("warranty", "includes", "shipping", "payment", "contact", "details"))
    flash = f'<div class="card" style="border-color:var(--ok)">{E(msg)}</div>' if msg else ""
    return page(_("ตั้งค่า"), f"""
<div class="top"><a class="btn" href="/">← {_("ทั้งหมด")}</a><b>⚙️ {_("ตั้งค่า")}</b></div>{flash}
<form method="post" action="/settings">
<h2>{_("ร้าน")}</h2><div class="card">
<label class="fl"><span>{_("ชื่อร้าน (แสดงบนการ์ดและรายงาน)")}</span><input name="shop" value="{E(cfg.get('shop', ''))}"></label>
<label class="fl"><span>{_("แฮชแท็กท้ายโพสต์")}</span><input name="fb_hashtags" value="{E(cfg.get('fb_hashtags', ''))}" placeholder="{E(_("#HDD #ฮาร์ดดิสก์ #6amdev"))}"></label>
<label class="fl"><span>{_("PIN เข้าเว็บ")}</span><input name="web_pin" value="{E(str(cfg.get('web_pin', '')))}" inputmode="numeric"></label></div>
<h2>{_("ภาษา")} / Language</h2><div class="card"><div class="two">
<label class="fl"><span>{_("ภาษาเริ่มต้นของเว็บและ terminal")}</span><select name="lang">{lang_opts(cfg.get("lang") or "th")}</select></label>
<label class="fl"><span>{_("ภาษาของรายงาน การ์ด และไฟล์โพสต์")}</span><select name="output_lang">{lang_opts(out_lang(cfg))}</select></label></div>
<p class="mut" style="margin:0;font-size:12.5px">{_("ปุ่ม TH / EN มุมบนขวาเปลี่ยนภาษาเว็บเฉพาะเครื่องที่กด · ข้อความสำเร็จรูปในหน้าดิสก์สลับดูได้ทั้งสองภาษา")}</p></div>
<h2>{_("ค่าเริ่มต้นของข้อมูลการขาย")}</h2>
<p class="mut">{_("ใช้กับทุกลูกที่ยังไม่ได้กรอกช่องนั้นเอง")}</p><div class="card">{fields}</div>
<button class="btn pri" style="width:100%;justify-content:center;margin-top:8px">💾 {_("บันทึกและอัปเดตทุกลูก")}</button></form>
<script>{copy_js()}</script>""")


def catalog_page(con, cfg):
    text = dc.catalog_text(con, cfg)
    return page(_("ข้อความรวมสินค้า"), f"""
<div class="top"><a class="btn" href="/">← {_("ทั้งหมด")}</a><b>📦 {_("ข้อความรวมสินค้า")}</b></div>
<p class="mut">{_("รวมทุกลูกที่ยังมีของ (เตรียมขาย / พร้อมขาย / กำลังขาย / จองแล้ว) ไว้โพสต์รวมในกลุ่ม · อัปเดตอัตโนมัติ")}</p>
<div class="card"><textarea id="post" class="post" style="min-height:360px">{E(text)}</textarea>
<div class="links"><button class="btn pri" onclick="copyText(document.getElementById('post').value)">📋 {_("คัดลอก")}</button></div></div>
<script>{copy_js()}</script>""")


def disk_name(r):
    return dc.product_name(r["brand"], r["series"], dc.human(r["capacity_bytes"]).replace(".0 ", " "), r["external"])


def sales_page(con, cfg):
    """สรุปยอดขาย / ทุน / กำไร"""
    rows = con.execute("SELECT * FROM disks ORDER BY no").fetchall()
    baht = lambda v: "-" if v is None else f"{v:,.0f}"
    stock = {"n": 0, "cost": 0.0, "price": 0.0, "no_cost": 0, "no_price": 0}
    months, sold = {}, []
    for r in rows:
        sale = dc.sale_info(r, cfg, private=True)
        grp = dc.status_meta(sale.get("status"))["group"]
        cost, price = dc.num_or(sale.get("cost")), dc.num_or(sale.get("price"))
        if grp in ("prep", "sell", "hold"):
            stock["n"] += 1
            stock["cost"] += cost or 0
            stock["price"] += price or 0
            stock["no_cost"] += cost is None
            stock["no_price"] += price is None
        elif dc.is_sold(sale):
            got = dc.num_or(sale.get("sold_price")) or price
            profit = dc.profit_of(sale)
            m = (sale.get("sold_at") or _("ไม่ทราบวันที่"))[:7]
            x = months.setdefault(m, {"n": 0, "rev": 0.0, "cost": 0.0, "profit": 0.0, "no_cost": 0})
            x["n"] += 1
            x["rev"] += got or 0
            x["cost"] += cost or 0
            x["profit"] += profit or 0
            x["no_cost"] += cost is None
            sold.append((sale.get("sold_at") or "", r, got, cost, profit, dc.warranty_info(sale), sale))
    k = lambda v, l: f'<div class="kpi"><b>{v}</b><span>{l}</span></div>'
    tot_rev = sum(x["rev"] for x in months.values())
    tot_profit = sum(x["profit"] for x in months.values())
    in_war = sum(1 for *_r, w, _s in sold if w["active"])
    kpis = (k(stock["n"], _("ยังมีของ (ลูก)")) + k(baht(stock["cost"]), _("ทุนจมอยู่ (บาท)")) +
            k(baht(stock["price"] - stock["cost"]), _("กำไรที่คาดไว้ (บาท)")) + k(len(sold), _("ขายแล้ว (ลูก)")) +
            k(baht(tot_rev), _("ยอดขายรวม (บาท)")) + k(baht(tot_profit), _("กำไรรวม (บาท)")) + k(in_war, _("ยังอยู่ในประกัน (ลูก)")))
    warn = []
    if stock["no_cost"]:
        warn.append(_("ของในคลัง {n} ลูกยังไม่กรอกทุน", n=stock['no_cost']))
    if stock["no_price"]:
        warn.append(_("{n} ลูกยังไม่ตั้งราคา", n=stock['no_price']))
    nc = sum(x["no_cost"] for x in months.values())
    if nc:
        warn.append(_("ที่ขายแล้ว {n} ลูกไม่มีทุน (ไม่นับในกำไร)", n=nc))
    mrows = "".join(f'<tr><td>{E(m)}</td><td class="num">{x["n"]}</td><td class="num">{baht(x["rev"])}</td>'
                    f'<td class="num">{baht(x["cost"])}</td><td class="num"><b>{baht(x["profit"])}</b></td></tr>'
                    for m, x in sorted(months.items(), reverse=True))
    srows = "".join(f'<tr><td>{E(at[:10]) or "-"}</td><td><a href="/disk/{r["no"]}">#{r["no"]}</a> {E(disk_name(r))}'
                    f'<br><small class="mut">{E(sale.get("buyer") or "")}</small></td><td class="num">{baht(got)}</td>'
                    f'<td class="num">{baht(cost)}</td><td class="num"><b>{baht(profit)}</b></td><td>{warranty_badge(w)}</td></tr>'
                    for at, r, got, cost, profit, w, sale in sorted(sold, key=lambda x: x[0], reverse=True))
    return page(_("สรุปยอดขาย"), f"""
<div class="top"><a class="btn" href="/">← {_("ทั้งหมด")}</a><b>📊 {_("สรุปยอดขาย")}</b></div>
<div class="kpis">{kpis}</div>
{('<div class="card" style="border-color:var(--warn)">⚠ ' + ' · '.join(warn) + '</div>') if warn else ''}
<h2>{_("รายเดือน")}</h2><div class="card" style="overflow-x:auto"><table><tr><th>{_("เดือน")}</th><th>{_("ลูก")}</th><th>{_("ยอดขาย")}</th><th>{_("ทุน")}</th><th>{_("กำไร")}</th></tr>
{mrows or f'<tr><td colspan="5" class="mut">{_("ยังไม่มีที่ขาย")}</td></tr>'}</table></div>
<h2>{_("รายการที่ขายแล้ว")}</h2><div class="card" style="overflow-x:auto"><table><tr><th>{_("วันที่ขาย")}</th><th>{_("ดิสก์")}</th><th>{_("ขายได้")}</th><th>{_("ทุน")}</th><th>{_("กำไร")}</th><th>{_("ประกัน")}</th></tr>
{srows or f'<tr><td colspan="6" class="mut">{_("ยังไม่มีที่ขาย")}</td></tr>'}</table></div>
<p class="mut" style="font-size:12.5px">{_("ขายได้ = ราคาที่ขายได้จริง (ถ้าไม่กรอกใช้ราคาตั้ง) · นับเฉพาะสถานะ ขายแล้ว / ส่งแล้ว · เดือนนับจากวันที่ขาย")}</p>""")


def claim_page(con, cfg, q=""):
    """ค้นดิสก์ที่ลูกค้าส่งเคลม จาก Serial / # / ชื่อผู้ซื้อ"""
    q = q.strip()
    res = []
    if q:
        ql = q.lower().lstrip("#")
        for r in con.execute("SELECT * FROM disks ORDER BY no DESC").fetchall():
            sale = dc.sale_info(r, cfg, private=True)
            hay = f"{r['serial']} {sale.get('buyer', '')} {r['model']}".lower()
            if ql == str(r["no"]) or ql.replace(" ", "") in hay.replace(" ", ""):
                res.append((r, sale))
    items = "".join(
        f'<a class="item" href="/disk/{r["no"]}#warranty"><div class="it"><small>#{r["no"]} · {st_badge(sale.get("status"))}</small>'
        f'<b>{E(disk_name(r))}</b><small>S/N {E(r["serial"])} · {E(r["model"] or "")}</small>'
        f'<small>{_("ขายเมื่อ {d}", d=E((sale.get("sold_at") or "-")[:10]))}{" · " + E(sale["buyer"]) if sale.get("buyer") else ""}'
        f'{_(" · เคลมแล้ว {n} ครั้ง", n=len(sale["claims"])) if sale.get("claims") else ""}</small>'
        f'<div style="margin-top:5px">{warranty_badge(dc.warranty_info(sale)) or "<span class=mut>" + _("ยังไม่ได้ขาย") + "</span>"}</div></div>'
        f'{grade_mini(r["last_grade"])}</a>' for r, sale in res)
    nf = _('ไม่พบ "{q}"', q=E(q))
    empty = f'<div class="empty">{nf}<br>{_("ลองพิมพ์บางส่วนของ Serial ก็ได้")}</div>' if q else ""
    return page(_("ประกัน / เคลม"), f"""
<div class="top"><a class="btn" href="/">← {_("ทั้งหมด")}</a><b>🛡 {_("ประกัน / เคลม")}</b></div>
<form method="get" action="/claim" style="margin-top:14px"><div class="sbox"><input class="search" name="q" value="{E(q)}" autofocus
placeholder="{E(_("🔍 Serial ที่ติดบนดิสก์ / #รหัส / ชื่อผู้ซื้อ"))}" autocomplete="off" enterkeyhint="search"></div></form>
<p class="mut" style="font-size:13px">{_("ลูกค้าส่งเคลม → ค้น Serial → ดูว่ายังอยู่ในประกันไหม → เสียบตรวจใหม่ ระบบจะเทียบกับผลตรวจตอนขายให้")}</p>
<div class="list">{items or empty}</div>""")


def pin_page(err=""):
    return page(_("เข้าสู่ระบบ"), f"""<div class="pin"><h1>🩺 Disk Check</h1><p class="mut">{_("ใส่ PIN (ดูได้ในเครื่องที่รันเว็บ)")}</p>
<form method="post" action="/login"><input name="pin" inputmode="numeric" autocomplete="off" autofocus maxlength="12">
<button class="btn pri" style="width:100%;justify-content:center">{_("เข้า")}</button></form>
{f'<p style="color:var(--crit)">{E(err)}</p>' if err else ''}</div>""")


def out_lang(cfg):
    """ภาษาของไฟล์ที่สร้าง (รายงาน การ์ด โพสต์) / language of generated files"""
    return norm_lang(cfg.get("output_lang")) or norm_lang(cfg.get("lang")) or "th"


def write_static_index(con, cfg):
    dc.DISKS_DIR.mkdir(exist_ok=True)
    p = dc.DISKS_DIR / "index.html"
    p.write_text(index_html(con, cfg, web=False), encoding="utf-8")
    c = dc.DISKS_DIR / "catalog.txt"
    c.write_text(dc.catalog_text(con, cfg), encoding="utf-8")
    dc.chown_user(p, c)
    return p


# ---------------------------------------------------------------------
#  เซิร์ฟเวอร์
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
#  สั่งตรวจจากเว็บ (ต้องเปิดเว็บด้วย sudo) — รัน diskcheck.py เป็นงานเบื้องหลัง
# ---------------------------------------------------------------------
import signal
import subprocess
import threading

JOBS = {}            # id → งานตรวจ
JOBS_LOCK = threading.Lock()
LEVELS = {           # ระดับ → (ชื่อ, เวลาโดยประมาณ, ตัวเลือกของ diskcheck.py) — ชื่อ/เวลาแปลตอนแสดงด้วย _()
    "quick": ("เร็ว", "~10 วินาที", ["--quick"]),
    "standard": ("มาตรฐาน", "3–5 นาที", []),
    "full": ("ละเอียด · สแกนเต็มจาน", "HDD 1TB ~2–3 ชม.", ["--full"]),
    "max": ("ละเอียดสุด · + self-test ยาว", "HDD 4TB 10 ชม.+", ["--full", "--selftest", "long"]),
}


def can_check():
    return dc.IS_ROOT and bool(dc.SMARTCTL)


def running_job(name):
    with JOBS_LOCK:
        return next((j for j in JOBS.values() if j["name"] == name and j["status"] == "running"), None)


def start_job(dev, level):
    if running_job(dev["name"]):
        return running_job(dev["name"])
    jid = f"{int(time.time())}{secrets.randbelow(1000):03d}"
    cmd = [sys.executable, str(dc.HERE / "diskcheck.py"), "--disk", dev["name"], "--yes", "--no-open",
           "--lang", get_lang(), *LEVELS[level][2]]
    env = {**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, cwd=str(dc.HERE), start_new_session=True)
    job = {"id": jid, "name": dev["name"], "dev": dev["dev"], "model": dev["model"], "serial": dev["serial"],
           "level": level, "status": "running", "lines": [], "current": "", "started": time.time(),
           "ended": None, "proc": proc, "disk_no": None, "test_id": None, "grade": None}
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=_pump, args=(job,), daemon=True).start()
    return job


def _pump(job):
    """อ่านผลจากโปรแกรมตรวจทีละตัวอักษร (บรรทัดความคืบหน้าใช้ \r)"""
    import io
    buf = ""
    out = io.TextIOWrapper(job["proc"].stdout, encoding="utf-8", errors="replace", newline="")  # newline="" = เก็บ \r ไว้
    while True:
        c = out.read(1)
        if not c:
            break
        if c == "\n":
            job["lines"].append(buf)
            job["lines"] = job["lines"][-400:]
            m = re.search(r"#(\d+)\s+\S+\s+(?:การตรวจ|[Tt]est) #(\d+)", buf)
            if m:
                job["disk_no"], job["test_id"] = int(m.group(1)), int(m.group(2))
            m = re.search(r"^\s*\[([ABCF?])\]", buf)
            if m:
                job["grade"] = m.group(1)
            buf = ""
            job["current"] = ""
        elif c == "\r":
            job["current"] = buf
            buf = ""
        else:
            buf += c
            job["current"] = buf
    rc = job["proc"].wait()
    job["ended"] = time.time()
    if job["status"] == "running":
        job["status"] = "done" if job["disk_no"] else ("failed" if rc not in (0, 1, 2) else "done")


def cancel_job(jid):
    j = JOBS.get(jid)
    if j and j["status"] == "running":
        j["status"] = "cancelled"
        try:
            os.killpg(j["proc"].pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def job_json(j):
    el = (j["ended"] or time.time()) - j["started"]
    return {"id": j["id"], "name": j["name"], "dev": j["dev"], "model": j["model"], "serial": j["serial"],
            "level": _(LEVELS[j["level"]][0]), "status": j["status"], "elapsed": round(el),
            "lines": j["lines"][-60:], "current": j["current"], "disk_no": j["disk_no"], "test_id": j["test_id"],
            "grade": j["grade"]}


def devices_json(con):
    """ดิสก์ที่เสียบอยู่ตอนนี้ (อ่านใหม่ทุกครั้ง — ถอด/เสียบ USB แล้วเห็นทันที)"""
    out = []
    for d in dc.list_disks():
        row = con.execute("SELECT no, last_grade, last_grade_label, last_tested FROM disks WHERE serial=? COLLATE NOCASE",
                          (d["serial"],)).fetchone() if d["serial"] not in ("", "-") else None
        j = running_job(d["name"])
        out.append({"name": d["name"], "dev": d["dev"], "model": d["model"], "brand": d.get("brand"),
                    "serial": d["serial"], "size": dc.human(d["size"]), "kind": d["kind"], "tran": d["tran"],
                    "system": d["system"], "usb": d["usb"],
                    "db_no": row["no"] if row else None, "db_grade": row["last_grade"] if row else None,
                    "db_tested": row["last_tested"] if row else None, "job": j["id"] if j else None,
                    "job_level": _(LEVELS[j["level"]][0]) if j else None,
                    "job_current": (j["current"] or (j["lines"][-1] if j["lines"] else "")).strip() if j else None,
                    "job_elapsed": round(time.time() - j["started"]) if j else None})
    return out


def api_state(con, since=0):
    on, lvl = auto_settings()
    return {"devices": devices_json(con), "can_check": can_check(), "auto": on, "auto_level": lvl,
            "events": [e for e in EVENTS if e["id"] > since], "last_event": EVENT_SEQ[0]}


# ---------- เฝ้าดูการเสียบ/ถอด แล้วตรวจอัตโนมัติ ----------
from collections import deque

EVENTS = deque(maxlen=50)   # เหตุการณ์ล่าสุด (เสียบ/ถอด/เริ่มตรวจ/เสร็จ)
EVENT_SEQ = [0]
WATCH = {"on": False}


def event(kind, msg, link=None):
    EVENT_SEQ[0] += 1
    EVENTS.append({"id": EVENT_SEQ[0], "t": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg, "link": link})
    print(dc.CYAN(f"  [{time.strftime('%H:%M:%S')}] {msg}"))


def auto_settings():
    cfg = Handler.cfg
    lvl = cfg.get("auto_level") if cfg.get("auto_level") in LEVELS else "standard"
    return bool(cfg.get("auto_check", True)), lvl


def watcher():
    """ดู lsblk ทุก 2 วินาที — ดิสก์ใหม่ที่อยู่นิ่งครบ 4 วินาที = เสียบแล้ว พร้อมตรวจ"""
    key = lambda d: f"{d['name']}|{d['serial']}|{d['size']}"
    present = {key(d): d for d in dc.list_disks()}      # ที่เสียบอยู่ก่อนเปิดเว็บ — ไม่ตรวจเอง
    pending = {}                                          # key → เวลาที่เห็นครั้งแรก
    watched_jobs = {}                                     # job id → แจ้งผลแล้วหรือยัง
    while True:
        time.sleep(2)
        try:
            cur = {key(d): d for d in dc.list_disks()}
        except Exception:
            continue
        for k in list(present):
            if k not in cur:
                d = present.pop(k)
                event("unplug", _("ถอด {dev} · {model}", dev=d['dev'], model=d['model']))
                j = running_job(d["name"])
                if j:
                    cancel_job(j["id"])
                    j["status"] = "failed"
                    event("error", _("การตรวจ {dev} หยุด — ดิสก์ถูกถอดระหว่างตรวจ", dev=d['dev']), f"/job/{j['id']}")
        for k, d in cur.items():
            if k in present:
                continue
            first = pending.setdefault(k, time.time())
            if time.time() - first < 4:
                continue
            pending.pop(k, None)
            present[k] = d
            event("plug", _("เสียบ {dev} · {brand} {model} · {size}", dev=d['dev'], brand=_(d.get('brand') or ''),
                            model=d['model'], size=dc.human(d['size'])))
            on, lvl = auto_settings()
            if on and not d["system"] and not running_job(d["name"]):
                j = start_job(d, lvl)
                watched_jobs[j["id"]] = False
                event("start", _("เริ่มตรวจอัตโนมัติ {dev} ({level})", dev=d['dev'], level=_(LEVELS[lvl][0])), f"/job/{j['id']}")
        for k in list(pending):
            if k not in cur:
                pending.pop(k)
        # แจ้งผลเมื่อการตรวจ (ทั้งอัตโนมัติและกดเอง) เสร็จ
        with JOBS_LOCK:
            jobs = list(JOBS.values())
        for j in jobs:
            if j["status"] != "running" and not watched_jobs.get(j["id"]):
                watched_jobs[j["id"]] = True
                if j["status"] == "done" and j["disk_no"]:
                    event("done", _("ตรวจเสร็จ {dev} → #{no} เกรด {g}", dev=j['dev'], no=j['disk_no'], g=j['grade'] or '?'),
                          f"/disk/{j['disk_no']}")
                elif j["status"] == "done":
                    event("error", _("ตรวจ {dev} เสร็จ แต่ไม่ได้บันทึก (อ่าน SMART ไม่ได้?)", dev=j['dev']), f"/job/{j['id']}")
                elif j["status"] == "cancelled":
                    event("error", _("ยกเลิกการตรวจ {dev}", dev=j['dev']), f"/job/{j['id']}")


def start_watcher():
    if can_check() and not WATCH["on"]:
        WATCH["on"] = True
        threading.Thread(target=watcher, daemon=True).start()


def level_select(name="level", default="standard"):
    return (f'<select name="{name}" class="lvl">' +
            "".join(f'<option value="{k}"{" selected" if k == default else ""}>{E(_(v[0]))} ({E(_(v[1]))})</option>'
                    for k, v in LEVELS.items()) + '</select>')


def need_root_box():
    why = _("ยังไม่ได้ติดตั้ง smartmontools (sudo apt install smartmontools)") if not dc.SMARTCTL else \
        _("เปิดเว็บแบบไม่ได้ใช้ sudo — ดูผล/อัปโหลดรูปได้ แต่สั่งตรวจไม่ได้")
    return (f'<div class="card" style="border-color:var(--warn)">⚠️ {E(why)}<br>'
            f'<span class="mut">{_("ปิดเว็บ (Ctrl+C) แล้วเปิดใหม่ด้วย")}</span> <code>sudo ./web.sh</code></div>')


def devices_page():
    warn = "" if can_check() else need_root_box()
    on, lvl = auto_settings()
    auto_box = "" if not can_check() else f"""
<div class="card auto"><label class="sw"><input type="checkbox" id="auto" {'checked' if on else ''} onchange="saveAuto()">
<span class="knob"></span><b>{_("ตรวจอัตโนมัติเมื่อเสียบดิสก์")}</b></label>
<div style="margin-top:10px">{level_select('auto_level', lvl).replace('<select', '<select id="alvl" onchange="saveAuto()"')}</div>
<p class="mut" style="margin:8px 0 0;font-size:12.5px">{_("ไม่ตรวจดิสก์ระบบ · ลูกที่เสียบอยู่ก่อนเปิดเว็บต้องกดตรวจเอง")}</p></div>"""
    tj = json.dumps({"saved": _("บันทึกแล้ว"), "nodisk": _("ไม่พบดิสก์ — เสียบ USB แล้วรอสักครู่"), "sysdisk": _("ดิสก์ระบบ"),
                     "dbline": _("#{no} · เกรด {g} · ตรวจ {d}"), "never": _("ยังไม่เคยตรวจ"), "checking": _("⏳ กำลังตรวจ · {level}"),
                     "starting": _("เริ่มต้น..."), "check": _("▶ ตรวจ")}, ensure_ascii=False).replace("</", "<\\/")
    return page(_("ดิสก์ที่เสียบอยู่"), f"""
<div class="top"><a class="btn" href="/">← {_("ทั้งหมด")}</a><b>🔌 {_("ดิสก์ที่เสียบอยู่")} <span class="live"></span></b></div>
{warn}{auto_box}
<div id="devs" class="list"><div class="empty">{_("กำลังโหลด...")}</div></div>
<h2>{_("เหตุการณ์ล่าสุด")}</h2><div class="card"><div id="ev" class="mut">{_("ยังไม่มี — ลองเสียบหรือถอด USB")}</div></div>
<script>
const T={tj};
const LV=`{level_select()}`;let last=0,first=true,evs=[];
function esc(s){{return String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
function pct(t){{const m=String(t||'').match(/(\\d+(?:\\.\\d+)?)%/);return m?parseFloat(m[1]):null;}}
const ICON={{plug:'🔌',unplug:'⏏️',start:'▶️',done:'✅',error:'⚠️'}};
async function saveAuto(){{
  const fd=new URLSearchParams({{on:document.getElementById('auto').checked?'1':'0',level:document.getElementById('alvl').value}});
  await fetch('/api/auto',{{method:'POST',body:fd}});toast(T.saved);
}}
async function load(){{
  let r;try{{r=await (await fetch('/api/state?since='+last)).json();}}catch(e){{return;}}
  // แจ้งเตือนเหตุการณ์ใหม่
  for(const e of r.events){{evs.unshift(e);if(!first){{toast(ICON[e.kind]+' '+e.msg);if(navigator.vibrate)navigator.vibrate(e.kind==='done'?[120,60,120]:80);}}}}
  evs=evs.slice(0,30);last=r.last_event;first=false;
  if(evs.length)document.getElementById('ev').innerHTML=evs.map(e=>`<div class="find"><span>${{ICON[e.kind]}}</span><div><span class="mut">${{e.t}}</span> ${{e.link?`<a href="${{e.link}}">${{esc(e.msg)}}</a>`:esc(e.msg)}}</div></div>`).join('');
  const el=document.getElementById('devs');
  if(document.activeElement&&document.activeElement.tagName==='SELECT'&&el.contains(document.activeElement))return;
  if(!r.devices.length){{el.innerHTML='<div class="empty">'+esc(T.nodisk)+'</div>';return;}}
  el.innerHTML=r.devices.map(d=>{{
    const tag=(d.system?' <span class="pill p-warn">'+esc(T.sysdisk)+'</span>':'')+(d.usb?' <span class="pill p-info">USB</span>':'');
    const db=d.db_no?`<a href="/disk/${{d.db_no}}">${{esc(T.dbline.replace('{{no}}',d.db_no).replace('{{g}}',d.db_grade??'').replace('{{d}}',(d.db_tested||'').slice(0,10)))}}</a>`:'<span class="mut">'+esc(T.never)+'</span>';
    let act='';
    if(d.job){{const p=pct(d.job_current);const m=Math.floor(d.job_elapsed/60),s=d.job_elapsed%60;
      act=`<a href="/job/${{d.job}}" style="text-decoration:none;color:inherit;display:block">
        <div style="display:flex;justify-content:space-between;font-size:13px"><span>${{esc(T.checking.replace('{{level}}',d.job_level))}}</span><span class="mut">${{m}}:${{String(s).padStart(2,'0')}}</span></div>
        <div class="bar" style="margin:8px 0"><i class="${{p===null?'ind':''}}" style="width:${{p===null?30:p}}%;background:linear-gradient(90deg,#7c5cff,#38bdf8)"></i></div>
        <div class="mono mut" style="font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{esc(d.job_current||T.starting)}}</div></a>`;}}
    else if(r.can_check)act=`<form method="post" action="/check" class="chk"><input type="hidden" name="name" value="${{esc(d.name)}}">${{LV}}<button class="btn pri">${{esc(T.check)}}</button></form>`;
    return `<div class="item${{d.job?' busy':''}}" style="display:block"><div class="it"><small>${{esc(d.dev)}} · ${{esc(d.brand||'')}}${{tag}}</small>
      <b>${{esc(d.model)}}</b><small>${{esc(d.size)}} · ${{esc(d.kind)}} · ${{esc(d.tran)}}</small>
      <button class="cp" style="margin-top:6px;width:100%" data-v="${{esc(d.serial)}}" onclick="copyText(this.dataset.v)"><span>Serial</span><b>${{esc(d.serial)}}</b><i>📋</i></button>
      <small style="margin-top:4px">${{db}}</small></div><div style="margin-top:10px">${{act}}</div></div>`;}}).join('');
}}
load();setInterval(load,2000);
{copy_js()}
</script>""")


def job_page(j):
    tj = json.dumps({"elapsed": _("ผ่านไป {m} นาที {s} วินาที"), "running": _("กำลังตรวจ"), "done": _("เสร็จแล้ว ✔"),
                     "failed": _("ผิดพลาด"), "cancelled": _("ยกเลิกแล้ว"), "result": _("ดูผล #{no}"),
                     "grade": _(" · เกรด {g}")}, ensure_ascii=False).replace("</", "<\\/")
    return page(_("ตรวจ {dev}", dev=j['dev']), f"""
<div class="top"><a class="btn" href="/devices">← {_("ดิสก์ที่เสียบอยู่")}</a><span class="mut" id="el"></span></div>
<div class="card"><div class="brand" id="lv">{E(_(LEVELS[j['level']][0]))}</div><h3 style="margin:2px 0">{E(j['model'])}</h3>
<div class="mono mut">{E(j['dev'])} · S/N {E(j['serial'])}</div>
<div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center"><span id="st" class="pill p-info">{_("กำลังตรวจ")}</span>
<form method="post" action="/job/{j['id']}/cancel" id="cf" onsubmit="return confirm({E(json.dumps(_("ยกเลิกการตรวจนี้?"), ensure_ascii=False))})">
<button class="btn del" style="padding:8px 14px;font-size:13px">{_("ยกเลิก")}</button></form><span id="res"></span></div>
<div class="mono" id="cur" style="margin-top:14px;font-size:13px;color:var(--info);min-height:20px"></div></div>
<h2>{_("บันทึกการทำงาน")}</h2><pre id="log" style="max-height:60vh">...</pre>
<script>
const T={tj};
function esc(s){{return String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
async function tick(){{
  let j;try{{j=await (await fetch('/api/job/{j['id']}')).json();}}catch(e){{setTimeout(tick,3000);return;}}
  const lg=document.getElementById('log');const atEnd=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-30;
  lg.textContent=j.lines.join('\\n');if(atEnd)lg.scrollTop=lg.scrollHeight;
  document.getElementById('cur').textContent=j.current;
  document.getElementById('el').textContent=T.elapsed.replace('{{m}}',Math.floor(j.elapsed/60)).replace('{{s}}',j.elapsed%60);
  const st=document.getElementById('st');
  const M={{running:['p-info',T.running],done:['p-ok',T.done],failed:['p-crit',T.failed],cancelled:['p-warn',T.cancelled]}};
  st.className='pill '+M[j.status][0];st.textContent=M[j.status][1];
  if(j.status!=='running'){{document.getElementById('cf').style.display='none';
    if(j.disk_no)document.getElementById('res').innerHTML=`<a class="btn pri" href="/disk/${{j.disk_no}}">${{esc(T.result.replace('{{no}}',j.disk_no))}}${{j.grade?esc(T.grade.replace('{{g}}',j.grade)):''}} →</a>`;
    return;}}
  setTimeout(tick,1500);
}}
tick();
</script>""")


class Handler(BaseHTTPRequestHandler):
    cfg = {}
    server_version = "diskcheck-web"

    def log_message(self, fmt, *args):
        sys.stdout.write(dc.DIM(f"  {self.client_address[0]} {fmt % args}\n"))

    # --- ตัวช่วย ---
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, headers=None):
        self.send(303, "", headers={"Location": to, **(headers or {})})

    def ui_lang(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return norm_lang(c["lang"].value if "lang" in c else "") or norm_lang(self.cfg.get("lang")) or "th"

    def authed(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "dcs" in c and c["dcs"].value in SESSIONS

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError(_("ไฟล์ใหญ่เกินไป"))
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
            fields, files = {}, []
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fname = part.get_filename()
                if fname:
                    files.append((fname, part.get_payload(decode=True)))
                else:
                    fields[name] = part.get_content()
            return fields, files
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}, []

    def disk_or_404(self, con, no):
        r = dc.find_disk(con, no)
        if not r:
            self.send(404, page(_("ไม่พบ"), f'<div class="empty">{_("ไม่พบดิสก์นี้")} <a href="/">{_("กลับหน้าแรก")}</a></div>'))
        return r

    def save_config(self, changes):
        path = dc.HERE / "config.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        raw.update(changes)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dc.chown_user(path)
        Handler.cfg = load_cfg()

    def save_settings(self, con, fields):
        path = dc.HERE / "config.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        raw["shop"] = fields.get("shop", "").strip()
        raw["fb_hashtags"] = fields.get("fb_hashtags", "").strip()
        raw["lang"] = norm_lang(fields.get("lang")) or "th"
        raw["output_lang"] = norm_lang(fields.get("output_lang")) or "th"
        pin = re.sub(r"\s", "", fields.get("web_pin", ""))
        if pin:
            raw["web_pin"] = pin
        raw["sale_defaults"] = {k[2:]: v.strip() for k, v in fields.items() if k.startswith("d_") and v.strip()}
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dc.chown_user(path)
        Handler.cfg = load_cfg()
        with open(os.devnull, "w") as devnull:
            old, sys.stdout = sys.stdout, devnull
            try:
                with using(out_lang(Handler.cfg)):
                    for row in con.execute("SELECT no FROM disks").fetchall():
                        dc.build_package(con, row["no"], Handler.cfg, render_card=True)
                    write_static_index(con, Handler.cfg)
            finally:
                sys.stdout = old
        return self.send(200, settings_page(Handler.cfg, _("บันทึกแล้ว และอัปเดตรายงาน/การ์ดทุกลูกแล้ว ✔")))

    def rebuild(self, con, no, card=False):
        with open(os.devnull, "w") as devnull:  # ไม่ต้องพิมพ์รายละเอียดลง terminal
            old, sys.stdout = sys.stdout, devnull
            try:
                with using(out_lang(self.cfg)):
                    dc.build_package(con, no, self.cfg, render_card=card)
                    write_static_index(con, self.cfg)
            finally:
                sys.stdout = old

    # --- GET ---
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        set_lang(self.ui_lang())
        if path == "/favicon.ico":
            return self.send(204, "")
        if path == "/lang":  # สลับภาษา / switch language
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            to = norm_lang(qs.get("to", [""])[0]) or "th"
            back = qs.get("back", ["/"])[0]
            back = back if back.startswith("/") and not back.startswith("//") else "/"
            return self.redirect(back, {"Set-Cookie": f"lang={to}; Path=/; SameSite=Lax; Max-Age=31536000"})
        if path == "/login":
            return self.send(200, pin_page())
        if not self.authed():
            return self.redirect("/login")
        con = dc.db()
        try:
            if path == "/":
                return self.send(200, index_html(con, self.cfg))
            if path == "/settings":
                return self.send(200, settings_page(self.cfg))
            if path == "/devices":
                return self.send(200, devices_page())
            if path == "/sales":
                return self.send(200, sales_page(con, self.cfg))
            if path == "/claim":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                return self.send(200, claim_page(con, self.cfg, q))
            if path == "/catalog":
                return self.send(200, catalog_page(con, self.cfg))
            if path == "/api/devices":
                return self.send(200, json.dumps(devices_json(con), ensure_ascii=False), "application/json; charset=utf-8")
            if path == "/api/state":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                since = int((q.get("since") or ["0"])[0] or 0)
                return self.send(200, json.dumps(api_state(con, since), ensure_ascii=False), "application/json; charset=utf-8")
            m = re.fullmatch(r"/(api/)?job/(\d+)", path)
            if m:
                j = JOBS.get(m.group(2))
                if not j:
                    return self.redirect("/devices")
                if m.group(1):
                    return self.send(200, json.dumps(job_json(j), ensure_ascii=False), "application/json; charset=utf-8")
                return self.send(200, job_page(j))
            m = re.fullmatch(r"/disk/(\d+)", path)
            if m:
                body = disk_page(con, self.cfg, m.group(1))
                return self.send(200, body) if body else self.disk_or_404(con, m.group(1))
            m = re.fullmatch(r"/disk/(\d+)/zip", path)
            if m:
                r = self.disk_or_404(con, m.group(1))
                if not r:
                    return
                import io
                import zipfile
                folder = dc.disk_folder(r["no"], r["serial"])
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(folder.rglob("*")):
                        if f.is_file():
                            z.write(f, f"{folder.name}/{f.relative_to(folder)}")
                return self.send(200, buf.getvalue(), "application/zip",
                                 {"Content-Disposition": f'attachment; filename="{folder.name}.zip"'})
            m = re.fullmatch(r"/files/(\d+)/(.+)", path)
            if m:
                r = self.disk_or_404(con, m.group(1))
                if not r:
                    return
                base = dc.disk_folder(r["no"], r["serial"]).resolve()
                f = (base / urllib.parse.unquote(m.group(2))).resolve()
                if base not in f.parents or not f.is_file():  # กันอ่านไฟล์นอกโฟลเดอร์ดิสก์
                    return self.send(404, "not found", "text/plain")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype == "application/json":
                    ctype += "; charset=utf-8"
                return self.send(200, f.read_bytes(), ctype)
            self.send(404, page(_("ไม่พบ"), f'<div class="empty">{_("ไม่พบหน้านี้")} <a href="/">{_("กลับหน้าแรก")}</a></div>'))
        finally:
            con.close()

    # --- POST ---
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        set_lang(self.ui_lang())
        try:
            fields, files = self.form()
        except ValueError as e:
            return self.send(413, str(e), "text/plain; charset=utf-8")
        if path == "/login":
            if fields.get("pin", "").strip() == str(self.cfg["web_pin"]):
                sid = secrets.token_urlsafe(24)
                SESSIONS.add(sid)
                return self.redirect("/", {"Set-Cookie": f"dcs={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})
            time.sleep(1)
            return self.send(401, pin_page(_("PIN ไม่ถูกต้อง")))
        if not self.authed():
            return self.redirect("/login")
        con = dc.db()
        try:
            if path == "/settings":
                return self.save_settings(con, fields)
            if path == "/api/auto":
                self.save_config({"auto_check": fields.get("on") == "1",
                                  "auto_level": fields.get("level") if fields.get("level") in LEVELS else "standard"})
                return self.send(200, "ok", "text/plain")
            if path == "/check":
                if not can_check():
                    return self.send(403, page(_("สั่งตรวจไม่ได้"), need_root_box()))
                dev = next((d for d in dc.list_disks() if d["name"] == fields.get("name")), None)
                if not dev:
                    return self.send(404, page(_("ไม่พบดิสก์"), f'<div class="empty">{_("ไม่พบดิสก์นี้แล้ว (ถอดออกไปหรือยัง?)")} '
                                                                 f'<a href="/devices">{_("กลับ")}</a></div>'))
                level = fields.get("level") if fields.get("level") in LEVELS else "standard"
                return self.redirect(f"/job/{start_job(dev, level)['id']}")
            m = re.fullmatch(r"/job/(\d+)/cancel", path)
            if m:
                cancel_job(m.group(1))
                return self.redirect(f"/job/{m.group(1)}")
            m = re.fullmatch(r"/disk/(\d+)/(upload|delete|note|sale|status|external|claim)", path)
            if not m:
                return self.send(404, "not found", "text/plain")
            r = self.disk_or_404(con, m.group(1))
            if not r:
                return
            folder = dc.disk_folder(r["no"], r["serial"])
            pdir = folder / "photos"
            pdir.mkdir(parents=True, exist_ok=True)
            action = m.group(2)
            if action == "upload":
                saved = 0
                for fname, data in files:
                    ext = Path(fname).suffix.lower()
                    if ext not in dc.PHOTO_EXT or not data:
                        continue
                    tmp = pdir / f"upload-{int(time.time() * 1000)}-{saved}{ext}"
                    tmp.write_bytes(data)
                    dc.chown_user(tmp)
                    saved += 1
                    time.sleep(0.002)  # ให้เวลาไฟล์ต่างกัน ลำดับรูปจะได้ตรงกับที่เลือก
                if not saved:
                    return self.send(400, _("ไม่มีรูปที่รองรับ (jpg, png, webp)"), "text/plain; charset=utf-8")
                self.rebuild(con, r["no"])
                return self.send(200, f"ok {saved}", "text/plain; charset=utf-8")
            if action == "delete":
                f = (pdir / Path(fields.get("name", "")).name)
                if f.is_file() and f.suffix.lower() in dc.PHOTO_EXT:
                    f.unlink()
                self.rebuild(con, r["no"])
                return self.redirect(f"/disk/{r['no']}")
            if action in ("sale", "status"):
                own = json.loads(r["sale_json"] or "{}")
                if action == "sale":
                    for k, *_rest in dc.SALE_FIELDS + dc.SALE_PRIVATE_FIELDS:
                        if k == "status":
                            continue
                        v = fields.get(k, "").strip()
                        if k in ("price", "sold_price", "cost"):
                            v = re.sub(r"[^0-9.]", "", v)
                        if k == "sold_at":  # วันที่ขาย: ว่าง = ใช้ค่าที่ระบบจดให้ตอนเปลี่ยนสถานะ
                            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) and v != str(own.get("sold_at", ""))[:10]:
                                own["sold_at"] = v
                            continue
                        if v:
                            own[k] = v
                        else:
                            own.pop(k, None)
                apply_status(own, fields.get("status", "").strip())
                con.execute("UPDATE disks SET sale_json=? WHERE no=?", (json.dumps(own, ensure_ascii=False), r["no"]))
                con.commit()
                self.rebuild(con, r["no"], card=(action == "sale"))
                back = fields.get("back", "")
                return self.redirect(back if back.startswith("/") and not back.startswith("//") else f"/disk/{r['no']}")
            if action == "claim":
                own = json.loads(r["sale_json"] or "{}")
                text = fields.get("text", "").strip()[:2000]
                if text:
                    last = con.execute("SELECT id FROM tests WHERE disk_no=? ORDER BY id DESC LIMIT 1", (r["no"],)).fetchone()
                    own["claims"] = (own.get("claims") or []) + [{"at": time.strftime("%Y-%m-%d %H:%M"), "text": text,
                                                                  "test_id": last["id"] if last else None}]
                    if fields.get("set_status"):
                        apply_status(own, "เคลม/ตีกลับ")
                    con.execute("UPDATE disks SET sale_json=? WHERE no=?", (json.dumps(own, ensure_ascii=False), r["no"]))
                    con.commit()
                    self.rebuild(con, r["no"])
                return self.redirect(f"/disk/{r['no']}#warranty")
            if action == "external":
                con.execute("UPDATE disks SET external=? WHERE no=?", (fields.get("external", "").strip()[:80] or None, r["no"]))
                con.commit()
                self.rebuild(con, r["no"], card=True)
                return self.redirect(f"/disk/{r['no']}")
            if action == "note":
                con.execute("UPDATE disks SET note=? WHERE no=?", (fields.get("note", "").strip(), r["no"]))
                con.commit()
                self.rebuild(con, r["no"])
                return self.redirect(f"/disk/{r['no']}")
        finally:
            con.close()


def serve(port=8080):
    cfg = load_cfg()
    Handler.cfg = cfg
    set_default(cfg.get("lang"))
    con = dc.db()
    with using(out_lang(cfg)):
        write_static_index(con, cfg)
    con.close()
    ip = lan_ip()
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        if e.errno == 98:  # Address already in use
            print(dc.WARN("\n  ⚠  " + _("พอร์ต {port} ถูกใช้อยู่ — น่าจะมีเว็บ diskcheck เปิดอยู่แล้วในหน้าต่างอื่น", port=port)))
            print("     • " + _("ถ้าเปิดอยู่แล้ว ใช้ตัวนั้นได้เลย: {url}", url=f"http://{lan_ip()}:{port}"))
            print("     • " + _("หรือปิดตัวเก่า (กด Ctrl+C ในหน้าต่างนั้น) แล้วรันใหม่"))
            print("     • " + _("หรือใช้พอร์ตอื่น: {cmd}", cmd=f"./web.sh --port {port + 1}") + "\n")
            sys.exit(1)
        raise
    print(dc.CYAN(dc.BANNER) + dc.DIM(f"  web v{dc.VERSION}"))
    dc.header(_("เว็บพร้อมใช้งาน"))
    col = lambda t, w: t + " " * max(1, w - sum(unicodedata.category(c) != "Mn" for c in t))  # จัดคอลัมน์ / align
    print(f"  📱 {col(_('เปิดในมือถือ (Wi-Fi เดียวกัน):'), 32)}{dc.BOLD(dc.CYAN(f'http://{ip}:{port}'))}")
    print(f"  💻 {col(_('เปิดในเครื่องนี้:'), 32)}http://localhost:{port}")
    print(f"  🔑 {col('PIN:', 32)}{dc.BOLD(cfg['web_pin'])}  {dc.DIM(_('(เปลี่ยนได้ในหน้าตั้งค่า)'))}")
    if can_check():
        print(dc.OK("  ✔ " + _("สั่งตรวจดิสก์จากเว็บได้ (รันด้วย sudo)")))
        on, lvl = auto_settings()
        print((dc.OK if on else dc.DIM)(f"  {'✔' if on else '✘'} " + _(
            "ตรวจอัตโนมัติเมื่อเสียบดิสก์: {state} · ระดับ {level}  (เปลี่ยนได้ในหน้า 🔌 ดิสก์ที่เสียบอยู่)",
            state=_("เปิด") if on else _("ปิด"), level=_(LEVELS[lvl][0]))))
        start_watcher()
    else:
        print(dc.WARN("  ⚠ " + _("สั่งตรวจจากเว็บไม่ได้ — ถ้าต้องการ ให้เปิดด้วย sudo ./web.sh")))
    print(dc.DIM("\n  " + _("กด Ctrl+C เพื่อปิดเว็บ") + "\n"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  " + _("ปิดเว็บแล้ว"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="diskcheck web")
    ap.add_argument("--port", type=int, default=8080)
    serve(ap.parse_args().port)
