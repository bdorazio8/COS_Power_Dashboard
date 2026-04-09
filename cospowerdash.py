import asyncio
import json
import logging
import sqlite3
import subprocess
import urllib3
from logging.handlers import RotatingFileHandler
from typing import Dict, Set, List, Optional

import requests as req_lib
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------------
# Constants
# ----------------------------

DB = "powerdash.db"
SNMP_COMMUNITY = "public"
DEFAULT_DASH_TITLE = "Power Dashboard"

# PDU model detection (both Server Tech and Raritan share enterprise 13742)
OID_PDU_MODEL = "1.3.6.1.4.1.13742.6.3.2.1.1.3.1"

# Server Tech PRO4X — three-phase
# Inlet aggregate sensors: .1.3.6.1.4.1.13742.6.5.2.3.1.4.1.1.{sensor_type}
# sensor_type: 1=rmsCurrent(÷1000), 4=rmsVoltage(direct V), 5=activePower(direct W), 7=powerFactor(÷100)
OID_STECH_INLET_BASE = "1.3.6.1.4.1.13742.6.5.2.3.1.4.1.1"
# Per-pole (per-phase) sensors: .1.3.6.1.4.1.13742.6.5.3.3.1.4.1.{pole}.{sensor_type}
# Only rmsCurrent (sensor 1) is exposed on this SKU; voltage and power per-phase are not.
# Per-phase watts must be derived: phase_current × inlet_voltage × inlet_pf
OID_STECH_PHASE_BASE = "1.3.6.1.4.1.13742.6.5.3.3.1.4.1"

# Raritan PX3 — 3-phase
# Per-phase inlet sensors: .1.3.6.1.4.1.13742.6.5.2.4.1.4.1.1.{phase}.{sensor_type}
# sensor_type: 1=rmsCurrent(÷1000), 5=activePower(direct W)
OID_RARITAN_PHASE_BASE = "1.3.6.1.4.1.13742.6.5.2.4.1.4.1.1"

# Redfish (iDRAC) endpoints
REDFISH_POWER_PATH = "/redfish/v1/Chassis/System.Embedded.1/Power"
REDFISH_THERMAL_PATH = "/redfish/v1/Chassis/System.Embedded.1/Thermal"
REDFISH_SYSTEM_PATH = "/redfish/v1/Systems/System.Embedded.1"
REDFISH_TIMEOUT = 5

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
    if "pdu2_ip" not in cols:
        cur.execute("ALTER TABLE racks ADD COLUMN pdu2_ip TEXT DEFAULT ''")
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rack_id INTEGER NOT NULL,
            idrac_ip TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()

    conn.close()

def get_racks():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id,label,COALESCE(sort_order,id),COALESCE(pdu_ip,''),COALESCE(pdu2_ip,'')
        FROM racks
        ORDER BY COALESCE(sort_order,id), id
    """)
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "label": r[1], "sort_order": r[2], "pdu_ip": r[3], "pdu2_ip": r[4]} for r in rows]

def _next_sort_order(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM racks")
    row = cur.fetchone()
    return int(row[0] or 0) + 1

def add_rack(label: str, pdu_ip: str = "", pdu2_ip: str = ""):
    conn = _connect()
    cur = conn.cursor()
    next_order = _next_sort_order(cur)
    cur.execute(
        "INSERT INTO racks(label, pdu_ip, community, sort_order, pdu2_ip) VALUES(?,?,?,?,?)",
        (label, pdu_ip, SNMP_COMMUNITY, next_order, pdu2_ip),
    )
    conn.commit()
    conn.close()

def update_rack(rack_id: int, label: str, pdu_ip: str, pdu2_ip: str = ""):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE racks SET label=?, pdu_ip=?, pdu2_ip=? WHERE id=?",
        (label, pdu_ip, pdu2_ip, rack_id),
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
# Servers DB helpers
# ----------------------------

def get_servers_for_rack(rack_id: int) -> List[Dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, rack_id, idrac_ip, name, sort_order "
        "FROM servers WHERE rack_id=? ORDER BY sort_order, id", (rack_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "rack_id": r[1], "idrac_ip": r[2], "name": r[3], "sort_order": r[4]} for r in rows]

def get_all_servers() -> Dict[int, List[Dict]]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id, rack_id, idrac_ip, name, sort_order FROM servers ORDER BY sort_order, id")
    rows = cur.fetchall()
    conn.close()
    grouped: Dict[int, List[Dict]] = {}
    for r in rows:
        entry = {"id": r[0], "rack_id": r[1], "idrac_ip": r[2], "name": r[3], "sort_order": r[4]}
        grouped.setdefault(r[1], []).append(entry)
    return grouped

def add_server(rack_id: int, idrac_ip: str, name: str = "") -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM servers WHERE rack_id=?", (rack_id,))
    count = cur.fetchone()[0]
    if count >= 20:
        conn.close()
        return -1
    cur.execute("SELECT COALESCE(MAX(sort_order),0) FROM servers WHERE rack_id=?", (rack_id,))
    next_order = cur.fetchone()[0] + 1
    cur.execute(
        "INSERT INTO servers(rack_id, idrac_ip, name, sort_order) VALUES(?,?,?,?)",
        (rack_id, idrac_ip, name, next_order),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def delete_server(server_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()

def delete_servers_for_rack(rack_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM servers WHERE rack_id=?", (rack_id,))
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

_pdu_rated_a_cache: Dict[str, float] = {}

# Configured upperCritical threshold for inlet rmsCurrent, in milliamps.
# Column 31 of the PDU2-MIB inletSensorConfigurationTable. This is the
# preferred source — it's the alarm threshold the device itself uses.
# Format: 1.3.6.1.4.1.13742.6.3.3.4.1.{col}.1(role=inlet).1(inletId).1(sensorType=rmsCurrent)
_RATED_AMPS_THRESHOLD_OIDS = [
    "1.3.6.1.4.1.13742.6.3.3.4.1.31.1.1.1",
    "1.3.6.1.4.1.13742.6.3.3.4.1.6.1.1.1",
    "1.3.6.1.4.1.13742.6.3.3.4.1.5.1.1.1",
]

# Inlet nameplate rated current, exposed as a STRING like "80A" by the
# inletConfigurationTable. Used as a fallback when no alarm threshold has
# been configured on the PDU (typical for unconfigured Raritan PX3s).
_RATED_AMPS_NAMEPLATE_OID = "1.3.6.1.4.1.13742.6.3.3.3.1.7.1.1"

RATED_AMPS_DEFAULT = 30.0  # Hardcoded fallback when auto-detection fails

def _parse_amps_string(raw: Optional[str]) -> Optional[float]:
    """Parse an SNMP STRING return like 'STRING: "80A"' into a float in amps."""
    if not raw or "STRING:" not in raw:
        return None
    s = raw.split("STRING:", 1)[1].strip().strip('"').strip()
    s = s.rstrip("Aa").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None

def get_pdu_rated_amps(ip: str) -> float:
    """Discover the PDU's per-phase rated inlet current in amps.

    Order of precedence:
      1. Configured upperCritical threshold from the inlet sensor config table.
         This reflects the user-set alarm threshold and is the most accurate
         "100% load" reference when commissioned correctly.
      2. Nameplate inletRatedCurrent string from the inlet config table
         (e.g. "80A"). Used when the PDU has no thresholds configured.
      3. RATED_AMPS_DEFAULT hardcoded fallback.
    Result is cached per IP for the process lifetime.
    """
    if ip in _pdu_rated_a_cache:
        return _pdu_rated_a_cache[ip]

    # 1. Configured upperCritical threshold (milliamps)
    for oid in _RATED_AMPS_THRESHOLD_OIDS:
        raw = snmp_get(ip, oid)
        val = _parse_int(raw) if raw else None
        if val is not None and 5000 <= val <= 200000:  # 5–200 A in milliamps
            amps = round(val / 1000.0, 1)
            _pdu_rated_a_cache[ip] = amps
            logger.info("PDU %s rated amps detected: %.1f A (upperCritical via %s)", ip, amps, oid)
            return amps

    # 2. Nameplate rated current string (e.g. "80A")
    raw = snmp_get(ip, _RATED_AMPS_NAMEPLATE_OID)
    nameplate_a = _parse_amps_string(raw)
    if nameplate_a is not None and 1 <= nameplate_a <= 200:
        _pdu_rated_a_cache[ip] = nameplate_a
        logger.info("PDU %s rated amps detected: %.1f A (nameplate via %s)", ip, nameplate_a, _RATED_AMPS_NAMEPLATE_OID)
        return nameplate_a

    _pdu_rated_a_cache[ip] = RATED_AMPS_DEFAULT
    logger.warning("PDU %s rated amps auto-detect failed, defaulting to %.0f A", ip, RATED_AMPS_DEFAULT)
    return RATED_AMPS_DEFAULT

def detect_pdu_type(ip: str) -> Optional[str]:
    """Auto-detect PDU type by querying model string OID. Returns 'servertech', 'raritan', or None."""
    raw = snmp_get(ip, OID_PDU_MODEL)
    if not raw or "STRING:" not in raw:
        return None
    model = raw.split("STRING:", 1)[1].strip().strip('"').strip()
    if model.upper().startswith("PRO4X"):
        return "servertech"
    elif model.upper().startswith("PX3"):
        return "raritan"
    return None

# ----------------------------
# Redfish Helpers
# ----------------------------

def _redfish_get(ip: str, path: str, username: str, password: str) -> Optional[dict]:
    """Blocking HTTP GET to a Redfish endpoint. Returns parsed JSON or None."""
    try:
        url = f"https://{ip}{path}"
        resp = req_lib.get(url, auth=(username, password), verify=False, timeout=REDFISH_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Redfish %s returned %d", url, resp.status_code)
        return None
    except Exception as e:
        logger.warning("Redfish error for %s: %s", ip, e)
        return None

# ----------------------------
# Poll Loop / Live State
# ----------------------------

latest_status: Dict[int, Dict] = {}
latest_systems_status: Dict[int, List[Dict]] = {}
latest_pdu_phases: Dict[int, List[Dict]] = {}

_poll_count = 0
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
        status["pdu2_ip"] = r.get("pdu2_ip", "")
        status["sort_order"] = r.get("sort_order")
        status["systems"] = latest_systems_status.get(rid, [])
        status["pdus"] = latest_pdu_phases.get(rid, [])
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

            # Poll PDUs for each rack
            global _poll_count
            _poll_count += 1
            pdu_ping_cache: Dict[str, bool] = {}
            pdu_type_cache: Dict[str, Optional[str]] = {}

            for rack in racks:
                rid = rack["id"]
                phases_all = []

                for pdu_key in ("pdu_ip", "pdu2_ip"):
                    pdu_ip = rack.get(pdu_key, "")
                    if not pdu_ip:
                        continue

                    if pdu_ip not in pdu_ping_cache:
                        pdu_ping_cache[pdu_ip] = ping_ok(pdu_ip)
                    if not pdu_ping_cache[pdu_ip]:
                        phases_all.append({"pdu_ip": pdu_ip, "pdu_key": pdu_key, "reachable": False, "total_w": None, "total_a": None, "rated_a": _pdu_rated_a_cache.get(pdu_ip, RATED_AMPS_DEFAULT), "phases": [
                            {"label": "Phase " + p, "current_a": 0, "power_w": 0, "reachable": False} for p in ("A", "B", "C")
                        ]})
                        continue

                    if pdu_ip not in pdu_type_cache:
                        pdu_type_cache[pdu_ip] = detect_pdu_type(pdu_ip)
                    pdu_type = pdu_type_cache[pdu_ip]

                    phases = []
                    total_w: Optional[int] = None
                    total_a: Optional[float] = None
                    if pdu_type == "servertech":
                        # PRO4X exposes per-phase current only — not per-phase voltage or watts.
                        # We deliberately do NOT derive per-phase watts (would be inaccurate on
                        # unbalanced loads). Per-phase watts are sent as null and dimmed in UI.
                        # Real, hardware-measured inlet total watts is shown in the total banner.
                        for phase_idx, phase_label in [(1, "A"), (2, "B"), (3, "C")]:
                            raw_a = snmp_get(pdu_ip, f"{OID_STECH_PHASE_BASE}.{phase_idx}.1")
                            amps_raw = _parse_int(raw_a) if raw_a else None
                            amps = round(amps_raw / 1000, 2) if amps_raw is not None else 0.0
                            phases.append({"label": "Phase " + phase_label, "current_a": amps, "power_w": None, "reachable": True})
                        raw_total_w = snmp_get(pdu_ip, f"{OID_STECH_INLET_BASE}.5")
                        total_w_raw = _parse_int(raw_total_w) if raw_total_w else None
                        total_w = total_w_raw if total_w_raw is not None else 0
                        total_a = round(sum(p["current_a"] for p in phases), 2)

                    elif pdu_type == "raritan":
                        for phase_idx, phase_label in [(1, "A"), (2, "B"), (3, "C")]:
                            raw_a = snmp_get(pdu_ip, f"{OID_RARITAN_PHASE_BASE}.{phase_idx}.1")
                            raw_w = snmp_get(pdu_ip, f"{OID_RARITAN_PHASE_BASE}.{phase_idx}.5")
                            amps_raw = _parse_int(raw_a) if raw_a else None
                            watts_raw = _parse_int(raw_w) if raw_w else None
                            amps = round(amps_raw / 1000, 2) if amps_raw is not None else 0.0
                            watts = watts_raw if watts_raw is not None else 0
                            phases.append({"label": "Phase " + phase_label, "current_a": amps, "power_w": watts, "reachable": True})
                        # Hardware-measured per-phase values → exact totals
                        total_w = sum(p["power_w"] for p in phases)
                        total_a = round(sum(p["current_a"] for p in phases), 2)
                    else:
                        phases = [{"label": "Phase " + p, "current_a": 0, "power_w": 0, "reachable": False} for p in ("A", "B", "C")]

                    rated_a = get_pdu_rated_amps(pdu_ip)
                    phases_all.append({"pdu_ip": pdu_ip, "pdu_key": pdu_key, "reachable": True, "type": pdu_type, "phases": phases, "total_w": total_w, "total_a": total_a, "rated_a": rated_a})

                latest_pdu_phases[rid] = phases_all

            # Clean up data for deleted racks
            for rid in list(latest_pdu_phases.keys()):
                if rid not in current_ids:
                    latest_pdu_phases.pop(rid, None)

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
    pdu2_ip: str = ""

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
    pdu2_ip = (r.pdu2_ip or "").strip()
    if not label or not pdu_ip:
        return {"ok": False, "error": "Missing label or PDU IP"}
    add_rack(label, pdu_ip, pdu2_ip)
    logger.info("Rack added: %s (PDU1 %s, PDU2 %s)", label, pdu_ip, pdu2_ip or "none")
    return {"ok": True}

@app.post("/api/racks/update")
def api_update_rack(data: dict):
    rack_id = data.get("id")
    label = (data.get("label") or "").strip()
    pdu_ip = (data.get("pdu_ip") or "").strip()
    pdu2_ip = (data.get("pdu2_ip") or "").strip()
    if not rack_id or not label or not pdu_ip:
        return {"ok": False, "error": "Missing id, label, or PDU IP"}
    update_rack(int(rack_id), label, pdu_ip, pdu2_ip)
    logger.info("Rack updated: id=%s %s (PDU1 %s, PDU2 %s)", rack_id, label, pdu_ip, pdu2_ip or "none")
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
        delete_servers_for_rack(rid)
    logger.info("Racks deleted: %s", ids_int)
    return {"ok": True}

@app.post("/api/check_pdu")
def api_check_pdu_ip(data: dict):
    ip = (data.get("pdu_ip") or "").strip()
    if not ip:
        return {"ok": False}
    if not ping_ok(ip):
        return {"ok": False, "error": "PDU not reachable"}
    pdu_type = detect_pdu_type(ip)
    if pdu_type:
        return {"ok": True, "type": pdu_type}
    return {"ok": False, "error": "Unknown PDU type"}

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

@app.get("/api/settings/idrac")
def api_get_idrac_creds():
    username = get_setting("idrac_username", "")
    has_password = bool(get_setting("idrac_password", ""))
    return {"ok": True, "username": username, "has_password": has_password}

@app.post("/api/settings/idrac")
def api_set_idrac_creds(data: dict):
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return {"ok": False, "error": "Username and password required"}
    set_setting("idrac_username", username)
    set_setting("idrac_password", password)
    logger.info("iDRAC credentials updated (user: %s)", username)
    return {"ok": True}

@app.post("/api/settings/idrac/test")
def api_test_idrac(data: dict):
    ip = (data.get("ip") or "").strip()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not password:
        password = get_setting("idrac_password", "")
    if not ip or not username or not password:
        return {"ok": False, "error": "Need IP and credentials"}
    result = _redfish_get(ip, REDFISH_SYSTEM_PATH, username, password)
    if result:
        model = result.get("Model", "Unknown")
        return {"ok": True, "model": model}
    return {"ok": False, "error": "Could not connect to iDRAC"}

# ----------------------------
# OME Settings & Reports API
# ----------------------------

def _ome_session() -> Optional[str]:
    """Authenticate to OME and return X-Auth-Token, or None on failure."""
    host = get_setting("ome_host", "")
    username = get_setting("ome_username", "")
    password = get_setting("ome_password", "")
    if not host or not username or not password:
        return None
    try:
        resp = req_lib.post(
            f"https://{host}/api/SessionService/Sessions",
            json={"UserName": username, "Password": password},
            verify=False, timeout=REDFISH_TIMEOUT,
        )
        if resp.status_code == 200 or resp.status_code == 201:
            return resp.headers.get("X-Auth-Token")
        return None
    except Exception as e:
        logger.warning("OME auth failed: %s", e)
        return None

def _ome_get(token: str, path: str) -> Optional[dict]:
    """GET request to OME API with auth token."""
    host = get_setting("ome_host", "")
    try:
        resp = req_lib.get(
            f"https://{host}/api{path}",
            headers={"X-Auth-Token": token},
            verify=False, timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logger.warning("OME GET %s failed: %s", path, e)
        return None

def _ome_post(token: str, path: str, body: dict):
    """POST request to OME API with auth token."""
    host = get_setting("ome_host", "")
    try:
        resp = req_lib.post(
            f"https://{host}/api{path}",
            headers={"X-Auth-Token": token, "Content-Type": "application/json"},
            json=body, verify=False, timeout=10,
        )
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return resp.text.strip()
        return None
    except Exception as e:
        logger.warning("OME POST %s failed: %s", path, e)
        return None

@app.get("/api/settings/ome")
def api_get_ome_creds():
    host = get_setting("ome_host", "")
    username = get_setting("ome_username", "")
    has_password = bool(get_setting("ome_password", ""))
    return {"ok": True, "host": host, "username": username, "has_password": has_password}

@app.post("/api/settings/ome")
def api_set_ome_creds(data: dict):
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not host or not username or not password:
        return {"ok": False, "error": "Host, username, and password required"}
    set_setting("ome_host", host)
    set_setting("ome_username", username)
    set_setting("ome_password", password)
    logger.info("OME credentials updated (host: %s, user: %s)", host, username)
    return {"ok": True}

@app.post("/api/settings/ome/test")
def api_test_ome(data: dict):
    host = (data.get("host") or "").strip() or get_setting("ome_host", "")
    username = (data.get("username") or "").strip() or get_setting("ome_username", "")
    password = (data.get("password") or "").strip()
    if not password:
        password = get_setting("ome_password", "")
    if not host or not username or not password:
        return {"ok": False, "error": "Need host, username, and password"}
    try:
        resp = req_lib.post(
            f"https://{host}/api/SessionService/Sessions",
            json={"UserName": username, "Password": password},
            verify=False, timeout=REDFISH_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return {"ok": True}
        return {"ok": False, "error": f"Auth failed (HTTP {resp.status_code})"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/reports/available")
def api_reports_available():
    token = _ome_session()
    if not token:
        return {"ok": False, "error": "OME not configured or auth failed"}
    data = _ome_get(token, "/ReportService/ReportDefs")
    if not data:
        return {"ok": False, "error": "Failed to fetch report definitions"}
    reports = []
    for r in data.get("value", []):
        cat = r.get("Category", "")
        name = r.get("Name", "")
        if "Power" in cat or "power" in name.lower() or "energy" in name.lower() or "greenhouse" in name.lower() or "gpu" in name.lower() or "cpu" in name.lower() or "thermal" in name.lower():
            cols = [c["Name"] for c in r.get("ColumnNames", [])]
            reports.append({"id": r["Id"], "name": name, "category": cat, "columns": cols})
    return {"ok": True, "reports": reports}

@app.post("/api/reports/run")
def api_run_report(data: dict):
    report_id = data.get("report_id")
    if not report_id:
        return {"ok": False, "error": "Missing report_id"}
    token = _ome_session()
    if not token:
        return {"ok": False, "error": "OME not configured or auth failed"}
    # Get all configured server iDRAC IPs for filtering
    all_servers = get_all_servers()
    configured_ips = set()
    for rack_servers in all_servers.values():
        for srv in rack_servers:
            configured_ips.add(srv["idrac_ip"].strip())
    # Run the report
    _ome_post(token, "/ReportService/Actions/ReportService.RunReport", {"ReportDefId": int(report_id)})
    # Fetch results
    results = _ome_get(token, f"/ReportService/ReportDefs({report_id})/ReportResults/ResultRows")
    if not results:
        return {"ok": False, "error": "Failed to fetch report results"}
    # Get column names
    report_def = _ome_get(token, f"/ReportService/ReportDefs({report_id})")
    columns = []
    if report_def:
        columns = [c["Name"] for c in report_def.get("ColumnNames", [])]
    # Filter rows to only servers configured in our dashboard
    # Server IP/Name is typically the first column in OME reports
    rows = []
    for row in results.get("value", []):
        values = row.get("Values", [])
        if not values:
            continue
        server_id = values[0].strip() if values[0] else ""
        if configured_ips and server_id in configured_ips:
            rows.append(values)
    if not configured_ips:
        return {"ok": False, "error": "No servers configured in dashboard.\nAdd servers to racks first."}
    return {"ok": True, "columns": columns, "rows": rows}

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
    if not ip:
        return {"ok": False}
    if not ping_ok(ip):
        return {"ok": False, "error": "PDU not reachable"}
    pdu_type = detect_pdu_type(ip)
    if pdu_type:
        return {"ok": True, "type": pdu_type}
    return {"ok": False, "error": "Unknown PDU type"}

# ----------------------------
# Servers API
# ----------------------------

class ServerPayload(BaseModel):
    rack_id: int
    idrac_ip: str
    name: str = ""

class ServerDeletePayload(BaseModel):
    id: int

@app.post("/api/servers")
def api_add_server(s: ServerPayload):
    idrac_ip = (s.idrac_ip or "").strip()
    name = (s.name or "").strip()
    if not idrac_ip:
        return {"ok": False, "error": "Missing iDRAC IP"}
    username = get_setting("idrac_username", "")
    password = get_setting("idrac_password", "")
    if not username or not password:
        return {"ok": False, "error": "Configure iDRAC credentials in Settings first"}
    result = _redfish_get(idrac_ip, REDFISH_SYSTEM_PATH, username, password)
    if not result:
        return {"ok": False, "error": "Cannot connect to iDRAC at " + idrac_ip}
    model = result.get("Model", "")
    new_id = add_server(s.rack_id, idrac_ip, name)
    if new_id == -1:
        return {"ok": False, "error": "Maximum 20 servers per rack"}
    logger.info("Server added: %s (%s, %s) on rack %d", name or idrac_ip, idrac_ip, model, s.rack_id)
    return {"ok": True, "id": new_id, "model": model}

@app.post("/api/servers/delete")
def api_delete_server(s: ServerDeletePayload):
    delete_server(s.id)
    logger.info("Server deleted: id=%d", s.id)
    return {"ok": True}

@app.get("/api/servers/{rack_id}")
def api_get_servers(rack_id: int):
    servers = get_servers_for_rack(rack_id)
    return {"ok": True, "servers": servers}

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
    border-radius: 0 0 18px 18px;
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
    position: relative;
  }

  .edit-pen {
    position: absolute;
    right: 6px;
    top: 50%;
    transform: translateY(-50%);
    width: 26px;
    height: 26px;
    background: transparent;
    border: none;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    opacity: 0.7;
    transition: opacity 150ms ease;
  }
  .edit-pen:hover {
    opacity: 1;
  }
  .edit-pen svg { width: 14px; height: 14px; display: block; }

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
  .pdu-load-section {
    /* Fixed-size section so 1-PDU and 2-PDU racks render identical
       load boxes. Phase blocks above absorb any extra vertical space. */
    flex: 0 0 auto;
    height: clamp(40px, 13cqi, 100px);
    margin-top: 4px;
    padding: 4px 7px;
    border-top: 2px solid rgba(167,139,250,0.55);
    background: rgba(15,23,42,0.35);
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
    overflow: hidden;
    box-sizing: border-box;
  }
  .pdu-load-section.split {
    height: clamp(100px, 38cqi, 270px);
    gap: 3px;
  }
  .pdu-load-row {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    align-items: center;
    gap: 6px;
    overflow: hidden;
  }
  .pdu-load-row-label {
    flex: 0 0 auto;
    font-weight: 900;
    font-size: clamp(13px, 4.4cqi, 40px);
    color: rgba(226,232,240,0.92);
    white-space: nowrap;
    letter-spacing: 0.3px;
    text-align: right;
    min-width: 3.4em;
  }
  .pdu-load-section.split .pdu-load-row-label { font-size: clamp(11px, 3.6cqi, 30px); min-width: 4em; }
  .pdu-load-track {
    flex: 1 1 0;
    min-width: 0;
    /* Fixed thickness driven by rack WIDTH only, so bars look identical
       on 1-PDU and 2-PDU racks regardless of how tall the section is. */
    height: clamp(18px, 5.2cqi, 44px);
    display: flex;
    gap: 0;
  }
  .pdu-load-seg {
    flex: 1 1 0;
    min-width: 0;
    border-right: 2px solid #0b1220;
    background: rgba(255,255,255,0.08);
    transition: background 300ms;
  }
  .pdu-load-seg:last-child { border-right: none; }
  .pdu-load-seg.on-green { background: #22c55e; box-shadow: 0 0 4px rgba(34,197,94,0.45); }
  .pdu-load-seg.on-amber { background: #f59e0b; box-shadow: 0 0 4px rgba(245,158,11,0.45); }
  .pdu-load-seg.on-red   { background: #ef4444; box-shadow: 0 0 4px rgba(239,68,68,0.45); }
  .pdu-load-row-label.offline { color: rgba(255,255,255,0.3); }
  /* Inline load bar inside each phase block (alternate render style) */
  .crt-block .pdu-load-track.inline-loadbar {
    flex: 0 0 auto;
    height: clamp(8px, 2.4cqi, 20px);
    margin-top: 3px;
    margin-bottom: 1px;
  }
  .crt-block .pdu-load-track.inline-loadbar .pdu-load-seg {
    border-right-width: 1px;
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
  .crt-metric-val.unavailable { color: rgba(167,139,250,0.25); font-size: 14px; font-style: italic; }
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
    padding: 14px 14px;
    gap: 10px;
  }
  .remove-row + .remove-row {
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .remove-left {
    display:flex;
    align-items:flex-start;
    gap: 12px;
    width: 100%;
  }
  .remove-text {
    display: flex;
    flex-direction: column;
  }
  .remove-label {
    font-weight: 900;
    font-size: 16px;
  }
  .remove-ip {
    font-size: 12px;
    opacity: 0.55;
    font-weight: 600;
    margin-top: 2px;
  }
  .remove-checkbox { width: 22px; height: 22px; min-width: 22px; max-width: 22px; flex-shrink: 0; margin-top: 2px; }

  .hint {
    opacity:0.7;
    font-size: 13px;
    margin-top: -4px;
    margin-bottom: 10px;
    line-height: 1.35;
  }

  #reportsTable th {
    background: #1e293b;
    color: #94a3b8;
    font-weight: 700;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 8px 6px;
    text-align: left;
    border-bottom: 1px solid rgba(255,255,255,0.1);
    white-space: nowrap;
  }
  #reportsTable td {
    padding: 6px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    white-space: nowrap;
  }
  #reportsTable tr:hover td {
    background: rgba(255,255,255,0.03);
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
      <div class="icon-btn" onclick="openReports()" title="Reports">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 2a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6H6z" fill="none" stroke="rgba(226,232,240,0.92)" stroke-width="1.8"/>
          <polyline points="14 2 14 8 20 8" fill="none" stroke="rgba(226,232,240,0.92)" stroke-width="1.8"/>
          <line x1="8" y1="13" x2="16" y2="13" stroke="rgba(226,232,240,0.92)" stroke-width="1.5" stroke-linecap="round"/>
          <line x1="8" y1="17" x2="13" y2="17" stroke="rgba(226,232,240,0.92)" stroke-width="1.5" stroke-linecap="round"/>
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
      <input id="pduIp" placeholder="PDU 1 IP" />
      <div class="status-wrap">
        <div id="pduLight" class="status-light"></div>
        <div id="pduStatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <input id="pdu2Ip" placeholder="PDU 2 IP (optional)" />
      <div class="status-wrap">
        <div id="pdu2Light" class="status-light"></div>
        <div id="pdu2StatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <div id="addError" style="color:#f87171;font-weight:700;font-size:13px;min-height:18px;margin-bottom:4px"></div>
      <div class="row">
        <button id="applyBtn" class="primary disabled" disabled onclick="applyRack()">Apply</button>
        <button onclick="closeAdd()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Edit Modal -->
  <div class="modal" id="editModal">
    <div class="modal-content">
      <h3>Edit Rack</h3>
      <input id="editLabel" placeholder="Rack Label" />
      <input id="editPduIp" placeholder="PDU 1 IP" />
      <div class="status-wrap">
        <div id="editPduLight" class="status-light"></div>
        <div id="editPduStatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <input id="editPdu2Ip" placeholder="PDU 2 IP (optional)" />
      <div class="status-wrap">
        <div id="editPdu2Light" class="status-light"></div>
        <div id="editPdu2StatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <div style="margin-top:12px;border-top:1px solid rgba(255,255,255,0.08);padding-top:12px">
        <div style="margin-bottom:6px"><span style="color:#cbd5e1;font-weight:600">Servers (iDRAC):</span></div>
        <div id="editServerList" style="margin-bottom:8px"></div>
        <div style="display:flex;gap:6px;align-items:center">
          <input id="editServerIp" placeholder="iDRAC IP" style="flex:1;margin-bottom:0" />
          <button onclick="addServerToRack()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Add</button>
        </div>
        <div id="editServerError" style="color:#f87171;font-weight:700;font-size:13px"></div>
      </div>

      <div id="editError" style="color:#f87171;font-weight:700;font-size:13px"></div>
      <div class="row">
        <button id="editApplyBtn" class="primary" onclick="applyEdit()">Save</button>
        <button onclick="closeEdit()">Cancel</button>
      </div>
      <div id="editRemoveRow" style="margin-top:8px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px">
        <button style="background:rgba(220,38,38,0.2);color:rgba(248,113,113,0.7);font-size:13px;padding:8px 14px;border-radius:10px;border:1px solid rgba(220,38,38,0.15);cursor:pointer;font-weight:700" onclick="showRemoveConfirm()">Remove Rack</button>
      </div>
      <div id="editConfirm" style="display:none;margin-top:8px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-weight:700;font-size:13px;color:rgba(248,113,113,0.7)">Remove this rack?</span>
          <button onclick="cancelRemoveConfirm()" style="font-size:13px;padding:8px 14px">Cancel</button>
          <button class="danger" onclick="confirmRemove()" style="font-size:13px;padding:8px 14px">Yes</button>
        </div>
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
      <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
        <span style="color:#cbd5e1;font-weight:600;white-space:nowrap">PDU Load Style:</span>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="pduLoadStyle" value="grouped" id="loadGrouped" checked /> Grouped Below</label>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="pduLoadStyle" value="inline" id="loadInline" /> Inline In Phase</label>
      </div>
      <div style="margin-top:16px;border-top:1px solid rgba(255,255,255,0.08);padding-top:12px">
        <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">iDRAC Credentials:</span></div>
        <div style="display:flex;gap:8px;margin-bottom:8px">
          <input id="idracUser" placeholder="Username" style="flex:1;margin-bottom:0" />
          <input id="idracPass" type="password" placeholder="Password" style="flex:1;margin-bottom:0" />
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <input id="idracTestIp" placeholder="Test IP (any iDRAC)" style="margin-bottom:0;flex:1" />
          <button onclick="testIdrac()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Test</button>
        </div>
        <div id="idracTestResult" style="font-size:13px;font-weight:700;min-height:0"></div>
      </div>
      <div style="margin-top:16px;border-top:1px solid rgba(255,255,255,0.08);padding-top:12px">
        <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">OME Connection:</span></div>
        <input id="omeHost" placeholder="OME Host (IP or hostname)" style="margin-bottom:8px" />
        <div style="display:flex;gap:8px;margin-bottom:8px">
          <input id="omeUser" placeholder="Username" style="flex:1;margin-bottom:0" />
          <input id="omePass" type="password" placeholder="Password" style="flex:1;margin-bottom:0" />
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <button onclick="testOme()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Test Connection</button>
          <span id="omeTestResult" style="font-size:13px;font-weight:700"></span>
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="primary" onclick="saveSettings()">Save</button>
        <button onclick="closeSettings()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Reports Modal -->
  <div class="modal" id="reportsModal">
    <div id="reportsContent" class="modal-content" style="max-height:85vh;display:flex;flex-direction:column">
      <h3>Reports</h3>
      <div id="reportsNotConfigured" style="display:none;padding:12px 0">
        <div style="font-weight:700;color:#f87171;margin-bottom:8px">OME connection not configured.</div>
        <div style="opacity:0.7;font-size:13px;margin-bottom:12px">Go to Settings and configure the OME connection first.</div>
        <button onclick="closeReports()">OK</button>
      </div>
      <div id="reportsError" style="display:none;padding:12px 0">
        <div id="reportsErrorMsg" style="font-weight:700;color:#f87171;margin-bottom:12px;white-space:pre-line"></div>
        <button onclick="closeReports()">OK</button>
      </div>
      <div id="reportsSelector" style="display:none">
        <div style="margin-bottom:8px"><span style="color:#cbd5e1;font-weight:600">Select Report:</span></div>
        <select id="reportSelect" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,0.16);background:#0b1220;color:white;font-size:14px;margin-bottom:10px"></select>
        <div class="row">
          <button class="primary" onclick="runReport()">Generate</button>
          <button onclick="closeReports()">Cancel</button>
        </div>
      </div>
      <div id="reportsLoading" style="display:none;padding:20px 0;text-align:center;opacity:0.6;font-weight:700">Loading...</div>
      <div id="reportsResults" style="display:none;flex:1;min-height:0;overflow:auto;margin-top:10px">
        <table id="reportsTable" style="width:100%;border-collapse:collapse;font-size:12px">
          <thead id="reportsHead" style="position:sticky;top:0"></thead>
          <tbody id="reportsBody"></tbody>
        </table>
      </div>
      <div id="reportsActions" style="display:none;margin-top:10px">
        <div class="row">
          <button onclick="backToReportSelect()">Back</button>
          <button onclick="closeReports()">Close</button>
        </div>
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
  let pduLoadStyle = "grouped"; // 'grouped' = three bars stacked below phases, 'inline' = bar inside each phase block

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
    let chrome = 50;
    const existingWrapper = container.querySelector(".rack-wrapper");
    if (existingWrapper) {
      const rackEl = existingWrapper.querySelector(".rack");
      if (rackEl) {
        chrome = existingWrapper.offsetHeight - rackEl.offsetHeight;
        if (chrome < 20) chrome = 50; // fallback if not yet laid out
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
      div.style.borderRadius = "0 0 18px 18px";
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
      label.textContent = r.label || "(no label)";
      let pen = document.createElement("div");
      pen.className = "edit-pen";
      pen.innerHTML = '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" fill="rgba(226,232,240,0.92)"/></svg>';
      pen.onclick = (e) => { e.stopPropagation(); openEdit(r); };
      label.appendChild(pen);
      wrapper.appendChild(label);

      // PDU sections
      let serverArea = document.createElement("div");
      serverArea.className = "server-area";

      const pdus = r.pdus || [];
      if (pdus.length > 0) {
        pdus.forEach((pdu, pduIdx) => {
          // PDU IP banner inside rack body
          let pduBanner = document.createElement("div");
          pduBanner.className = "ip-banner";
          pduBanner.textContent = pdu.pdu_ip ? ((pdu.type === "servertech" ? "Server Tech" : pdu.type === "raritan" ? "Raritan" : "PDU") + ": " + pdu.pdu_ip) : "\u00A0";
          serverArea.appendChild(pduBanner);

          // 3 phase boxes
          const ratedAForInline = (pdu.rated_a && pdu.rated_a > 0) ? pdu.rated_a : 30;
          const blocks = (pdu.phases || []).slice();
          blocks.forEach(phase => {
            let block = document.createElement("div");
            block.className = "crt-block";

            let lbl = document.createElement("div");
            lbl.className = "crt-label";
            lbl.textContent = phase.label || "Phase";
            block.appendChild(lbl);

            let body = document.createElement("div");
            body.className = "crt-body";

            let ampsCol = document.createElement("div");
            ampsCol.className = "crt-metric-col";
            let ampsUnit = document.createElement("div");
            ampsUnit.className = "crt-metric-unit";
            ampsUnit.textContent = "AMPS";
            let ampsVal = document.createElement("div");
            if (phase.reachable) {
              ampsVal.className = "crt-metric-val amps";
              ampsVal.textContent = phase.current_a.toFixed(2);
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
            if (!phase.reachable) {
              wattsVal.className = "crt-metric-val offline";
              wattsVal.textContent = "--";
            } else if (phase.power_w === null || phase.power_w === undefined) {
              // Per-phase watts not exposed by hardware (e.g. ServerTech PRO4X).
              // Dim placeholder so layout matches Raritan but does not imply real data.
              wattsVal.className = "crt-metric-val unavailable";
              wattsVal.textContent = "---";
            } else {
              wattsVal.className = "crt-metric-val watts";
              wattsVal.textContent = phase.power_w.toFixed(0);
            }
            wattsCol.appendChild(wattsUnit);
            wattsCol.appendChild(wattsVal);
            body.appendChild(wattsCol);

            block.appendChild(body);

            // Inline load bar at the bottom of the phase block (only when style=inline)
            if (pduLoadStyle === "inline") {
              block.appendChild(buildInlineLoadBar(phase, ratedAForInline));
            }

            serverArea.appendChild(block);
          });

          // Grouped load section below all phases (only when style=grouped)
          const loadSection = buildLoadSection(pdu);
          if (loadSection) serverArea.appendChild(loadSection);
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

  function loadColorClass(pct) {
    if (pct >= 85) return "red";
    if (pct >= 70) return "amber";
    return "green";
  }

  const PDU_LOAD_SEGMENTS = 30;
  const PDU_LOAD_SEGMENTS_INLINE = 15;

  function buildSegmentedTrack(pct, available, segmentCount) {
    const n = segmentCount || PDU_LOAD_SEGMENTS;
    const track = document.createElement("div");
    track.className = "pdu-load-track";
    const lit = available ? Math.round((Math.min(100, Math.max(0, pct)) / 100) * n) : 0;
    const colorCls = available ? "on-" + loadColorClass(pct) : "";
    for (let s = 0; s < n; s++) {
      const seg = document.createElement("div");
      seg.className = "pdu-load-seg" + (s < lit ? " " + colorCls : "");
      track.appendChild(seg);
    }
    return track;
  }

  function buildInlineLoadBar(phase, ratedA) {
    const available = phase.reachable && ratedA > 0;
    const pct = available ? Math.min(100, (phase.current_a / ratedA) * 100) : 0;
    const track = buildSegmentedTrack(pct, available, PDU_LOAD_SEGMENTS_INLINE);
    track.classList.add("inline-loadbar");
    return track;
  }

  function buildLoadRow(labelText, pct, available) {
    const row = document.createElement("div");
    row.className = "pdu-load-row";
    const label = document.createElement("div");
    label.className = "pdu-load-row-label" + (available ? "" : " offline");
    label.textContent = available ? (labelText + " " + pct.toFixed(0) + "%") : (labelText + " --");
    row.appendChild(label);
    row.appendChild(buildSegmentedTrack(pct, available));
    return row;
  }

  function buildLoadSection(pdu) {
    // Inline mode renders bars inside each phase block, not in a section.
    if (pduLoadStyle === "inline") return null;

    const section = document.createElement("div");
    section.className = "pdu-load-section split";

    const ratedA = (pdu.rated_a && pdu.rated_a > 0) ? pdu.rated_a : 30;
    const phases = pdu.phases || [];

    phases.forEach(phase => {
      const letter = (phase.label || "").replace("Phase ", "");
      const available = phase.reachable && ratedA > 0;
      const pct = available ? Math.min(100, (phase.current_a / ratedA) * 100) : 0;
      section.appendChild(buildLoadRow(letter, pct, available));
    });
    return section;
  }

  function autoSizeMetrics() {
    document.querySelectorAll(".crt-block").forEach(block => {
      const body = block.querySelector(".crt-body");
      if (!body) return;
      const bh = body.clientHeight;
      const bw = body.clientWidth;
      if (bh <= 0 || bw <= 0) return;

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

      const lbl = block.querySelector(".crt-label");
      const blockH = block.clientHeight || bh;
      if (lbl) lbl.style.fontSize = Math.max(8, Math.min(bw * 0.055, blockH * 0.2)) + "px";
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
  let pdu2CheckTimer = null;
  let lastPdu2CheckToken = 0;
  let pduOk = false;
  let pdu2Ok = true;  // empty PDU 2 is OK

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
    if (pdu2CheckTimer) clearTimeout(pdu2CheckTimer);
    pduCheckTimer = null;
    pdu2CheckTimer = null;
    lastPduCheckToken++;
    lastPdu2CheckToken++;
    pduOk = false;
    pdu2Ok = true;

    document.getElementById("pduLight").classList.remove("green");
    document.getElementById("pduStatusText").innerText = "Waiting for PDU\u2026";
    document.getElementById("pdu2Light").classList.remove("green");
    document.getElementById("pdu2StatusText").innerText = "Waiting for PDU\u2026";
    document.getElementById("addError").textContent = "";

    updateApplyBtn();

    if (clearInputs) {
      document.getElementById("label").value = "";
      document.getElementById("pduIp").value = "";
      document.getElementById("pdu2Ip").value = "";
    }
  }

  function updateApplyBtn() {
    const btn = document.getElementById("applyBtn");
    if (pduOk && pdu2Ok) {
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

  function setPdu2Ok(ok, msg) {
    const light = document.getElementById("pdu2Light");
    const txt = document.getElementById("pdu2StatusText");
    pdu2Ok = ok;
    if (ok) {
      light.classList.add("green");
      txt.innerText = msg || "PDU SNMP OK";
    } else {
      light.classList.remove("green");
      txt.innerText = msg || "Not connected";
    }
    updateApplyBtn();
  }

  async function checkPduIp(ip, setFn, tokenProp) {
    const token = tokenProp === 2 ? ++lastPdu2CheckToken : ++lastPduCheckToken;
    if (!ip || ip.length < 7) {
      setFn(tokenProp === 2 ? true : false, "Waiting for PDU\u2026");
      if (tokenProp === 2) document.getElementById("pdu2Light").classList.remove("green");
      else if (tokenProp === 1) { /* PDU 1 required, keep red */ }
      updateApplyBtn();
      return;
    }
    setFn(false, "Checking\u2026");
    try {
      const res = await fetch("/api/check_pdu", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({pdu_ip: ip})
      });
      const data = await res.json();
      const currentToken = tokenProp === 2 ? lastPdu2CheckToken : lastPduCheckToken;
      if (token !== currentToken) return;
      if (data.ok) {
        const typeLabel = data.type === "servertech" ? "Server Tech" : data.type === "raritan" ? "Raritan" : "Unknown";
        setFn(true, typeLabel + " detected");
      } else {
        setFn(false, data.error || "No PDU SNMP response");
      }
    } catch(e) {
      const currentToken = tokenProp === 2 ? lastPdu2CheckToken : lastPduCheckToken;
      if (token !== currentToken) return;
      setFn(false, "Check failed");
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const r = await fetch("/api/settings/title");
      const d = await r.json();
      if (d && d.ok && d.title) applyTitle(d.title);
    } catch(e) {}
    viewportStyle = localStorage.getItem("viewportStyle") || "racks";
    let savedLoadStyle = localStorage.getItem("pduLoadStyle") || "grouped";
    // Legacy migration: old "single" and "split" both map to "grouped"
    if (savedLoadStyle !== "inline") savedLoadStyle = "grouped";
    pduLoadStyle = savedLoadStyle;
    computeRackSize(racksCache.length);

    document.getElementById("idracPass").addEventListener("focus", function() {
      if (this.dataset.unchanged === "true") { this.value = ""; this.dataset.unchanged = "false"; }
    });
    document.getElementById("omePass").addEventListener("focus", function() {
      if (this.dataset.unchanged === "true") { this.value = ""; this.dataset.unchanged = "false"; }
    });

    const pduIpEl = document.getElementById("pduIp");
    pduIpEl.addEventListener("input", () => {
      const ip = pduIpEl.value.trim();
      if (pduCheckTimer) clearTimeout(pduCheckTimer);
      pduCheckTimer = setTimeout(() => checkPduIp(ip, setPduOk, 1), 900);
    });

    const pdu2IpEl = document.getElementById("pdu2Ip");
    pdu2IpEl.addEventListener("input", () => {
      const ip = pdu2IpEl.value.trim();
      if (pdu2CheckTimer) clearTimeout(pdu2CheckTimer);
      if (!ip) {
        setPdu2Ok(true, "Waiting for PDU\u2026");
        document.getElementById("pdu2Light").classList.remove("green");
        return;
      }
      pdu2CheckTimer = setTimeout(() => checkPduIp(ip, setPdu2Ok, 2), 900);
    });
  });

  async function applyRack() {
    const btn = document.getElementById("applyBtn");
    if (btn.disabled) return;

    const label = document.getElementById("label").value.trim();
    const pduIp = document.getElementById("pduIp").value.trim();
    const pdu2Ip = document.getElementById("pdu2Ip").value.trim();

    const errEl = document.getElementById("addError");
    errEl.textContent = "";

    if (!label || !pduIp) {
      errEl.textContent = "Enter label + PDU IP";
      return;
    }

    btn.disabled = true;
    btn.classList.add("disabled");

    try {
      const res = await fetch("/api/racks", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({label: label, pdu_ip: pduIp, pdu2_ip: pdu2Ip})
      });
      const data = await res.json();
      if (data.ok) closeAdd();
      else {
        errEl.textContent = data.error || "Add failed";
        btn.disabled = false;
        btn.classList.remove("disabled");
      }
    } catch(e) {
      errEl.textContent = "Add failed";
      btn.disabled = false;
      btn.classList.remove("disabled");
    }
  }

  // ---------------- Edit Modal ----------------
  let editRackId = null;
  let editPduCheckTimer = null;
  let editPdu2CheckTimer = null;
  let lastEditPduToken = 0;
  let lastEditPdu2Token = 0;
  let editPduOk = false;
  let editPdu2Ok = true;

  function openEdit(rack) {
    editRackId = rack.id;
    document.getElementById("editModal").style.display = "flex";
    document.getElementById("editLabel").value = rack.label || "";
    document.getElementById("editPduIp").value = rack.pdu_ip || "";
    document.getElementById("editPdu2Ip").value = rack.pdu2_ip || "";
    document.getElementById("editError").textContent = "";

    // Run checks on existing IPs
    editPduOk = false;
    editPdu2Ok = true;
    document.getElementById("editPduLight").classList.remove("green");
    document.getElementById("editPduStatusText").innerText = "Checking\u2026";
    document.getElementById("editPdu2Light").classList.remove("green");
    document.getElementById("editPdu2StatusText").innerText = "Waiting for PDU\u2026";

    if (rack.pdu_ip) checkEditPdu(rack.pdu_ip, 1);
    if (rack.pdu2_ip) {
      editPdu2Ok = false;
      document.getElementById("editPdu2StatusText").innerText = "Checking\u2026";
      checkEditPdu(rack.pdu2_ip, 2);
    }
    loadServersForEdit(rack.id);
  }

  function closeEdit() {
    document.getElementById("editModal").style.display = "none";
    editRackId = null;
    if (editPduCheckTimer) clearTimeout(editPduCheckTimer);
    if (editPdu2CheckTimer) clearTimeout(editPdu2CheckTimer);
    document.getElementById("editConfirm").style.display = "none";
    document.getElementById("editRemoveRow").style.display = "block";
  }

  function showRemoveConfirm() {
    document.getElementById("editRemoveRow").style.display = "none";
    document.getElementById("editConfirm").style.display = "block";
  }

  function cancelRemoveConfirm() {
    document.getElementById("editConfirm").style.display = "none";
    document.getElementById("editRemoveRow").style.display = "block";
  }

  async function confirmRemove() {
    if (!editRackId) return;
    try {
      await fetch("/api/delete", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ids: [editRackId]})
      });
    } catch(e) {}
    closeEdit();
  }

  function setEditPduOk(ok, msg) {
    const light = document.getElementById("editPduLight");
    const txt = document.getElementById("editPduStatusText");
    editPduOk = ok;
    if (ok) { light.classList.add("green"); txt.innerText = msg || "PDU SNMP OK"; }
    else { light.classList.remove("green"); txt.innerText = msg || "Not connected"; }
    updateEditApplyBtn();
  }

  function setEditPdu2Ok(ok, msg) {
    const light = document.getElementById("editPdu2Light");
    const txt = document.getElementById("editPdu2StatusText");
    editPdu2Ok = ok;
    if (ok) { light.classList.add("green"); txt.innerText = msg || "PDU SNMP OK"; }
    else { light.classList.remove("green"); txt.innerText = msg || "Not connected"; }
    updateEditApplyBtn();
  }

  function updateEditApplyBtn() {
    const btn = document.getElementById("editApplyBtn");
    if (editPduOk && editPdu2Ok) { btn.disabled = false; btn.classList.remove("disabled"); }
    else { btn.disabled = true; btn.classList.add("disabled"); }
  }

  async function checkEditPdu(ip, which) {
    const token = which === 2 ? ++lastEditPdu2Token : ++lastEditPduToken;
    const setFn = which === 2 ? setEditPdu2Ok : setEditPduOk;
    if (!ip || ip.length < 7) {
      if (which === 2) { setEditPdu2Ok(true, "Waiting for PDU\u2026"); document.getElementById("editPdu2Light").classList.remove("green"); }
      else setEditPduOk(false, "Waiting for PDU\u2026");
      return;
    }
    setFn(false, "Checking\u2026");
    try {
      const res = await fetch("/api/check_pdu", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({pdu_ip: ip}) });
      const data = await res.json();
      const currentToken = which === 2 ? lastEditPdu2Token : lastEditPduToken;
      if (token !== currentToken) return;
      if (data.ok) {
        const typeLabel = data.type === "servertech" ? "Server Tech" : data.type === "raritan" ? "Raritan" : "Unknown";
        setFn(true, typeLabel + " detected");
      } else setFn(false, data.error || "No PDU SNMP response");
    } catch(e) {
      const currentToken = which === 2 ? lastEditPdu2Token : lastEditPduToken;
      if (token !== currentToken) return;
      setFn(false, "Check failed");
    }
  }

  async function applyEdit() {
    const btn = document.getElementById("editApplyBtn");
    if (btn.disabled) return;
    const label = document.getElementById("editLabel").value.trim();
    const pduIp = document.getElementById("editPduIp").value.trim();
    const pdu2Ip = document.getElementById("editPdu2Ip").value.trim();
    const errEl = document.getElementById("editError");
    errEl.textContent = "";

    if (!label || !pduIp) { errEl.textContent = "Enter label + PDU IP"; return; }

    btn.disabled = true; btn.classList.add("disabled");
    try {
      const res = await fetch("/api/racks/update", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({id: editRackId, label: label, pdu_ip: pduIp, pdu2_ip: pdu2Ip})
      });
      const data = await res.json();
      if (data.ok) closeEdit();
      else { errEl.textContent = data.error || "Update failed"; btn.disabled = false; btn.classList.remove("disabled"); }
    } catch(e) { errEl.textContent = "Update failed"; btn.disabled = false; btn.classList.remove("disabled"); }
  }

  // Wire up edit modal input listeners
  (function() {
    document.addEventListener("DOMContentLoaded", () => {
      const pduEl = document.getElementById("editPduIp");
      pduEl.addEventListener("input", () => {
        if (editPduCheckTimer) clearTimeout(editPduCheckTimer);
        editPduCheckTimer = setTimeout(() => checkEditPdu(pduEl.value.trim(), 1), 900);
      });
      const pdu2El = document.getElementById("editPdu2Ip");
      pdu2El.addEventListener("input", () => {
        if (editPdu2CheckTimer) clearTimeout(editPdu2CheckTimer);
        const ip = pdu2El.value.trim();
        if (!ip) { setEditPdu2Ok(true, "Waiting for PDU\u2026"); document.getElementById("editPdu2Light").classList.remove("green"); return; }
        editPdu2CheckTimer = setTimeout(() => checkEditPdu(ip, 2), 900);
      });
    });
  })();

  // ---------------- Edit Modal: Server Management ----------------
  async function loadServersForEdit(rackId) {
    try {
      const res = await fetch("/api/servers/" + rackId);
      const data = await res.json();
      if (data.ok) renderEditServerList(data.servers || []);
    } catch(e) {}
  }

  function renderEditServerList(servers) {
    const list = document.getElementById("editServerList");
    list.innerHTML = "";
    if (servers.length === 0) {
      list.innerHTML = '<div style="opacity:0.4;font-size:12px;font-weight:600">No servers added</div>';
      return;
    }
    servers.forEach(s => {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 8px;background:rgba(15,23,42,0.55);border-radius:8px;margin-bottom:4px;border:1px solid rgba(255,255,255,0.06)";
      const label = document.createElement("span");
      label.style.cssText = "font-weight:700;font-size:13px";
      label.textContent = s.idrac_ip;
      const removeBtn = document.createElement("button");
      removeBtn.style.cssText = "background:rgba(220,38,38,0.2);color:rgba(248,113,113,0.7);font-size:11px;padding:4px 10px;border-radius:8px;border:1px solid rgba(220,38,38,0.15);cursor:pointer;font-weight:700";
      removeBtn.textContent = "Remove";
      removeBtn.onclick = async () => {
        await fetch("/api/servers/delete", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:s.id})});
        loadServersForEdit(editRackId);
      };
      row.appendChild(label);
      row.appendChild(removeBtn);
      list.appendChild(row);
    });
  }

  async function addServerToRack() {
    const ip = document.getElementById("editServerIp").value.trim();
    const errEl = document.getElementById("editServerError");
    errEl.textContent = "";
    if (!ip) { errEl.textContent = "Enter iDRAC IP"; return; }
    errEl.textContent = "Verifying iDRAC..."; errEl.style.color = "#94a3b8";
    try {
      const res = await fetch("/api/servers", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({rack_id: editRackId, idrac_ip: ip})
      });
      const data = await res.json();
      if (data.ok) {
        errEl.textContent = ""; errEl.style.color = "#f87171";
        document.getElementById("editServerIp").value = "";
        loadServersForEdit(editRackId);
      } else { errEl.textContent = data.error || "Add failed"; errEl.style.color = "#f87171"; }
    } catch(e) { errEl.textContent = "Add failed"; errEl.style.color = "#f87171"; }
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

      const textWrap = document.createElement("div");
      textWrap.className = "remove-text";

      const nameEl = document.createElement("div");
      nameEl.className = "remove-label";
      nameEl.textContent = r.label || "(no label)";
      textWrap.appendChild(nameEl);

      let ipText = r.pdu_ip || "";
      if (r.pdu2_ip) ipText += " / " + r.pdu2_ip;
      if (ipText) {
        const ipEl = document.createElement("div");
        ipEl.className = "remove-ip";
        ipEl.textContent = ipText;
        textWrap.appendChild(ipEl);
      }

      left.appendChild(cb);
      left.appendChild(textWrap);

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
    document.getElementById(viewportStyle === "fill" ? "vpFill" : "vpRacks").checked = true;
    document.getElementById(pduLoadStyle === "inline" ? "loadInline" : "loadGrouped").checked = true;
    document.getElementById("idracTestResult").textContent = "";
    document.getElementById("idracTestIp").value = "";
    try {
      const ir = await fetch("/api/settings/idrac");
      const id = await ir.json();
      if (id && id.ok) {
        document.getElementById("idracUser").value = id.username || "";
        const passEl = document.getElementById("idracPass");
        passEl.value = id.has_password ? "********" : "";
        passEl.dataset.unchanged = id.has_password ? "true" : "false";
      }
    } catch(e) {}
    document.getElementById("omeTestResult").textContent = "";
    try {
      const or = await fetch("/api/settings/ome");
      const od = await or.json();
      if (od && od.ok) {
        document.getElementById("omeHost").value = od.host || "";
        document.getElementById("omeUser").value = od.username || "";
        const omePassEl = document.getElementById("omePass");
        omePassEl.value = od.has_password ? "********" : "";
        omePassEl.dataset.unchanged = od.has_password ? "true" : "false";
      }
    } catch(e) {}
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
    localStorage.setItem("viewportStyle", vpVal);
    viewportStyle = vpVal;
    const loadVal = document.querySelector('input[name="pduLoadStyle"]:checked').value;
    localStorage.setItem("pduLoadStyle", loadVal);
    pduLoadStyle = loadVal;
    computeRackSize(racksCache.length);
    render(racksCache);
    try {
      const titleRes = await fetch("/api/settings/title", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({title: titleVal})
      });
      const titleData = await titleRes.json();
      if (titleData && titleData.ok && titleData.title) applyTitle(titleData.title);
    } catch(e) {}
    // Save iDRAC credentials if changed
    const idracUser = document.getElementById("idracUser").value.trim();
    const idracPassEl = document.getElementById("idracPass");
    const idracPass = idracPassEl.value.trim();
    if (idracUser && idracPass && idracPassEl.dataset.unchanged !== "true") {
      try {
        await fetch("/api/settings/idrac", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({username: idracUser, password: idracPass})
        });
      } catch(e) {}
    }
    // Save OME credentials if changed
    const omeHost = document.getElementById("omeHost").value.trim();
    const omeUser = document.getElementById("omeUser").value.trim();
    const omePassEl = document.getElementById("omePass");
    const omePass = omePassEl.value.trim();
    if (omeHost && omeUser && omePass && omePassEl.dataset.unchanged !== "true") {
      try {
        await fetch("/api/settings/ome", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({host: omeHost, username: omeUser, password: omePass})
        });
      } catch(e) {}
    }
    closeSettings();
  }

  async function testIdrac() {
    const ip = document.getElementById("idracTestIp").value.trim();
    const user = document.getElementById("idracUser").value.trim();
    const passEl = document.getElementById("idracPass");
    const pass = passEl.dataset.unchanged === "true" ? "" : passEl.value.trim();
    const result = document.getElementById("idracTestResult");
    if (!ip || !user || (!pass && passEl.dataset.unchanged !== "true")) {
      result.textContent = "Enter IP, username, and password";
      result.style.color = "#f87171";
      return;
    }
    result.textContent = "Testing..."; result.style.color = "#94a3b8";
    try {
      const body = {ip, username: user};
      if (pass) body.password = pass;
      else body.password = ""; // server will use stored password
      const res = await fetch("/api/settings/idrac/test", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (data.ok) { result.textContent = "Connected — " + (data.model || "OK"); result.style.color = "#22c55e"; }
      else { result.textContent = data.error || "Connection failed"; result.style.color = "#f87171"; }
    } catch(e) { result.textContent = "Test failed"; result.style.color = "#f87171"; }
  }

  async function testOme() {
    const host = document.getElementById("omeHost").value.trim();
    const user = document.getElementById("omeUser").value.trim();
    const passEl = document.getElementById("omePass");
    const pass = passEl.dataset.unchanged === "true" ? "" : passEl.value.trim();
    const result = document.getElementById("omeTestResult");
    if (!host || !user || (!pass && passEl.dataset.unchanged !== "true")) {
      result.textContent = "Enter host, username, and password"; result.style.color = "#f87171"; return;
    }
    result.textContent = "Testing..."; result.style.color = "#94a3b8";
    try {
      const body = {host, username: user, password: pass || ""};
      const res = await fetch("/api/settings/ome/test", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const data = await res.json();
      if (data.ok) { result.textContent = "Connected"; result.style.color = "#22c55e"; }
      else { result.textContent = data.error || "Failed"; result.style.color = "#f87171"; }
    } catch(e) { result.textContent = "Test failed"; result.style.color = "#f87171"; }
  }

  // ---------------- Reports ----------------
  async function openReports() {
    const modal = document.getElementById("reportsModal");
    modal.style.display = "flex";
    document.getElementById("reportsNotConfigured").style.display = "none";
    document.getElementById("reportsError").style.display = "none";
    document.getElementById("reportsSelector").style.display = "none";
    document.getElementById("reportsResults").style.display = "none";
    document.getElementById("reportsActions").style.display = "none";
    document.getElementById("reportsLoading").style.display = "block";

    try {
      const res = await fetch("/api/reports/available");
      const data = await res.json();
      document.getElementById("reportsLoading").style.display = "none";
      if (!data.ok) {
        document.getElementById("reportsNotConfigured").style.display = "block";
        return;
      }
      const select = document.getElementById("reportSelect");
      select.innerHTML = "";
      (data.reports || []).forEach(r => {
        const opt = document.createElement("option");
        opt.value = r.id;
        opt.textContent = r.name;
        select.appendChild(opt);
      });
      document.getElementById("reportsSelector").style.display = "block";
    } catch(e) {
      document.getElementById("reportsLoading").style.display = "none";
      document.getElementById("reportsNotConfigured").style.display = "block";
    }
  }

  function closeReports() {
    document.getElementById("reportsModal").style.display = "none";
  }

  async function runReport() {
    const reportId = document.getElementById("reportSelect").value;
    if (!reportId) return;
    document.getElementById("reportsSelector").style.display = "none";
    document.getElementById("reportsLoading").style.display = "block";
    document.getElementById("reportsResults").style.display = "none";
    document.getElementById("reportsActions").style.display = "none";

    try {
      const res = await fetch("/api/reports/run", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({report_id: parseInt(reportId)})
      });
      const data = await res.json();
      document.getElementById("reportsLoading").style.display = "none";
      if (!data.ok) {
        document.getElementById("reportsErrorMsg").textContent = data.error || "Report failed";
        document.getElementById("reportsError").style.display = "block";
        return;
      }
      renderReportTable(data.columns, data.rows);
    } catch(e) {
      document.getElementById("reportsLoading").style.display = "none";
      document.getElementById("reportsSelector").style.display = "block";
    }
  }

  function renderReportTable(columns, rows) {
    const head = document.getElementById("reportsHead");
    const body = document.getElementById("reportsBody");
    head.innerHTML = "";
    body.innerHTML = "";
    const tr = document.createElement("tr");
    columns.forEach(col => {
      const th = document.createElement("th");
      th.textContent = col;
      tr.appendChild(th);
    });
    head.appendChild(tr);
    rows.forEach(row => {
      const tr = document.createElement("tr");
      row.forEach(val => {
        const td = document.createElement("td");
        td.textContent = (val || "").trim() || "--";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    document.getElementById("reportsContent").style.width = "min(900px, 95vw)";
    document.getElementById("reportsResults").style.display = "block";
    document.getElementById("reportsActions").style.display = "block";
  }

  function backToReportSelect() {
    document.getElementById("reportsContent").style.width = "";
    document.getElementById("reportsResults").style.display = "none";
    document.getElementById("reportsActions").style.display = "none";
    document.getElementById("reportsSelector").style.display = "block";
  }

</script>
</body>
</html>
"""
    html = html.replace("__TITLE__", initial_title)
    return HTMLResponse(html)
