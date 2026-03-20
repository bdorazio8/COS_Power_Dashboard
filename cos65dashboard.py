import asyncio
import json
import logging
import sqlite3
import subprocess
from logging.handlers import RotatingFileHandler
from typing import Dict, Set, List, Optional

import httpx

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ----------------------------
# Constants
# ----------------------------

DB = "upsdash.db"
SNMP_COMMUNITY = "COS65"
OID_LOAD_PCT = "1.3.6.1.2.1.33.1.4.4.1.5.1"                 # UPS-MIB upsOutputLoad.1
OID_STATE = "1.3.6.1.4.1.318.1.1.1.2.2.2.0"                 # APC PowerNet UPS state (5=bypass)
OID_UPS_STATUS = "1.3.6.1.4.1.318.1.1.1.2.1.1.0"            # APC UPS basic output status
OID_TEMP = "1.3.6.1.2.1.33.1.2.7.0"                         # UPS-MIB upsBatteryTemperature (°C)
OID_RUNTIME = "1.3.6.1.2.1.33.1.2.3.0"                      # UPS-MIB upsEstimatedMinutesRemaining
OID_OUTPUT_AMPS = "1.3.6.1.4.1.318.1.1.1.4.3.4.0"          # APC high-prec output current (tenths A)
OID_OUTPUT_WATTS = "1.3.6.1.4.1.318.1.1.1.4.2.8.0"         # APC output watts
DEFAULT_DASH_TITLE = "COS65 Power Dashboard"

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

# APC UPS basic output status codes -> display labels
UPS_STATUS_MAP = {
    1: "UNKNOWN",
    2: "ONLINE",
    3: "ON BATTERY",
    4: "ON BYPASS",      # smart boost / smart trim may also report here
    5: "REBOOTING",
    6: "OFFLINE",
    7: "OVERLOAD",
}

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

# Dedicated UPS event logger -> upsdash_events.log (independent of standard logs)
event_logger = logging.getLogger("upsdash.events")
event_logger.setLevel(logging.INFO)
event_logger.propagate = False

_event_handler = RotatingFileHandler("upsdash_events.log", maxBytes=1_000_000, backupCount=5)
_event_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
event_logger.addHandler(_event_handler)

# ----------------------------
# Slack Alerts
# ----------------------------

_slack_client: Optional[httpx.AsyncClient] = None

def _get_slack_client() -> httpx.AsyncClient:
    global _slack_client
    if _slack_client is None:
        _slack_client = httpx.AsyncClient(timeout=10)
    return _slack_client

async def send_slack_alert(label: str, ups_status: str, alert_type: str,
                           load_pct=None, runtime_min=None,
                           output_watts=None, output_amps=None, temp_c=None):
    """Send a context-specific power alert to the configured Slack webhook."""
    webhook_url = get_setting("slack_webhook_url", "")
    if not webhook_url:
        return

    emoji_map = {
        "ON BATTERY": ":zap:",
        "ON BYPASS": ":warning:",
        "OFFLINE": ":red_circle:",
        "ONLINE": ":large_green_circle:",
    }
    emoji = emoji_map.get(ups_status, ":rotating_light:")
    is_recovery = alert_type.endswith("_recovery")
    header = "*COS65 POWER RECOVERY*" if is_recovery else "*COS65 POWER ALERT*"

    lines = [f"{emoji} {header}", f"UPS: {label}", f"Status: {ups_status}"]

    base_type = alert_type.removesuffix("_recovery")

    if base_type == "offline":
        if is_recovery:
            # Back online — show current load to confirm healthy
            if load_pct is not None:
                lines.append(f"Load: {load_pct}%")
        # Offline alert: no SNMP data available, nothing else to show

    elif base_type == "bypass":
        # Bypass: UPS is passing utility power directly — runtime is irrelevant
        if load_pct is not None:
            lines.append(f"Load: {load_pct}%")
        if output_watts is not None:
            lines.append(f"Output: {output_watts}W")
        elif output_amps is not None:
            lines.append(f"Output: {output_amps}A")

    elif base_type == "on_battery":
        # On battery: runtime and load are the critical metrics
        if load_pct is not None:
            lines.append(f"Load: {load_pct}%")
        if runtime_min is not None:
            lines.append(f"Runtime Remaining: {runtime_min} min")

    else:
        # Fallback for any future alert types — show all available metrics
        if load_pct is not None:
            lines.append(f"Load: {load_pct}%")
        if runtime_min is not None:
            lines.append(f"Runtime Remaining: {runtime_min} min")

    text = "\n".join(lines)
    try:
        client = _get_slack_client()
        resp = await client.post(webhook_url, json={"text": text})
        if resp.status_code != 200:
            logger.warning("Slack webhook returned %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Failed to send Slack alert: %s", e)

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
            ups_ip TEXT NOT NULL,
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
        SELECT id,label,ups_ip,COALESCE(sort_order,id),COALESCE(pdu_ip,''),COALESCE(has_additional_pdu,0)
        FROM racks
        ORDER BY COALESCE(sort_order,id), id
    """)
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "label": r[1], "ups_ip": r[2], "sort_order": r[3], "pdu_ip": r[4], "has_additional_pdu": bool(r[5])} for r in rows]

def _next_sort_order(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM racks")
    row = cur.fetchone()
    return int(row[0] or 0) + 1

def add_rack(label: str, ups_ip: str, pdu_ip: str = "", has_additional_pdu: bool = False):
    conn = _connect()
    cur = conn.cursor()
    next_order = _next_sort_order(cur)
    cur.execute(
        "INSERT INTO racks(label, ups_ip, community, sort_order, pdu_ip, has_additional_pdu) VALUES(?,?,?,?,?,?)",
        (label, ups_ip, SNMP_COMMUNITY, next_order, pdu_ip, int(has_additional_pdu)),
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
previous_status: Dict[int, Dict] = {}
latest_systems_status: Dict[int, List[Dict]] = {}
latest_pdu_circuits: Dict[int, List[Dict]] = {}

# Slack alert debounce: tracks when a UPS first entered an alert state.
# Key = (rid, alert_type), value = monotonic timestamp of first detection.
# Alert is only sent after the state persists for SLACK_ALERT_DELAY seconds.
import time as _time
SLACK_ALERT_DELAY = 15  # seconds a UPS must remain in alert state before notifying
_alert_pending: Dict[tuple, float] = {}   # (rid, alert_type) -> first_seen timestamp
_alert_sent: Dict[tuple, bool] = {}       # (rid, alert_type) -> True if already notified
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
            "ups_ip": r["ups_ip"],
            "reachable": False,
            "load_pct": None,
            "bypass": False,
            "ups_status": "UNKNOWN",
            "temp_c": None,
            "runtime_min": None,
            "output_amps": None,
            "output_watts": None,
            "sort_order": r.get("sort_order"),
        }
        status["label"] = r["label"]
        status["ups_ip"] = r["ups_ip"]
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

def _maybe_slack_alert(rid: int, alert_type: str, label: str, status_text: str,
                       load_pct=None, runtime_min=None,
                       output_watts=None, output_amps=None, temp_c=None):
    """Debounce Slack alerts: only fire after the condition persists for SLACK_ALERT_DELAY seconds."""
    key = (rid, alert_type)
    now = _time.monotonic()
    if key not in _alert_pending:
        _alert_pending[key] = now
        logger.debug("Alert pending: %s %s — waiting %ds", label, alert_type, SLACK_ALERT_DELAY)
        return
    elapsed = now - _alert_pending[key]
    if elapsed >= SLACK_ALERT_DELAY and not _alert_sent.get(key):
        _alert_sent[key] = True
        logger.info("Alert confirmed after %ds: %s %s — sending Slack notification", int(elapsed), label, alert_type)
        asyncio.create_task(send_slack_alert(
            label, status_text, alert_type,
            load_pct=load_pct, runtime_min=runtime_min,
            output_watts=output_watts, output_amps=output_amps, temp_c=temp_c,
        ))

# Tracks which (rid, alert_type) need a recovery message
_recovery_pending: Dict[tuple, bool] = {}  # (rid, alert_type) -> True if recovery needed

def _clear_alert(rid: int, alert_type: str):
    """Clear a pending/sent alert when the condition resolves. Mark recovery if alert was sent."""
    key = (rid, alert_type)
    was_sent = _alert_sent.get(key, False)
    _alert_pending.pop(key, None)
    _alert_sent.pop(key, None)
    if was_sent:
        _recovery_pending[key] = True

def _check_recovery(rid: int, alert_type: str, label: str,
                     load_pct=None, runtime_min=None,
                     output_watts=None, output_amps=None, temp_c=None):
    """Debounce recovery: only send ONLINE after condition is clear for SLACK_ALERT_DELAY."""
    key = (rid, alert_type)
    if not _recovery_pending.get(key):
        return
    recovery_key = (rid, alert_type + "_recovery")
    _maybe_slack_alert(rid, alert_type + "_recovery", label, "ONLINE",
                       load_pct=load_pct, runtime_min=runtime_min,
                       output_watts=output_watts, output_amps=output_amps, temp_c=temp_c)
    if _alert_sent.get(recovery_key):
        # Recovery sent, clean up
        _recovery_pending.pop(key, None)
        _alert_pending.pop(recovery_key, None)
        _alert_sent.pop(recovery_key, None)

def _cancel_recovery(rid: int, alert_type: str):
    """Cancel recovery if UPS goes back into alert state before recovery fires."""
    key = (rid, alert_type)
    _recovery_pending.pop(key, None)
    recovery_key = (rid, alert_type + "_recovery")
    _alert_pending.pop(recovery_key, None)
    _alert_sent.pop(recovery_key, None)

def _check_events(rid: int, label: str, ip: str, reachable: bool, load_pct, bypass: bool,
                   ups_status: str = "UNKNOWN", runtime_min=None,
                   output_watts=None, output_amps=None, temp_c=None):
    """Compare current poll result to previous state and log significant events."""
    prev = previous_status.get(rid)
    metrics = dict(load_pct=load_pct, runtime_min=runtime_min,
                   output_watts=output_watts, output_amps=output_amps, temp_c=temp_c)

    if prev is None:
        event_logger.info(
            "NEW — %s (%s) first poll, load=%s%%, reachable=%s",
            label, ip, load_pct if load_pct is not None else "N/A", reachable,
        )
        return

    # Reachability changes
    if prev["reachable"] and not reachable:
        event_logger.info("OFFLINE — %s (%s) is unreachable", label, ip)
    elif not prev["reachable"] and reachable:
        event_logger.info("ONLINE — %s (%s) is now reachable", label, ip)

    # Debounced Slack: offline
    if not reachable:
        _maybe_slack_alert(rid, "offline", label, "OFFLINE", **metrics)
        _cancel_recovery(rid, "offline")
    else:
        _clear_alert(rid, "offline")
        _check_recovery(rid, "offline", label, **metrics)

    # Bypass changes
    if not prev["bypass"] and bypass:
        event_logger.info("BYPASS ON — %s (%s) entered bypass mode", label, ip)
    elif prev["bypass"] and not bypass:
        event_logger.info("BYPASS OFF — %s (%s) exited bypass mode", label, ip)

    # Debounced Slack: bypass
    if bypass:
        _maybe_slack_alert(rid, "bypass", label, "ON BYPASS", **metrics)
        _cancel_recovery(rid, "bypass")
    else:
        _clear_alert(rid, "bypass")
        _check_recovery(rid, "bypass", label, **metrics)

    # ON BATTERY transition logging
    prev_status = prev.get("ups_status", "UNKNOWN")
    if ups_status == "ON BATTERY" and prev_status != "ON BATTERY":
        event_logger.info("ON BATTERY — %s (%s) running on battery", label, ip)

    # Debounced Slack: on battery
    if ups_status == "ON BATTERY":
        _maybe_slack_alert(rid, "on_battery", label, "ON BATTERY", **metrics)
        _cancel_recovery(rid, "on_battery")
    else:
        _clear_alert(rid, "on_battery")
        _check_recovery(rid, "on_battery", label, **metrics)

    # Load threshold crossings
    prev_load = prev.get("load_pct")
    if load_pct is not None:
        prev_above_95 = prev_load is not None and prev_load >= 95
        prev_above_85 = prev_load is not None and prev_load >= 90

        if load_pct >= 95 and not prev_above_95:
            event_logger.info("CRITICAL LOAD — %s (%s) at %d%%", label, ip, load_pct)
        elif load_pct >= 90 and not prev_above_85:
            event_logger.info("HIGH LOAD — %s (%s) at %d%%", label, ip, load_pct)
        elif prev_above_85 and load_pct < 90:
            event_logger.info("LOAD NORMAL — %s (%s) at %d%%", label, ip, load_pct)

async def poll_loop():
    while True:
        try:
            racks = get_racks()
            current_ids = {r["id"] for r in racks}

            for rid in list(latest_status.keys()):
                if rid not in current_ids:
                    latest_status.pop(rid, None)
                    previous_status.pop(rid, None)

            for rack in racks:
                rid = rack["id"]
                ip = rack["ups_ip"]

                reachable = ping_ok(ip)
                load_pct = None
                bypass = False
                temp_val = None
                runtime_val = None
                out_amps_val = None
                out_watts_val = None

                ups_status = "UNKNOWN"

                if reachable:
                    load_raw = snmp_get(ip, OID_LOAD_PCT)
                    load_val = _parse_int(load_raw or "")
                    if load_val is not None:
                        load_pct = load_val

                    state_raw = snmp_get(ip, OID_STATE)
                    state_val = _parse_int(state_raw or "")
                    if state_val is not None and state_val == 5:
                        bypass = True

                    status_raw = snmp_get(ip, OID_UPS_STATUS)
                    status_val = _parse_int(status_raw or "")
                    if status_val is not None:
                        ups_status = UPS_STATUS_MAP.get(status_val, "UNKNOWN")

                    temp_raw = snmp_get(ip, OID_TEMP)
                    temp_val = _parse_int(temp_raw or "")

                    runtime_raw = snmp_get(ip, OID_RUNTIME)
                    runtime_val = _parse_int(runtime_raw or "")

                    out_amps_raw = snmp_get(ip, OID_OUTPUT_AMPS)
                    out_amps_int = _parse_int(out_amps_raw or "")
                    out_amps_val = round(out_amps_int / 10, 1) if out_amps_int is not None else None

                    out_watts_raw = snmp_get(ip, OID_OUTPUT_WATTS)
                    out_watts_val = _parse_int(out_watts_raw or "")

                    # If host is pingable but all SNMP queries failed, treat as offline
                    if ups_status == "UNKNOWN" and load_pct is None:
                        reachable = False
                        ups_status = "OFFLINE"
                else:
                    ups_status = "OFFLINE"

                _check_events(rid, rack["label"], ip, reachable, load_pct, bypass,
                              ups_status=ups_status, runtime_min=runtime_val,
                              output_watts=out_watts_val, output_amps=out_amps_val,
                              temp_c=temp_val)

                latest_status[rid] = {
                    "id": rid,
                    "label": rack["label"],
                    "ups_ip": ip,
                    "reachable": reachable,
                    "load_pct": load_pct,
                    "bypass": bypass,
                    "ups_status": ups_status,
                    "temp_c": temp_val,
                    "runtime_min": runtime_val,
                    "output_amps": out_amps_val,
                    "output_watts": out_watts_val,
                    "sort_order": rack.get("sort_order"),
                }

                previous_status[rid] = {
                    "reachable": reachable,
                    "load_pct": load_pct,
                    "bypass": bypass,
                    "ups_status": ups_status,
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
    logger.info("UPSDash starting, database initialized")
    asyncio.create_task(poll_loop())

# ----------------------------
# REST API
# ----------------------------

class Rack(BaseModel):
    label: str
    ups_ip: str
    pdu_ip: str = ""
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
    ip = (r.ups_ip or "").strip()
    pdu_ip = (r.pdu_ip or "").strip()
    if not label or not ip:
        return {"ok": False, "error": "Missing label or IP"}
    add_rack(label, ip, pdu_ip, r.has_additional_pdu)
    logger.info("Rack added: %s (UPS %s, PDU %s, additional_pdu=%s)", label, ip, pdu_ip or "none", r.has_additional_pdu)
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

@app.post("/api/check")
def api_check(data: dict):
    ip = (data.get("ups_ip") or "").strip()
    if not ip:
        return {"ok": False}
    if not ping_ok(ip):
        return {"ok": False}
    test = snmp_get(ip, OID_LOAD_PCT)
    ok = False
    if test:
        ok = ("INTEGER:" in test) or ("Gauge32:" in test)
    return {"ok": ok}

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

@app.get("/api/settings/slack_webhook")
def api_get_slack_webhook():
    url = get_setting("slack_webhook_url", "")
    return {"ok": True, "url": url}

@app.post("/api/settings/slack_webhook")
def api_set_slack_webhook(p: dict):
    url = (p.get("url") or "").strip()
    set_setting("slack_webhook_url", url)
    logger.info("Slack webhook URL %s", "configured" if url else "cleared")
    return {"ok": True, "url": url}

@app.post("/api/settings/slack_test")
async def api_test_slack(p: dict):
    url = (p.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "No webhook URL provided"}
    try:
        client = _get_slack_client()
        text = ":rotating_light: *COS65 POWER ALERT*\nThis is a test alert from UPSDash."
        resp = await client.post(url, json={"text": text})
        if resp.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"Slack returned {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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

  .bypass-tag {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #ff4d4d;
    font-weight: 900;
    animation: blink 1s infinite;
    letter-spacing: 2px;
    font-size: clamp(14px, 5cqi, 28px);
    text-shadow: 0 0 10px rgba(255,77,77,0.6);
    background: none;
    z-index: 1;
    pointer-events: none;
  }
  @keyframes blink { 50% { opacity:0; } }

  /* PDU circuit area (upper portion of rack) */
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

  /* UPS info section (bottom of rack) */
  .ups-info {
    border-top: 1px solid rgba(255,255,255,0.10);
    padding: 8px 10px 6px;
    background: rgba(0,0,0,0.18);
    flex-shrink: 0;
  }

  .ups-info-layout {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto auto;
    gap: 2px 8px;
  }

  .ups-top-left {
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-width: 0;
    padding: 4px 2px;
  }

  .ups-top-right {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .ups-output-row {
    grid-column: 1 / -1;
    display: flex;
    justify-content: space-around;
    align-items: center;
    padding: 2px 6px;
  }
  .ups-output-metric {
    display: flex;
    align-items: baseline;
    gap: 5px;
  }
  .ups-output-val {
    font-weight: 900;
    font-size: clamp(16px, 5cqi, 48px);
    line-height: 1.1;
  }
  .ups-output-val.amps { color: #38bdf8; }
  .ups-output-val.watts { color: #a78bfa; }
  .ups-output-unit {
    font-size: clamp(10px, 2.5cqi, 36px);
    font-weight: 700;
    color: rgba(148,163,184,0.5);
    text-transform: uppercase;
  }
  .ups-bottom-full {
    grid-column: 1 / -1;
  }

  .ups-metric {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 13px;
    line-height: 1.4;
    padding: 2px 6px;
    gap: 4px;
  }
  .ups-metric-label {
    opacity: 0.6;
    font-weight: 600;
    white-space: nowrap;
    font-size: clamp(13px, 3.5cqi, 36px);
  }
  .ups-metric-value {
    font-weight: 900;
    white-space: nowrap;
    font-size: clamp(14px, 4cqi, 40px);
  }

  /* Horizontal segmented load bar */
  .load-bar-row {
    display: flex;
    align-items: center;
    gap: 5px;
    margin-top: 4px;
  }
  .load-pct-label {
    font-weight: 900;
    font-size: clamp(11px, 3.5cqi, 36px);
    white-space: nowrap;
    min-width: 38px;
  }
  .load-bar-track {
    flex: 1;
    display: flex;
    height: 28px;
  }
  .load-seg {
    flex: 1;
    border-right: 2px solid #1a1f2e;
    background: rgba(255,255,255,0.08);
    transition: background 300ms;
  }
  .load-seg:last-child {
    border-right: none;
  }
  .load-seg.on-green { background: #22c55e; box-shadow: 0 0 4px rgba(34,197,94,0.4); }
  .load-seg.on-amber { background: #f59e0b; box-shadow: 0 0 4px rgba(245,158,11,0.4); }
  .load-seg.on-red   { background: #ef4444; box-shadow: 0 0 4px rgba(239,68,68,0.4); }

  /* Large battery icon */
  .battery-lg {
    width: 100%;
    aspect-ratio: 2 / 1;
    max-height: 42px;
    border: 2px solid rgba(255,255,255,0.5);
    border-radius: 4px;
    position: relative;
    display: flex;
    align-items: center;
    padding: 3px;
  }
  .battery-lg::after {
    content: "";
    position: absolute;
    right: -6px;
    top: 50%;
    transform: translateY(-50%);
    width: 4px;
    height: 40%;
    background: rgba(255,255,255,0.5);
    border-radius: 0 2px 2px 0;
  }
  .battery-lg-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 300ms, background 300ms;
  }
  .battery-lg-text {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 900;
    font-size: 11px;
    color: white;
    text-shadow: 0 1px 3px rgba(0,0,0,0.6);
    z-index: 1;
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

  .status-banner {
    margin-top: -1px;
    padding: 6px 10px;
    border-radius: 0 0 18px 18px;
    text-align: center;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: white;
    font-size: clamp(15px, 5cqi, 48px);
    border: 1px solid rgba(255,255,255,0.14);
    border-top: none;
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
      <input id="ip" placeholder="UPS IP" />

      <div class="status-wrap">
        <div id="light" class="status-light"></div>
        <div id="statusText" class="status-text">Waiting for UPS…</div>
      </div>

      <input id="pduIp" placeholder="PDU IP (optional)" />
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
      <div style="margin-top:12px">
        <div style="margin-bottom:4px"><span style="color:#cbd5e1;font-weight:600">Slack Webhook URL:</span></div>
        <input id="slackWebhookInput" style="width:100%;box-sizing:border-box" />
        <button onclick="testSlack()" style="margin-top:4px;font-size:0.85em;padding:4px 10px;background:#444;color:#ccc;border:1px solid #555;border-radius:4px;cursor:pointer">Test Slack</button>
        <span id="slackTestResult" style="margin-left:8px;font-size:0.85em"></span>
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

    const STATUS_COLORS = {
      "ONLINE":     "#166534",
      "ON BATTERY": "#d97706",
      "ON BYPASS":  "#9333ea",
      "REBOOTING":  "#2563eb",
      "OFFLINE":    "#dc2626",
      "OVERLOAD":   "#dc2626",
      "UNKNOWN":    "#475569",
    };

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

      // --- "Additional PDUs" delta block when rack is flagged ---
      if (r.has_additional_pdu && circuits.length > 0 && r.output_watts !== null && r.output_watts !== undefined) {
        const pduTotalW = circuits.reduce((sum, c) => sum + (c.reachable ? c.power_w : 0), 0);
        const pduTotalA = circuits.reduce((sum, c) => sum + (c.reachable ? c.current_a : 0), 0);
        const deltaW = Math.max(0, r.output_watts - pduTotalW);
        const deltaA = (r.output_amps !== null && r.output_amps !== undefined) ? Math.max(0, r.output_amps - pduTotalA) : null;
        {
          let block = document.createElement("div");
          block.className = "crt-block additional-pdu";

          let lbl = document.createElement("div");
          lbl.className = "crt-label";
          lbl.textContent = "ADDITIONAL PDUs";
          block.appendChild(lbl);

          let body = document.createElement("div");
          body.className = "crt-body";

          let spacer = document.createElement("div");
          spacer.className = "crt-outlets";
          spacer.innerHTML = '<div class="crt-outlet empty" style="font-style:italic;opacity:0.5">Unmonitored</div>';
          body.appendChild(spacer);

          let ampsCol = document.createElement("div");
          ampsCol.className = "crt-metric-col";
          let ampsUnit = document.createElement("div");
          ampsUnit.className = "crt-metric-unit";
          ampsUnit.textContent = "AMPS";
          let ampsVal = document.createElement("div");
          ampsVal.className = "crt-metric-val amps";
          ampsVal.textContent = (deltaA !== null && deltaA >= 0) ? deltaA.toFixed(2) : "--";
          ampsCol.appendChild(ampsUnit);
          ampsCol.appendChild(ampsVal);
          body.appendChild(ampsCol);

          let wattsCol = document.createElement("div");
          wattsCol.className = "crt-metric-col";
          let wattsUnit = document.createElement("div");
          wattsUnit.className = "crt-metric-unit";
          wattsUnit.textContent = "WATTS";
          let wattsVal = document.createElement("div");
          wattsVal.className = "crt-metric-val watts";
          wattsVal.textContent = Math.round(deltaW);
          wattsCol.appendChild(wattsUnit);
          wattsCol.appendChild(wattsVal);
          body.appendChild(wattsCol);

          block.appendChild(body);
          serverArea.appendChild(block);
        }
      }

      div.appendChild(serverArea);

      // --- UPS IP banner (above metrics) ---
      let upsBanner = document.createElement("div");
      upsBanner.className = "ip-banner";
      upsBanner.textContent = r.ups_ip ? "UPS: " + r.ups_ip : "";
      div.appendChild(upsBanner);

      // --- UPS info section (bottom of rack) ---
      let upsInfo = document.createElement("div");
      upsInfo.className = "ups-info";

      let loadVal = (r.load_pct !== null && r.load_pct !== undefined) ? r.load_pct + "%" : "--";
      let tempVal = "--";
      let tempColor = "inherit";
      if (r.temp_c !== null && r.temp_c !== undefined) {
        tempVal = r.temp_c + "°C";
        if (r.temp_c <= 25) tempColor = "#38bdf8";
        else if (r.temp_c <= 35) tempColor = "#facc15";
        else tempColor = "#ef4444";
      }
      let runtimeVal = (r.runtime_min !== null && r.runtime_min !== undefined) ? r.runtime_min + " min" : "--";

      // Segmented load bar
      const totalSegs = 40;
      let litSegs = 0;
      let segColor = "green";
      if (r.load_pct !== null && r.load_pct !== undefined) {
        litSegs = Math.round((r.load_pct / 100) * totalSegs);
        if (r.bypass || r.load_pct >= 90) segColor = "red";
        else if (r.load_pct >= 70) segColor = "amber";
        else segColor = "green";
      }

      let segsHTML = '';
      for (let s = 0; s < totalSegs; s++) {
        segsHTML += '<div class="load-seg' + (s < litSegs ? ' on-' + segColor : '') + '"></div>';
      }

      // Battery (large, right side)
      let battPct = 100; // placeholder until we poll battery capacity
      let battColor = "#22c55e";
      if (battPct < 30) battColor = "#ef4444";
      else if (battPct < 60) battColor = "#f59e0b";
      let battLabel = battPct + "%";

      let metricsHTML = '<div class="ups-info-layout">';

      // Top-left quadrant: Temp, Runtime
      metricsHTML += '<div class="ups-top-left">';
      metricsHTML += '<div class="ups-metric"><span class="ups-metric-label">Temp</span><span class="ups-metric-value" style="color:' + tempColor + '">' + tempVal + '</span></div>';
      metricsHTML += '<div class="ups-metric"><span class="ups-metric-label">Runtime</span><span class="ups-metric-value">' + runtimeVal + '</span></div>';
      metricsHTML += '</div>';

      // Top-right quadrant: Large battery
      metricsHTML += '<div class="ups-top-right">';
      metricsHTML += '<div class="battery-lg">';
      metricsHTML += '<div class="battery-lg-fill" style="width:' + battPct + '%;background:' + battColor + ';"></div>';
      metricsHTML += '<div class="battery-lg-text">' + battLabel + '</div>';
      metricsHTML += '</div>';
      metricsHTML += '</div>';

      // Output amps/watts row
      let outAmps = (r.output_amps !== null && r.output_amps !== undefined) ? r.output_amps.toFixed(1) : "--";
      let outWatts = (r.output_watts !== null && r.output_watts !== undefined) ? r.output_watts : "--";
      metricsHTML += '<div class="ups-output-row">';
      metricsHTML += '<div class="ups-output-metric"><span class="ups-output-val amps">' + outAmps + '</span><span class="ups-output-unit">A</span></div>';
      metricsHTML += '<div class="ups-output-metric"><span class="ups-output-val watts">' + outWatts + '</span><span class="ups-output-unit">W</span></div>';
      metricsHTML += '</div>';

      // Bottom full-width: Load bar
      metricsHTML += '<div class="ups-bottom-full">';
      metricsHTML += '<div class="load-bar-row">';
      metricsHTML += '<span class="load-pct-label">' + loadVal + '</span>';
      metricsHTML += '<div class="load-bar-track">' + segsHTML + '</div>';
      metricsHTML += '</div>';
      metricsHTML += '</div>';

      metricsHTML += '</div>';

      upsInfo.innerHTML = metricsHTML;
      div.appendChild(upsInfo);

      wrapper.appendChild(div);

      // Status banner
      let banner = document.createElement("div");
      banner.className = "status-banner";
      let st = (r.ups_status || "UNKNOWN").toUpperCase();
      banner.innerText = st;
      banner.style.background = STATUS_COLORS[st] || STATUS_COLORS["UNKNOWN"];
      wrapper.appendChild(banner);

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

      // For "Additional PDUs" block, copy font sizes from a sibling circuit block
      if (block.classList.contains("additional-pdu")) {
        const sibling = block.parentElement.querySelector(".crt-block:not(.additional-pdu) .crt-metric-val");
        const siblingUnit = block.parentElement.querySelector(".crt-block:not(.additional-pdu) .crt-metric-unit");
        if (sibling) {
          body.querySelectorAll(".crt-metric-val").forEach(v => v.style.fontSize = sibling.style.fontSize);
          body.querySelectorAll(".crt-metric-unit").forEach(u => { if (siblingUnit) u.style.fontSize = siblingUnit.style.fontSize; });
        }
        const siblingOutlet = block.parentElement.querySelector(".crt-block:not(.additional-pdu) .crt-outlet");
        const outletsCol = body.querySelector(".crt-outlets");
        if (outletsCol) {
          outletsCol.style.flex = isFill ? "3 1 0" : "5 1 0";
          if (siblingOutlet) outletsCol.querySelectorAll(".crt-outlet").forEach(r => r.style.fontSize = siblingOutlet.style.fontSize);
        }
        const siblingLbl = block.parentElement.querySelector(".crt-block:not(.additional-pdu) .crt-label");
        const lbl = block.querySelector(".crt-label");
        if (lbl && siblingLbl) lbl.style.fontSize = siblingLbl.style.fontSize;
        return;
      }

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
    document.querySelectorAll(".ups-metric").forEach(row => {
      const w = row.clientWidth;
      if (w <= 0) return;
      const lbl = row.querySelector(".ups-metric-label");
      const val = row.querySelector(".ups-metric-value");
      if (lbl) lbl.style.fontSize = Math.max(8, w * 0.12) + "px";
      if (val) val.style.fontSize = Math.max(8, w * 0.13) + "px";
    });
    // Scale UPS output amps/watts values
    document.querySelectorAll(".ups-output-metric").forEach(metric => {
      const parent = metric.closest(".ups-output-row");
      if (!parent) return;
      const pw = parent.clientWidth;
      if (pw <= 0) return;
      const val = metric.querySelector(".ups-output-val");
      const unit = metric.querySelector(".ups-output-unit");
      if (val) {
        const text = val.textContent || "000";
        val.style.fontSize = Math.max(10, Math.min(pw * 0.08, pw / (text.length * 1.3))) + "px";
      }
      if (unit) unit.style.fontSize = Math.max(8, pw * 0.03) + "px";
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
  let checkTimer = null;
  let lastCheckToken = 0;

  function openAdd() {
    document.getElementById("addModal").style.display = "flex";
    resetAddState(true);
  }
  function closeAdd() {
    document.getElementById("addModal").style.display = "none";
    resetAddState(false);
  }

  let pduCheckTimer = null;
  let lastPduCheckToken = 0;
  let upsOk = false;
  let pduOk = false;  // true when verified OR when field is empty

  function resetAddState(clearInputs) {
    if (checkTimer) clearTimeout(checkTimer);
    if (pduCheckTimer) clearTimeout(pduCheckTimer);
    checkTimer = null;
    pduCheckTimer = null;
    lastCheckToken++;
    lastPduCheckToken++;
    upsOk = false;
    pduOk = true;  // empty PDU is OK

    document.getElementById("light").classList.remove("green");
    document.getElementById("statusText").innerText = "Waiting for UPS\u2026";
    document.getElementById("pduLight").classList.remove("green");
    document.getElementById("pduStatusText").innerText = "Waiting for PDU\u2026";

    updateApplyBtn();

    if (clearInputs) {
      document.getElementById("label").value = "";
      document.getElementById("ip").value = "";
      document.getElementById("pduIp").value = "";
      document.getElementById("addPduNo").checked = true;
    }
  }

  function updateApplyBtn() {
    const btn = document.getElementById("applyBtn");
    if (upsOk && pduOk) {
      btn.disabled = false;
      btn.classList.remove("disabled");
    } else {
      btn.disabled = true;
      btn.classList.add("disabled");
    }
  }

  function setAddOk(ok, msg) {
    const light = document.getElementById("light");
    const txt = document.getElementById("statusText");
    upsOk = ok;

    if (ok) {
      light.classList.add("green");
      txt.innerText = msg || "SNMP OK";
    } else {
      light.classList.remove("green");
      txt.innerText = msg || "Not connected";
    }
    updateApplyBtn();
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

  async function checkIp(ip) {
    const token = ++lastCheckToken;

    if (!ip || ip.length < 7) {
      setAddOk(false, "Waiting for UPS…");
      return;
    }

    setAddOk(false, "Checking…");

    try {
      const res = await fetch("/api/check", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ups_ip: ip})
      });
      const data = await res.json();

      if (token !== lastCheckToken) return;

      if (data.ok) setAddOk(true, "SNMP OK");
      else setAddOk(false, "No SNMP response (COS65)");
    } catch(e) {
      if (token !== lastCheckToken) return;
      setAddOk(false, "Check failed");
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const r = await fetch("/api/settings/title");
      const d = await r.json();
      if (d && d.ok && d.title) applyTitle(d.title);
    } catch(e) {}
    viewportStyle = localStorage.getItem("viewportStyle") || "racks";
    computeRackSize(racksCache.length);

    const ipEl = document.getElementById("ip");
    ipEl.addEventListener("input", () => {
      const ip = ipEl.value.trim();
      if (checkTimer) clearTimeout(checkTimer);
      checkTimer = setTimeout(() => checkIp(ip), 900);
    });

    const pduIpEl = document.getElementById("pduIp");
    pduIpEl.addEventListener("input", () => {
      const ip = pduIpEl.value.trim();
      if (pduCheckTimer) clearTimeout(pduCheckTimer);
      if (!ip) {
        setPduOk(true, "Waiting for PDU\u2026");
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
    const ip = document.getElementById("ip").value.trim();
    const pduIp = document.getElementById("pduIp").value.trim();
    const hasAdditionalPdu = document.getElementById("addPduYes").checked;

    if (!label || !ip) {
      setAddOk(false, "Enter label + IP");
      return;
    }

    btn.disabled = true;
    btn.classList.add("disabled");

    try {
      const res = await fetch("/api/racks", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({label: label, ups_ip: ip, pdu_ip: pduIp, has_additional_pdu: hasAdditionalPdu})
      });
      const data = await res.json();
      if (data.ok) closeAdd();
      else setAddOk(false, data.error || "Add failed");
    } catch(e) {
      setAddOk(false, "Add failed");
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
      ipSpan.textContent = r.ups_ip ? ("• " + r.ups_ip) : "";

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
    document.getElementById("slackTestResult").textContent = "";
    // Load viewport style
    document.getElementById(viewportStyle === "fill" ? "vpFill" : "vpRacks").checked = true;
    try {
      const r = await fetch("/api/settings/slack_webhook");
      const d = await r.json();
      document.getElementById("slackWebhookInput").value = d.url || "";
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

  async function testSlack() {
    const url = (document.getElementById("slackWebhookInput").value || "").trim();
    const el = document.getElementById("slackTestResult");
    if (!url) { el.textContent = "Enter a URL first"; el.style.color = "#f87171"; return; }
    el.textContent = "Sending..."; el.style.color = "#aaa";
    try {
      const res = await fetch("/api/settings/slack_test", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({url: url})
      });
      const data = await res.json();
      if (data.ok) { el.textContent = "Sent!"; el.style.color = "#4ade80"; }
      else { el.textContent = data.error || "Failed"; el.style.color = "#f87171"; }
    } catch(e) { el.textContent = "Error"; el.style.color = "#f87171"; }
  }

  async function saveSettings() {
    const titleVal = (document.getElementById("titleInput").value || "").trim();
    const webhookVal = (document.getElementById("slackWebhookInput").value || "").trim();
    const vpVal = document.querySelector('input[name="viewportStyle"]:checked').value;
    // Save viewport style to browser localStorage (per-device)
    localStorage.setItem("viewportStyle", vpVal);
    viewportStyle = vpVal;
    computeRackSize(racksCache.length);
    try {
      const [titleRes, webhookRes] = await Promise.all([
        fetch("/api/settings/title", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({title: titleVal})
        }),
        fetch("/api/settings/slack_webhook", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({url: webhookVal})
        })
      ]);
      const titleData = await titleRes.json();
      if (titleData && titleData.ok && titleData.title) applyTitle(titleData.title);
      closeSettings();
    } catch(e) {}
  }

</script>
</body>
</html>
"""
    html = html.replace("__COMMUNITY__", SNMP_COMMUNITY)
    html = html.replace("__TITLE__", initial_title)
    return HTMLResponse(html)
