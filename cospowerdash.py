import asyncio
import json
import logging
import sqlite3
import subprocess
from logging.handlers import RotatingFileHandler
from typing import Dict, Set, List, Optional

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ----------------------------
# Constants
# ----------------------------

DB = "upsdash.db"
SNMP_COMMUNITY = "public"
DEFAULT_DASH_TITLE = "Power Dashboard"

# FS MPDU (enterprise 30966) per-outlet OIDs
OID_PDU_OUTLET_CURRENT = "1.3.6.1.4.1.30966.8.1.8.1"   # .{port}.0 -> STRING (Amps)
OID_PDU_OUTLET_POWER   = "1.3.6.1.4.1.30966.8.1.11"     # .{port}.0 -> STRING (kW)
OID_PDU_OUTLET_STATE   = "1.3.6.1.4.1.30966.8.1.7"      # .{port}.0 -> STRING "ON "/"-- "
OID_PDU_OUTLET_NAME    = "1.3.6.1.4.1.30966.8.1.5"      # .{port}.0 -> STRING (outlet label)
OID_PDU_OUTLET_PF      = "1.3.6.1.4.1.30966.8.1.9"      # .{port}.0 -> STRING (power factor)

# FS MPDU 24-port circuit mapping: physical label -> SNMP measurement index
# Each circuit covers 4 outlets; the current/power reading lives at one SNMP index per circuit
PDU_CIRCUITS = [
    {"circuit": 1, "snmp_idx": 1,  "ports": [1, 2, 3, 4]},
    {"circuit": 2, "snmp_idx": 8,  "ports": [5, 6, 7, 8]},
    {"circuit": 3, "snmp_idx": 9,  "ports": [9, 10, 11, 12]},
    {"circuit": 4, "snmp_idx": 16, "ports": [13, 14, 15, 16]},
    {"circuit": 5, "snmp_idx": 17, "ports": [17, 18, 19, 20]},
    {"circuit": 6, "snmp_idx": 24, "ports": [21, 22, 23, 24]},
]

# ----------------------------
# Logging
# ----------------------------

# Standard operational logger -> upsdash.log + stderr
logger = logging.getLogger("upsdash")
logger.setLevel(logging.DEBUG)

_file_handler = RotatingFileHandler("upsdash.log", maxBytes=1_000_000, backupCount=3)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)

_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.INFO)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_stream_handler)

# ----------------------------
# App
# ----------------------------

app = FastAPI()

# ----------------------------
# Database
# ----------------------------

def _connect():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS racks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            pdu_ip TEXT NOT NULL DEFAULT '',
            community TEXT NOT NULL DEFAULT 'COS65'
        )
    """)
    conn.commit()

    cur.execute("PRAGMA table_info(racks)")
    cols = [row[1] for row in cur.fetchall()]
    if "community" not in cols:
        cur.execute("ALTER TABLE racks ADD COLUMN community TEXT NOT NULL DEFAULT 'COS65'")
        conn.commit()

    cur.execute("PRAGMA table_info(racks)")
    cols = [row[1] for row in cur.fetchall()]
    if "sort_order" not in cols:
        cur.execute("ALTER TABLE racks ADD COLUMN sort_order INTEGER")
        conn.commit()
        cur.execute("UPDATE racks SET sort_order=id WHERE sort_order IS NULL")
        conn.commit()

    cur.execute("PRAGMA table_info(racks)")
    cols = [row[1] for row in cur.fetchall()]
    if "pdu_ip" not in cols:
        cur.execute("ALTER TABLE racks ADD COLUMN pdu_ip TEXT DEFAULT ''")
        conn.commit()

    cur.execute("PRAGMA table_info(racks)")
    cols = [row[1] for row in cur.fetchall()]
    if "has_additional_pdu" not in cols:
        cur.execute("ALTER TABLE racks ADD COLUMN has_additional_pdu INTEGER DEFAULT 0")
        conn.commit()

    cur.execute(
        "UPDATE racks SET community=? WHERE community IS NULL OR TRIM(community)=''",
        (SNMP_COMMUNITY,)
    )
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()

    cur.execute("SELECT value FROM settings WHERE key='dashboard_title'")
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO settings(key,value) VALUES('dashboard_title', ?)", (DEFAULT_DASH_TITLE,))
        conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS systems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rack_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            pdu_ip TEXT NOT NULL,
            pdu_community TEXT NOT NULL DEFAULT 'COS65',
            ports TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()

    conn.close()

def get_racks():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id,label,COALESCE(sort_order,id),COALESCE(pdu_ip,''),COALESCE(has_additional_pdu,0)
        FROM racks
        ORDER BY COALESCE(sort_order,id), id
    """)
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "label": r[1], "sort_order": r[2], "pdu_ip": r[3], "has_additional_pdu": bool(r[4])} for r in rows]

def _next_sort_order(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM racks")
    row = cur.fetchone()
    return int(row[0] or 0) + 1

def add_rack(label: str, pdu_ip: str = "", has_additional_pdu: bool = False):
    conn = _connect()
    cur = conn.cursor()
    next_order = _next_sort_order(cur)
    cur.execute(
        "INSERT INTO racks(label, pdu_ip, community, sort_order, has_additional_pdu) VALUES(?,?,?,?,?)",
        (label, pdu_ip, SNMP_COMMUNITY, next_order, int(has_additional_pdu)),
    )
    conn.commit()
    conn.close()

def delete_racks(ids: List[int]):
    if not ids:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.executemany("DELETE FROM racks WHERE id=?", [(int(rid),) for rid in ids])
    conn.commit()
    conn.close()

def set_rack_order(ids_in_order: List[int]):
    conn = _connect()
    cur = conn.cursor()

    cur.execute("SELECT id FROM racks ORDER BY COALESCE(sort_order,id), id")
    current = [int(r[0]) for r in cur.fetchall()]
    current_set = set(current)

    cleaned = []
    seen = set()
    for rid in ids_in_order:
        try:
            rid = int(rid)
        except Exception:
            continue
        if rid in current_set and rid not in seen:
            cleaned.append(rid)
            seen.add(rid)

    for rid in current:
        if rid not in seen:
            cleaned.append(rid)

    order = 1
    for rid in cleaned:
        cur.execute("UPDATE racks SET sort_order=? WHERE id=?", (order, rid))
        order += 1

    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings(key,value)
        VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()

# ----------------------------
# Systems DB helpers
# ----------------------------

def get_systems_for_rack(rack_id: int) -> List[Dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, rack_id, name, pdu_ip, pdu_community, ports, sort_order "
        "FROM systems WHERE rack_id=? ORDER BY sort_order, id", (rack_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "rack_id": r[1], "name": r[2], "pdu_ip": r[3],
         "pdu_community": r[4], "ports": r[5], "sort_order": r[6]}
        for r in rows
    ]

def get_all_systems() -> Dict[int, List[Dict]]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, rack_id, name, pdu_ip, pdu_community, ports, sort_order "
        "FROM systems ORDER BY sort_order, id"
    )
    rows = cur.fetchall()
    conn.close()
    grouped: Dict[int, List[Dict]] = {}
    for r in rows:
        entry = {"id": r[0], "rack_id": r[1], "name": r[2], "pdu_ip": r[3],
                 "pdu_community": r[4], "ports": r[5], "sort_order": r[6]}
        grouped.setdefault(r[1], []).append(entry)
    return grouped

def add_system(rack_id: int, name: str, pdu_ip: str, ports: str) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM systems WHERE rack_id=?", (rack_id,))
    count = cur.fetchone()[0]
    if count >= 10:
        conn.close()
        return -1
    cur.execute("SELECT COALESCE(MAX(sort_order),0) FROM systems WHERE rack_id=?", (rack_id,))
    next_order = cur.fetchone()[0] + 1
    cur.execute(
        "INSERT INTO systems(rack_id, name, pdu_ip, pdu_community, ports, sort_order) "
        "VALUES(?,?,?,?,?,?)",
        (rack_id, name, pdu_ip, SNMP_COMMUNITY, ports, next_order),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def delete_system(system_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM systems WHERE id=?", (system_id,))
    conn.commit()
    conn.close()

def delete_systems_for_rack(rack_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM systems WHERE rack_id=?", (rack_id,))
    conn.commit()
    conn.close()

# ----------------------------
# SNMP Helpers
# ----------------------------

def snmp_get(ip: str, oid: str, community: str = SNMP_COMMUNITY) -> Optional[str]:
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-t", "1", "-r", "1", ip, oid],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("SNMP query failed for %s OID %s", ip, oid)
            return None
        return result.stdout.strip()
    except Exception as e:
        logger.error("SNMP exception for %s: %s", ip, e)
        return None

def ping_ok(ip: str) -> bool:
    try:
        ping = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ping.returncode == 0
    except Exception as e:
        logger.error("Ping exception for %s: %s", ip, e)
        return False

def _parse_int(text: str) -> Optional[int]:
    if not text:
        return None
    for token in ("INTEGER:", "Gauge32:"):
        if token in text:
            try:
                part = text.split(token, 1)[1].strip()
                num = ""
                for ch in part:
                    if ch.isdigit() or (ch == "-" and not num):
                        num += ch
                    elif num:
                        break
                if not num or num == "-":
                    return None
                return int(num)
            except Exception:
                return None
    return None

def _parse_float(text: str) -> Optional[float]:
    """Parse a float from SNMP STRING response like: STRING: "1.53" """
    if not text:
        return None
    if "STRING:" in text:
        try:
            part = text.split("STRING:", 1)[1].strip().strip('"').strip()
            if part == "--" or not part:
                return None
            return float(part)
        except (ValueError, IndexError):
            return None
    for token in ("INTEGER:", "Gauge32:"):
        if token in text:
            val = _parse_int(text)
            return float(val) if val is not None else None
    return None

# ----------------------------
# Poll Loop / Live State
# ----------------------------

latest_status: Dict[int, Dict] = {}
latest_systems_status: Dict[int, List[Dict]] = {}
latest_pdu_circuits: Dict[int, List[Dict]] = {}

_pdu_name_cache: Dict[str, Dict[int, str]] = {}   # pdu_ip -> {port: name}
_pdu_name_cache_tick: Dict[str, int] = {}          # pdu_ip -> poll count at last refresh
_poll_count = 0
PDU_NAME_REFRESH_INTERVAL = 5  # refresh outlet names every 5 poll cycles (~10s)
clients: Set[WebSocket] = set()

def build_ordered_snapshot() -> List[Dict]:
    ordered = get_racks()
    out = []
    for r in ordered:
        rid = r["id"]
        status = latest_status.get(rid) or {
            "id": rid,
            "label": r["label"],
            "sort_order": r.get("sort_order"),
        }
        status["label"] = r["label"]
        status["pdu_ip"] = r.get("pdu_ip", "")
        status["has_additional_pdu"] = r.get("has_additional_pdu", False)
        status["sort_order"] = r.get("sort_order")
        status["systems"] = latest_systems_status.get(rid, [])
        status["circuits"] = latest_pdu_circuits.get(rid, [])
        out.append(status)
    return out

async def broadcast_snapshot():
    if not clients:
        return
    payload = json.dumps(build_ordered_snapshot())
    dead = []
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)

async def poll_loop():
    while True:
        try:
            racks = get_racks()
            current_ids = {r["id"] for r in racks}

            for rid in list(latest_status.keys()):
                if rid not in current_ids:
                    latest_status.pop(rid, None)

            for rack in racks:
                rid = rack["id"]
                latest_status[rid] = {
                    "id": rid,
                    "label": rack["label"],
                    "sort_order": rack.get("sort_order"),
                }

            # Poll PDU circuits for racks that have a pdu_ip
            global _poll_count
            _poll_count += 1
            pdu_ping_cache: Dict[str, bool] = {}
            for rack in racks:
                rid = rack["id"]
                pdu_ip = rack.get("pdu_ip", "")
                if not pdu_ip:
                    latest_pdu_circuits.pop(rid, None)
                    continue

                if pdu_ip not in pdu_ping_cache:
                    pdu_ping_cache[pdu_ip] = ping_ok(pdu_ip)
                pdu_reachable = pdu_ping_cache[pdu_ip]

                circuits = []
                if pdu_reachable:
                    # Fetch outlet names (cached, refresh periodically)
                    last_tick = _pdu_name_cache_tick.get(pdu_ip, 0)
                    if pdu_ip not in _pdu_name_cache or (_poll_count - last_tick) >= PDU_NAME_REFRESH_INTERVAL:
                        outlet_names: Dict[int, str] = {}
                        for port in range(1, 25):
                            raw_name = snmp_get(pdu_ip, f"{OID_PDU_OUTLET_NAME}.{port}.0")
                            if raw_name and "STRING:" in raw_name:
                                name = raw_name.split("STRING:", 1)[1].strip().strip('"').strip()
                                outlet_names[port] = name if name else f"Port {port}"
                            else:
                                outlet_names[port] = f"Port {port}"
                        _pdu_name_cache[pdu_ip] = outlet_names
                        _pdu_name_cache_tick[pdu_ip] = _poll_count
                    outlet_names = _pdu_name_cache[pdu_ip]

                    for cdef in PDU_CIRCUITS:
                        idx = cdef["snmp_idx"]
                        raw_a = snmp_get(pdu_ip, f"{OID_PDU_OUTLET_CURRENT}.{idx}.0")
                        raw_w = snmp_get(pdu_ip, f"{OID_PDU_OUTLET_POWER}.{idx}.0")
                        raw_pf = snmp_get(pdu_ip, f"{OID_PDU_OUTLET_PF}.{idx}.0")
                        amps = _parse_float(raw_a) or 0.0
                        kw = _parse_float(raw_w) or 0.0
                        pf = _parse_float(raw_pf) or 0.0

                        port_names = []
                        for p in cdef["ports"]:
                            port_names.append({"port": p, "name": outlet_names.get(p, f"Port {p}")})

                        circuits.append({
                            "circuit": cdef["circuit"],
                            "ports": cdef["ports"],
                            "current_a": round(amps, 2),
                            "power_w": round(kw * 1000, 1),
                            "pf": round(pf, 2),
                            "outlets": port_names,
                            "reachable": True,
                        })
                else:
                    for cdef in PDU_CIRCUITS:
                        port_names = [{"port": p, "name": f"Port {p}"} for p in cdef["ports"]]
                        circuits.append({
                            "circuit": cdef["circuit"],
                            "ports": cdef["ports"],
                            "current_a": 0,
                            "power_w": 0,
                            "pf": 0,
                            "outlets": port_names,
                            "reachable": False,
                        })

                latest_pdu_circuits[rid] = circuits

            # Clean up circuit data for deleted racks
            for rid in list(latest_pdu_circuits.keys()):
                if rid not in current_ids:
                    latest_pdu_circuits.pop(rid, None)

            await broadcast_snapshot()
        except Exception as e:
            logger.error("Error in poll loop: %s", e)
        await asyncio.sleep(2)

# ----------------------------
# App Lifecycle
# ----------------------------

@app.on_event("startup")
async def startup():
    init_db()
    logger.info("Power Dashboard starting, database initialized")
    asyncio.create_task(poll_loop())

# ----------------------------
# REST API
# ----------------------------

class Rack(BaseModel):
    label: str
    pdu_ip: str
    has_additional_pdu: bool = False

class TitlePayload(BaseModel):
    title: str

class SystemPayload(BaseModel):
    rack_id: int
    name: str
    pdu_ip: str
    ports: str

class SystemDeletePayload(BaseModel):
    id: int

class OrderPayload(BaseModel):
    ids: List[int]

@app.post("/api/racks")
def api_add_rack(r: Rack):
    label = (r.label or "").strip()
    pdu_ip = (r.pdu_ip or "").strip()
    if not label or not pdu_ip:
        return {"ok": False, "error": "Missing label or PDU IP"}
    add_rack(label, pdu_ip, r.has_additional_pdu)
    logger.info("Rack added: %s (PDU %s, additional_pdu=%s)", label, pdu_ip, r.has_additional_pdu)
    return {"ok": True}

@app.post("/api/delete")
def api_delete(data: dict):
    ids = data.get("ids", [])
    try:
        ids_int = [int(x) for x in ids]
    except Exception:
        ids_int = []
    delete_racks(ids_int)
    for rid in ids_int:
        latest_status.pop(rid, None)
        latest_systems_status.pop(rid, None)
        delete_systems_for_rack(rid)
    logger.info("Racks deleted: %s", ids_int)
    return {"ok": True}

@app.post("/api/check_pdu")
def api_check_pdu_ip(data: dict):
    ip = (data.get("pdu_ip") or "").strip()
    if not ip:
        return {"ok": False}
    if not ping_ok(ip):
        return {"ok": False}
    # Test reading outlet name OID to confirm it's an FS MPDU
    test = snmp_get(ip, f"{OID_PDU_OUTLET_NAME}.1.0")
    ok = test is not None and "STRING:" in test
    return {"ok": ok}

@app.post("/api/order")
def api_order(p: OrderPayload):
    try:
        ids = [int(x) for x in p.ids]
    except Exception:
        ids = []
    set_rack_order(ids)
    return {"ok": True}

@app.get("/api/settings/title")
def api_get_title():
    title = get_setting("dashboard_title", DEFAULT_DASH_TITLE)
    return {"ok": True, "title": title}

@app.post("/api/settings/title")
def api_set_title(p: TitlePayload):
    title = (p.title or "").strip()
    if not title:
        title = DEFAULT_DASH_TITLE
    title = title[:64]
    set_setting("dashboard_title", title)
    logger.info("Dashboard title changed to: %s", title)
    return {"ok": True, "title": title}

# ----------------------------
# Systems API
# ----------------------------

@app.post("/api/systems")
def api_add_system(s: SystemPayload):
    name = (s.name or "").strip()
    pdu_ip = (s.pdu_ip or "").strip()
    ports = (s.ports or "").strip()
    if not name or not pdu_ip or not ports:
        return {"ok": False, "error": "Missing name, PDU IP, or ports"}
    # Validate ports format
    port_list = [p.strip() for p in ports.split(",") if p.strip()]
    if not port_list or not all(p.isdigit() and 1 <= int(p) <= 42 for p in port_list):
        return {"ok": False, "error": "Invalid port numbers (1-42)"}
    clean_ports = ",".join(port_list)
    new_id = add_system(s.rack_id, name, pdu_ip, clean_ports)
    if new_id == -1:
        return {"ok": False, "error": "Maximum 10 systems per rack"}
    logger.info("System added: %s on rack %d (PDU %s ports %s)", name, s.rack_id, pdu_ip, clean_ports)
    return {"ok": True, "id": new_id}

@app.post("/api/systems/delete")
def api_delete_system(s: SystemDeletePayload):
    delete_system(s.id)
    logger.info("System deleted: id=%d", s.id)
    return {"ok": True}

@app.post("/api/systems/check")
def api_check_pdu(data: dict):
    ip = (data.get("pdu_ip") or "").strip()
    port = data.get("port", 1)
    if not ip:
        return {"ok": False}
    if not ping_ok(ip):
        return {"ok": False, "error": "PDU not reachable"}
    oid = f"{OID_PDU_OUTLET_CURRENT}.{port}.0"
    raw = snmp_get(ip, oid)
    ok = raw is not None and "STRING:" in raw
    return {"ok": ok}

# ----------------------------
# WebSocket
# ----------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    logger.debug("WebSocket client connected (%d total)", len(clients))
    try:
        await ws.send_text(json.dumps(build_ordered_snapshot()))
        while True:
            await ws.receive_text()
    except Exception:
        clients.discard(ws)
        logger.debug("WebSocket client disconnected (%d remaining)", len(clients))

# ----------------------------
# UI
# ----------------------------

@app.get("/")
def ui():
    initial_title = get_setting("dashboard_title", DEFAULT_DASH_TITLE)

    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  *, *::before, *::after { box-sizing: border-box; }

  body {
    background:#0f172a;
    color:white;
    font-family:Arial, sans-serif;
    text-align:center;
    margin:0;
    padding: clamp(18px, 2.5vw, 42px);
    height: 100vh;
    overflow: hidden;
  }

  .page {
    max-width: min(100%, 130vh);
    margin: 0 auto;
    position: relative;
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .top-icons {
    position: absolute;
    top: 6px;
    right: 6px;
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .icon-btn, .gear {
    width: 44px;
    height: 44px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.10);
    background: rgba(2, 6, 23, 0.55);
    display:flex;
    align-items:center;
    justify-content:center;
    cursor:pointer;
    transition: transform 180ms ease, background 180ms ease, border-color 180ms ease, box-shadow 180ms ease;
    backdrop-filter: blur(8px);
    box-shadow: 0 10px 28px rgba(0,0,0,0.35);
  }
  .icon-btn:hover, .gear:hover {
    background: rgba(2, 6, 23, 0.72);
    border-color: rgba(255,255,255,0.16);
    transform: scale(1.08);
    box-shadow: 0 14px 34px rgba(0,0,0,0.45);
  }
  .gear:hover {
    transform: rotate(18deg) scale(1.03);
  }
  .icon-btn svg, .gear svg { width: 22px; height: 22px; display:block; }
  .gear path { fill: rgba(226,232,240,0.92); }

  h2 { margin: 0 0 8px; font-size: clamp(22px, 2.2vw, 34px); }
  .subtle {
    font-size: clamp(12px, 1.1vw, 14px);
    opacity:0.7;
    margin: 0 auto 18px;
    line-height: 1.35;
    max-width: 980px;
  }


  button {
    padding: 12px 18px;
    border-radius:12px;
    border:none;
    cursor:pointer;
    margin:4px;
    font-weight:800;
    font-size: clamp(13px, 1.15vw, 16px);
  }
  .primary { background:#2563eb; color:white; }
  .danger { background:#dc2626; color:white; }
  .disabled { background:#475569 !important; cursor:not-allowed !important; opacity:0.75 !important; }

  /* Rack area wrapper so we can place the edit icon on top-left
     NEW: left padding so icon doesn't overlap the first rack */
  .rackArea {
    position: relative;
    width: 100%;
    margin-top: 6px;
    padding-top: 14px;
    padding-left: 64px;  /* <-- this shifts the racks to the right */
    flex: 1 1 0;
    min-height: 0;
    overflow: hidden;
  }

  /* Edit Layout drag-handle icon button */
  .layoutBtn {
    position: absolute;
    top: 0px;
    left: 12px;          /* <-- placed inside the new padding */
    width: 44px;
    height: 44px;
    border-radius: 14px;
    border: 1px solid rgba(255,255,255,0.12);
    background: rgba(2, 6, 23, 0.45);
    display:flex;
    align-items:center;
    justify-content:center;
    cursor:pointer;
    transition: transform 160ms ease, background 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
    backdrop-filter: blur(8px);
    box-shadow: 0 10px 28px rgba(0,0,0,0.22);
  }
  .layoutBtn:hover {
    transform: translateY(-1px);
    background: rgba(2, 6, 23, 0.62);
    border-color: rgba(255,255,255,0.18);
    box-shadow: 0 14px 34px rgba(0,0,0,0.32);
  }
  .layoutBtn.on {
    background: rgba(99,102,241,0.20);
    border-color: rgba(99,102,241,0.42);
    box-shadow: 0 16px 38px rgba(0,0,0,0.40), 0 0 0 4px rgba(99,102,241,0.10);
  }
  .layoutBtn svg { width: 22px; height: 22px; display:block; }
  .layoutBtn .dot { fill: rgba(226,232,240,0.92); }
  .layoutBtn.on .dot { fill: rgba(255,255,255,0.96); }

  .grid {
    --rackW: 300px;
    --rackH: 400px;
    display:flex;
    flex-wrap: nowrap;
    justify-content:center;
    align-items:flex-end;
    gap: 26px;
    width: 100%;
    max-width: 100%;
    height: 100%;
    margin: 0 auto;
  }

  .rack {
    width: 100%;
    height: var(--rackH);
    border:1px solid rgba(255,255,255,0.14);
    border-radius: 0;
    position:relative;
    background: rgba(0,0,0,0.25);
    border-top: none;
    overflow:hidden;
    user-select: none;
    transition: transform 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
    display: flex;
    flex-direction: column;
  }

  .rack.edit {
    cursor: grab;
    border-color: rgba(99,102,241,0.50);
    box-shadow: 0 10px 30px rgba(0,0,0,0.35);
  }
  .rack.edit:active { cursor: grabbing; }
  .rack.dragOver {
    outline: 3px dashed rgba(226,232,240,0.55);
    outline-offset: 6px;
  }

  .rack-wrapper.edit-mode { cursor: grab; }
  .rack-wrapper.edit-mode:active { cursor: grabbing; }

  .label {
    padding: 6px 10px;
    text-align: center;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: white;
    font-size: clamp(13px, 5cqi, 48px);
    background: #1e3a5f;
    border: 1px solid rgba(255,255,255,0.14);
    border-bottom: none;
    border-radius: 18px 18px 0 0;
  }

  /* PDU circuit area */
  .server-area {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    padding: 3px 4px;
    gap: 2px;
    overflow: hidden;
    position: relative;
  }

  .crt-block {
    flex: 1 1 0;
    min-height: 0;
    border-radius: 4px;
    background: rgba(15,23,42,0.55);
    border: 1px solid rgba(255,255,255,0.08);
    padding: 1px 3px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .crt-label {
    font-weight: 900;
    font-size: 10px;
    letter-spacing: 0.5px;
    color: rgba(148,163,184,0.9);
    text-transform: uppercase;
    white-space: nowrap;
    flex-shrink: 1;
    min-height: 0;
    overflow: hidden;
  }
  .crt-body {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    gap: 2px;
    overflow: hidden;
  }
  .crt-outlets {
    flex: 5 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: flex-start;
    gap: 0;
    overflow: hidden;
    text-align: left;
  }
  .crt-metric-col {
    flex: 3 0 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    padding: 2px;
  }
  .crt-metric-unit {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    color: rgba(148,163,184,0.5);
    white-space: nowrap;
  }
  .crt-metric-val {
    font-weight: 900;
    font-size: 16px;
    line-height: 1.1;
    white-space: nowrap;
  }
  .crt-metric-val.amps { color: #38bdf8; }
  .crt-metric-val.watts { color: #a78bfa; }
  .crt-metric-val.offline { color: rgba(255,255,255,0.3); font-size: 14px; }
  .crt-outlet {
    font-size: 9px;
    line-height: 1.25;
    color: rgba(226,232,240,0.7);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding-left: 4px;
  }
  .crt-outlet.empty {
    opacity: 0.35;
  }
  .crt-port-label {
    color: rgba(99,102,241,0.6);
    margin-right: 4px;
  }
  .crt-outlet.empty .crt-port-label {
    color: rgba(255,255,255,0.15);
  }
  .no-pdu-msg {
    flex: 1 1 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: rgba(255,255,255,0.2);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.5px;
  }

  .rack-wrapper {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    width: var(--rackW);
    container-type: inline-size;
    container-name: rack;
  }

  .ip-banner {
    margin-top: -1px;
    padding: 2px 10px;
    text-align: center;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: white;
    font-size: clamp(9px, 3.2cqi, 36px);
    background: #162f4d;
    border: 1px solid rgba(255,255,255,0.14);
    border-top: none;
    border-bottom: none;
  }

  .modal {
    position:fixed;
    inset:0;
    background:rgba(0,0,0,0.65);
    display:none;
    align-items:center;
    justify-content:center;
    padding: 18px;
    z-index: 9999;
  }
  .modal-content {
    background:#1e293b;
    padding:24px;
    border-radius:18px;
    width:min(440px, 95vw);
    text-align:left;
    box-shadow: 0 30px 70px rgba(0,0,0,0.55);
    z-index: 10000;
  }
  h3 { margin: 0 0 14px; font-size:22px; }

  input:not([type="radio"]) {
    width:100%;
    padding:12px;
    margin-bottom:12px;
    border-radius:10px;
    border:1px solid rgba(255,255,255,0.16);
    background:#0b1220;
    color:white;
    font-size:16px;
    outline:none;
    display:block;
  }

  .row {
    display:flex;
    gap:10px;
    align-items:center;
    justify-content:space-between;
    margin-top: 8px;
  }

  .status-wrap {
    display:flex;
    align-items:center;
    gap:10px;
    margin: 6px 0 14px;
  }
  .status-light {
    width:18px;
    height:18px;
    border-radius:999px;
    background:#ef4444;
    box-shadow: 0 0 0 4px rgba(239,68,68,0.15);
  }
  .status-light.green {
    background:#22c55e;
    box-shadow: 0 0 0 4px rgba(34,197,94,0.15);
  }
  .status-text { font-weight:900; opacity:0.85; font-size:13px; }

  .remove-list {
    margin-top: 6px;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 14px;
    overflow:hidden;
    background: rgba(15, 23, 42, 0.55);
  }
  .remove-row {
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding: 16px 14px;
    gap: 10px;
  }
  .remove-row + .remove-row {
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .remove-left {
    display:flex;
    align-items:center;
    gap: 14px;
    min-width: 0;
  }
  .remove-label {
    font-weight: 900;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: clamp(18px, 1.7vw, 24px);
  }
  .remove-ip {
    font-size: clamp(16px, 1.5vw, 22px);
    opacity: 0.75;
    margin-left: 10px;
    font-weight: 800;
  }
  .remove-checkbox { width: 22px; height: 22px; }

  .hint {
    opacity:0.7;
    font-size: 13px;
    margin-top: -4px;
    margin-bottom: 10px;
    line-height: 1.35;
  }
</style>
</head>
<body>
  <div class="page">
    <div class="top-icons">
      <div class="icon-btn" onclick="openAdd()" title="Add Rack">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="3" width="18" height="18" rx="4" ry="4" fill="none" stroke="rgba(226,232,240,0.92)" stroke-width="1.8"/>
          <line x1="12" y1="7.5" x2="12" y2="16.5" stroke="rgba(226,232,240,0.92)" stroke-width="2" stroke-linecap="round"/>
          <line x1="7.5" y1="12" x2="16.5" y2="12" stroke="rgba(226,232,240,0.92)" stroke-width="2" stroke-linecap="round"/>
        </svg>
      </div>
      <div class="icon-btn" onclick="openRemove()" title="Remove Rack">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3" y="3" width="18" height="18" rx="4" ry="4" fill="none" stroke="rgba(226,232,240,0.92)" stroke-width="1.8"/>
          <line x1="7.5" y1="12" x2="16.5" y2="12" stroke="rgba(226,232,240,0.92)" stroke-width="2" stroke-linecap="round"/>
        </svg>
      </div>
      <div class="gear" onclick="openSettings()" title="Settings">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.4 7.4 0 0 0-1.63-.94l-.36-2.54A.5.5 0 0 0 13.9 1h-3.8a.5.5 0 0 0-.49.42l-.36 2.54c-.58.23-1.12.54-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.71 7.48a.5.5 0 0 0 .12.64l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94L2.83 14.52a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.31.6.22l2.39-.96c.5.4 1.05.71 1.63.94l.36 2.54c.04.24.25.42.49.42h3.8c.24 0 .45-.18.49-.42l.36-2.54c.58-.23 1.12-.54 1.63-.94l2.39.96c.22.09.47 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7z"/>
        </svg>
      </div>
    </div>

    <h2 id="dashTitle">__TITLE__</h2>
    <div class="rackArea">
      <div id="layoutBtn" class="layoutBtn" onclick="toggleEdit()" title="Edit Layout">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle class="dot" cx="8" cy="7" r="1.6"></circle>
          <circle class="dot" cx="16" cy="7" r="1.6"></circle>
          <circle class="dot" cx="8" cy="12" r="1.6"></circle>
          <circle class="dot" cx="16" cy="12" r="1.6"></circle>
          <circle class="dot" cx="8" cy="17" r="1.6"></circle>
          <circle class="dot" cx="16" cy="17" r="1.6"></circle>
        </svg>
      </div>

      <div class="grid" id="racks"></div>
    </div>
  </div>

  <!-- Add Modal -->
  <div class="modal" id="addModal">
    <div class="modal-content">
      <h3>Add Rack</h3>
      <input id="label" placeholder="Rack Label" />
      <input id="pduIp" placeholder="PDU IP" />
      <div class="status-wrap">
        <div id="pduLight" class="status-light"></div>
        <div id="pduStatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <div style="margin:8px 0;display:flex;align-items:center;gap:12px">
        <span style="color:#cbd5e1;font-weight:600;white-space:nowrap">Unmonitored PDU?</span>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="additionalPdu" value="no" id="addPduNo" checked /> No</label>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="additionalPdu" value="yes" id="addPduYes" /> Yes</label>
      </div>

      <div class="row">
        <button id="applyBtn" class="primary disabled" disabled onclick="applyRack()">Apply</button>
        <button onclick="closeAdd()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Remove Modal -->
  <div class="modal" id="removeModal">
    <div class="modal-content">
      <h3>Remove Rack</h3>
      <div class="subtle" style="margin-bottom:10px;">Select one or more racks to delete.</div>
      <div class="remove-list" id="removeList"></div>
      <div class="row" style="margin-top:14px;">
        <button class="danger" onclick="deleteSelected()">Delete Selected</button>
        <button onclick="closeRemove()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Settings Modal -->
  <div class="modal" id="settingsModal">
    <div class="modal-content">
      <h3>Settings</h3>
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Dashboard Title:</span></div>
      <input id="titleInput" />
      <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
        <span style="color:#cbd5e1;font-weight:600;white-space:nowrap">Viewport Style:</span>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="viewportStyle" value="racks" id="vpRacks" checked /> Racks</label>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="viewportStyle" value="fill" id="vpFill" /> Fill</label>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="primary" onclick="saveSettings()">Save</button>
        <button onclick="closeSettings()">Cancel</button>
      </div>
    </div>
  </div>


<script>
  let ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  let racksCache = [];
  let isEdit = false;
  let draggedId = null;
  let pendingOrderSave = false;
  let viewportStyle = "racks";

  ws.onmessage = (event) => {
    let incoming = [];
    try { incoming = JSON.parse(event.data) || []; } catch(e) { incoming = []; }

    if (isEdit || pendingOrderSave) {
      const byId = new Map(incoming.map(r => [String(r.id), r]));
      racksCache = racksCache
        .filter(r => byId.has(String(r.id)))
        .map(r => Object.assign({}, r, byId.get(String(r.id))));

      const existingIds = new Set(racksCache.map(r => String(r.id)));
      for (const r of incoming) {
        if (!existingIds.has(String(r.id))) racksCache.push(r);
      }
    } else {
      racksCache = incoming;
    }

    render(racksCache);
  };

  function computeRackSize(count) {
    const container = document.getElementById("racks");
    if (!container || count <= 0) return;

    // Available height for the entire rack-wrapper (label + rack + ip + status)
    const gridH = container.clientHeight || 400;
    // Measure actual chrome height from an existing rack wrapper, or estimate
    let chrome = 120;
    const existingWrapper = container.querySelector(".rack-wrapper");
    if (existingWrapper) {
      const rackEl = existingWrapper.querySelector(".rack");
      if (rackEl) {
        chrome = existingWrapper.offsetHeight - rackEl.offsetHeight;
        if (chrome < 40) chrome = 120; // fallback if not yet laid out
      }
    }
    const rackH = Math.max(80, gridH - chrome);

    // Available width per rack
    const containerW = container.clientWidth || 1000;
    const gap = 26;
    const totalGap = gap * Math.max(0, count - 1);
    const maxWidthByContainer = (containerW - totalGap) / count;

    let w, finalH;
    if (viewportStyle === "fill") {
      // Fill: racks stretch to fill container width
      w = Math.max(80, maxWidthByContainer);
      finalH = rackH;
    } else {
      // Racks: width from height via 4:9 aspect ratio
      const maxWidthByAspect = rackH * (4 / 9);
      w = Math.max(80, Math.min(maxWidthByContainer, maxWidthByAspect));
      finalH = rackH;
    }
    container.style.setProperty("--rackW", w + "px");
    container.style.setProperty("--rackH", finalH + "px");

    // In fill mode, remove page max-width constraint; in racks mode, restore it
    const page = document.querySelector(".page");
    if (page) page.style.maxWidth = (viewportStyle === "fill") ? "100%" : "";
  }

  window.addEventListener("resize", () => {
    computeRackSize(racksCache.length);
    // Second pass after layout settles to re-measure cqi-scaled chrome
    requestAnimationFrame(() => computeRackSize(racksCache.length));
  });

  function render(racks) {
    let container = document.getElementById("racks");
    container.innerHTML = "";

    racks = [...racks];
    computeRackSize(racks.length);

    racks.forEach(r => {
      let wrapper = document.createElement("div");
      wrapper.className = "rack-wrapper";
      wrapper.dataset.id = String(r.id);

      let div = document.createElement("div");
      div.className = "rack" + (isEdit ? " edit" : "");
      div.style.borderRadius = "0";
      div.style.width = "100%";

      if (isEdit) {
        wrapper.draggable = true;

        wrapper.addEventListener("dragstart", (e) => {
          draggedId = wrapper.dataset.id;
          try { e.dataTransfer.setData("text/plain", draggedId); } catch (_) {}
          e.dataTransfer.effectAllowed = "move";
        });

        wrapper.addEventListener("dragover", (e) => {
          e.preventDefault();
          div.classList.add("dragOver");
          e.dataTransfer.dropEffect = "move";
        });

        wrapper.addEventListener("dragleave", () => div.classList.remove("dragOver"));

        wrapper.addEventListener("drop", async (e) => {
          e.preventDefault();
          div.classList.remove("dragOver");

          const targetId = wrapper.dataset.id;
          const sourceId = draggedId || (function(){ try { return e.dataTransfer.getData("text/plain"); } catch(_) { return null; } })();
          draggedId = null;

          if (!sourceId || !targetId || sourceId === targetId) return;

          const ids = racksCache.map(x => String(x.id));
          const from = ids.indexOf(String(sourceId));
          const to = ids.indexOf(String(targetId));
          if (from === -1 || to === -1) return;

          const moved = racksCache.splice(from, 1)[0];
          racksCache.splice(to, 0, moved);

          render(racksCache);
          await persistOrder();
        });

        wrapper.addEventListener("dragend", () => {
          draggedId = null;
          div.classList.remove("dragOver");
        });
      }

      // Label
      let label = document.createElement("div");
      label.className = "label";
      label.innerText = r.label || "(no label)";
      wrapper.appendChild(label);

      // IP banner
      let pduBanner = document.createElement("div");
      pduBanner.className = "ip-banner";
      pduBanner.textContent = r.pdu_ip ? "PDU: " + r.pdu_ip : "\u00A0";
      wrapper.appendChild(pduBanner);

      // --- PDU circuit area (upper section) ---
      let serverArea = document.createElement("div");
      serverArea.className = "server-area";

      const circuits = r.circuits || [];
      if (circuits.length > 0) {
        circuits.forEach(crt => {
          let block = document.createElement("div");
          block.className = "crt-block";

          let lbl = document.createElement("div");
          lbl.className = "crt-label";
          lbl.textContent = "CIRCUIT " + crt.circuit;
          block.appendChild(lbl);

          let body = document.createElement("div");
          body.className = "crt-body";

          let outlets = document.createElement("div");
          outlets.className = "crt-outlets";
          (crt.outlets || []).forEach(o => {
            let row = document.createElement("div");
            row.className = "crt-outlet";
            let name = o.name || ("Port " + o.port);
            let portLabel = document.createElement("span");
            portLabel.className = "crt-port-label";
            portLabel.textContent = "P" + o.port;
            row.appendChild(portLabel);
            let isDefault = /^(Output\d+|Port \d+)$/.test(name);
            if (isDefault) {
              row.classList.add("empty");
              row.appendChild(document.createTextNode("(empty)"));
            } else {
              row.appendChild(document.createTextNode(name));
            }
            outlets.appendChild(row);
          });
          body.appendChild(outlets);

          let ampsCol = document.createElement("div");
          ampsCol.className = "crt-metric-col";
          let ampsUnit = document.createElement("div");
          ampsUnit.className = "crt-metric-unit";
          ampsUnit.textContent = "AMPS";
          let ampsVal = document.createElement("div");
          if (crt.reachable) {
            ampsVal.className = "crt-metric-val amps";
            ampsVal.textContent = crt.current_a.toFixed(2);
          } else {
            ampsVal.className = "crt-metric-val offline";
            ampsVal.textContent = "--";
          }
          ampsCol.appendChild(ampsUnit);
          ampsCol.appendChild(ampsVal);
          body.appendChild(ampsCol);

          let wattsCol = document.createElement("div");
          wattsCol.className = "crt-metric-col";
          let wattsUnit = document.createElement("div");
          wattsUnit.className = "crt-metric-unit";
          wattsUnit.textContent = "WATTS";
          let wattsVal = document.createElement("div");
          if (crt.reachable) {
            wattsVal.className = "crt-metric-val watts";
            wattsVal.textContent = crt.power_w.toFixed(0);
          } else {
            wattsVal.className = "crt-metric-val offline";
            wattsVal.textContent = "--";
          }
          wattsCol.appendChild(wattsUnit);
          wattsCol.appendChild(wattsVal);
          body.appendChild(wattsCol);

          block.appendChild(body);
          serverArea.appendChild(block);
        });
      } else {
        let msg = document.createElement("div");
        msg.className = "no-pdu-msg";
        msg.textContent = "NO PDU";
        serverArea.appendChild(msg);
      }

      div.appendChild(serverArea);

      wrapper.appendChild(div);

      container.appendChild(wrapper);
    });

    // Re-measure chrome after DOM update so rack heights account for
    // cqi-scaled fonts (which grow with rack width in fill mode)
    requestAnimationFrame(() => {
      computeRackSize(racks.length);
      autoSizeMetrics();
    });
  }

  function autoSizeMetrics() {
    const isFill = viewportStyle === "fill";
    document.querySelectorAll(".crt-block").forEach(block => {
      const body = block.querySelector(".crt-body");
      if (!body) return;
      const bh = body.clientHeight;
      const bw = body.clientWidth;
      if (bh <= 0 || bw <= 0) return;

      // Scale metric values/units based on body height, constrained by column width
      // Use same font size for both amps and watts within a circuit row
      const metricCols = body.querySelectorAll(".crt-metric-col");
      let minValSize = Infinity;
      let minUnitSize = Infinity;
      metricCols.forEach(col => {
        const cw = col.clientWidth - 4;
        if (cw <= 0) return;
        const val = col.querySelector(".crt-metric-val");
        if (val) {
          const text = val.textContent || "000";
          const maxByWidth = cw / (text.length * 0.65);
          const size = Math.max(8, Math.min(bh * 0.55, maxByWidth));
          if (size < minValSize) minValSize = size;
        }
        const unitSize = Math.max(6, Math.min(bh * 0.22, cw * 0.22));
        if (unitSize < minUnitSize) minUnitSize = unitSize;
      });
      metricCols.forEach(col => {
        const unit = col.querySelector(".crt-metric-unit");
        const val = col.querySelector(".crt-metric-val");
        if (unit) unit.style.fontSize = minUnitSize + "px";
        if (val) val.style.fontSize = minValSize + "px";
      });

      // Scale outlet names based on body height
      const outletsCol = body.querySelector(".crt-outlets");
      if (outletsCol) {
        outletsCol.style.flex = isFill ? "3 1 0" : "5 1 0";
        const outletCount = Math.max(1, outletsCol.querySelectorAll(".crt-outlet").length);
        const outletSize = Math.min(minValSize * 0.6, Math.max(7, (bh / outletCount) * 0.75)) + "px";
        outletsCol.querySelectorAll(".crt-outlet").forEach(row => {
          row.style.fontSize = outletSize;
        });
      }

      // Scale circuit label based on block width and height
      const lbl = block.querySelector(".crt-label");
      const blockH = block.clientHeight || bh;
      if (lbl) lbl.style.fontSize = Math.max(6, Math.min(bw * 0.035, blockH * 0.12)) + "px";
    });
  }

  window.addEventListener("resize", () => requestAnimationFrame(autoSizeMetrics));

  async function persistOrder() {
    const ids = racksCache.map(r => r.id);
    pendingOrderSave = true;
    try {
      const resp = await fetch("/api/order", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ ids })
      });
      await resp.json().catch(()=>{});
    } catch (e) {}

    setTimeout(() => { pendingOrderSave = false; }, 1200);
  }

  function toggleEdit() {
    isEdit = !isEdit;

    const btn = document.getElementById("layoutBtn");
    if (isEdit) btn.classList.add("on");
    else btn.classList.remove("on");

    render(racksCache);
  }

  // ---------------- Add Modal ----------------
  let pduCheckTimer = null;
  let lastPduCheckToken = 0;
  let pduOk = false;

  function openAdd() {
    document.getElementById("addModal").style.display = "flex";
    resetAddState(true);
  }
  function closeAdd() {
    document.getElementById("addModal").style.display = "none";
    resetAddState(false);
  }

  function resetAddState(clearInputs) {
    if (pduCheckTimer) clearTimeout(pduCheckTimer);
    pduCheckTimer = null;
    lastPduCheckToken++;
    pduOk = false;

    document.getElementById("pduLight").classList.remove("green");
    document.getElementById("pduStatusText").innerText = "Waiting for PDU\u2026";

    updateApplyBtn();

    if (clearInputs) {
      document.getElementById("label").value = "";
      document.getElementById("pduIp").value = "";
      document.getElementById("addPduNo").checked = true;
    }
  }

  function updateApplyBtn() {
    const btn = document.getElementById("applyBtn");
    if (pduOk) {
      btn.disabled = false;
      btn.classList.remove("disabled");
    } else {
      btn.disabled = true;
      btn.classList.add("disabled");
    }
  }

  function setPduOk(ok, msg) {
    const light = document.getElementById("pduLight");
    const txt = document.getElementById("pduStatusText");
    pduOk = ok;

    if (ok) {
      light.classList.add("green");
      txt.innerText = msg || "PDU SNMP OK";
    } else {
      light.classList.remove("green");
      txt.innerText = msg || "Not connected";
    }
    updateApplyBtn();
  }

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const r = await fetch("/api/settings/title");
      const d = await r.json();
      if (d && d.ok && d.title) applyTitle(d.title);
    } catch(e) {}
    viewportStyle = localStorage.getItem("viewportStyle") || "racks";
    computeRackSize(racksCache.length);

    const pduIpEl = document.getElementById("pduIp");
    pduIpEl.addEventListener("input", () => {
      const ip = pduIpEl.value.trim();
      if (pduCheckTimer) clearTimeout(pduCheckTimer);
      if (!ip) {
        setPduOk(false, "Waiting for PDU\u2026");
        document.getElementById("pduLight").classList.remove("green");
        return;
      }
      pduCheckTimer = setTimeout(() => checkPduIp(ip), 900);
    });
  });

  async function checkPduIp(ip) {
    const token = ++lastPduCheckToken;
    if (!ip || ip.length < 7) {
      pduOk = true;
      document.getElementById("pduLight").classList.remove("green");
      document.getElementById("pduStatusText").innerText = "Waiting for PDU\u2026";
      updateApplyBtn();
      return;
    }
    setPduOk(false, "Checking\u2026");
    try {
      const res = await fetch("/api/check_pdu", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({pdu_ip: ip})
      });
      const data = await res.json();
      if (token !== lastPduCheckToken) return;
      if (data.ok) setPduOk(true, "PDU SNMP OK");
      else setPduOk(false, "No PDU SNMP response");
    } catch(e) {
      if (token !== lastPduCheckToken) return;
      setPduOk(false, "Check failed");
    }
  }

  async function applyRack() {
    const btn = document.getElementById("applyBtn");
    if (btn.disabled) return;

    const label = document.getElementById("label").value.trim();
    const pduIp = document.getElementById("pduIp").value.trim();
    const hasAdditionalPdu = document.getElementById("addPduYes").checked;

    if (!label || !pduIp) {
      setPduOk(false, "Enter label + PDU IP");
      return;
    }

    btn.disabled = true;
    btn.classList.add("disabled");

    try {
      const res = await fetch("/api/racks", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({label: label, pdu_ip: pduIp, has_additional_pdu: hasAdditionalPdu})
      });
      const data = await res.json();
      if (data.ok) closeAdd();
      else setPduOk(false, data.error || "Add failed");
    } catch(e) {
      setPduOk(false, "Add failed");
    }
  }

  // ---------------- Remove Modal ----------------
  function openRemove() {
    document.getElementById("removeModal").style.display = "flex";
    const list = document.getElementById("removeList");
    list.innerHTML = "";

    const items = [...racksCache];

    if (items.length === 0) {
      const empty = document.createElement("div");
      empty.className = "remove-row";
      empty.innerHTML = '<div style="opacity:0.7;font-weight:900;">No racks to remove.</div>';
      list.appendChild(empty);
      return;
    }

    items.forEach(r => {
      const row = document.createElement("label");
      row.className = "remove-row";

      const left = document.createElement("div");
      left.className = "remove-left";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "remove-checkbox";
      cb.value = String(r.id);

      const text = document.createElement("div");
      text.className = "remove-label";
      text.textContent = r.label || "(no label)";

      const ipSpan = document.createElement("span");
      ipSpan.className = "remove-ip";
      ipSpan.textContent = r.pdu_ip ? ("• " + r.pdu_ip) : "";

      left.appendChild(cb);
      left.appendChild(text);
      text.appendChild(ipSpan);

      row.appendChild(left);
      list.appendChild(row);
    });
  }

  function closeRemove() { document.getElementById("removeModal").style.display = "none"; }

  async function deleteSelected() {
    const ids = Array.from(document.querySelectorAll("#removeList input:checked"))
      .map(cb => parseInt(cb.value, 10))
      .filter(n => !isNaN(n));

    if (ids.length === 0) { closeRemove(); return; }

    await fetch("/api/delete", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ids: ids})
    });

    closeRemove();
  }

  // ---------------- Settings ----------------
  async function openSettings() {
    document.getElementById("settingsModal").style.display = "flex";
    const current = document.getElementById("dashTitle").innerText || "";
    document.getElementById("titleInput").value = current.trim();
    // Load viewport style
    document.getElementById(viewportStyle === "fill" ? "vpFill" : "vpRacks").checked = true;
    setTimeout(() => document.getElementById("titleInput").focus(), 50);
  }

  function closeSettings() { document.getElementById("settingsModal").style.display = "none"; }

  function applyTitle(title) {
    const t = (title || "").trim();
    if (!t) return;
    document.getElementById("dashTitle").innerText = t;
    document.title = t;
  }

  async function saveSettings() {
    const titleVal = (document.getElementById("titleInput").value || "").trim();
    const vpVal = document.querySelector('input[name="viewportStyle"]:checked').value;
    // Save viewport style to browser localStorage (per-device)
    localStorage.setItem("viewportStyle", vpVal);
    viewportStyle = vpVal;
    computeRackSize(racksCache.length);
    try {
      const titleRes = await fetch("/api/settings/title", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({title: titleVal})
      });
      const titleData = await titleRes.json();
      if (titleData && titleData.ok && titleData.title) applyTitle(titleData.title);
      closeSettings();
    } catch(e) {}
  }

</script>
</body>
</html>
"""
    html = html.replace("__TITLE__", initial_title)
    return HTMLResponse(html)
