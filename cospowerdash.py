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

# Directory where scheduled Lab Overview Reports are dropped as timestamped PDFs.
# The companion mailer app (see ./mailer/) SCPs config.ini + new PDFs from here.
REPORTS_DIR = "reports"
REPORT_RETENTION_DAYS = 90
REPORT_SCHEDULES = {
    # key → (human label, cron-ish "HH:MM" when to fire, predicate: (now_local) -> bool)
    "disabled":         "Disabled (no scheduled reports)",
    "daily_23_00":      "Daily at 23:00",
    "weekdays_17_00":   "Weekdays (Mon–Fri) at 17:00",
    "weekly_sat_23_30": "Weekly on Saturday at 23:30",
    "monthly_1st_02_00":"Monthly on the 1st at 02:00",
}
REPORT_TIME_RANGES = {
    "trailing_24h":      "Trailing 24 hours",
    "trailing_7d":       "Trailing 7 days",
    "trailing_30d":      "Trailing 30 days",
    "prev_week_mon_fri": "Previous Mon–Fri (business week)",
}

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

# Standard operational logger -> powerdash.log + stderr
logger = logging.getLogger("powerdash")
logger.setLevel(logging.DEBUG)

_file_handler = RotatingFileHandler("powerdash.log", maxBytes=1_000_000, backupCount=3)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)

_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.INFO)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_stream_handler)

# ----------------------------
# Slack Alerts
# ----------------------------

import time as _time

SLACK_ALERT_DELAY = 15  # seconds a condition must persist before firing an alert
PHASE_OVERLOAD_PCT = 80  # % of rated amps that triggers the alert

# Debounce state: tracks when a condition first appeared and whether we've notified.
# Key = (rack_id, pdu_key, phase_label, alert_type) e.g. (3, "pdu_ip", "Phase A", "overload")
_alert_pending: Dict[tuple, float] = {}   # key -> first_seen monotonic timestamp
_alert_sent: Dict[tuple, bool] = {}       # key -> True if alert already sent
_recovery_pending: Dict[tuple, bool] = {} # key -> True if recovery msg needed

async def _send_slack_message(webhook_url: str, text: str):
    """POST a message to a Slack webhook. Fire-and-forget from the poll loop."""
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: req_lib.post(webhook_url, json={"text": text}, timeout=10))
        if resp.status_code != 200:
            logger.warning("Slack webhook returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Failed to send Slack alert: %s", e)

async def send_slack_alert(rack_label: str, pdu_ip: str, pdu_type: str,
                           phase_label: str, current_a: float, rated_a: float,
                           pct: float, is_recovery: bool = False):
    """Format and send a phase-overload or recovery alert to Slack."""
    webhook_url = get_setting("slack_webhook_url", "")
    if not webhook_url:
        return
    pdu_name = "Server Tech" if pdu_type == "servertech" else "Raritan" if pdu_type == "raritan" else "PDU"
    if is_recovery:
        emoji = ":large_green_circle:"
        header = "*POWER RECOVERY*"
        lines = [
            f"{emoji} {header}",
            f"Rack: {rack_label}",
            f"{pdu_name}: {pdu_ip}",
            f"{phase_label} load dropped to {pct:.0f}% ({current_a:.2f}A / {rated_a:.0f}A rated)",
            f"Threshold: {PHASE_OVERLOAD_PCT}%",
        ]
    else:
        emoji = ":rotating_light:"
        header = "*POWER ALERT*"
        lines = [
            f"{emoji} {header}",
            f"Rack: {rack_label}",
            f"{pdu_name}: {pdu_ip}",
            f"{phase_label} load at {pct:.0f}% ({current_a:.2f}A / {rated_a:.0f}A rated)",
            f"Threshold: {PHASE_OVERLOAD_PCT}%",
        ]
    await _send_slack_message(webhook_url, "\n".join(lines))

def _maybe_slack_alert(key: tuple, rack_label: str, pdu_ip: str, pdu_type: str,
                       phase_label: str, current_a: float, rated_a: float, pct: float):
    """Debounce: only fire after the condition persists for SLACK_ALERT_DELAY seconds."""
    now = _time.monotonic()
    if key not in _alert_pending:
        _alert_pending[key] = now
        logger.debug("Alert pending: %s %s %s — waiting %ds", rack_label, pdu_ip, phase_label, SLACK_ALERT_DELAY)
        return
    elapsed = now - _alert_pending[key]
    if elapsed >= SLACK_ALERT_DELAY and not _alert_sent.get(key):
        _alert_sent[key] = True
        logger.info("Alert confirmed after %ds: %s %s %s at %.0f%% — sending Slack",
                     int(elapsed), rack_label, pdu_ip, phase_label, pct)
        asyncio.create_task(send_slack_alert(
            rack_label, pdu_ip, pdu_type, phase_label, current_a, rated_a, pct))

def _clear_alert(key: tuple):
    """Clear a pending/sent alert when the condition resolves."""
    was_sent = _alert_sent.get(key, False)
    _alert_pending.pop(key, None)
    _alert_sent.pop(key, None)
    if was_sent:
        _recovery_pending[key] = True

def _check_recovery(key: tuple, rack_label: str, pdu_ip: str, pdu_type: str,
                    phase_label: str, current_a: float, rated_a: float, pct: float):
    """Debounce recovery: only send after condition is clear for SLACK_ALERT_DELAY."""
    if not _recovery_pending.get(key):
        return
    recovery_key = key + ("recovery",)
    now = _time.monotonic()
    if recovery_key not in _alert_pending:
        _alert_pending[recovery_key] = now
        return
    elapsed = now - _alert_pending[recovery_key]
    if elapsed >= SLACK_ALERT_DELAY and not _alert_sent.get(recovery_key):
        _alert_sent[recovery_key] = True
        logger.info("Recovery confirmed after %ds: %s %s %s at %.0f%%",
                     int(elapsed), rack_label, pdu_ip, phase_label, pct)
        asyncio.create_task(send_slack_alert(
            rack_label, pdu_ip, pdu_type, phase_label, current_a, rated_a, pct, is_recovery=True))
        # Clean up
        _recovery_pending.pop(key, None)
        _alert_pending.pop(recovery_key, None)
        _alert_sent.pop(recovery_key, None)

def _cancel_recovery(key: tuple):
    """Cancel recovery if condition returns before recovery fires."""
    _recovery_pending.pop(key, None)
    recovery_key = key + ("recovery",)
    _alert_pending.pop(recovery_key, None)
    _alert_sent.pop(recovery_key, None)

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
                        # PDU unreachable — clear any pending overload alerts
                        for p in ("A", "B", "C"):
                            _clear_alert((rid, pdu_key, "Phase " + p, "overload"))
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

                    # --- Slack phase-overload checks ---
                    rack_label = rack.get("label", "?")
                    for phase in phases:
                        plabel = phase.get("label", "?")
                        p_amps = phase.get("current_a", 0)
                        alert_key = (rid, pdu_key, plabel, "overload")
                        if rated_a > 0 and phase.get("reachable"):
                            load_pct = (p_amps / rated_a) * 100
                            if load_pct >= PHASE_OVERLOAD_PCT:
                                _maybe_slack_alert(alert_key, rack_label, pdu_ip,
                                                   pdu_type or "", plabel, p_amps, rated_a, load_pct)
                                _cancel_recovery(alert_key)
                            else:
                                _clear_alert(alert_key)
                                _check_recovery(alert_key, rack_label, pdu_ip,
                                                pdu_type or "", plabel, p_amps, rated_a, load_pct)
                        else:
                            # Unreachable or no rated_a — can't evaluate, clear any pending
                            _clear_alert(alert_key)

                latest_pdu_phases[rid] = phases_all

            # Clean up data for deleted racks
            for rid in list(latest_pdu_phases.keys()):
                if rid not in current_ids:
                    latest_pdu_phases.pop(rid, None)

            await broadcast_snapshot()
        except Exception as e:
            logger.error("Error in poll loop: %s", e)
        await asyncio.sleep(2)

def _should_fire_now(schedule: str) -> bool:
    """Does the configured schedule want to fire at this exact minute?"""
    from datetime import datetime
    n = datetime.now()
    if schedule == "daily_23_00":
        return n.hour == 23 and n.minute == 0
    if schedule == "weekdays_17_00":
        return n.weekday() < 5 and n.hour == 17 and n.minute == 0
    if schedule == "weekly_sat_23_30":
        return n.weekday() == 5 and n.hour == 23 and n.minute == 30
    if schedule == "monthly_1st_02_00":
        return n.day == 1 and n.hour == 2 and n.minute == 0
    return False

async def report_scheduler_loop():
    """Wake twice a minute; when the wall clock crosses the configured fire time,
    generate a Lab Overview PDF into REPORTS_DIR. Uses report_last_fire_iso to
    dedupe across restarts within the same minute."""
    from datetime import datetime
    await asyncio.sleep(10)  # let startup settle before first check
    while True:
        try:
            schedule = get_setting("report_schedule", "disabled")
            if schedule != "disabled" and _should_fire_now(schedule):
                key = datetime.now().strftime("%Y-%m-%dT%H:%M")
                last = get_setting("report_last_fire_iso", "")
                if last != key:
                    set_setting("report_last_fire_iso", key)
                    logger.info("Report scheduler firing (schedule=%s, at=%s)", schedule, key)
                    try:
                        await asyncio.get_event_loop().run_in_executor(None, _run_scheduled_report)
                    except Exception:
                        logger.exception("Scheduled report generation failed")
        except Exception:
            logger.exception("Report scheduler loop error")
        await asyncio.sleep(30)

# ----------------------------
# App Lifecycle
# ----------------------------

@app.on_event("startup")
async def startup():
    init_db()
    _ensure_reports_dir()
    try:
        _write_report_config_ini()
    except Exception as e:
        logger.warning("Could not write initial reports/config.ini: %s", e)
    logger.info("Power Dashboard starting, database initialized")
    asyncio.create_task(poll_loop())
    asyncio.create_task(report_scheduler_loop())

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
    if not label:
        return {"ok": False, "error": "Missing label"}
    add_rack(label, pdu_ip, pdu2_ip)
    logger.info("Rack added: %s (Left %s, Right %s)", label, pdu_ip or "none", pdu2_ip or "none")
    return {"ok": True}

@app.post("/api/racks/update")
def api_update_rack(data: dict):
    rack_id = data.get("id")
    label = (data.get("label") or "").strip()
    pdu_ip = (data.get("pdu_ip") or "").strip()
    pdu2_ip = (data.get("pdu2_ip") or "").strip()
    if not rack_id or not label:
        return {"ok": False, "error": "Missing id or label"}
    update_rack(int(rack_id), label, pdu_ip, pdu2_ip)
    logger.info("Rack updated: id=%s %s (Left %s, Right %s)", rack_id, label, pdu_ip or "none", pdu2_ip or "none")
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

def _ome_get_all_pages(token: str, path: str) -> List[dict]:
    """GET an OME OData endpoint and follow @odata.nextLink to collect all rows.

    OME's report row endpoints paginate at 100 rows by default. Without
    walking nextLink we silently miss every server beyond the first page.
    """
    all_rows: List[dict] = []
    next_path: Optional[str] = path
    safety = 0
    while next_path and safety < 200:
        safety += 1
        data = _ome_get(token, next_path)
        if not data:
            break
        all_rows.extend(data.get("value", []) or [])
        next_link = data.get("@odata.nextLink") or ""
        if not next_link:
            break
        # @odata.nextLink is typically returned as "/api/<path>?$skip=N".
        # _ome_get prepends "/api" itself, so strip it here.
        if next_link.startswith("/api"):
            next_path = next_link[4:]
        elif next_link.startswith("http"):
            # Absolute URL — extract everything after "/api"
            idx = next_link.find("/api/")
            next_path = next_link[idx + 4:] if idx >= 0 else None
        else:
            next_path = next_link
    if safety >= 200:
        logger.warning("OME pagination safety cap hit at 200 pages for %s", path)
    return all_rows

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

# ----------------------------
# Slack Webhook Settings API
# ----------------------------

@app.get("/api/settings/slack_webhook")
def api_get_slack_webhook():
    url = get_setting("slack_webhook_url", "")
    return {"ok": True, "url": url}

@app.post("/api/settings/slack_webhook")
def api_set_slack_webhook(data: dict):
    url = (data.get("url") or "").strip()
    set_setting("slack_webhook_url", url)
    logger.info("Slack webhook URL %s", "configured" if url else "cleared")
    return {"ok": True, "url": url}

@app.post("/api/settings/slack_test")
async def api_test_slack(data: dict):
    url = (data.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "No webhook URL provided"}
    try:
        text = ":rotating_light: *POWER ALERT*\nThis is a test alert from COS Power Dashboard."
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: req_lib.post(url, json={"text": text}, timeout=10))
        if resp.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"Slack returned {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ----------------------------
# Report Delivery Settings API
# Writes to both the SQLite settings table (for the dashboard UI)
# and reports/config.ini (for the external mailer app to read over SCP).
# ----------------------------

import configparser as _configparser
import os as _os

def _ensure_reports_dir():
    _os.makedirs(REPORTS_DIR, exist_ok=True)
    sent_log = _os.path.join(REPORTS_DIR, "sent.log")
    if not _os.path.exists(sent_log):
        with open(sent_log, "w") as f:
            f.write("# Appended by the mailer app after each successful send.\n")
            f.write("# Format: ISO8601\tfilename\trecipients\n")

def _write_report_config_ini():
    """Serialize the current report-delivery settings into reports/config.ini.
    The mailer app reads this over SCP to learn recipients, schedule, etc."""
    _ensure_reports_dir()
    cfg = _configparser.ConfigParser()
    cfg["delivery"] = {
        "recipients":       get_setting("report_recipients", ""),
        "schedule":         get_setting("report_schedule", "disabled"),
        "scope":            get_setting("report_scope", ""),
        "time_range":       get_setting("report_time_range", "trailing_7d"),
        "subject_template": get_setting("report_subject", "COS Lab Weekly Power Report — {start} to {end}"),
        "sender_label":     get_setting("report_sender_label", "COS Power Dashboard"),
    }
    path = _os.path.join(REPORTS_DIR, "config.ini")
    with open(path, "w") as f:
        f.write("# Written by cospowerdash on every Save in Settings → Configure Report Delivery.\n")
        f.write("# The mailer app reads this to know who to send to, on what cadence, etc.\n")
        cfg.write(f)

@app.get("/api/settings/report_delivery")
def api_get_report_delivery():
    return {
        "ok": True,
        "recipients":        get_setting("report_recipients", ""),
        "schedule":          get_setting("report_schedule", "disabled"),
        "scope":             get_setting("report_scope", ""),
        "time_range":        get_setting("report_time_range", "trailing_7d"),
        "subject_template":  get_setting("report_subject", "COS Lab Weekly Power Report — {start} to {end}"),
        "sender_label":      get_setting("report_sender_label", "COS Power Dashboard"),
        "schedule_options":  REPORT_SCHEDULES,
        "time_range_options":REPORT_TIME_RANGES,
    }

@app.post("/api/settings/report_delivery")
def api_set_report_delivery(data: dict):
    recipients = (data.get("recipients") or "").strip()
    schedule   = (data.get("schedule") or "disabled").strip()
    scope      = (data.get("scope") or "").strip()
    time_range = (data.get("time_range") or "trailing_7d").strip()
    subject    = (data.get("subject_template") or "").strip() or "COS Lab Weekly Power Report — {start} to {end}"
    sender_lbl = (data.get("sender_label") or "").strip() or "COS Power Dashboard"
    if schedule not in REPORT_SCHEDULES:
        return {"ok": False, "error": f"Invalid schedule '{schedule}'"}
    if time_range not in REPORT_TIME_RANGES:
        return {"ok": False, "error": f"Invalid time_range '{time_range}'"}
    set_setting("report_recipients", recipients)
    set_setting("report_schedule", schedule)
    set_setting("report_scope", scope)
    set_setting("report_time_range", time_range)
    set_setting("report_subject", subject)
    set_setting("report_sender_label", sender_lbl)
    try:
        _write_report_config_ini()
    except Exception as e:
        logger.warning("Failed to write reports/config.ini: %s", e)
        return {"ok": False, "error": f"Saved settings but failed to write config.ini: {e}"}
    logger.info("Report delivery settings updated (schedule=%s, recipients=%s, scope=%s)",
                schedule, recipients or "—", scope or "all")
    return {"ok": True}

def _resolve_time_range(key: str):
    """Convert a time_range enum into (start_epoch, end_epoch) seconds."""
    import time as _time
    from datetime import datetime, timedelta
    now = _time.time()
    if key == "trailing_24h":
        return int(now - 86400), int(now)
    if key == "trailing_30d":
        return int(now - 30 * 86400), int(now)
    if key == "prev_week_mon_fri":
        today = datetime.now()
        # weekday(): Mon=0 ... Sun=6. Walk back to the most recent completed Friday.
        days_since_fri = (today.weekday() - 4) % 7
        if days_since_fri == 0 and today.hour < 23:
            days_since_fri = 7
        fri = today - timedelta(days=days_since_fri)
        fri_eod = fri.replace(hour=23, minute=59, second=0, microsecond=0)
        mon_sod = (fri - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(mon_sod.timestamp()), int(fri_eod.timestamp())
    # default / "trailing_7d"
    return int(now - 7 * 86400), int(now)

def _run_scheduled_report():
    """Generate the Lab Overview PDF using current delivery settings and drop it in REPORTS_DIR.
    Prunes files older than REPORT_RETENTION_DAYS. Returns the output path on success.
    Raises ReportError or other exceptions on failure — the caller should log them."""
    import time as _time
    _ensure_reports_dir()
    time_range_key = get_setting("report_time_range", "trailing_7d")
    scope          = get_setting("report_scope", "")
    start, end     = _resolve_time_range(time_range_key)
    pdf_bytes, _internal_name, pages, warnings = _generate_graph_report_pdf(start, end, scope)
    ts = _time.strftime("%Y-%m-%dT%H%M", _time.localtime(end))
    filename = f"lab_overview_{ts}.pdf"
    out_path = _os.path.join(REPORTS_DIR, filename)
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    logger.info("Scheduled report written: %s (%d pages, %d warnings)", out_path, pages, len(warnings))
    # Retention prune: drop PDFs older than N days.
    cutoff = _time.time() - REPORT_RETENTION_DAYS * 86400
    for fn in _os.listdir(REPORTS_DIR):
        if not fn.startswith("lab_overview_") or not fn.endswith(".pdf"):
            continue
        fp = _os.path.join(REPORTS_DIR, fn)
        try:
            if _os.path.getmtime(fp) < cutoff:
                _os.remove(fp)
                logger.info("Pruned aged report: %s", fn)
        except OSError:
            pass
    return out_path

@app.post("/api/reports/generate_now")
async def api_generate_report_now():
    """Kick off an immediate report generation, writing the PDF into REPORTS_DIR.
    Useful for verifying the scheduler pipeline without waiting for the cron time."""
    try:
        path = await asyncio.get_event_loop().run_in_executor(None, _run_scheduled_report)
        return {"ok": True, "filename": _os.path.basename(path)}
    except ReportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("Generate Now failed")
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
    # Collect configured server iDRAC IPs from racks (Edit Rack dialog).
    # Also build rack_assignments (ip -> rack label) and rack_order so the
    # frontend can group the human-readable view by rack.
    all_servers = get_all_servers()
    racks_list = get_racks()
    rack_id_to_label = {r["id"]: r["label"] for r in racks_list}
    rack_order = [r["label"] for r in racks_list]
    rack_assignments: Dict[str, str] = {}
    configured_ips = set()
    for rack_id, rack_servers in all_servers.items():
        label = rack_id_to_label.get(rack_id, "Unassigned")
        for srv in rack_servers:
            ip = (srv.get("idrac_ip") or "").strip()
            if ip:
                configured_ips.add(ip)
                rack_assignments[ip] = label
    if not configured_ips:
        return {"ok": False, "error": "No servers configured in dashboard.\nAdd servers to racks first."}
    # Lowercase for case-insensitive matching against report cells
    configured_ips_lc = {ip.lower() for ip in configured_ips}
    # Run the report
    _ome_post(token, "/ReportService/Actions/ReportService.RunReport", {"ReportDefId": int(report_id)})
    # Fetch ALL pages of result rows (OME paginates at 100 by default)
    raw_rows = _ome_get_all_pages(token, f"/ReportService/ReportDefs({report_id})/ReportResults/ResultRows")
    if not raw_rows:
        return {"ok": False, "error": "Failed to fetch report results"}
    # Get column names
    report_def = _ome_get(token, f"/ReportService/ReportDefs({report_id})")
    columns = []
    if report_def:
        columns = [c["Name"] for c in report_def.get("ColumnNames", [])]
    # Filter rows: include any row where ANY column value matches a
    # configured iDRAC IP. Different OME reports place the IP in
    # different columns (device name, service tag, IP, etc.), so we
    # scan all cells rather than assuming a fixed column position.
    rows = []
    for row in raw_rows:
        values = row.get("Values", [])
        if not values:
            continue
        for cell in values:
            if cell is None:
                continue
            cell_norm = str(cell).strip().lower()
            if not cell_norm:
                continue
            if cell_norm in configured_ips_lc:
                rows.append(values)
                break
    logger.info("Report %s: fetched %d total rows, %d matched configured iDRAC IPs", report_id, len(raw_rows), len(rows))
    if not rows and raw_rows:
        # Filtering removed everything — log a sample so we can see
        # what OME is actually returning vs what we tried to match.
        sample = raw_rows[0].get("Values", []) if raw_rows else []
        logger.warning(
            "Report %s returned %d rows but none matched configured iDRAC IPs. "
            "Configured: %s. Sample row cells: %s",
            report_id, len(raw_rows), sorted(configured_ips), sample,
        )
    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "rack_assignments": rack_assignments,
        "rack_order": rack_order,
    }

# ----------------------------
# Custom Reporting (Prometheus + Grafana) — Graph Report PDF
# ----------------------------
#
# Settings keys: prom_url, grafana_url, grafana_user, grafana_pass
#
# The Graph Report PDF combines:
#   - Grafana panel renders (PNGs from /render/d-solo) — pixel-identical to live dashboards
#   - matplotlib charts and tables built from direct Prometheus PromQL queries
#
# Prometheus is queried via /api/v1/query and /api/v1/query_range over HTTP.
# matplotlib is imported lazily inside the endpoint so the module still loads
# on hosts that haven't pip-installed it yet (the endpoint will return a clean
# JSON error in that case).

# Phase 0 lab cluster scope — used for the rack-scope picker on the Graph
# Report form. Each entry is (display_label, server_label_regex_fragment).
GRAPH_REPORT_CLUSTERS = [
    ("R1C2", "r1c2s[1-4]"),
    ("R2C3", "r2c3s[1-4]"),
    ("R2C4", "r2c4s[1-4]"),
    ("R2C5", "r2c5s[1-4]"),
    ("R2C7", "r2c7s[1-4]"),
    ("R2C8", "r2c8s[1-4]"),
]
ALL_CLUSTERS_REGEX = "r[12]c[2345678]s[1-4]"

def _prom_query(prom_url: str, query: str, t: Optional[float] = None) -> List[dict]:
    """Run an instant Prometheus query. Returns the .data.result list, or []."""
    try:
        params = {"query": query}
        if t is not None:
            params["time"] = f"{t:.3f}"
        resp = req_lib.get(f"{prom_url.rstrip('/')}/api/v1/query", params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning("prom query HTTP %d: %s", resp.status_code, query)
            return []
        return resp.json().get("data", {}).get("result", []) or []
    except Exception as e:
        logger.warning("prom query failed (%s): %s", query, e)
        return []

def _prom_query_range(prom_url: str, query: str, start: float, end: float, step: float) -> List[dict]:
    """Run a Prometheus range query. Returns the .data.result list, or []."""
    try:
        params = {
            "query": query,
            "start": f"{start:.3f}",
            "end": f"{end:.3f}",
            "step": f"{int(max(step, 1))}s",
        }
        resp = req_lib.get(f"{prom_url.rstrip('/')}/api/v1/query_range", params=params, timeout=60)
        if resp.status_code != 200:
            logger.warning("prom query_range HTTP %d: %s", resp.status_code, query)
            return []
        return resp.json().get("data", {}).get("result", []) or []
    except Exception as e:
        logger.warning("prom query_range failed (%s): %s", query, e)
        return []

def _grafana_render_panel(grafana_url: str, user: str, pw: str, uid: str, panel_id: int,
                           start_ms: int, end_ms: int, width: int = 1000, height: int = 360) -> Optional[bytes]:
    """Fetch a rendered Grafana panel as PNG bytes via /render/d-solo. Returns None on failure."""
    try:
        url = f"{grafana_url.rstrip('/')}/render/d-solo/{uid}"
        params = {
            "panelId": panel_id,
            "from": start_ms,
            "to": end_ms,
            "width": width,
            "height": height,
            "tz": "America/Denver",
        }
        auth = (user, pw) if user else None
        resp = req_lib.get(url, params=params, auth=auth, timeout=30)
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
            return resp.content
        logger.warning("grafana render HTTP %d (uid=%s panel=%d)", resp.status_code, uid, panel_id)
        return None
    except Exception as e:
        logger.warning("grafana render failed (uid=%s panel=%d): %s", uid, panel_id, e)
        return None

@app.get("/api/settings/custom_reporting")
def api_get_custom_reporting():
    return {
        "ok": True,
        "prom_url": get_setting("prom_url", ""),
        "grafana_url": get_setting("grafana_url", ""),
        "grafana_user": get_setting("grafana_user", ""),
        "has_grafana_pass": bool(get_setting("grafana_pass", "")),
    }

@app.post("/api/settings/custom_reporting")
def api_set_custom_reporting(data: dict):
    prom_url = (data.get("prom_url") or "").strip().rstrip("/")
    grafana_url = (data.get("grafana_url") or "").strip().rstrip("/")
    grafana_user = (data.get("grafana_user") or "").strip()
    grafana_pass = (data.get("grafana_pass") or "").strip()
    # Empty password = keep existing (lets the user edit URLs without retyping)
    if not grafana_pass:
        grafana_pass = get_setting("grafana_pass", "")
    if not prom_url or not grafana_url or not grafana_user or not grafana_pass:
        return {"ok": False, "error": "Prometheus URL, Grafana URL, username, and password all required"}
    set_setting("prom_url", prom_url)
    set_setting("grafana_url", grafana_url)
    set_setting("grafana_user", grafana_user)
    set_setting("grafana_pass", grafana_pass)
    logger.info("Custom reporting config updated (prom=%s grafana=%s user=%s)", prom_url, grafana_url, grafana_user)
    return {"ok": True}

@app.post("/api/settings/custom_reporting/test")
def api_test_custom_reporting(data: dict):
    prom_url = (data.get("prom_url") or "").strip().rstrip("/") or get_setting("prom_url", "")
    grafana_url = (data.get("grafana_url") or "").strip().rstrip("/") or get_setting("grafana_url", "")
    grafana_user = (data.get("grafana_user") or "").strip() or get_setting("grafana_user", "")
    grafana_pass = (data.get("grafana_pass") or "").strip() or get_setting("grafana_pass", "")
    if not prom_url or not grafana_url or not grafana_user or not grafana_pass:
        return {"ok": False, "error": "Need Prometheus URL, Grafana URL, username, and password"}
    # Probe Prometheus
    try:
        r = req_lib.get(f"{prom_url}/api/v1/status/buildinfo", timeout=10)
        if r.status_code != 200:
            return {"ok": False, "error": f"Prometheus unreachable (HTTP {r.status_code})"}
        prom_version = r.json().get("data", {}).get("version", "?")
    except Exception as e:
        return {"ok": False, "error": f"Prometheus error: {e}"}
    # Probe Grafana — health (no auth) + auth-gated dashboard list
    try:
        r = req_lib.get(f"{grafana_url}/api/health", timeout=10)
        if r.status_code != 200:
            return {"ok": False, "error": f"Grafana unreachable (HTTP {r.status_code})"}
        graf_version = r.json().get("version", "?")
        r2 = req_lib.get(
            f"{grafana_url}/api/search",
            params={"type": "dash-db", "limit": 1},
            auth=(grafana_user, grafana_pass),
            timeout=10,
        )
        if r2.status_code == 401 or r2.status_code == 403:
            return {"ok": False, "error": "Grafana auth failed (bad username/password)"}
        if r2.status_code != 200:
            return {"ok": False, "error": f"Grafana auth probe failed (HTTP {r2.status_code})"}
    except Exception as e:
        return {"ok": False, "error": f"Grafana error: {e}"}
    return {"ok": True, "prom_version": prom_version, "grafana_version": graf_version}

class ReportError(Exception):
    """Raised by _generate_graph_report_pdf when config/input is missing or invalid."""

def _generate_graph_report_pdf(start: int, end: int, clusters: str = ""):
    """Render the Lab Overview Report PDF.

    Args:
      start, end  — unix epoch seconds, end > start
      clusters    — comma-separated cluster labels; empty = all

    Returns:
      (pdf_bytes, filename, pages_written, warnings) tuple.
    Raises ReportError on bad input or missing config.
    """
    import io
    import time as _time

    if not start or not end or end <= start:
        raise ReportError("Invalid start/end (require unix seconds, end > start)")

    prom_url = get_setting("prom_url", "")
    grafana_url = get_setting("grafana_url", "")
    if not prom_url or not grafana_url:
        raise ReportError("Custom reporting not configured. Open Settings → Configure Custom Reporting.")

    # Lazy matplotlib import so this module still loads on hosts without matplotlib.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
        import matplotlib.dates as mdates
    except ImportError:
        raise ReportError("matplotlib not installed on this host. Run: pip install matplotlib")

    # Resolve cluster scope
    requested = [c.strip() for c in (clusters or "").split(",") if c.strip()]
    selected_clusters = [(label, regex) for (label, regex) in GRAPH_REPORT_CLUSTERS
                         if not requested or label in requested]
    if not selected_clusters:
        selected_clusters = list(GRAPH_REPORT_CLUSTERS)
    scope_regex = "|".join(f"({r})" for _, r in selected_clusters)

    duration_s = end - start
    # Step picked so each panel has roughly 300–600 points; floor at 30s (scrape interval)
    step = max(30, int(duration_s / 500))

    start_ms = start * 1000
    end_ms = end * 1000

    pdf_buf = io.BytesIO()
    pages_written = 0
    warnings: List[str] = []

    with PdfPages(pdf_buf) as pdf:

        # ---------- Page 1: Cover ----------
        cover_q = {
            "lab_kw_now":   f'sum(power{{sensor="total",server=~"{scope_regex}"}}) / 1000',
            "server_count": f'count(power{{sensor="total",server=~"{scope_regex}"}})',
            "nv_gpu_count": 'count(DCGM_FI_DEV_POWER_USAGE{job!="local-gpu"})',
            "amd_gpu_count":'count(gpu_health)',
            "lab_kwh":      f'(sum(increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[{duration_s}s]))) / 3.6e12',
            "peak_lab_kw":  f'max_over_time((sum(power{{sensor="total",server=~"{scope_regex}"}}))[{duration_s}s:{step}s]) / 1000',
        }
        cover_vals = {}
        for k, q in cover_q.items():
            res = _prom_query(prom_url, q, t=end)
            if res:
                try:
                    cover_vals[k] = float(res[0]["value"][1])
                except Exception:
                    cover_vals[k] = None
            else:
                cover_vals[k] = None

        fig = plt.figure(figsize=(8.5, 11))
        fig.text(0.5, 0.92, "Lab Overview Report", ha="center", fontsize=22, fontweight="bold")
        fig.text(0.5, 0.88, get_setting("dashboard_title", DEFAULT_DASH_TITLE), ha="center", fontsize=12, color="#444")
        start_str = _time.strftime("%Y-%m-%d %H:%M %Z", _time.localtime(start))
        end_str   = _time.strftime("%Y-%m-%d %H:%M %Z", _time.localtime(end))
        fig.text(0.5, 0.83, f"{start_str}   →   {end_str}", ha="center", fontsize=11)
        fig.text(0.5, 0.80, f"Window: {duration_s/3600:.1f} hours    Scope: {', '.join(label for label,_ in selected_clusters)}",
                 ha="center", fontsize=10, color="#555")

        def _fmt(v, suffix="", nd=2):
            if v is None: return "—"
            return f"{v:,.{nd}f}{suffix}"

        # Big stat cards
        cards = [
            ("Lab Power (now)",   _fmt(cover_vals.get("lab_kw_now"), " kW")),
            ("Peak Lab Power",    _fmt(cover_vals.get("peak_lab_kw"), " kW")),
            ("GPU Energy (window)", _fmt(cover_vals.get("lab_kwh"), " kWh")),
            ("Servers in Scope",  _fmt(cover_vals.get("server_count"), "", nd=0)),
            ("NVIDIA GPUs",       _fmt(cover_vals.get("nv_gpu_count"), "", nd=0)),
            ("AMD GPUs",          _fmt(cover_vals.get("amd_gpu_count"), "", nd=0)),
        ]
        # 2 columns x 3 rows of cards
        for i, (label, val) in enumerate(cards):
            row = i // 2
            col = i % 2
            x = 0.10 + col * 0.45
            y = 0.62 - row * 0.13
            fig.text(x, y, label, fontsize=10, color="#666")
            fig.text(x, y - 0.04, val, fontsize=18, fontweight="bold", color="#1e3a8a")

        fig.text(0.5, 0.10, f"Generated {_time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                 ha="center", fontsize=8, color="#888")
        pdf.savefig(fig); plt.close(fig); pages_written += 1

        # ---------- Timeseries chart helper ----------
        # All timeseries pages render via this helper. Each chart fits into the
        # axis you give it. Two charts per page = (2 rows, 1 col) subplot grid.
        # NOTE: Grafana panel embedding is shelved until the image-renderer
        # plugin is installed on the Grafana server. _grafana_render_panel()
        # is left in the file for that future flip.
        from datetime import datetime
        def _draw_timeseries(ax, query, ylabel, title, scale=1.0, label_key="server", max_legend=8):
            series = _prom_query_range(prom_url, query, start, end, step)
            if not series:
                ax.text(0.5, 0.5, "(no data)", ha="center", va="center",
                        transform=ax.transAxes, color="#a00", fontsize=10)
                ax.set_title(title, fontsize=10, fontweight="bold")
                return
            n_lines = 0
            for s in series:
                xs = [datetime.fromtimestamp(float(p[0])) for p in s["values"]]
                ys = [float(p[1]) * scale for p in s["values"]]
                lbl = s.get("metric", {}).get(label_key, "")
                ax.plot(xs, ys, linewidth=1.0, label=lbl if lbl else None)
                n_lines += 1
            ax.set_title(title, fontsize=10, fontweight="bold")
            ax.set_ylabel(ylabel, fontsize=9)
            ax.grid(True, alpha=0.3)
            ax.tick_params(axis="both", labelsize=7)
            # Date formatting on x-axis
            locator = mdates.AutoDateLocator()
            ax.xaxis.set_major_locator(locator)
            # Pick formatter based on duration
            if duration_s <= 36 * 3600:
                fmtr = mdates.DateFormatter("%H:%M")
            elif duration_s <= 7 * 86400:
                fmtr = mdates.DateFormatter("%m/%d %H:%M")
            else:
                fmtr = mdates.DateFormatter("%m/%d")
            ax.xaxis.set_major_formatter(fmtr)
            for label_obj in ax.get_xticklabels():
                label_obj.set_rotation(0)
            # Legend only if it fits
            if 0 < n_lines <= max_legend and any(s.get("metric", {}).get(label_key) for s in series):
                ax.legend(fontsize=6, loc="upper right", ncol=2, framealpha=0.85)

        def _new_2up_figure(page_title):
            """Create a portrait letter figure with a 2-row, 1-col chart grid
            and a centered page title. Returns (fig, ax_top, ax_bottom)."""
            fig = plt.figure(figsize=(8.5, 11))
            fig.text(0.5, 0.96, page_title, ha="center", fontsize=14, fontweight="bold")
            ax_top = fig.add_axes([0.10, 0.55, 0.82, 0.34])
            ax_bot = fig.add_axes([0.10, 0.10, 0.82, 0.34])
            return fig, ax_top, ax_bot

        # ---------- Page 2: Lab overview — 2 charts ----------
        fig, ax_top, ax_bot = _new_2up_figure("Lab Overview")
        _draw_timeseries(
            ax_top,
            f'sum(power{{sensor="total",server=~"{scope_regex}"}}) / 1000',
            "kW", "Total Server Power (kW)",
            label_key="__none__", max_legend=0,
        )
        _draw_timeseries(
            ax_bot,
            f'sum(DCGM_FI_DEV_POWER_USAGE) / 1000',
            "kW", "Total NVIDIA GPU Power (kW)",
            label_key="__none__", max_legend=0,
        )
        pdf.savefig(fig); plt.close(fig); pages_written += 1

        # ---------- Cluster pages — 2 clusters per page ----------
        for i in range(0, len(selected_clusters), 2):
            pair = selected_clusters[i:i+2]
            fig, ax_top, ax_bot = _new_2up_figure("Cluster Server Power")
            for ax_obj, (label, regex) in zip([ax_top, ax_bot], pair):
                _draw_timeseries(
                    ax_obj,
                    f'power{{sensor="total",server=~"{regex}"}}',
                    "Watts", f"{label} — per server",
                    label_key="server",
                )
                # Append cluster kWh next to the title
                kwh_res = _prom_query(
                    prom_url,
                    f'(sum_over_time((sum(power{{sensor="total",server=~"{regex}"}}))[{duration_s}s:{step}s])) * {step} / 3600 / 1000',
                    t=end,
                )
                kwh_val = None
                if kwh_res:
                    try: kwh_val = float(kwh_res[0]["value"][1])
                    except Exception: pass
                ax_obj.set_title(f"{label} — per server   |   {_fmt(kwh_val, ' kWh')} in window",
                                 fontsize=10, fontweight="bold")
            pdf.savefig(fig); plt.close(fig); pages_written += 1

        # ---------- Page N: Per-server table ----------
        avg_res = _prom_query(prom_url, f'avg_over_time(power{{sensor="total",server=~"{scope_regex}"}}[{duration_s}s])', t=end)
        max_res = _prom_query(prom_url, f'max_over_time(power{{sensor="total",server=~"{scope_regex}"}}[{duration_s}s])', t=end)
        kwh_res = _prom_query(prom_url, f'(sum_over_time(power{{sensor="total",server=~"{scope_regex}"}}[{duration_s}s])) * {step} / 3600 / 1000', t=end)
        # That last query is approximate — better: use increase on energy counter if available.
        # We'll fall back to integrating the gauge for chassis power since Prom doesn't expose a chassis Wh counter.
        avg_by_srv = {r["metric"].get("server","?"): float(r["value"][1]) for r in avg_res}
        max_by_srv = {r["metric"].get("server","?"): float(r["value"][1]) for r in max_res}
        # Per-server kWh: integrate watts → Wh by avg_w * duration_h
        srv_rows = []
        total_kwh = 0.0
        for srv, avg_w in avg_by_srv.items():
            kwh = avg_w * (duration_s / 3600.0) / 1000.0
            total_kwh += kwh
            srv_rows.append((srv, avg_w, max_by_srv.get(srv, 0.0), kwh))
        srv_rows.sort(key=lambda r: r[3], reverse=True)

        fig = plt.figure(figsize=(8.5, 11))
        fig.text(0.5, 0.95, "Per-Server Power Summary", ha="center", fontsize=16, fontweight="bold")
        fig.text(0.5, 0.92, f"Window: {duration_s/3600:.1f} h   |   Total: {total_kwh:,.2f} kWh", ha="center", fontsize=10, color="#555")
        ax = fig.add_axes([0.05, 0.05, 0.90, 0.85])
        ax.axis("off")
        table_data = [["Server", "Avg W", "Peak W", "kWh", "% of Total"]]
        for srv, a, m, k in srv_rows:
            pct = (k/total_kwh*100) if total_kwh > 0 else 0
            table_data.append([srv, f"{a:,.0f}", f"{m:,.0f}", f"{k:,.2f}", f"{pct:.1f}%"])
        if len(table_data) > 1:
            tbl = ax.table(cellText=table_data, loc="upper center", cellLoc="center", colWidths=[0.20,0.15,0.15,0.15,0.15])
            tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1, 1.4)
            for c in range(5):
                tbl[(0,c)].set_facecolor("#1e40af"); tbl[(0,c)].set_text_props(color="white", fontweight="bold")
        else:
            fig.text(0.5, 0.5, "(no per-server data returned)", ha="center", color="#a00")
        pdf.savefig(fig); plt.close(fig); pages_written += 1

        # ---------- Page N+1: GPU energy summary + top-10 ----------
        # Total NV GPU energy in window from monotonic counter
        nv_kwh_res = _prom_query(prom_url,
            f'(sum(increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[{duration_s}s]))) / 3.6e12', t=end)
        nv_total_kwh = float(nv_kwh_res[0]["value"][1]) if nv_kwh_res else 0.0

        # Top-10 GPUs by energy in window
        top_q = (f'topk(10, sum by(Hostname,gpu) '
                 f'(increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[{duration_s}s]))) / 3.6e9')  # → kWh
        top_res = _prom_query(prom_url, top_q, t=end)
        gpu_rows = []
        for r in top_res:
            host = r["metric"].get("Hostname", "?")
            gpu = r["metric"].get("gpu", "?")
            try:
                kwh = float(r["value"][1])
            except Exception:
                kwh = 0.0
            gpu_rows.append((host, gpu, kwh))
        gpu_rows.sort(key=lambda r: r[2], reverse=True)

        fig = plt.figure(figsize=(8.5, 11))
        fig.text(0.5, 0.95, "GPU Energy Summary", ha="center", fontsize=16, fontweight="bold")
        fig.text(0.5, 0.91, f"Total NVIDIA GPU energy in window: {nv_total_kwh:,.2f} kWh",
                 ha="center", fontsize=12, color="#1e3a8a", fontweight="bold")

        # Top-10 horizontal bar
        if gpu_rows:
            ax = fig.add_axes([0.18, 0.42, 0.75, 0.42])
            labels = [f"{h} GPU{g}" for (h,g,_) in gpu_rows]
            vals = [k for (_,_,k) in gpu_rows]
            ypos = list(range(len(labels)))[::-1]
            ax.barh(ypos, vals, color="#1e40af")
            ax.set_yticks(ypos); ax.set_yticklabels(labels, fontsize=8)
            ax.set_xlabel("kWh in window", fontsize=9)
            ax.set_title("Top 10 NVIDIA GPUs by Energy", fontsize=10)
            ax.grid(True, axis="x", alpha=0.3)
        else:
            fig.text(0.5, 0.5, "(no GPU energy data)", ha="center", color="#a00")
        pdf.savefig(fig); plt.close(fig); pages_written += 1

        # ---------- Page N+2: Thermals appendix ----------
        in_res = _prom_query(prom_url, f'max_over_time(temperature{{sensor="Inlet_F",server=~"{scope_regex}"}}[{duration_s}s])', t=end)
        ex_res = _prom_query(prom_url, f'max_over_time(temperature{{sensor="Exhaust_F",server=~"{scope_regex}"}}[{duration_s}s])', t=end)
        in_by = {r["metric"].get("server","?"): float(r["value"][1]) for r in in_res}
        ex_by = {r["metric"].get("server","?"): float(r["value"][1]) for r in ex_res}
        all_servers_set = sorted(set(in_by) | set(ex_by))

        fig = plt.figure(figsize=(8.5, 11))
        fig.text(0.5, 0.95, "Thermals Appendix", ha="center", fontsize=16, fontweight="bold")
        fig.text(0.5, 0.92, f"Max inlet / exhaust temperatures over the window (°F)", ha="center", fontsize=10, color="#555")
        ax = fig.add_axes([0.05, 0.05, 0.90, 0.85]); ax.axis("off")
        td = [["Server", "Max Inlet °F", "Max Exhaust °F"]]
        for srv in all_servers_set:
            td.append([srv, f"{in_by.get(srv,0):.1f}", f"{ex_by.get(srv,0):.1f}"])
        if len(td) > 1:
            tbl = ax.table(cellText=td, loc="upper center", cellLoc="center", colWidths=[0.30,0.25,0.25])
            tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1, 1.5)
            for c in range(3):
                tbl[(0,c)].set_facecolor("#1e40af"); tbl[(0,c)].set_text_props(color="white", fontweight="bold")
        else:
            fig.text(0.5, 0.5, "(no temperature data)", ha="center", color="#a00")
        pdf.savefig(fig); plt.close(fig); pages_written += 1

        # PDF metadata
        d = pdf.infodict()
        d["Title"] = f"Lab Overview Report {start_str} to {end_str}"
        d["Author"] = "cospowerdash"
        d["Subject"] = "Lab overview report"

    pdf_buf.seek(0)
    fn_start = _time.strftime("%Y-%m-%d_%H%M", _time.localtime(start))
    fn_end   = _time.strftime("%Y-%m-%d_%H%M", _time.localtime(end))
    filename = f"lab_overview_report_{fn_start}_to_{fn_end}.pdf"
    return pdf_buf.getvalue(), filename, pages_written, warnings

@app.get("/api/reports/graph")
def api_graph_report(start: int = 0, end: int = 0, clusters: str = ""):
    """Generate the Graph Report PDF and stream it back to the browser."""
    from fastapi.responses import Response, JSONResponse
    try:
        pdf_bytes, filename, pages_written, warnings = _generate_graph_report_pdf(start, end, clusters)
    except ReportError as e:
        code = 500 if "matplotlib" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-Report-Pages": str(pages_written),
    }
    if warnings:
        headers["X-Report-Warnings"] = "; ".join(warnings)[:500]
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

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

  /* Vertical PDU view: side-by-side columns within the rack */
  .pdu-columns {
    display: flex;
    flex: 1 1 0;
    min-height: 0;
    gap: 3px;
  }
  .pdu-columns .pdu-col,
  .pdu-columns .pdu-col-left,
  .pdu-columns .pdu-col-right {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
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
    gap: 0;
    overflow: hidden;
  }
  .pdu-load-row-label {
    flex: 0 0 auto;
    font-weight: 900;
    font-size: clamp(13px, 4.4cqi, 40px);
    color: rgba(226,232,240,0.92);
    white-space: nowrap;
    letter-spacing: 0.3px;
    text-align: center;
    min-width: 3.4em;
  }
  .pdu-load-section.split .pdu-load-row-label { font-size: clamp(11px, 3.6cqi, 30px); min-width: 1.8em; }
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
    height: clamp(12px, 3.6cqi, 30px);
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
  /* Vertical PDU view: use CSS grid on each phase block so the label spans
     the top, the AMPS/WATTS body fills the left, and the inline load bar
     becomes a vertical strip on the right side of the block. */
  .pdu-columns .crt-block {
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-rows: auto 1fr;
  }
  .pdu-columns .crt-block .crt-label {
    grid-column: 1 / -1;
  }
  .pdu-columns .crt-block .crt-body {
    grid-column: 1;
    grid-row: 2;
    flex-direction: column;
  }
  .pdu-columns .crt-block .pdu-load-track.inline-loadbar {
    grid-column: 2;
    grid-row: 2;
    flex-direction: column-reverse;
    width: clamp(10px, 2.8cqi, 22px);
    height: auto;
    margin-top: 0;
    margin-bottom: 0;
    margin-left: 2px;
  }
  .pdu-columns .crt-block .pdu-load-track.inline-loadbar .pdu-load-seg {
    border-right: none;
    border-bottom: 2px solid #0b1220;
  }
  .pdu-columns .crt-block .pdu-load-track.inline-loadbar .pdu-load-seg:last-child {
    border-bottom: none;
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
  /* When a modal is stacked on top of another visible modal, suppress its
     backdrop so the dimming doesn't compound. The bottom-most visible
     modal still provides the single shared backdrop covering the page. */
  .modal.stacked {
    background: transparent;
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
  /* Override the white background Chromium applies to autofilled inputs.
     -webkit-box-shadow inset is the standard trick — autofill sets a
     background-color that can't be overridden directly, but a giant inset
     box-shadow paints over it. Long transition delays the autofill flash. */
  input:-webkit-autofill,
  input:-webkit-autofill:hover,
  input:-webkit-autofill:focus,
  input:-webkit-autofill:active {
    -webkit-box-shadow: 0 0 0 1000px #0b1220 inset !important;
    -webkit-text-fill-color: white !important;
    caret-color: white !important;
    transition: background-color 9999s ease-in-out 0s;
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
    color: #cbd5e1;
    font-weight: 800;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 10px 8px;
    text-align: left;
    border-bottom: 1px solid rgba(255,255,255,0.12);
    white-space: nowrap;
  }
  #reportsTable td {
    padding: 8px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    white-space: nowrap;
    font-weight: 600;
    color: #e2e8f0;
  }
  #reportsTable tr:hover td {
    background: rgba(255,255,255,0.03);
  }

  /* Human-readable report view */
  .human-report {
    padding: 8px 4px;
    color: #e2e8f0;
    line-height: 1.55;
  }
  .human-rack-header {
    margin: 18px 0 10px 0;
    padding: 8px 14px;
    font-size: 20px;
    font-weight: 900;
    color: #f8fafc;
    background: linear-gradient(90deg, rgba(37,99,235,0.45), rgba(37,99,235,0.05));
    border-left: 5px solid #60a5fa;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .human-rack-header:first-child { margin-top: 4px; }
  .human-server {
    margin-bottom: 14px;
    margin-left: 14px;
    padding: 12px 14px;
    background: rgba(15,23,42,0.55);
    border-left: 3px solid #2563eb;
    border-radius: 4px;
  }
  .human-server-header {
    display: flex;
    align-items: stretch;
    flex-wrap: wrap;
    line-height: 1.2;
  }
  .human-server-header .hdr-cell {
    font-family: inherit;
    font-size: 22px;
    font-weight: 800;
    color: #f8fafc;
    letter-spacing: 0.3px;
    padding: 0 16px;
    line-height: 1.2;
  }
  .human-server-header .hdr-cell:first-child {
    padding-left: 0;
  }
  .human-server-header .hdr-cell:not(:last-child) {
    border-right: 2px solid rgba(226,232,240,0.2);
  }
  .human-server-summary {
    margin-top: 12px;
    padding: 8px 0;
    font-size: 16px;
    font-weight: 700;
    color: #cbd5e1;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }
  .human-server-summary.attention { color: #f87171; }
  .human-server-summary.healthy   { color: #4ade80; }
  .human-gpu-list {
    list-style: none;
    margin: 12px 0 0 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px 10px;
  }
  .human-gpu-list li {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 7px 10px;
    font-size: 14px;
    line-height: 1.3;
    color: #cbd5e1;
    background: rgba(255,255,255,0.04);
    border-left: 3px solid #475569;
    border-radius: 3px;
    overflow: hidden;
  }
  .human-gpu-list li .slot-num {
    font-weight: 900;
    color: #f8fafc;
    font-size: 16px;
    flex: 0 0 auto;
    min-width: 1.5em;
  }
  .human-gpu-list li .serial {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 15px;
    font-weight: 700;
    color: #e2e8f0;
    letter-spacing: 0.4px;
    flex: 1 1 auto;
    min-width: 0;
    text-align: center;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .human-gpu-list li .status {
    font-weight: 800;
    font-size: 13px;
    color: #4ade80;
    flex: 0 0 auto;
  }
  .human-gpu-list li.abnormal {
    background: rgba(239,68,68,0.12);
    border-left-color: #ef4444;
    color: #fecaca;
  }
  .human-gpu-list li.abnormal .slot-num { color: #fecaca; }
  .human-gpu-list li.abnormal .serial { color: #fca5a5; }
  .human-gpu-list li.abnormal .status { color: #f87171; }
  .human-empty {
    padding: 24px;
    text-align: center;
    color: #94a3b8;
    font-weight: 700;
  }

  /* Report type chooser cards (Reports modal landing page) */
  .reportCard {
    display:flex;
    align-items:center;
    gap:14px;
    padding:14px 16px;
    margin-bottom:10px;
    background:rgba(37,99,235,0.10);
    border:1px solid rgba(37,99,235,0.35);
    border-radius:12px;
    cursor:pointer;
    transition: background 0.15s, border-color 0.15s, transform 0.12s;
  }
  .reportCard:hover {
    background:rgba(37,99,235,0.22);
    border-color:rgba(96,165,250,0.65);
    transform:translateY(-1px);
  }
  .reportCardIcon {
    flex-shrink:0;
    width:54px;
    height:54px;
    display:flex;
    align-items:center;
    justify-content:center;
    background:rgba(37,99,235,0.25);
    border-radius:11px;
    color:#93c5fd;
  }
  .reportCardIcon svg { width:30px; height:30px; }
  .reportCardText { flex:1; min-width:0; }
  .reportCardTitle {
    font-size:16px;
    font-weight:800;
    color:#f1f5f9;
    margin-bottom:4px;
    letter-spacing:0.2px;
  }
  .reportCardDesc {
    font-size:12px;
    color:rgba(148,163,184,0.85);
    line-height:1.45;
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
      <input id="pduIp" placeholder="Left PDU IP" />
      <div class="status-wrap">
        <div id="pduLight" class="status-light"></div>
        <div id="pduStatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <input id="pdu2Ip" placeholder="Right PDU IP" />
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
      <input id="editPduIp" placeholder="Left PDU IP" />
      <div class="status-wrap">
        <div id="editPduLight" class="status-light"></div>
        <div id="editPduStatusText" class="status-text">Waiting for PDU…</div>
      </div>

      <input id="editPdu2Ip" placeholder="Right PDU IP" />
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
        <span style="color:#cbd5e1;font-weight:600;white-space:nowrap">PDU Bar Style:</span>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="pduLoadStyle" value="grouped" id="loadGrouped" checked /> Grouped</label>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="pduLoadStyle" value="inline" id="loadInline" /> Individual</label>
      </div>
      <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
        <span style="color:#cbd5e1;font-weight:600;white-space:nowrap">PDU View Style:</span>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="pduViewStyle" value="horizontal" id="viewHorizontal" checked /> Horizontal</label>
        <label style="cursor:pointer;white-space:nowrap"><input type="radio" name="pduViewStyle" value="vertical" id="viewVertical" /> Vertical</label>
      </div>
      <div style="margin-top:16px;border-top:1px solid rgba(255,255,255,0.08);padding-top:12px;display:flex;gap:8px">
        <button onclick="openIdracDialog()" style="flex:1;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Configure iDRAC</button>
        <button onclick="openOmeDialog()" style="flex:1;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Configure OME</button>
      </div>
      <div style="margin-top:8px;display:flex;gap:8px">
        <button onclick="openCustomReportingDialog()" style="flex:1;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Configure Custom Reporting</button>
        <button onclick="openSlackDialog()" style="flex:1;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Configure Slack</button>
      </div>
      <div style="margin-top:8px;display:flex;gap:8px">
        <button onclick="openReportDeliveryDialog()" style="flex:1;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Configure Report Delivery</button>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="primary" onclick="saveSettings()">Save</button>
        <button onclick="closeSettings()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- iDRAC Configuration Modal -->
  <div class="modal" id="idracModal">
    <div class="modal-content">
      <h3>Configure iDRAC</h3>
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">iDRAC Credentials:</span></div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input id="idracUser" placeholder="Username" style="flex:1;margin-bottom:0" />
        <input id="idracPass" type="password" placeholder="Password" style="flex:1;margin-bottom:0" />
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <input id="idracTestIp" placeholder="Test IP (any iDRAC)" style="margin-bottom:0;flex:1" />
        <button onclick="testIdrac()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Test</button>
      </div>
      <div id="idracTestResult" style="font-size:13px;font-weight:700;min-height:0;margin-bottom:8px"></div>
      <div style="font-size:12px;color:rgba(148,163,184,0.85);margin-bottom:6px">A successful Test connection is required before saving.</div>
      <div class="row" style="margin-top:12px">
        <button id="idracSaveBtn" class="primary disabled" onclick="saveIdracDialog()">Save</button>
        <button onclick="closeIdracDialog()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- OME Configuration Modal -->
  <div class="modal" id="omeModal">
    <div class="modal-content">
      <h3>Configure OME</h3>
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">OME Connection:</span></div>
      <input id="omeHost" placeholder="OME Host (IP or hostname)" style="margin-bottom:8px" />
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input id="omeUser" placeholder="Username" style="flex:1;margin-bottom:0" />
        <input id="omePass" type="password" placeholder="Password" style="flex:1;margin-bottom:0" />
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <button onclick="testOme()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Test Connection</button>
        <span id="omeTestResult" style="font-size:13px;font-weight:700"></span>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="primary" onclick="saveOmeDialog()">Save</button>
        <button onclick="closeOmeDialog()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Slack Configuration Modal -->
  <div class="modal" id="slackModal">
    <div class="modal-content">
      <h3>Configure Slack</h3>
      <div style="font-size:12px;color:rgba(148,163,184,0.85);margin-bottom:10px">Sends power alerts to a Slack channel via an Incoming Webhook. Alerts fire when any PDU phase exceeds 80% of its rated amperage for 15+ seconds.</div>
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Webhook URL:</span></div>
      <input id="slackWebhookUrl" style="margin-bottom:8px" />
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <button onclick="testSlack()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Test</button>
        <span id="slackTestResult" style="font-size:13px;font-weight:700"></span>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="primary" onclick="saveSlackDialog()">Save</button>
        <button onclick="closeSlackDialog()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Report Delivery Configuration Modal -->
  <div class="modal" id="reportDeliveryModal">
    <div class="modal-content">
      <h3>Configure Report Delivery</h3>
      <div style="font-size:12px;color:rgba(148,163,184,0.85);margin-bottom:10px">
        The dashboard generates the Lab Overview Report on a schedule and drops the PDF into <code>reports/</code>.
        The companion <strong>mailer</strong> app (see <code>mailer/</code> in this repo) runs outside the grey network, fetches the PDFs, and emails them to the recipients below.
      </div>

      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Recipients:</span> <span style="font-size:11px;color:rgba(148,163,184,0.7);font-weight:400">(comma-separated)</span></div>
      <input id="rdRecipients" placeholder="you@signal65.com, teammate@signal65.com" style="margin-bottom:10px" />

      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Schedule:</span></div>
      <select id="rdSchedule" style="margin-bottom:10px;width:100%;padding:8px;border-radius:8px;background:rgba(15,23,42,0.6);color:#e5e7eb;border:1px solid rgba(148,163,184,0.3)"></select>

      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Time Range for Each Report:</span></div>
      <select id="rdTimeRange" style="margin-bottom:10px;width:100%;padding:8px;border-radius:8px;background:rgba(15,23,42,0.6);color:#e5e7eb;border:1px solid rgba(148,163,184,0.3)"></select>

      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Scope:</span> <span style="font-size:11px;color:rgba(148,163,184,0.7);font-weight:400">(comma-separated cluster labels, e.g. R1C2,R2C3 — blank = all)</span></div>
      <input id="rdScope" placeholder="leave blank for all clusters" style="margin-bottom:10px" />

      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Email Subject Template:</span> <span style="font-size:11px;color:rgba(148,163,184,0.7);font-weight:400">(<code>{start}</code> and <code>{end}</code> are replaced with the report window)</span></div>
      <input id="rdSubject" placeholder="COS Lab Weekly Power Report — {start} to {end}" style="margin-bottom:10px" />

      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Sender Label:</span> <span style="font-size:11px;color:rgba(148,163,184,0.7);font-weight:400">(friendly From name the mailer shows)</span></div>
      <input id="rdSenderLabel" placeholder="COS Power Dashboard" style="margin-bottom:12px" />

      <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:10px;margin-bottom:4px">
        <span style="color:#cbd5e1;font-weight:600">Test the pipeline:</span>
        <span style="font-size:11px;color:rgba(148,163,184,0.7);font-weight:400">(generates one PDF in <code>reports/</code> right now using the values above — save first)</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <button onclick="generateReportNow()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Generate Now</button>
        <span id="rdGenResult" style="font-size:13px;font-weight:700"></span>
      </div>

      <div class="row" style="margin-top:12px">
        <button class="primary" onclick="saveReportDeliveryDialog()">Save</button>
        <button onclick="closeReportDeliveryDialog()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Custom Reporting Configuration Modal -->
  <div class="modal" id="customReportingModal">
    <div class="modal-content">
      <h3>Configure Custom Reporting</h3>
      <div style="font-size:12px;color:rgba(148,163,184,0.85);margin-bottom:10px">Used by the Reports → Lab Overview Report feature. Pulls panel renders from Grafana and time-series data from Prometheus.</div>
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Prometheus URL:</span></div>
      <input id="crPromUrl" style="margin-bottom:8px" />
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Grafana URL:</span></div>
      <input id="crGrafanaUrl" style="margin-bottom:8px" />
      <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Grafana Credentials:</span></div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input id="crGrafanaUser" style="flex:1;margin-bottom:0" />
        <input id="crGrafanaPass" type="password" style="flex:1;margin-bottom:0" />
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <button onclick="testCustomReporting()" style="white-space:nowrap;padding:10px 14px;font-size:13px;background:rgba(37,99,235,0.3);color:#93c5fd;border:1px solid rgba(37,99,235,0.3);border-radius:10px;cursor:pointer;font-weight:700">Test Connection</button>
        <span id="crTestResult" style="font-size:13px;font-weight:700"></span>
      </div>
      <div style="font-size:12px;color:rgba(148,163,184,0.85);margin-bottom:6px">A successful Test connection is required before saving.</div>
      <div class="row" style="margin-top:12px">
        <button id="crSaveBtn" class="primary disabled" onclick="saveCustomReportingDialog()">Save</button>
        <button onclick="closeCustomReportingDialog()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- Reports Modal -->
  <div class="modal" id="reportsModal">
    <div id="reportsContent" class="modal-content" style="max-height:85vh;display:flex;flex-direction:column">
      <h3 id="reportsTitle">Reports</h3>
      <div id="reportsChooser" style="display:none">
        <div style="font-size:13px;color:rgba(148,163,184,0.85);margin-bottom:14px">Choose a report type:</div>
        <div class="reportCard" onclick="showGraphFlow()">
          <div class="reportCardIcon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <line x1="3" y1="20" x2="21" y2="20"/>
              <line x1="3" y1="4" x2="3" y2="20"/>
              <polyline points="6 16 10 11 14 14 19 6"/>
              <circle cx="6" cy="16" r="1.2" fill="currentColor"/>
              <circle cx="10" cy="11" r="1.2" fill="currentColor"/>
              <circle cx="14" cy="14" r="1.2" fill="currentColor"/>
              <circle cx="19" cy="6"  r="1.2" fill="currentColor"/>
            </svg>
          </div>
          <div class="reportCardText">
            <div class="reportCardTitle">Lab Overview Report</div>
            <div class="reportCardDesc">Custom date-range PDF with time-series charts built from Prometheus and Grafana data.</div>
          </div>
        </div>
        <div class="reportCard" onclick="showOmeFlow()">
          <div class="reportCardIcon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 3 14 8 19 8"/>
              <line x1="9" y1="13" x2="15" y2="13"/>
              <line x1="9" y1="17" x2="15" y2="17"/>
              <line x1="9" y1="9" x2="11" y2="9"/>
            </svg>
          </div>
          <div class="reportCardText">
            <div class="reportCardTitle">OME Reports</div>
            <div class="reportCardDesc">Pre-built fixed reports from OpenManage Enterprise — Power, Energy, GPU, Thermal, and more.</div>
          </div>
        </div>
        <div class="row" style="margin-top:14px">
          <button onclick="closeReports()">Cancel</button>
        </div>
      </div>
      <div id="reportsNotConfigured" style="display:none;padding:12px 0">
        <div style="font-weight:700;color:#f87171;margin-bottom:8px">OME connection not configured.</div>
        <div style="opacity:0.7;font-size:13px;margin-bottom:12px">Go to Settings and configure the OME connection first.</div>
        <div class="row">
          <button onclick="showReportsChooser()">Back</button>
          <button onclick="closeReports()">Close</button>
        </div>
      </div>
      <div id="reportsError" style="display:none;padding:12px 0">
        <div id="reportsErrorMsg" style="font-weight:700;color:#f87171;margin-bottom:12px;white-space:pre-line"></div>
        <div class="row">
          <button onclick="showReportsChooser()">Back</button>
          <button onclick="closeReports()">Close</button>
        </div>
      </div>
      <div id="reportsSelector" style="display:none">
        <div style="margin-bottom:8px"><span style="color:#cbd5e1;font-weight:600">Select OME Report:</span></div>
        <select id="reportSelect" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,0.16);background:#0b1220;color:white;font-size:14px;margin-bottom:10px"></select>
        <div class="row">
          <button class="primary" onclick="runReport()">Generate</button>
          <button onclick="showReportsChooser()">Back</button>
          <button onclick="closeReports()">Cancel</button>
        </div>
      </div>
      <div id="graphReportForm" style="display:none">
        <div style="font-size:12px;color:rgba(148,163,184,0.85);margin-bottom:10px">Generates a PDF combining Grafana panel renders and Prometheus charts for the selected window.</div>
        <div style="margin-bottom:8px"><span style="color:#cbd5e1;font-weight:600">Time Range:</span></div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:10px">
          <label style="cursor:pointer"><input type="radio" name="grRange" value="24h" checked /> Last 24 hours</label>
          <label style="cursor:pointer"><input type="radio" name="grRange" value="7d" /> Last 7 days</label>
          <label style="cursor:pointer"><input type="radio" name="grRange" value="30d" /> Last 30 days</label>
          <label style="cursor:pointer"><input type="radio" name="grRange" value="custom" /> Custom range</label>
        </div>
        <div id="grCustomRange" style="display:none;margin-bottom:10px;padding:10px;border:1px solid rgba(255,255,255,0.12);border-radius:8px;background:rgba(255,255,255,0.02)">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
            <span style="color:#cbd5e1;font-size:13px;width:50px">Start:</span>
            <input id="grStartDt" type="datetime-local" step="3600" style="flex:1;margin-bottom:0" />
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <span style="color:#cbd5e1;font-size:13px;width:50px">End:</span>
            <input id="grEndDt" type="datetime-local" step="3600" style="flex:1;margin-bottom:0" />
          </div>
          <div style="font-size:11px;color:rgba(148,163,184,0.7);margin-top:6px">Hour resolution. Up to 90 days back.</div>
        </div>
        <div style="margin-bottom:6px"><span style="color:#cbd5e1;font-weight:600">Scope:</span></div>
        <div id="grRackList" style="display:flex;flex-wrap:wrap;gap:6px 14px;margin-bottom:10px">
          <label style="cursor:pointer"><input type="checkbox" id="grAllRacks" checked /> All</label>
          <label style="cursor:pointer"><input type="checkbox" class="grCluster" value="R1C2" checked /> R1C2</label>
          <label style="cursor:pointer"><input type="checkbox" class="grCluster" value="R2C3" checked /> R2C3</label>
          <label style="cursor:pointer"><input type="checkbox" class="grCluster" value="R2C4" checked /> R2C4</label>
          <label style="cursor:pointer"><input type="checkbox" class="grCluster" value="R2C5" checked /> R2C5</label>
          <label style="cursor:pointer"><input type="checkbox" class="grCluster" value="R2C7" checked /> R2C7</label>
          <label style="cursor:pointer"><input type="checkbox" class="grCluster" value="R2C8" checked /> R2C8</label>
        </div>
        <div id="grError" style="color:#f87171;font-size:13px;font-weight:700;margin-bottom:8px;min-height:0"></div>
        <div class="row">
          <button class="primary" onclick="generateGraphReport()">Generate</button>
          <button onclick="showReportsChooser()">Back</button>
          <button onclick="closeReports()">Cancel</button>
        </div>
      </div>
      <div id="reportsLoading" style="display:none;padding:20px 0;text-align:center;opacity:0.6;font-weight:700">Loading...</div>
      <div id="reportsResults" style="display:none;flex:1;min-height:0;overflow:auto;margin-top:10px">
        <div id="reportsHumanView" style="display:none"></div>
        <div id="reportsTableWrapper" style="display:none">
          <table id="reportsTable" style="width:100%;border-collapse:collapse;font-size:14px">
            <thead id="reportsHead" style="position:sticky;top:0"></thead>
            <tbody id="reportsBody"></tbody>
          </table>
        </div>
      </div>
      <div id="reportsActions" style="display:none;margin-top:10px">
        <div class="row">
          <button onclick="backToReportSelect()">Back</button>
          <button id="reportsToggleViewBtn" onclick="toggleReportView()" style="display:none">View Source</button>
          <button id="reportsDownloadBtn" onclick="downloadReportCurrent()">Download CSV</button>
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
  // Read persisted view settings synchronously so the very first paint
  // already reflects the user's saved viewport style. Otherwise the
  // dashboard renders one frame in the default Racks mode before the
  // DOMContentLoaded handler reads localStorage and switches modes.
  let viewportStyle = localStorage.getItem("viewportStyle") || "racks";
  let pduLoadStyle = (function() {
    let s = localStorage.getItem("pduLoadStyle") || "grouped";
    // Legacy migration: old "single" and "split" both map to "grouped"
    if (s !== "inline") s = "grouped";
    return s;
  })();
  let pduViewStyle = localStorage.getItem("pduViewStyle") || "horizontal";
  // Apply the fill-mode page override immediately (the .page element
  // already exists in the DOM by the time this script tag runs, since
  // the script is at the bottom of <body>).
  (function() {
    const page = document.querySelector(".page");
    if (page && viewportStyle === "fill") page.style.maxWidth = "100%";
  })();

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
        // Helper: build all DOM content for a single PDU and return in a container
        function buildPduColumn(pdu) {
          let col = document.createElement("div");
          col.className = "pdu-col";

          let pduBanner = document.createElement("div");
          pduBanner.className = "ip-banner";
          pduBanner.textContent = pdu.pdu_ip ? ((pdu.type === "servertech" ? "Server Tech" : pdu.type === "raritan" ? "Raritan" : "PDU") + ": " + pdu.pdu_ip) : "\u00A0";
          col.appendChild(pduBanner);

          const ratedAForInline = (pdu.rated_a && pdu.rated_a > 0) ? pdu.rated_a : 30;
          (pdu.phases || []).forEach(phase => {
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

            if (pduLoadStyle === "inline") {
              block.appendChild(buildInlineLoadBar(phase, ratedAForInline));
            }

            col.appendChild(block);
          });

          const loadSection = buildLoadSection(pdu);
          if (loadSection) col.appendChild(loadSection);
          return col;
        }

        if (pduViewStyle === "vertical") {
          // Side-by-side: Left PDU (pdu_key=pdu_ip) on the left,
          // Right PDU (pdu_key=pdu2_ip) on the right. Find each by
          // its pdu_key rather than array index so a rack with only a
          // Right PDU correctly shows it in the right column.
          const leftPdu = pdus.find(p => p.pdu_key === "pdu_ip") || null;
          const rightPdu = pdus.find(p => p.pdu_key === "pdu2_ip") || null;
          let row = document.createElement("div");
          row.className = "pdu-columns";
          let leftCol = leftPdu ? buildPduColumn(leftPdu) : document.createElement("div");
          leftCol.classList.add("pdu-col-left");
          row.appendChild(leftCol);
          let rightCol = rightPdu ? buildPduColumn(rightPdu) : document.createElement("div");
          rightCol.classList.add("pdu-col-right");
          row.appendChild(rightCol);
          serverArea.appendChild(row);
        } else {
          // Horizontal (default): PDUs stacked top-to-bottom
          pdus.forEach(pdu => {
            let col = buildPduColumn(pdu);
            // Append children directly into serverArea to preserve the flat layout
            while (col.firstChild) serverArea.appendChild(col.firstChild);
          });
        }
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
  const PDU_LOAD_SEGMENTS_INLINE = 30;

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
    label.textContent = labelText;
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
    const blocks = document.querySelectorAll(".crt-block");
    // First pass: compute the ideal font size for each block, track the
    // global minimum so every metric on the page is sized uniformly.
    let globalMinVal = Infinity;
    let globalMinUnit = Infinity;
    let globalMinLabel = Infinity;
    const blockInfos = [];
    blocks.forEach(block => {
      const body = block.querySelector(".crt-body");
      if (!body) return;
      const bh = body.clientHeight;
      const bw = body.clientWidth;
      if (bh <= 0 || bw <= 0) return;

      const metricCols = body.querySelectorAll(".crt-metric-col");
      let localMinVal = Infinity;
      let localMinUnit = Infinity;
      metricCols.forEach(col => {
        const cw = col.clientWidth - 4;
        if (cw <= 0) return;
        const val = col.querySelector(".crt-metric-val");
        if (val) {
          const text = val.textContent || "000";
          const maxByWidth = cw / (text.length * 0.65);
          const size = Math.max(8, Math.min(bh * 0.55, maxByWidth));
          if (size < localMinVal) localMinVal = size;
        }
        const unitSize = Math.max(6, Math.min(bh * 0.22, cw * 0.22));
        if (unitSize < localMinUnit) localMinUnit = unitSize;
      });
      const blockH = block.clientHeight || bh;
      const labelSize = Math.max(8, Math.min(bw * 0.055, blockH * 0.2));

      if (localMinVal < globalMinVal) globalMinVal = localMinVal;
      if (localMinUnit < globalMinUnit) globalMinUnit = localMinUnit;
      if (labelSize < globalMinLabel) globalMinLabel = labelSize;
      blockInfos.push({ block, metricCols });
    });

    // Second pass: apply the global minimum to every block for uniformity
    blockInfos.forEach(({ block, metricCols }) => {
      metricCols.forEach(col => {
        const unit = col.querySelector(".crt-metric-unit");
        const val = col.querySelector(".crt-metric-val");
        if (unit) unit.style.fontSize = globalMinUnit + "px";
        if (val) val.style.fontSize = globalMinVal + "px";
      });
      const lbl = block.querySelector(".crt-label");
      if (lbl) lbl.style.fontSize = globalMinLabel + "px";
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
  let pduOk = true;   // empty Left PDU is OK
  let pdu2Ok = true;  // empty Right PDU is OK

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
    pduOk = true;
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
      // Empty field is fine for either PDU — mark OK and reset the light
      setFn(true, "Waiting for PDU\u2026");
      const lightId = tokenProp === 2 ? "pdu2Light" : "pduLight";
      document.getElementById(lightId).classList.remove("green");
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
    // viewportStyle and pduLoadStyle were already initialized synchronously
    // from localStorage at script start so the first paint is correct.
    computeRackSize(racksCache.length);

    // Single-backdrop dimming for stacked modals: when more than one modal
    // is visible, only the first (DOM-order) keeps its backdrop; the rest
    // get .stacked which makes their background transparent so the dimming
    // doesn't compound. We watch every .modal's style attribute so we don't
    // need to touch every show/hide call site.
    (function setupModalStacking() {
      const modals = document.querySelectorAll(".modal");
      function update() {
        const visible = Array.from(modals).filter(m => m.style.display === "flex");
        modals.forEach(m => m.classList.remove("stacked"));
        visible.slice(1).forEach(m => m.classList.add("stacked"));
      }
      const obs = new MutationObserver(update);
      modals.forEach(m => obs.observe(m, { attributes: true, attributeFilter: ["style"] }));
      update();
    })();

    document.getElementById("idracPass").addEventListener("focus", function() {
      if (this.dataset.unchanged === "true") { this.value = ""; this.dataset.unchanged = "false"; }
    });
    document.getElementById("omePass").addEventListener("focus", function() {
      if (this.dataset.unchanged === "true") { this.value = ""; this.dataset.unchanged = "false"; }
    });
    document.getElementById("crGrafanaPass").addEventListener("focus", function() {
      if (this.dataset.unchanged === "true") { this.value = ""; this.dataset.unchanged = "false"; }
    });
    // Any edit to Custom Reporting fields invalidates the prior Test result
    ["crPromUrl", "crGrafanaUrl", "crGrafanaUser", "crGrafanaPass"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", function() {
        document.getElementById("crTestResult").textContent = "";
        setCrSaveEnabled(false);
      });
    });
    // Any edit to iDRAC fields invalidates the prior Test result
    ["idracUser", "idracPass", "idracTestIp"].forEach(id => {
      document.getElementById(id).addEventListener("input", invalidateIdracTest);
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

    if (!label) {
      errEl.textContent = "Enter a rack label";
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
  let editPduOk = true;   // empty Left PDU is OK
  let editPdu2Ok = true;  // empty Right PDU is OK

  function openEdit(rack) {
    editRackId = rack.id;
    document.getElementById("editModal").style.display = "flex";
    document.getElementById("editLabel").value = rack.label || "";
    document.getElementById("editPduIp").value = rack.pdu_ip || "";
    document.getElementById("editPdu2Ip").value = rack.pdu2_ip || "";
    document.getElementById("editError").textContent = "";

    // Run checks on existing IPs — both start OK (empty is fine)
    editPduOk = true;
    editPdu2Ok = true;
    document.getElementById("editPduLight").classList.remove("green");
    document.getElementById("editPduStatusText").innerText = "Waiting for PDU\u2026";
    document.getElementById("editPdu2Light").classList.remove("green");
    document.getElementById("editPdu2StatusText").innerText = "Waiting for PDU\u2026";

    if (rack.pdu_ip) {
      editPduOk = false;
      document.getElementById("editPduStatusText").innerText = "Checking\u2026";
      checkEditPdu(rack.pdu_ip, 1);
    }
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
      // Empty field is fine for either PDU
      const lightId = which === 2 ? "editPdu2Light" : "editPduLight";
      document.getElementById(lightId).classList.remove("green");
      setFn(true, "Waiting for PDU\u2026");
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

    if (!label) { errEl.textContent = "Enter a rack label"; return; }

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
  function openSettings() {
    document.getElementById("settingsModal").style.display = "flex";
    const current = document.getElementById("dashTitle").innerText || "";
    document.getElementById("titleInput").value = current.trim();
    document.getElementById(viewportStyle === "fill" ? "vpFill" : "vpRacks").checked = true;
    document.getElementById(pduLoadStyle === "inline" ? "loadInline" : "loadGrouped").checked = true;
    document.getElementById(pduViewStyle === "vertical" ? "viewVertical" : "viewHorizontal").checked = true;
    setTimeout(() => document.getElementById("titleInput").focus(), 50);
  }

  function closeSettings() { document.getElementById("settingsModal").style.display = "none"; }

  // ---------------- iDRAC Configuration Dialog ----------------
  function setIdracSaveEnabled(enabled) {
    const btn = document.getElementById("idracSaveBtn");
    if (!btn) return;
    if (enabled) btn.classList.remove("disabled");
    else btn.classList.add("disabled");
  }

  function invalidateIdracTest() {
    setIdracSaveEnabled(false);
    const result = document.getElementById("idracTestResult");
    if (result && result.textContent && !result.textContent.startsWith("Re-test")) {
      result.textContent = "Re-test required after change";
      result.style.color = "#94a3b8";
    }
  }

  async function openIdracDialog() {
    // Fetch BEFORE showing the modal to avoid a race where the user
    // starts typing and the fetch response then overwrites their input.
    let userVal = "";
    let hasPassword = false;
    try {
      const ir = await fetch("/api/settings/idrac");
      const id = await ir.json();
      if (id && id.ok) {
        userVal = id.username || "";
        hasPassword = !!id.has_password;
      }
    } catch(e) {}
    document.getElementById("idracUser").value = userVal;
    const passEl = document.getElementById("idracPass");
    passEl.value = hasPassword ? "********" : "";
    passEl.dataset.unchanged = hasPassword ? "true" : "false";
    document.getElementById("idracTestIp").value = "";
    document.getElementById("idracTestResult").textContent = "";
    setIdracSaveEnabled(false);
    document.getElementById("idracModal").style.display = "flex";
  }

  function closeIdracDialog() { document.getElementById("idracModal").style.display = "none"; }

  async function saveIdracDialog() {
    // Save is gated on a successful Test connection — refuse if not verified.
    if (document.getElementById("idracSaveBtn").classList.contains("disabled")) return;
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
    closeIdracDialog();
  }

  // ---------------- OME Configuration Dialog ----------------
  async function openOmeDialog() {
    // Fetch BEFORE showing the modal to avoid a race where the user
    // starts typing and the fetch response then overwrites their input.
    let hostVal = "";
    let userVal = "";
    let hasPassword = false;
    try {
      const or = await fetch("/api/settings/ome");
      const od = await or.json();
      if (od && od.ok) {
        hostVal = od.host || "";
        userVal = od.username || "";
        hasPassword = !!od.has_password;
      }
    } catch(e) {}
    document.getElementById("omeHost").value = hostVal;
    document.getElementById("omeUser").value = userVal;
    const omePassEl = document.getElementById("omePass");
    omePassEl.value = hasPassword ? "********" : "";
    omePassEl.dataset.unchanged = hasPassword ? "true" : "false";
    document.getElementById("omeTestResult").textContent = "";
    document.getElementById("omeModal").style.display = "flex";
  }

  function closeOmeDialog() { document.getElementById("omeModal").style.display = "none"; }

  async function saveOmeDialog() {
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
    closeOmeDialog();
  }

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
    const viewVal = document.querySelector('input[name="pduViewStyle"]:checked').value;
    localStorage.setItem("pduViewStyle", viewVal);
    pduViewStyle = viewVal;
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
    closeSettings();
  }

  async function testIdrac() {
    setIdracSaveEnabled(false);
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
      if (data.ok) {
        result.textContent = "Connected — " + (data.model || "OK");
        result.style.color = "#22c55e";
        setIdracSaveEnabled(true);
      } else {
        result.textContent = data.error || "Connection failed";
        result.style.color = "#f87171";
      }
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

  // ---------------- Custom Reporting (Prometheus + Grafana) ----------------
  function setCrSaveEnabled(enabled) {
    const btn = document.getElementById("crSaveBtn");
    if (!btn) return;
    if (enabled) btn.classList.remove("disabled");
    else btn.classList.add("disabled");
  }

  async function openCustomReportingDialog() {
    let promUrl = "", grafUrl = "", grafUser = "", hasPass = false;
    try {
      const r = await fetch("/api/settings/custom_reporting");
      const d = await r.json();
      if (d && d.ok) {
        promUrl = d.prom_url || "";
        grafUrl = d.grafana_url || "";
        grafUser = d.grafana_user || "";
        hasPass = !!d.has_grafana_pass;
      }
    } catch(e) {}
    document.getElementById("crPromUrl").value = promUrl;
    document.getElementById("crGrafanaUrl").value = grafUrl;
    document.getElementById("crGrafanaUser").value = grafUser;
    const passEl = document.getElementById("crGrafanaPass");
    passEl.value = hasPass ? "********" : "";
    passEl.dataset.unchanged = hasPass ? "true" : "false";
    document.getElementById("crTestResult").textContent = "";
    setCrSaveEnabled(false);
    document.getElementById("customReportingModal").style.display = "flex";
  }

  function closeCustomReportingDialog() {
    document.getElementById("customReportingModal").style.display = "none";
  }

  async function testCustomReporting() {
    setCrSaveEnabled(false);
    const promUrl = document.getElementById("crPromUrl").value.trim();
    const grafUrl = document.getElementById("crGrafanaUrl").value.trim();
    const grafUser = document.getElementById("crGrafanaUser").value.trim();
    const passEl = document.getElementById("crGrafanaPass");
    const grafPass = passEl.dataset.unchanged === "true" ? "" : passEl.value.trim();
    const result = document.getElementById("crTestResult");
    if (!promUrl || !grafUrl || !grafUser || (!grafPass && passEl.dataset.unchanged !== "true")) {
      result.textContent = "Fill in all fields"; result.style.color = "#f87171"; return;
    }
    result.textContent = "Testing..."; result.style.color = "#94a3b8";
    try {
      const body = {prom_url: promUrl, grafana_url: grafUrl, grafana_user: grafUser, grafana_pass: grafPass || ""};
      const res = await fetch("/api/settings/custom_reporting/test", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const data = await res.json();
      if (data.ok) {
        result.textContent = `OK — Prom v${data.prom_version || "?"}, Grafana v${data.grafana_version || "?"}`;
        result.style.color = "#22c55e";
        setCrSaveEnabled(true);
      } else {
        result.textContent = data.error || "Test failed"; result.style.color = "#f87171";
      }
    } catch(e) { result.textContent = "Test failed"; result.style.color = "#f87171"; }
  }

  async function saveCustomReportingDialog() {
    const btn = document.getElementById("crSaveBtn");
    if (btn.classList.contains("disabled")) return;
    const promUrl = document.getElementById("crPromUrl").value.trim();
    const grafUrl = document.getElementById("crGrafanaUrl").value.trim();
    const grafUser = document.getElementById("crGrafanaUser").value.trim();
    const passEl = document.getElementById("crGrafanaPass");
    // If user didn't touch the password, send empty -> backend keeps the stored value.
    const grafPass = passEl.dataset.unchanged === "true" ? "" : passEl.value.trim();
    try {
      await fetch("/api/settings/custom_reporting", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({prom_url: promUrl, grafana_url: grafUrl, grafana_user: grafUser, grafana_pass: grafPass})
      });
    } catch(e) {}
    closeCustomReportingDialog();
  }

  // ---------------- Slack Configuration ----------------
  async function openSlackDialog() {
    let url = "";
    try {
      const r = await fetch("/api/settings/slack_webhook");
      const d = await r.json();
      if (d && d.ok) url = d.url || "";
    } catch(e) {}
    document.getElementById("slackWebhookUrl").value = url;
    document.getElementById("slackTestResult").textContent = "";
    document.getElementById("slackModal").style.display = "flex";
  }

  function closeSlackDialog() {
    document.getElementById("slackModal").style.display = "none";
  }

  async function testSlack() {
    const url = document.getElementById("slackWebhookUrl").value.trim();
    const result = document.getElementById("slackTestResult");
    if (!url) { result.textContent = "Enter a webhook URL"; result.style.color = "#f87171"; return; }
    result.textContent = "Sending..."; result.style.color = "#94a3b8";
    try {
      const res = await fetch("/api/settings/slack_test", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({url: url})
      });
      const data = await res.json();
      if (data.ok) { result.textContent = "Sent! Check your Slack channel."; result.style.color = "#22c55e"; }
      else { result.textContent = data.error || "Failed"; result.style.color = "#f87171"; }
    } catch(e) { result.textContent = "Test failed"; result.style.color = "#f87171"; }
  }

  async function saveSlackDialog() {
    const url = document.getElementById("slackWebhookUrl").value.trim();
    try {
      await fetch("/api/settings/slack_webhook", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({url: url})
      });
    } catch(e) {}
    closeSlackDialog();
  }

  // ---------------- Report Delivery Configuration ----------------
  async function openReportDeliveryDialog() {
    let s = {recipients:"", schedule:"disabled", scope:"", time_range:"trailing_7d",
             subject_template:"", sender_label:"", schedule_options:{}, time_range_options:{}};
    try {
      const r = await fetch("/api/settings/report_delivery");
      const d = await r.json();
      if (d && d.ok) s = Object.assign(s, d);
    } catch(e) {}
    // Populate the two dropdowns from server-provided option maps.
    const schedSel = document.getElementById("rdSchedule");
    schedSel.innerHTML = "";
    Object.entries(s.schedule_options || {}).forEach(([k, label]) => {
      const opt = document.createElement("option");
      opt.value = k; opt.textContent = label;
      if (k === s.schedule) opt.selected = true;
      schedSel.appendChild(opt);
    });
    const trSel = document.getElementById("rdTimeRange");
    trSel.innerHTML = "";
    Object.entries(s.time_range_options || {}).forEach(([k, label]) => {
      const opt = document.createElement("option");
      opt.value = k; opt.textContent = label;
      if (k === s.time_range) opt.selected = true;
      trSel.appendChild(opt);
    });
    document.getElementById("rdRecipients").value = s.recipients || "";
    document.getElementById("rdScope").value = s.scope || "";
    document.getElementById("rdSubject").value = s.subject_template || "";
    document.getElementById("rdSenderLabel").value = s.sender_label || "";
    document.getElementById("rdGenResult").textContent = "";
    document.getElementById("reportDeliveryModal").style.display = "flex";
  }

  function closeReportDeliveryDialog() {
    document.getElementById("reportDeliveryModal").style.display = "none";
  }

  function _reportDeliveryBody() {
    return {
      recipients: document.getElementById("rdRecipients").value.trim(),
      schedule: document.getElementById("rdSchedule").value,
      time_range: document.getElementById("rdTimeRange").value,
      scope: document.getElementById("rdScope").value.trim(),
      subject_template: document.getElementById("rdSubject").value.trim(),
      sender_label: document.getElementById("rdSenderLabel").value.trim(),
    };
  }

  async function saveReportDeliveryDialog() {
    const result = document.getElementById("rdGenResult");
    try {
      const res = await fetch("/api/settings/report_delivery", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(_reportDeliveryBody())
      });
      const data = await res.json();
      if (data && data.ok) { closeReportDeliveryDialog(); return; }
      result.textContent = (data && data.error) || "Save failed";
      result.style.color = "#f87171";
    } catch(e) {
      result.textContent = "Save failed"; result.style.color = "#f87171";
    }
  }

  async function generateReportNow() {
    const result = document.getElementById("rdGenResult");
    result.textContent = "Generating... (can take 30–90s)"; result.style.color = "#94a3b8";
    try {
      const res = await fetch("/api/reports/generate_now", {method:"POST"});
      const data = await res.json();
      if (data && data.ok) {
        result.textContent = "Wrote " + data.filename;
        result.style.color = "#22c55e";
      } else {
        result.textContent = (data && data.error) || "Generate failed";
        result.style.color = "#f87171";
      }
    } catch(e) {
      result.textContent = "Generate failed"; result.style.color = "#f87171";
    }
  }

  // ---------------- Reports ----------------
  // Hide every panel inside the Reports modal. Helper used before showing one.
  function hideAllReportPanels() {
    document.getElementById("reportsChooser").style.display = "none";
    document.getElementById("reportsNotConfigured").style.display = "none";
    document.getElementById("reportsError").style.display = "none";
    document.getElementById("reportsSelector").style.display = "none";
    document.getElementById("reportsResults").style.display = "none";
    document.getElementById("reportsActions").style.display = "none";
    document.getElementById("graphReportForm").style.display = "none";
    document.getElementById("reportsLoading").style.display = "none";
  }

  function openReports() {
    document.getElementById("reportsModal").style.display = "flex";
    showReportsChooser();
  }

  function showReportsChooser() {
    hideAllReportPanels();
    document.getElementById("reportsTitle").textContent = "Select Report Type";
    document.getElementById("reportsChooser").style.display = "block";
  }

  // OME flow: fetch the list of fixed reports, populate the dropdown, show
  // the OME selector. If OME isn't configured or the fetch fails, show the
  // not-configured panel (with a Back button to return to the chooser).
  async function showOmeFlow() {
    hideAllReportPanels();
    document.getElementById("reportsTitle").textContent = "OME Reports";
    document.getElementById("reportsLoading").style.display = "block";
    try {
      const res = await fetch("/api/reports/available");
      const data = await res.json();
      hideAllReportPanels();
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
      hideAllReportPanels();
      document.getElementById("reportsNotConfigured").style.display = "block";
    }
  }

  // Lab Overview Report flow: hide everything else and reuse the existing form opener.
  function showGraphFlow() {
    hideAllReportPanels();
    document.getElementById("reportsTitle").textContent = "Lab Overview Report";
    openGraphReportForm();
  }

  function closeReports() {
    document.getElementById("reportsModal").style.display = "none";
    // Reset internal panels so the next openReports() starts in a clean state
    hideAllReportPanels();
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
      renderReportTable(data.columns, data.rows, data.rack_assignments || {}, data.rack_order || []);
    } catch(e) {
      document.getElementById("reportsLoading").style.display = "none";
      document.getElementById("reportsSelector").style.display = "block";
    }
  }

  function renderReportTable(columns, rows, rackAssignments, rackOrder) {
    rackAssignments = rackAssignments || {};
    rackOrder = rackOrder || [];
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
    // Size the modal to fit the table without horizontal scrolling. Use
    // most of the viewport so wide reports fit; the body still scrolls
    // vertically if there are too many rows.
    document.getElementById("reportsContent").style.width = "min(1800px, 98vw)";
    document.getElementById("reportsContent").style.maxWidth = "98vw";
    document.getElementById("reportsResults").style.display = "block";
    document.getElementById("reportsActions").style.display = "block";

    // Build a human-readable view if a recipe exists for this report
    const humanContainer = document.getElementById("reportsHumanView");
    humanContainer.innerHTML = "";
    const humanContent = buildHumanReport(columns, rows, rackAssignments, rackOrder);
    const toggleBtn = document.getElementById("reportsToggleViewBtn");
    if (humanContent) {
      humanContainer.appendChild(humanContent);
      toggleBtn.style.display = "";
      setReportView("human");
    } else {
      // No recipe — just show the source table
      toggleBtn.style.display = "none";
      setReportView("source");
    }
  }

  // ---------------- Human-readable report views ----------------
  // Per-report recipes that turn the spreadsheet into a structured
  // document. To add support for a new report, write a builder
  // function and add it to the dispatch in buildHumanReport().

  function buildHumanReport(columns, rows, rackAssignments, rackOrder) {
    // GPU Details report — detected by GPU-specific column names
    if (columns.indexOf("GPU Name") >= 0 && columns.indexOf("GPU FQDD") >= 0) {
      return buildGpuDetailsHumanReport(columns, rows, rackAssignments || {}, rackOrder || []);
    }
    return null;
  }

  function colIndexMap(columns, names) {
    const map = {};
    names.forEach(function(n) { map[n] = columns.indexOf(n); });
    return map;
  }

  function extractSlotNumber(fqdd) {
    // fqdd looks like "Video.Slot.21-1" — pull the digits after "Slot."
    if (!fqdd) return "";
    var key = "Slot.";
    var i = fqdd.indexOf(key);
    if (i < 0) return "";
    var after = fqdd.substring(i + key.length);
    var n = "";
    for (var j = 0; j < after.length; j++) {
      var c = after.charCodeAt(j);
      if (c >= 48 && c <= 57) n += after.charAt(j);
      else break;
    }
    return n;
  }

  function isHealthyState(name, value) {
    if (!value) return true;
    var v = value.toLowerCase().trim();
    if (name === "GPU Health")              return v === "online" || v === "ok";
    if (name === "GPU Status")              return v === "available" || v === "enabled";
    if (name === "GPU Power Supply Status") return v === "enabled";
    if (name === "GPU Thermal Alert State") return v === "not pending" || v === "off" || v === "released";
    if (name === "GPU Power Brake State")   return v === "released" || v === "off" || v === "not pending";
    return true;
  }

  function buildGpuDetailsHumanReport(columns, rows, rackAssignments, rackOrder) {
    var COLS = colIndexMap(columns, [
      "Server Name", "Server Model", "Server Identifier",
      "GPU Name", "GPU FQDD", "GPU Firmware Version", "GPU Health",
      "GPU Status", "GPU Manufacturer", "GPU Marketing Name",
      "GPU Serial Number", "GPU Power Supply Status",
      "GPU Thermal Alert State", "GPU Power Brake State"
    ]);
    function cell(row, name) {
      var i = COLS[name];
      return (i >= 0 && i < row.length && row[i] != null) ? String(row[i]).trim() : "";
    }

    // Group rows by Server Name (iDRAC IP)
    var byIp = new Map();
    rows.forEach(function(row) {
      var ip = cell(row, "Server Name");
      if (!ip) return;
      if (!byIp.has(ip)) byIp.set(ip, []);
      byIp.get(ip).push(row);
    });

    var container = document.createElement("div");
    container.className = "human-report";

    if (byIp.size === 0) {
      var empty = document.createElement("div");
      empty.className = "human-empty";
      empty.textContent = "No GPU data for any configured server.";
      container.appendChild(empty);
      return container;
    }

    // Group IPs by rack label so the report renders one rack section
    // at a time. Use the rack_order from the backend so racks appear
    // in the same order as they do on the dashboard. Any IPs without
    // a known rack assignment fall into a trailing "Unassigned" group.
    var rackBuckets = new Map();
    function addToRack(label, ip) {
      if (!rackBuckets.has(label)) rackBuckets.set(label, []);
      rackBuckets.get(label).push(ip);
    }
    Array.from(byIp.keys()).forEach(function(ip) {
      var label = (rackAssignments && rackAssignments[ip]) || "Unassigned";
      addToRack(label, ip);
    });

    // Build the final ordered list of rack labels: backend order first,
    // then any extra labels (e.g. "Unassigned") that weren't in rackOrder.
    var orderedLabels = [];
    rackOrder.forEach(function(lbl) {
      if (rackBuckets.has(lbl)) orderedLabels.push(lbl);
    });
    rackBuckets.forEach(function(_v, lbl) {
      if (orderedLabels.indexOf(lbl) < 0) orderedLabels.push(lbl);
    });

    orderedLabels.forEach(function(rackLabel) {
      var ipsInRack = rackBuckets.get(rackLabel) || [];
      ipsInRack.sort();

      // Rack section header
      var rackHeader = document.createElement("div");
      rackHeader.className = "human-rack-header";
      rackHeader.textContent = rackLabel;
      container.appendChild(rackHeader);

      ipsInRack.forEach(function(ip) {
        var serverRows = byIp.get(ip);
        if (!serverRows || !serverRows.length) return;
        var first = serverRows[0];
        var model = cell(first, "Server Model") || "Unknown Model";
        var tag = cell(first, "Server Identifier") || "\u2014";

        var serverDiv = document.createElement("div");
        serverDiv.className = "human-server";

        // Model | IP | Service Tag on the same primary heading line,
        // separated by faded vertical dividers. Identical font for all.
        var headerLine = document.createElement("div");
        headerLine.className = "human-server-header";
        var modelSpan = document.createElement("span");
        modelSpan.className = "hdr-cell";
        modelSpan.textContent = model;
        var ipSpan = document.createElement("span");
        ipSpan.className = "hdr-cell";
        ipSpan.textContent = ip;
        var tagSpan = document.createElement("span");
        tagSpan.className = "hdr-cell";
        tagSpan.textContent = "Service Tag: " + tag;
        headerLine.appendChild(modelSpan);
        headerLine.appendChild(ipSpan);
        headerLine.appendChild(tagSpan);
        serverDiv.appendChild(headerLine);

      // Aggregate summary
      var firmwares = new Set();
      var marketingNames = new Set();
      var allHealthy = true;
      serverRows.forEach(function(r) {
        firmwares.add(cell(r, "GPU Firmware Version"));
        marketingNames.add(cell(r, "GPU Marketing Name"));
        if (!isHealthyState("GPU Health", cell(r, "GPU Health"))) allHealthy = false;
        if (!isHealthyState("GPU Status", cell(r, "GPU Status"))) allHealthy = false;
        if (!isHealthyState("GPU Thermal Alert State", cell(r, "GPU Thermal Alert State"))) allHealthy = false;
        if (!isHealthyState("GPU Power Brake State", cell(r, "GPU Power Brake State"))) allHealthy = false;
        if (!isHealthyState("GPU Power Supply Status", cell(r, "GPU Power Supply Status"))) allHealthy = false;
      });

      var summary = document.createElement("div");
      summary.className = "human-server-summary " + (allHealthy ? "healthy" : "attention");
      var marketing = Array.from(marketingNames).filter(function(s){ return s; }).join(", ") || "GPU";
      var summaryParts = [serverRows.length + " \u00D7 " + marketing];
      if (firmwares.size === 1) {
        var fwOnly = Array.from(firmwares)[0];
        if (fwOnly) summaryParts.push("Firmware " + fwOnly);
      } else if (firmwares.size > 1) {
        summaryParts.push("Mixed firmware (" + firmwares.size + " versions)");
      }
      summaryParts.push(allHealthy ? "All Healthy" : "ATTENTION REQUIRED");
      summary.textContent = summaryParts.join("    \u00B7    ");
      serverDiv.appendChild(summary);

      // Per-GPU bullet list, sorted by slot number
      var sortedRows = serverRows.slice().sort(function(a, b) {
        var sa = parseInt(extractSlotNumber(cell(a, "GPU FQDD")), 10) || 0;
        var sb = parseInt(extractSlotNumber(cell(b, "GPU FQDD")), 10) || 0;
        return sa - sb;
      });
      var ul = document.createElement("ul");
      ul.className = "human-gpu-list";
      sortedRows.forEach(function(r) {
        var slot = extractSlotNumber(cell(r, "GPU FQDD"));
        var slotLabel = slot || (cell(r, "GPU FQDD") || "?");
        var serial = cell(r, "GPU Serial Number") || "\u2014";
        var li = document.createElement("li");

        // Check for any abnormal states and surface them
        var problems = [];
        var checks = [
          ["GPU Health", "Health"],
          ["GPU Status", "Status"],
          ["GPU Power Supply Status", "PSU"],
          ["GPU Thermal Alert State", "Therm"],
          ["GPU Power Brake State", "Brake"]
        ];
        checks.forEach(function(pair) {
          var v = cell(r, pair[0]);
          if (v && !isHealthyState(pair[0], v)) {
            problems.push(pair[1] + ":" + v);
          }
        });

        var slotSpan = document.createElement("span");
        slotSpan.className = "slot-num";
        slotSpan.textContent = slotLabel;
        var serialSpan = document.createElement("span");
        serialSpan.className = "serial";
        serialSpan.textContent = serial;
        var statusSpan = document.createElement("span");
        statusSpan.className = "status";
        if (problems.length > 0) {
          statusSpan.textContent = problems.join(" ");
          li.classList.add("abnormal");
        } else {
          statusSpan.textContent = "OK";
        }
        li.appendChild(slotSpan);
        li.appendChild(serialSpan);
        li.appendChild(statusSpan);
        ul.appendChild(li);
      });
      serverDiv.appendChild(ul);

        container.appendChild(serverDiv);
      });
    });

    return container;
  }

  // ---------------- View toggling ----------------
  var currentReportView = "human";

  function setReportView(view) {
    currentReportView = view;
    var human = document.getElementById("reportsHumanView");
    var tableWrap = document.getElementById("reportsTableWrapper");
    var toggleBtn = document.getElementById("reportsToggleViewBtn");
    var dlBtn = document.getElementById("reportsDownloadBtn");
    if (view === "human") {
      human.style.display = "";
      tableWrap.style.display = "none";
      toggleBtn.textContent = "View Source";
      dlBtn.textContent = "Download PDF";
    } else {
      human.style.display = "none";
      tableWrap.style.display = "";
      toggleBtn.textContent = "View Summary";
      dlBtn.textContent = "Download CSV";
    }
  }

  function toggleReportView() {
    setReportView(currentReportView === "human" ? "source" : "human");
  }

  function downloadReportCurrent() {
    if (currentReportView === "human") downloadReportPdf();
    else downloadReportCsv();
  }

  function downloadReportPdf() {
    var human = document.getElementById("reportsHumanView");
    if (!human || !human.innerHTML.trim()) return;
    var sel = document.getElementById("reportSelect");
    var name = "Report";
    if (sel && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
      name = sel.options[sel.selectedIndex].textContent || "Report";
    }
    var w = window.open("", "_blank");
    if (!w) {
      alert("Pop-up blocked. Allow pop-ups for this site to download the PDF.");
      return;
    }
    // Build print-friendly HTML in-memory using DOM, then serialize.
    // This avoids any escape-sequence pitfalls in source code.
    var doc = w.document;
    doc.open();
    var head = "<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + name + "</title>";
    var css = "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a;background:#fff;padding:24px;}"
      + "h1{font-size:24px;margin:0 0 18px 0;}"
      + ".human-rack-header{margin:18px 0 10px 0;padding:6px 12px;font-size:18px;font-weight:900;color:#0f172a;background:#dbeafe;border-left:5px solid #1d4ed8;border-radius:3px;text-transform:uppercase;letter-spacing:1px;page-break-after:avoid;}"
      + ".human-rack-header:first-child{margin-top:0;}"
      + ".human-server{margin-bottom:14px;margin-left:14px;padding:12px 14px;background:#f8fafc;border-left:3px solid #1e40af;border-radius:4px;page-break-inside:avoid;}"
      + ".human-server-header{display:flex;align-items:stretch;flex-wrap:wrap;line-height:1.2;}"
      + ".human-server-header .hdr-cell{font-family:inherit;font-size:20px;font-weight:800;color:#0f172a;letter-spacing:0.3px;padding:0 14px;line-height:1.2;}"
      + ".human-server-header .hdr-cell:first-child{padding-left:0;}"
      + ".human-server-header .hdr-cell:not(:last-child){border-right:2px solid #cbd5e1;}"
      + ".human-server-summary{margin-top:10px;padding:8px 0;font-size:15px;font-weight:700;color:#1e293b;border-bottom:1px solid #e2e8f0;}"
      + ".human-server-summary.attention{color:#b91c1c;}"
      + ".human-server-summary.healthy{color:#166534;}"
      + ".human-gpu-list{list-style:none;margin:10px 0 0 0;padding:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px 8px;}"
      + ".human-gpu-list li{display:flex;align-items:baseline;gap:6px;padding:5px 8px;font-size:12px;background:#eef2f7;border-left:3px solid #cbd5e1;border-radius:2px;color:#1e293b;overflow:hidden;}"
      + ".human-gpu-list li .slot-num{font-weight:900;font-size:14px;color:#0f172a;min-width:1.5em;}"
      + ".human-gpu-list li .serial{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;font-weight:700;color:#0f172a;letter-spacing:0.4px;flex:1;min-width:0;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
      + ".human-gpu-list li .status{font-weight:800;font-size:11px;color:#166534;}"
      + ".human-gpu-list li.abnormal{background:#fee2e2;border-left-color:#dc2626;color:#7f1d1d;}"
      + ".human-gpu-list li.abnormal .slot-num{color:#7f1d1d;}"
      + ".human-gpu-list li.abnormal .serial{color:#991b1b;}"
      + ".human-gpu-list li.abnormal .status{color:#b91c1c;}"
      + "@media print{body{padding:14px;} .human-server{box-shadow:none;} .human-gpu-list{grid-template-columns:repeat(4,minmax(0,1fr));}}";
    head += "<style>" + css + "</style></head><body>";
    head += "<h1>" + name + "</h1>";
    doc.write(head);
    doc.write(human.innerHTML);
    doc.write("</body></html>");
    doc.close();
    setTimeout(function() {
      try { w.focus(); w.print(); } catch (e) {}
    }, 300);
  }

  function backToReportSelect() {
    document.getElementById("reportsContent").style.width = "";
    document.getElementById("reportsContent").style.maxWidth = "";
    document.getElementById("reportsResults").style.display = "none";
    document.getElementById("reportsActions").style.display = "none";
    document.getElementById("graphReportForm").style.display = "none";
    document.getElementById("reportsSelector").style.display = "block";
    document.getElementById("reportsHumanView").innerHTML = "";
    document.getElementById("reportsTableWrapper").style.display = "none";
    document.getElementById("reportsHumanView").style.display = "none";
  }

  // ---------------- Graph Report (Prometheus + Grafana) ----------------
  function openGraphReportForm() {
    document.getElementById("reportsSelector").style.display = "none";
    document.getElementById("reportsResults").style.display = "none";
    document.getElementById("reportsActions").style.display = "none";
    document.getElementById("graphReportForm").style.display = "block";
    document.getElementById("grError").textContent = "";
    // Default custom range to "last 24h" formatted for datetime-local inputs
    const now = new Date();
    const yest = new Date(now.getTime() - 24*3600*1000);
    const fmt = (d) => {
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:00`;
    };
    document.getElementById("grStartDt").value = fmt(yest);
    document.getElementById("grEndDt").value = fmt(now);
    // Wire up the radio change handler
    document.querySelectorAll('input[name="grRange"]').forEach(el => {
      el.onchange = () => {
        const isCustom = document.querySelector('input[name="grRange"]:checked').value === "custom";
        document.getElementById("grCustomRange").style.display = isCustom ? "block" : "none";
      };
    });
    // Reset scope checkboxes to "all checked" each time the form opens
    document.getElementById("grAllRacks").checked = true;
    document.querySelectorAll(".grCluster").forEach(el => { el.checked = true; });
    // Wire scope toggle handlers (All ↔ clusters in sync)
    document.getElementById("grAllRacks").onchange = function() {
      const checked = this.checked;
      document.querySelectorAll(".grCluster").forEach(el => { el.checked = checked; });
    };
    document.querySelectorAll(".grCluster").forEach(el => {
      el.onchange = function() {
        const all = Array.from(document.querySelectorAll(".grCluster"));
        document.getElementById("grAllRacks").checked = all.every(c => c.checked);
      };
    });
  }

  async function generateGraphReport() {
    const errEl = document.getElementById("grError");
    errEl.textContent = "";
    const range = document.querySelector('input[name="grRange"]:checked').value;
    let startSec, endSec;
    const nowSec = Math.floor(Date.now() / 1000);
    if (range === "24h") { endSec = nowSec; startSec = nowSec - 24*3600; }
    else if (range === "7d") { endSec = nowSec; startSec = nowSec - 7*86400; }
    else if (range === "30d") { endSec = nowSec; startSec = nowSec - 30*86400; }
    else {
      const s = document.getElementById("grStartDt").value;
      const e = document.getElementById("grEndDt").value;
      if (!s || !e) { errEl.textContent = "Pick start and end date/time"; return; }
      startSec = Math.floor(new Date(s).getTime() / 1000);
      endSec = Math.floor(new Date(e).getTime() / 1000);
      if (endSec <= startSec) { errEl.textContent = "End must be after start"; return; }
    }
    const allCluster = Array.from(document.querySelectorAll(".grCluster"));
    const picked = allCluster.filter(el => el.checked).map(el => el.value);
    if (picked.length === 0) { errEl.textContent = "Pick at least one rack"; return; }
    // If everything is selected, omit the param (backend defaults to all clusters)
    const clusters = (picked.length === allCluster.length) ? "" : picked.join(",");
    // Quick precheck: confirm custom reporting is configured before opening a tab
    try {
      const cr = await fetch("/api/settings/custom_reporting");
      const crd = await cr.json();
      if (!crd.ok || !crd.prom_url || !crd.grafana_url) {
        errEl.textContent = "Configure Custom Reporting in Settings first";
        return;
      }
    } catch(e) { errEl.textContent = "Could not check reporting settings"; return; }
    const params = new URLSearchParams({start: startSec, end: endSec});
    if (clusters) params.set("clusters", clusters);
    window.open("/api/reports/graph?" + params.toString(), "_blank");
  }

  // CSV download for the currently rendered report. Reads the live
  // table from the DOM (no extra state) and serves it as a Blob.
  // IMPORTANT: this code is embedded in a Python triple-quoted string,
  // so we MUST avoid backslash escape sequences like backslash-n or
  // backslash-r in JS string literals — Python would convert them to
  // raw control characters and break the JS parse. Use char codes.
  function downloadReportCsv() {
    var table = document.getElementById("reportsTable");
    if (!table) return;
    var headerCells = table.querySelectorAll("thead th");
    if (!headerCells || headerCells.length === 0) return;
    var LF = String.fromCharCode(10);
    var CR = String.fromCharCode(13);
    var EOL = CR + LF;
    function csvCell(v) {
      if (v === null || v === undefined) return "";
      var s = String(v);
      if (s.indexOf(",") >= 0 || s.indexOf('"') >= 0 || s.indexOf(LF) >= 0 || s.indexOf(CR) >= 0) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }
    var lines = [];
    var headers = [];
    headerCells.forEach(function(th) { headers.push(csvCell(th.textContent || "")); });
    lines.push(headers.join(","));
    table.querySelectorAll("tbody tr").forEach(function(tr) {
      var cells = [];
      tr.querySelectorAll("td").forEach(function(td) { cells.push(csvCell(td.textContent || "")); });
      lines.push(cells.join(","));
    });
    var blob = new Blob([lines.join(EOL)], {type: "text/csv;charset=utf-8;"});
    var url = URL.createObjectURL(blob);
    var sel = document.getElementById("reportSelect");
    var name = "report";
    if (sel && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
      name = sel.options[sel.selectedIndex].textContent || "report";
    }
    var safeName = name.replace(/[^A-Za-z0-9._-]+/g, "_");
    var d = new Date();
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    var stamp = d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "_" + pad(d.getHours()) + pad(d.getMinutes());
    var a = document.createElement("a");
    a.href = url;
    a.download = safeName + "_" + stamp + ".csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
  }

</script>
</body>
</html>
"""
    html = html.replace("__TITLE__", initial_title)
    return HTMLResponse(html)
