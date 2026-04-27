#!/usr/bin/env python3
"""COS Power Dashboard — Report Mailer.

Runs OUTSIDE the grey network on a host that has:
  1. Passwordless SSH to the cospowerdash jumpbox (verify: `ssh <host>` prompts
     nothing and lands you on the jumpbox shell).
  2. Outbound internet SMTP (587/STARTTLS to smtp.gmail.com or your relay).

On each cycle, the mailer:
  1. SCPs reports/config.ini from the jumpbox (recipients, subject template,
     schedule, time range — written by Configure Report Delivery in the dashboard UI).
  2. Lists reports/*.pdf on the jumpbox.
  3. For each PDF: downloads it, emails it to the recipients, then SSHes to
     the jumpbox to move the original into reports/archive/.
  4. Runs a health check: if the newest report (across reports/ and
     reports/archive/) is older than the schedule's expected cadence plus a
     grace period, emails an alert to the admin. Debounced to once per day
     via mailer.state.json.

Usage:
  python mailer.py                 first run launches the wizard, then daemon
  python mailer.py --once          single cycle (good for cron)
  python mailer.py --config        re-run the wizard
  python mailer.py --dry-run       list what would be mailed; do not send or archive
"""

import argparse
import configparser
import getpass
import io
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "mailer.ini"
STATE_PATH = HERE / "mailer.state.json"
DOWNLOAD_DIR = HERE / "downloads"

# Maps a dashboard schedule key to the nominal interval between fires.
# Used by the health check to decide whether a report is overdue.
SCHEDULE_INTERVAL = {
    "disabled":          None,
    "daily_23_00":       timedelta(days=1),
    "weekdays_17_00":    timedelta(days=1),   # weekend skip tolerated by grace
    "weekly_sat_23_30":  timedelta(days=7),
    "monthly_1st_02_00": timedelta(days=31),
}

# How long past the nominal next-fire time we wait before considering a report
# "overdue" and alerting the admin.
OVERDUE_GRACE = timedelta(hours=12)

# Report filename format the dashboard writes. We parse the timestamp out of
# the filename so the subject template's {start}/{end} placeholders work.
FILENAME_RE = re.compile(r"^lab_overview_(\d{4}-\d{2}-\d{2}T\d{4})\.pdf$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mailer")


# ----------------------------------------------------------------------------
# Config wizard and loader
# ----------------------------------------------------------------------------

def _ask(prompt_text, default=None, secret=False, validate=None):
    """Prompt for a value. Re-prompts on empty or failed validation."""
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            val = getpass.getpass(f"{prompt_text}{suffix}: ").strip()
        else:
            val = input(f"{prompt_text}{suffix}: ").strip()
        if not val and default is not None:
            val = default
        if not val:
            print("  (required)")
            continue
        if validate:
            err = validate(val)
            if err:
                print(f"  {err}")
                continue
        return val


def _ask_int(prompt_text, default, lo=1, hi=1440):
    def v(s):
        try:
            n = int(s)
        except ValueError:
            return "must be a whole number"
        if n < lo or n > hi:
            return f"must be between {lo} and {hi}"
        return None
    return int(_ask(prompt_text, default=str(default), validate=v))


def run_wizard():
    print("\nCOS Report Mailer — first-run configuration\n")
    print("Answers are saved to mailer.ini in this directory. You can edit that\n"
          "file directly later; re-run with --config to use this wizard again.\n")

    ssh_host = _ask("SSH alias or user@host for the jumpbox (must be passwordless)",
                    default="coslab")
    # Quick SSH sanity check
    try:
        subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh_host, "true"],
            stderr=subprocess.STDOUT, timeout=15,
        )
        print(f"  ✓ SSH to {ssh_host} works")
    except Exception as e:
        print(f"  ⚠ SSH test to {ssh_host} failed: {e}")
        print("   Fix your ~/.ssh/config or cert auth before running the mailer.")

    remote_dir = _ask("Remote path to the reports/ dir on the jumpbox",
                      default="~/Projects/COS_Power_Dashboard/reports")

    print("\n-- SMTP settings (the mailer uses these to send email) --")
    smtp_host = _ask("SMTP host", default="smtp.gmail.com")
    smtp_port = _ask_int("SMTP port", default=587, lo=1, hi=65535)
    smtp_user = _ask("SMTP username (full email address)")
    smtp_pass = _ask("SMTP password (app password, typed input hidden)", secret=True)

    print("\n-- Operational settings --")
    admin_email = _ask("Admin email for health-check alerts",
                       default=smtp_user)
    poll_min = _ask_int("Poll interval in minutes", default=30, lo=1, hi=1440)

    cfg = configparser.ConfigParser()
    cfg["ssh"] = {
        "host": ssh_host,
        "remote_reports_dir": remote_dir,
    }
    cfg["smtp"] = {
        "host": smtp_host,
        "port": str(smtp_port),
        "user": smtp_user,
        "password": smtp_pass,
    }
    cfg["daemon"] = {
        "poll_interval_min": str(poll_min),
        "admin_email": admin_email,
    }

    with open(CONFIG_PATH, "w") as f:
        f.write("# COS Report Mailer config — rewrite via `python mailer.py --config`\n")
        f.write("# or hand-edit. Do not commit: this file contains SMTP credentials.\n")
        cfg.write(f)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"\nWrote {CONFIG_PATH} (mode 600).")


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"{CONFIG_PATH} does not exist; run with --config first")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ----------------------------------------------------------------------------
# SSH / SCP helpers — shell out so we reuse the host's existing ssh config
# ----------------------------------------------------------------------------

def ssh_run(cfg, remote_cmd, timeout=30):
    """Run a command on the jumpbox via SSH. Returns stdout (str). Raises on failure."""
    host = cfg["ssh"]["host"]
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ssh '{remote_cmd}' failed (rc={res.returncode}): {res.stderr.strip()}")
    return res.stdout


def resolve_remote_dir(cfg):
    """Resolve the configured remote_reports_dir to an absolute path on the jumpbox.
    Tilde expansion would normally happen on the remote, but OpenSSH 9.0+ scp uses
    SFTP by default and the SFTP server does NOT expand ~. So we run the expansion
    through an actual shell on the jumpbox before any SCP invocations."""
    raw = cfg["ssh"]["remote_reports_dir"]
    if not raw.startswith("~"):
        return raw  # already absolute
    # `echo` through the remote shell expands tilde, symlinks, env vars, etc.
    out = ssh_run(cfg, f"echo {raw}").strip()
    if not out:
        raise RuntimeError(f"Could not resolve remote path {raw!r} on jumpbox")
    return out


def scp_download(cfg, remote_path, local_path, timeout=120):
    host = cfg["ssh"]["host"]
    res = subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         f"{host}:{remote_path}", str(local_path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(f"scp {remote_path} failed (rc={res.returncode}): {res.stderr.strip()}")


# ----------------------------------------------------------------------------
# Dashboard config.ini fetch + remote file listing
# ----------------------------------------------------------------------------

def fetch_remote_config(cfg):
    """SCP reports/config.ini from the jumpbox. Parse and return as ConfigParser."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    local = DOWNLOAD_DIR / "config.ini"
    remote_dir = cfg["ssh"]["remote_reports_dir"]
    try:
        scp_download(cfg, f"{remote_dir}/config.ini", local)
    except RuntimeError as e:
        if "No such file" in str(e):
            raise RuntimeError(
                f"{remote_dir}/config.ini does not exist on the jumpbox yet.\n"
                f"  Fix: open the dashboard UI and click Settings → Configure Report Delivery → Save.\n"
                f"  That action writes config.ini with the current recipients/schedule/etc."
            ) from None
        raise
    parsed = configparser.ConfigParser()
    parsed.read(local)
    if "delivery" not in parsed:
        raise RuntimeError("Remote config.ini has no [delivery] section — has Report Delivery been saved in the dashboard yet?")
    return parsed["delivery"]


def list_pending_pdfs(cfg):
    """List lab_overview_*.pdf files directly in reports/ (not in archive/)."""
    remote_dir = cfg["ssh"]["remote_reports_dir"]
    # -1 one per line, quiet fail if dir empty. Restrict to top level by listing *.pdf directly.
    out = ssh_run(cfg, f"ls -1 {remote_dir}/lab_overview_*.pdf 2>/dev/null || true")
    names = []
    for line in out.splitlines():
        base = line.strip().rsplit("/", 1)[-1]
        if base and base.endswith(".pdf"):
            names.append(base)
    return sorted(names)


def newest_report_mtime(cfg):
    """Return the newest mtime across reports/ and reports/archive/, or None."""
    remote_dir = cfg["ssh"]["remote_reports_dir"]
    cmd = (
        f"find {remote_dir} -maxdepth 2 -name 'lab_overview_*.pdf' "
        f"-printf '%T@\\n' 2>/dev/null | sort -nr | head -1"
    )
    out = ssh_run(cfg, cmd).strip()
    if not out:
        return None
    try:
        return datetime.fromtimestamp(float(out))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Time-range resolution (mirror of dashboard's _resolve_time_range)
# ----------------------------------------------------------------------------

def resolve_window(time_range, end_dt):
    """Given a time_range key and the report's end datetime, return (start_dt, end_dt)."""
    if time_range == "trailing_24h":
        return end_dt - timedelta(hours=24), end_dt
    if time_range == "trailing_30d":
        return end_dt - timedelta(days=30), end_dt
    if time_range == "prev_week_mon_fri":
        days_since_fri = (end_dt.weekday() - 4) % 7
        if days_since_fri == 0 and end_dt.hour < 23:
            days_since_fri = 7
        fri = end_dt - timedelta(days=days_since_fri)
        fri_eod = fri.replace(hour=23, minute=59, second=0, microsecond=0)
        mon_sod = (fri - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
        return mon_sod, fri_eod
    # default trailing_7d
    return end_dt - timedelta(days=7), end_dt


def parse_filename_end(filename):
    """Extract the end datetime from a lab_overview_YYYY-MM-DDTHHMM.pdf filename."""
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H%M")


# ----------------------------------------------------------------------------
# Body rendering — HTML + plain-text alternatives from the summary sidecar
# ----------------------------------------------------------------------------

def _fmt_num(v, suffix="", nd=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_int(v, suffix=""):
    if v is None:
        return "—"
    try:
        return f"{int(v):,}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _sum_gpus(lab):
    """Total GPU count across vendors (NV + AMD). Returns None only when both
    sources are missing, so absence stays distinguishable from a real zero."""
    nv = lab.get("nv_gpu_count")
    amd = lab.get("amd_gpu_count")
    if nv is None and amd is None:
        return None
    return (nv or 0) + (amd or 0)


def render_text_body(meta):
    """Plain-text body for clients that can't render HTML, and as the spam-filter
    friendly sibling of the HTML alternative. Uses aligned ASCII for readability."""
    s = meta.get("summary") or {}
    lab = s.get("lab", {})
    gpu = s.get("gpu", {})
    therm = s.get("thermals", {})
    top_srv = s.get("top_servers", []) or []
    top_gpu = (gpu.get("top_gpus") or [])

    lines = []
    lines.append("COS Lab Power Report")
    lines.append("=" * 40)
    lines.append(f"Window:  {meta['window_start']}  →  {meta['window_end']}")
    lines.append(f"Hours:   {meta['window_hours']}")
    lines.append(f"Scope:   {meta['scope_str']}")
    lines.append("")
    lines.append("KEY STATS")
    lines.append(f"  Total energy in window:    {_fmt_num(lab.get('total_kwh'), ' kWh')}")
    lines.append(f"  Peak lab power:            {_fmt_num(lab.get('peak_kw'), ' kW')}")
    lines.append(f"  Current lab power:         {_fmt_num(lab.get('kw_now'), ' kW')}")
    lines.append(f"  GPU energy in window:      {_fmt_num(lab.get('gpu_kwh'), ' kWh')}")
    lines.append(f"  Servers in scope:          {_fmt_int(lab.get('server_count'))}")
    lines.append(f"  GPUs:                      {_fmt_int(_sum_gpus(lab))}")
    lines.append("")
    if top_srv:
        lines.append("TOP SERVERS BY ENERGY")
        for i, row in enumerate(top_srv, 1):
            lines.append(f"  {i}. {row.get('server','?'):<14}"
                         f"avg {_fmt_num(row.get('avg_w'),' W',nd=0):>8}  "
                         f"peak {_fmt_num(row.get('peak_w'),' W',nd=0):>8}  "
                         f"{_fmt_num(row.get('kwh'),' kWh'):>12}  "
                         f"({_fmt_num(row.get('pct_of_total'),'%',nd=1)})")
        lines.append("")
    if top_gpu:
        lines.append("TOP GPUS BY ENERGY (NVIDIA)")
        for i, row in enumerate(top_gpu, 1):
            lines.append(f"  {i}. {row.get('host','?')} GPU{row.get('gpu','?')}   "
                         f"{_fmt_num(row.get('kwh'),' kWh'):>10}")
        lines.append("")
    if therm:
        lines.append("THERMALS (max over window)")
        if "max_inlet_c" in therm:
            lines.append(f"  Inlet:   {_fmt_num(therm.get('max_inlet_c'), '°C', nd=1)}  on {therm.get('max_inlet_server','?')}")
        if "max_exhaust_c" in therm:
            lines.append(f"  Exhaust: {_fmt_num(therm.get('max_exhaust_c'), '°C', nd=1)}  on {therm.get('max_exhaust_server','?')}")
        lines.append("")
    lines.append("Full report attached as PDF.")
    lines.append("")
    lines.append("--")
    lines.append(f"Sent automatically by {meta.get('sender_label') or 'COS Power Dashboard'}.")
    lines.append(f"Schedule: {meta.get('schedule','?')}  |  Attachment: {meta.get('filename','?')}")
    return "\n".join(lines)


def render_html_body(meta):
    """Inline-styled HTML body. Email clients strip <head> and most external CSS,
    so every style lives on the element via the style attribute. Uses tables for
    layout because Outlook/Gmail renderers still treat flex/grid inconsistently."""
    s = meta.get("summary") or {}
    lab = s.get("lab", {})
    gpu = s.get("gpu", {})
    therm = s.get("thermals", {})
    top_srv = s.get("top_servers", []) or []
    top_gpu = (gpu.get("top_gpus") or [])

    # Key-stats cards: 3 rows x 2 cols
    stat_cards = [
        ("Total Energy",  _fmt_num(lab.get("total_kwh"), " kWh")),
        ("Peak Power",    _fmt_num(lab.get("peak_kw"), " kW")),
        ("Current Load",  _fmt_num(lab.get("kw_now"), " kW")),
        ("GPU Energy",    _fmt_num(lab.get("gpu_kwh"), " kWh")),
        ("Servers",       _fmt_int(lab.get("server_count"))),
        ("GPUs",          _fmt_int(_sum_gpus(lab))),
    ]
    card_rows_html = ""
    for i in range(0, len(stat_cards), 2):
        cells = stat_cards[i:i+2]
        tds = ""
        for label, value in cells:
            tds += (
                '<td width="50%" style="background:#eff6ff;border-radius:6px;'
                'padding:16px 18px;vertical-align:top;border:1px solid #dbeafe">'
                f'<div style="font-size:11px;letter-spacing:0.5px;text-transform:uppercase;color:#6b7280;font-weight:600">{label}</div>'
                f'<div style="font-size:22px;font-weight:700;color:#1e3a8a;margin-top:4px;line-height:1.2">{value}</div>'
                '</td>'
            )
        card_rows_html += (
            '<tr>' + tds.replace('</td><td', '</td><td style="width:12px"></td><td') + '</tr>'
            '<tr><td colspan="3" style="height:12px"></td></tr>'
        )

    def _table(title, header_cells, data_rows):
        thead = "".join(
            f'<th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#6b7280;font-weight:600;border-bottom:2px solid #e5e7eb">{h}</th>'
            for h in header_cells
        )
        body = ""
        for i, row in enumerate(data_rows):
            bg = "#ffffff" if i % 2 == 0 else "#f9fafb"
            tds = "".join(
                f'<td style="padding:8px 10px;font-size:13px;color:#1f2937;border-bottom:1px solid #f3f4f6">{c}</td>'
                for c in row
            )
            body += f'<tr style="background:{bg}">{tds}</tr>'
        return (
            f'<h3 style="margin:24px 0 10px;font-size:14px;font-weight:700;color:#1f2937;letter-spacing:-0.2px">{title}</h3>'
            f'<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif">'
            f'<thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>'
        )

    top_srv_html = ""
    if top_srv:
        rows = [
            [
                r.get("server", "?"),
                _fmt_num(r.get("avg_w"), " W", nd=0),
                _fmt_num(r.get("peak_w"), " W", nd=0),
                _fmt_num(r.get("kwh"), " kWh"),
                _fmt_num(r.get("pct_of_total"), "%", nd=1),
            ]
            for r in top_srv
        ]
        top_srv_html = _table("Top Servers by Energy", ["Server", "Avg", "Peak", "Energy", "% of Total"], rows)

    top_gpu_html = ""
    if top_gpu:
        rows = [
            [f'{r.get("host","?")} GPU{r.get("gpu","?")}', _fmt_num(r.get("kwh"), " kWh")]
            for r in top_gpu
        ]
        top_gpu_html = _table("Top GPUs by Energy (NVIDIA)", ["GPU", "Energy in window"], rows)

    thermals_html = ""
    if therm:
        inlet_line  = exhaust_line = ""
        if "max_inlet_c" in therm:
            inlet_line = (
                f'<div style="font-size:13px;color:#1f2937;margin:2px 0">'
                f'<strong>Max inlet:</strong> {_fmt_num(therm.get("max_inlet_c"), "°C", nd=1)} '
                f'on <code style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">{therm.get("max_inlet_server","?")}</code></div>'
            )
        if "max_exhaust_c" in therm:
            exhaust_line = (
                f'<div style="font-size:13px;color:#1f2937;margin:2px 0">'
                f'<strong>Max exhaust:</strong> {_fmt_num(therm.get("max_exhaust_c"), "°C", nd=1)} '
                f'on <code style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">{therm.get("max_exhaust_server","?")}</code></div>'
            )
        thermals_html = (
            '<h3 style="margin:24px 0 10px;font-size:14px;font-weight:700;color:#1f2937;letter-spacing:-0.2px">Thermals</h3>'
            + inlet_line + exhaust_line
        )

    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1f2937">
  <table width="640" cellspacing="0" cellpadding="0" align="center"
         style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);border:1px solid #e5e7eb">
    <tr><td style="background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#ffffff;padding:28px 32px">
      <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;opacity:0.8;font-weight:600">Automated Report</div>
      <div style="font-size:22px;font-weight:700;letter-spacing:-0.4px;margin-top:2px">COS Lab Power Report</div>
      <div style="font-size:13px;opacity:0.85;margin-top:6px">
        <strong>{meta['window_start']}</strong> &rarr; <strong>{meta['window_end']}</strong>
        &nbsp;·&nbsp; {meta['window_hours']}h
        &nbsp;·&nbsp; {meta['scope_str']}
      </div>
    </td></tr>
    <tr><td style="padding:24px 32px 4px">
      <p style="margin:0 0 18px;font-size:14px;color:#4b5563;line-height:1.5">
        Below is an automated summary of lab power and thermal activity for the reported window.
        The full PDF report (per-cluster charts, per-server tables, top-10 GPU energy, thermals appendix)
        is attached to this message.
      </p>
      <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:separate;border-spacing:0">
        {card_rows_html}
      </table>
      {top_srv_html}
      {top_gpu_html}
      {thermals_html}
    </td></tr>
    <tr><td style="padding:16px 32px 24px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280;line-height:1.6">
      Sent automatically by <strong>{meta.get('sender_label') or 'COS Power Dashboard'}</strong>
      &nbsp;·&nbsp; Schedule: <code style="font-family:ui-monospace,Menlo,Consolas,monospace">{meta.get('schedule','?')}</code><br/>
      Attachment: <code style="font-family:ui-monospace,Menlo,Consolas,monospace">{meta.get('filename','?')}</code>
    </td></tr>
  </table>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Email send
# ----------------------------------------------------------------------------

def send_email(cfg, to_list, subject, text_body, html_body, attach_path, sender_label):
    smtp = cfg["smtp"]
    from_addr = smtp["user"]
    msg = EmailMessage()
    msg["From"] = f"{sender_label} <{from_addr}>" if sender_label else from_addr
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    # Plain-text alternative goes in first; add_alternative promotes to
    # multipart/alternative, then add_attachment wraps that in multipart/mixed.
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    with open(attach_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application", subtype="pdf",
            filename=Path(attach_path).name,
        )
    ctx = ssl.create_default_context()
    port = int(smtp["port"])
    with smtplib.SMTP(smtp["host"], port, timeout=30) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(smtp["user"], smtp["password"])
        s.send_message(msg)


def send_plain(cfg, to_addr, subject, body):
    smtp = cfg["smtp"]
    msg = EmailMessage()
    msg["From"] = smtp["user"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=30) as s:
        s.ehlo(); s.starttls(context=ctx); s.ehlo()
        s.login(smtp["user"], smtp["password"])
        s.send_message(msg)


# ----------------------------------------------------------------------------
# Health check
# ----------------------------------------------------------------------------

def health_check(cfg, delivery, state):
    """If the newest report is overdue and we haven't alerted today, email admin."""
    schedule = delivery.get("schedule", "disabled")
    interval = SCHEDULE_INTERVAL.get(schedule)
    if interval is None:
        return  # disabled or unknown — nothing to check
    newest = newest_report_mtime(cfg)
    now = datetime.now()
    if newest is None:
        # No reports at all yet — consider this overdue only if we've been
        # running for a while. Don't spam on day one.
        return
    if now - newest <= interval + OVERDUE_GRACE:
        return  # fresh enough
    last_alert = state.get("last_stale_alert_iso")
    if last_alert:
        try:
            last_dt = datetime.fromisoformat(last_alert)
            if now - last_dt < timedelta(hours=24):
                return  # already alerted within the last day
        except Exception:
            pass
    admin = cfg["daemon"].get("admin_email", "").strip()
    if not admin:
        log.warning("Report overdue but no admin_email configured")
        return
    body = (
        f"The cospowerdash scheduled report looks stuck.\n\n"
        f"Schedule:         {schedule}\n"
        f"Expected interval:{interval}\n"
        f"Newest report:    {newest.isoformat()} (age {now - newest})\n"
        f"Jumpbox:          {cfg['ssh']['host']}\n"
        f"Reports dir:      {cfg['ssh']['remote_reports_dir']}\n\n"
        f"Check the dashboard scheduler (powerdash.log on the jumpbox) and the\n"
        f"Custom Reporting configuration (Prometheus URL reachable?).\n"
    )
    try:
        send_plain(cfg, admin, "[cospowerdash] Scheduled report overdue", body)
        state["last_stale_alert_iso"] = now.isoformat(timespec="seconds")
        save_state(state)
        log.warning("Sent overdue-report alert to %s", admin)
    except Exception as e:
        log.exception("Failed to send overdue alert: %s", e)


# ----------------------------------------------------------------------------
# Archive step
# ----------------------------------------------------------------------------

def archive_on_remote(cfg, filename, json_sidecar=None):
    """Move the mailed PDF (and its JSON sidecar, if present) into reports/archive/
    on the jumpbox in a single SSH round-trip."""
    remote_dir = cfg["ssh"]["remote_reports_dir"]
    parts = [f"mkdir -p {remote_dir}/archive", f"mv {remote_dir}/{filename} {remote_dir}/archive/"]
    if json_sidecar:
        # `|| true` so a missing sidecar doesn't fail the whole archive move.
        parts.append(f"mv {remote_dir}/{json_sidecar} {remote_dir}/archive/ 2>/dev/null || true")
    ssh_run(cfg, " && ".join(parts))


# ----------------------------------------------------------------------------
# Main cycle
# ----------------------------------------------------------------------------

def run_cycle(cfg, state, dry_run=False):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    # Resolve the remote reports dir once so every SSH + SCP uses the absolute
    # path (works around the OpenSSH 9+ SFTP tilde-expansion regression).
    abs_remote = resolve_remote_dir(cfg)
    if abs_remote != cfg["ssh"]["remote_reports_dir"]:
        log.info("Resolved %s → %s", cfg["ssh"]["remote_reports_dir"], abs_remote)
        cfg["ssh"]["remote_reports_dir"] = abs_remote
    log.info("Cycle start (host=%s, dir=%s)", cfg["ssh"]["host"], cfg["ssh"]["remote_reports_dir"])
    delivery = fetch_remote_config(cfg)
    recipients = [r.strip() for r in delivery.get("recipients", "").split(",") if r.strip()]
    subject_tpl = delivery.get("subject_template") or "COS Lab Power Report — {start} to {end}"
    sender_label = delivery.get("sender_label") or ""
    time_range = delivery.get("time_range", "trailing_7d")
    schedule = delivery.get("schedule", "disabled")
    log.info("Loaded dashboard config: schedule=%s, recipients=%d, time_range=%s",
             schedule, len(recipients), time_range)

    pending = list_pending_pdfs(cfg)
    log.info("Pending reports on jumpbox: %d", len(pending))

    if not pending:
        health_check(cfg, delivery, state)
        return

    # sent_filenames tracks PDFs we've already emailed but whose archive move
    # hadn't been confirmed yet. If the same filename turns up in pending next
    # cycle, we skip the email (avoiding a duplicate to the recipients) and
    # just retry the archive. On archive success we drop the entry so the
    # state stays bounded.
    sent_filenames = state.setdefault("sent_filenames", {})

    if not recipients:
        # Only skip the new-sends; still retry any previously-emailed archives
        # that are in a stuck state. Recipients aren't needed for that.
        stuck = [f for f in pending if f in sent_filenames]
        if not stuck:
            log.warning("Reports are pending but recipients list is empty — fix in dashboard Settings. Skipping.")
            return
        log.warning("Recipients empty; only retrying archive moves for %d previously-sent report(s)", len(stuck))
        pending = stuck

    remote_dir = cfg["ssh"]["remote_reports_dir"]
    for filename in pending:
        already_sent = filename in sent_filenames

        if already_sent:
            # Email was confirmed on a previous cycle but archive didn't land.
            # Skip download + send entirely; go straight to the archive retry.
            log.info("Retrying archive for %s (already emailed %s)", filename, sent_filenames[filename])
            if dry_run:
                log.info("[DRY RUN] Would retry archive for %s", filename)
                continue
            try:
                archive_on_remote(cfg, filename)
                log.info("Archived %s on jumpbox (retry succeeded)", filename)
                sent_filenames.pop(filename, None)
                save_state(state)
            except Exception as e:
                log.error("Archive retry still failing for %s: %s", filename, e)
            continue

        local_path = DOWNLOAD_DIR / filename
        try:
            scp_download(cfg, f"{remote_dir}/{filename}", local_path)
        except Exception as e:
            log.error("Download failed for %s: %s", filename, e)
            continue

        # Sidecar JSON (added to the dashboard in Phase-B polish). Missing
        # sidecars are tolerated — older PDFs generated before the sidecar
        # change won't have one; the email still goes out with just headline
        # metadata, no stats cards.
        json_filename = filename[:-4] + ".json"
        json_local = DOWNLOAD_DIR / json_filename
        summary = None
        try:
            scp_download(cfg, f"{remote_dir}/{json_filename}", json_local)
            summary = json.loads(json_local.read_text())
        except Exception as e:
            log.warning("No summary sidecar for %s (%s) — will email without stats cards", filename, e)

        # Prefer the actual window the dashboard embedded in the sidecar JSON
        # over recomputing here — the dashboard already resolved the time_range
        # correctly when generating the PDF, and the PDF's contents reflect
        # that window. If we recompute from the config.ini time_range we can
        # disagree with the PDF (happens if config.ini lags the generation,
        # or if the user changed the time_range between generations).
        win = (summary or {}).get("window") or {}
        start_dt = end_dt = None
        if win.get("start_iso") and win.get("end_iso"):
            try:
                start_dt = datetime.fromisoformat(win["start_iso"])
                end_dt   = datetime.fromisoformat(win["end_iso"])
            except ValueError:
                start_dt = end_dt = None
        if not (start_dt and end_dt):
            # Legacy PDFs without a sidecar — fall back to recompute from filename.
            end_dt = parse_filename_end(filename) or datetime.fromtimestamp(local_path.stat().st_mtime)
            start_dt, end_dt = resolve_window(time_range, end_dt)
        subject = subject_tpl.format(
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
        )
        scope_list = (summary or {}).get("scope") or []
        meta = {
            "filename": filename,
            "schedule": schedule,
            "sender_label": sender_label,
            "summary": summary,
            "window_start": start_dt.strftime("%Y-%m-%d %H:%M"),
            "window_end":   end_dt.strftime("%Y-%m-%d %H:%M"),
            "window_hours": win.get("hours") or round((end_dt - start_dt).total_seconds() / 3600.0, 1),
            "scope_str":    ", ".join(scope_list) if scope_list else "All clusters",
        }
        text_body = render_text_body(meta)
        html_body = render_html_body(meta)

        if dry_run:
            log.info("[DRY RUN] Would email %s to %s with subject %r (summary=%s)",
                     filename, recipients, subject, "yes" if summary else "no")
            continue

        try:
            send_email(cfg, recipients, subject, text_body, html_body, local_path, sender_label)
            # Record the send BEFORE trying to archive. If archive fails (or the
            # process dies right after), the next cycle sees this entry and
            # won't re-email.
            sent_filenames[filename] = datetime.now().isoformat(timespec="seconds")
            save_state(state)
            log.info("Emailed %s to %s", filename, recipients)
        except Exception as e:
            log.exception("Email failed for %s: %s — leaving on jumpbox, will retry next cycle", filename, e)
            continue

        try:
            archive_on_remote(cfg, filename, json_sidecar=json_filename if summary else None)
            log.info("Archived %s on jumpbox", filename)
            sent_filenames.pop(filename, None)
            save_state(state)
        except Exception as e:
            log.error("Archive move failed for %s: %s — will retry next cycle (no duplicate email)", filename, e)

        for p in (local_path, json_local):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    health_check(cfg, delivery, state)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="COS Power Dashboard — Report Mailer")
    ap.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    ap.add_argument("--config", action="store_true", help="(Re)run the interactive config wizard")
    ap.add_argument("--dry-run", action="store_true", help="List pending reports without sending or archiving")
    args = ap.parse_args()

    if args.config or not CONFIG_PATH.exists():
        run_wizard()
        if args.config:
            return

    cfg = load_config()
    interval = int(cfg["daemon"]["poll_interval_min"]) * 60
    state = load_state()

    if args.once or args.dry_run:
        try:
            run_cycle(cfg, state, dry_run=args.dry_run)
        except Exception as e:
            log.exception("Cycle failed: %s", e)
        return

    log.info("Daemon mode: polling every %d minutes. Ctrl-C to stop.", interval // 60)
    while True:
        try:
            run_cycle(cfg, state)
        except Exception as e:
            log.exception("Cycle failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
