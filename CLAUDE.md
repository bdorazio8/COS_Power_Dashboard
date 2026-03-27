# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UPSDash is a single-file UPS power monitoring dashboard. It polls APC UPS devices via SNMP, displays real-time load percentages in a browser UI, and pushes updates over WebSocket.

## Running the Application

```bash
# Activate the virtualenv
source venv/bin/activate

# Run the server (default: http://localhost:8000)
uvicorn cospowerdash:app --host 0.0.0.0 --port 8000
```

Requires `snmpget` and `ping` system commands to be available on the host.

## Architecture

Everything lives in `cospowerdash.py` — a single-file FastAPI application with inline HTML/CSS/JS served from the `GET /` endpoint.

### Key layers (top to bottom in the file):

- **Database** — SQLite (`upsdash.db`), two tables: `racks` (UPS devices) and `settings` (key-value config). Schema migrations are handled inline in `init_db()`.
- **SNMP helpers** — Shell out to `snmpget` (SNMPv2c, community `COS65`) for load percentage (`upsOutputLoad`) and APC bypass state. `ping` is used for reachability checks.
- **Poll loop** — `asyncio` background task runs every 2 seconds, polls all racks, updates `latest_status` dict, and broadcasts JSON snapshots to all connected WebSocket clients.
- **REST API** — `/api/racks` (add), `/api/delete`, `/api/check` (SNMP connectivity test), `/api/order` (drag-and-drop reorder), `/api/settings/title`.
- **WebSocket** — `/ws` endpoint pushes the full rack status array on every poll cycle.
- **Frontend** — Inline SPA with rack visualizations (fill-bar height = load %), drag-and-drop reordering, and modals for add/remove/settings. No build step or external JS dependencies.

### Important constants

- `SNMP_COMMUNITY = "COS65"` — hardcoded SNMPv2c community string
- `OID_LOAD_PCT` / `OID_STATE` — SNMP OIDs for UPS load and bypass state
- Bypass mode is indicated by state value `5`
- Load colors: green (<70%), amber (70-84%), red (>=85% or bypass)

## Dependencies

Python 3.12 virtualenv with FastAPI, Uvicorn, Pydantic, and websockets. No frontend build tooling.
