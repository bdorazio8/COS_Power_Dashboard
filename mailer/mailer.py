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
    scp_download(cfg, f"{remote_dir}/config.ini", local)
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
# Email send
# ----------------------------------------------------------------------------

def send_email(cfg, to_list, subject, body, attach_path, sender_label):
    smtp = cfg["smtp"]
    from_addr = smtp["user"]
    msg = EmailMessage()
    msg["From"] = f"{sender_label} <{from_addr}>" if sender_label else from_addr
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.set_content(body)
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

def archive_on_remote(cfg, filename):
    """Move the successfully-mailed report into reports/archive/ on the jumpbox."""
    remote_dir = cfg["ssh"]["remote_reports_dir"]
    # Single SSH call: mkdir -p then mv. Quoting the filename in case of spaces.
    cmd = (
        f"mkdir -p {remote_dir}/archive && "
        f"mv {remote_dir}/{filename} {remote_dir}/archive/"
    )
    ssh_run(cfg, cmd)


# ----------------------------------------------------------------------------
# Main cycle
# ----------------------------------------------------------------------------

def run_cycle(cfg, state, dry_run=False):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
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

        end_dt = parse_filename_end(filename) or datetime.fromtimestamp(local_path.stat().st_mtime)
        start_dt, end_dt = resolve_window(time_range, end_dt)
        subject = subject_tpl.format(
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
        )
        body = (
            f"Attached: {filename}\n\n"
            f"Report window: {start_dt.strftime('%Y-%m-%d %H:%M')} → {end_dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"Schedule:      {schedule}\n"
            f"Source:        COS Power Dashboard (grey network)\n\n"
            f"This message was sent automatically by the COS Report Mailer.\n"
        )
        if dry_run:
            log.info("[DRY RUN] Would email %s to %s with subject %r",
                     filename, recipients, subject)
            continue

        try:
            send_email(cfg, recipients, subject, body, local_path, sender_label)
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
            archive_on_remote(cfg, filename)
            log.info("Archived %s on jumpbox", filename)
            sent_filenames.pop(filename, None)
            save_state(state)
        except Exception as e:
            log.error("Archive move failed for %s: %s — will retry next cycle (no duplicate email)", filename, e)

        try:
            local_path.unlink()
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
