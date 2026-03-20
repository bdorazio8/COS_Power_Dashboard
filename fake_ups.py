"""
Fake UPS SNMP Simulator for UPSDash testing.

Run in a separate terminal:
    sudo python fake_ups.py

Then add a rack in the dashboard using this machine's IP address.

Commands (type and press Enter):
    online    - UPS normal operation (default)
    battery   - UPS running on battery
    bypass    - UPS in bypass mode
    offline   - UPS unreachable (stops responding to SNMP)
    load XX   - Set load percentage (e.g. "load 85")
    quit      - Exit the simulator

Requires root/sudo because SNMP uses port 161.
"""

import socket
import threading
import sys
import time

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto.api import v2c
from pyasn1.type.univ import Integer, OctetString

# --- OIDs the dashboard polls ---
OID_LOAD_PCT     = (1,3,6,1,2,1,33,1,4,4,1,5,1)
OID_STATE        = (1,3,6,1,4,1,318,1,1,1,2,2,2,0)
OID_UPS_STATUS   = (1,3,6,1,4,1,318,1,1,1,2,1,1,0)
OID_TEMP         = (1,3,6,1,2,1,33,1,2,7,0)
OID_RUNTIME      = (1,3,6,1,2,1,33,1,2,3,0)
OID_OUTPUT_AMPS  = (1,3,6,1,4,1,318,1,1,1,4,3,4,0)
OID_OUTPUT_WATTS = (1,3,6,1,4,1,318,1,1,1,4,2,8,0)

# APC UPS status codes
STATUS_UNKNOWN    = 1
STATUS_ONLINE     = 2
STATUS_ON_BATTERY = 3
STATUS_ON_BYPASS  = 4
STATUS_OFFLINE    = 6

# --- Simulated UPS state (mutable from command thread) ---
state = {
    "mode": "online",       # online, battery, bypass, offline
    "load_pct": 45,
    "temp_c": 24,
    "runtime_min": 15,
    "output_amps_tenths": 120,   # 12.0A
    "output_watts": 2500,
}
state_lock = threading.Lock()


def get_ups_status_code():
    mode = state["mode"]
    if mode == "online":
        return STATUS_ONLINE
    elif mode == "battery":
        return STATUS_ON_BATTERY
    elif mode == "bypass":
        return STATUS_ON_BYPASS
    elif mode == "offline":
        return STATUS_OFFLINE
    return STATUS_UNKNOWN


def get_bypass_state():
    return 5 if state["mode"] == "bypass" else 1


class FakeUPSResponder(cmdrsp.GetCommandResponder):
    """Override GET handler to return our simulated values."""

    def handleMgmtOperation(self, snmpEngine, stateReference, contextName, PDU, acInfo):
        varBinds = v2c.apiPDU.getVarBinds(PDU)
        rspVarBinds = []

        with state_lock:
            if state["mode"] == "offline":
                # Silently drop the request - simulate unreachable by not sending a response
                self.releaseStateInformation(stateReference)
                return

            for oid, val in varBinds:
                oid_tuple = tuple(oid)
                if oid_tuple == OID_LOAD_PCT:
                    rspVarBinds.append((oid, v2c.Integer(state["load_pct"])))
                elif oid_tuple == OID_STATE:
                    rspVarBinds.append((oid, v2c.Integer(get_bypass_state())))
                elif oid_tuple == OID_UPS_STATUS:
                    rspVarBinds.append((oid, v2c.Integer(get_ups_status_code())))
                elif oid_tuple == OID_TEMP:
                    rspVarBinds.append((oid, v2c.Integer(state["temp_c"])))
                elif oid_tuple == OID_RUNTIME:
                    rspVarBinds.append((oid, v2c.Integer(state["runtime_min"])))
                elif oid_tuple == OID_OUTPUT_AMPS:
                    rspVarBinds.append((oid, v2c.Integer(state["output_amps_tenths"])))
                elif oid_tuple == OID_OUTPUT_WATTS:
                    rspVarBinds.append((oid, v2c.Integer(state["output_watts"])))
                else:
                    rspVarBinds.append((oid, v2c.Integer(0)))

        self.sendVarBinds(snmpEngine, stateReference, 0, 0, rspVarBinds)
        self.releaseStateInformation(stateReference)


def print_state():
    mode = state["mode"].upper()
    print(f"\n  [UPS State] Mode: {mode}  |  Load: {state['load_pct']}%  |  "
          f"Runtime: {state['runtime_min']}min  |  Temp: {state['temp_c']}C  |  "
          f"Amps: {state['output_amps_tenths']/10:.1f}A  |  Watts: {state['output_watts']}W")
    print("  Commands: online | battery | bypass | offline | load <0-100> | quit")
    print("> ", end="", flush=True)


def command_loop():
    """Read commands from stdin to change UPS state."""
    time.sleep(1)
    print_state()

    while True:
        try:
            line = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        with state_lock:
            if line == "online":
                state["mode"] = "online"
                print("  -> UPS is now ONLINE")
            elif line == "battery":
                state["mode"] = "battery"
                print("  -> UPS is now ON BATTERY")
            elif line == "bypass":
                state["mode"] = "bypass"
                print("  -> UPS is now ON BYPASS")
            elif line == "offline":
                state["mode"] = "offline"
                print("  -> UPS is now OFFLINE (not responding to SNMP)")
            elif line.startswith("load"):
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    val = int(parts[1])
                    state["load_pct"] = max(0, min(100, val))
                    print(f"  -> Load set to {state['load_pct']}%")
                else:
                    print("  Usage: load <0-100>")
            elif line == "quit":
                print("  Shutting down...")
                import os
                os._exit(0)
            else:
                print(f"  Unknown command: {line}")

        print_state()


def get_local_ips():
    """Get usable IP addresses for this machine."""
    ips = ["127.0.0.1"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
        if lan_ip != "127.0.0.1":
            ips.append(lan_ip)
    except Exception:
        pass
    return ips


def main():
    ips = get_local_ips()
    print("=" * 60)
    print("  Fake UPS SNMP Simulator")
    print("  Listening on UDP port 161 (SNMPv2c, community: COS65)")
    print("  Use one of these IPs in the dashboard:")
    for ip in ips:
        print(f"    -> {ip}")
    print("=" * 60)

    # Create SNMP engine
    snmpEngine = engine.SnmpEngine()

    # Transport - listen on all interfaces, port 161
    config.addTransport(
        snmpEngine,
        udp.domainName,
        udp.UdpTransport().openServerMode(('0.0.0.0', 161))
    )

    # SNMPv2c community
    config.addV1System(snmpEngine, 'my-area', 'COS65')

    # Allow read access
    config.addVacmUser(snmpEngine, 2, 'my-area', 'noAuthNoPriv', (1, 3, 6))

    # Create SNMP context
    snmpContext = context.SnmpContext(snmpEngine)

    # Register our custom GET handler
    FakeUPSResponder(snmpEngine, snmpContext)

    # Start command input on a separate thread
    cmd_thread = threading.Thread(target=command_loop, daemon=True)
    cmd_thread.start()

    # Run the SNMP engine
    snmpEngine.transportDispatcher.jobStarted(1)
    try:
        snmpEngine.transportDispatcher.runDispatcher()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        snmpEngine.transportDispatcher.closeDispatcher()


if __name__ == "__main__":
    main()
