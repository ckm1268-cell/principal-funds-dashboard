#!/usr/bin/env python3
"""
Principal Funds Daily NAV Tracker
==================================
Fetches the latest NAV for three Principal Asset Management (Malaysia) unit
trust funds, compares each to the previous available NAV, builds a compact
"light" PDF dashboard, and emails it via the Resend API.

Runs Mon-Fri only, skipping Malaysian public holidays (via the Nager.Date
public holiday API). Designed to run inside GitHub Actions, but works from
any machine with the required environment variables set.

Environment variables required:
    RESEND_API_KEY   - Resend API key (repo secret)
    RESEND_FROM      - verified "from" address, e.g. reports@yourdomain.com
    EMAIL_TO         - comma-separated recipient list, e.g.
                        "ckm1268@gmail.com,lyn1268@gmail.com"

Optional:
    FORCE_RUN        - set to "1" to bypass the weekday/holiday skip check
                        (useful for manual workflow_dispatch testing)
"""

import base64
import csv
import io
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from fpdf import FPDF
from fpdf.enums import XPos, YPos

MYT = ZoneInfo("Asia/Kuala_Lumpur")

# Principal Malaysia website "node" IDs behind each fund's NAV-history CSV
# export. Found via each fund's public factsheet page on principal.com.my.
FUNDS = [
    {
        "name": "Principal Islamic Asia Pacific Dynamic Equity Fund",
        "short": "Islamic Asia Pacific Dynamic Equity",
        "page": "https://www.principal.com.my/en/iapef",
        "nav_node": 1280,
    },
    {
        "name": "Principal DALI Asia Pacific Equity Growth Fund",
        "short": "DALI Asia Pacific Equity Growth",
        "page": "https://www.principal.com.my/en/ief",
        "nav_node": 1270,
    },
    {
        "name": "Principal Greater China Equity Fund (Class MYR)",
        "short": "Greater China Equity (Class MYR)",
        "page": "https://www.principal.com.my/en/gcef",
        "nav_node": 6677,
    },
]

NAV_CSV_URL = "https://www.principal.com.my/en/nav/{node}"
HOLIDAY_API_URL = "https://date.nager.at/api/v3/publicholidays/{year}/MY"


def is_working_day(today: date) -> tuple[bool, str]:
    """Return (should_run, reason_if_skipped)."""
    if today.weekday() >= 5:  # 5=Sat, 6=Sun
        return False, "weekend"

    try:
        resp = requests.get(HOLIDAY_API_URL.format(year=today.year), timeout=15)
        resp.raise_for_status()
        holidays = {h["date"] for h in resp.json()}
        if today.isoformat() in holidays:
            return False, "Malaysian public holiday"
    except Exception as exc:  # noqa: BLE001 - don't let holiday-check failure block the run
        print(f"[warn] holiday check failed ({exc}); proceeding anyway", file=sys.stderr)

    return True, ""


def fetch_nav_history(node_id: int, days_back: int = 21) -> list[dict]:
    """Fetch recent NAV rows for one fund as a list of dicts sorted newest-first."""
    today = datetime.now(MYT).date()
    start = today - timedelta(days=days_back)

    params = {
        "field_fund_nav_date_value[min]": start.strftime("%d-%m-%Y"),
        "field_fund_nav_date_value[max]": today.strftime("%d-%m-%Y"),
        "page": "",
        "_format": "csv",
    }
    resp = requests.get(NAV_CSV_URL.format(node=node_id), params=params, timeout=30)
    resp.raise_for_status()

    rows = []
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        try:
            row_date = datetime.strptime(row["Date"].strip(), "%d-%m-%Y").date()
        except (KeyError, ValueError):
            continue
        try:
            nav = float(row["NAV"].strip())
        except (KeyError, ValueError):
            continue
        rows.append({"date": row_date, "nav": nav})

    rows.sort(key=lambda r: r["date"], reverse=True)

    # If the windowed request came back empty (site quirks, holidays, etc.)
    # retry once without a date filter as a fallback.
    if not rows:
        resp = requests.get(
            NAV_CSV_URL.format(node=node_id), params={"_format": "csv"}, timeout=30
        )
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            try:
                row_date = datetime.strptime(row["Date"].strip(), "%d-%m-%Y").date()
                nav = float(row["NAV"].strip())
            except (KeyError, ValueError):
                continue
            rows.append({"date": row_date, "nav": nav})
        rows.sort(key=lambda r: r["date"], reverse=True)

    return rows


def build_fund_results() -> list[dict]:
    results = []
    for fund in FUNDS:
        entry = {**fund, "error": None, "latest": None, "previous": None, "pct_change": None}
        try:
            rows = fetch_nav_history(fund["nav_node"])
            if len(rows) >= 1:
                entry["latest"] = rows[0]
            if len(rows) >= 2:
                entry["previous"] = rows[1]
                latest_nav = rows[0]["nav"]
                prev_nav = rows[1]["nav"]
                if prev_nav:
                    entry["pct_change"] = (latest_nav - prev_nav) / prev_nav * 100
            if not rows:
                entry["error"] = "No NAV data returned"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        results.append(entry)
    return results


def _line(pdf: FPDF, h: float, text: str) -> None:
    """Print one full-width line and drop to the next line at the left margin."""
    pdf.cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _wrapped(pdf: FPDF, h: float, text: str) -> None:
    """Print a (possibly multi-line) block and drop to the next line at the left margin."""
    pdf.multi_cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def build_pdf(results: list[dict], run_date: date) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    _line(pdf, 10, "Principal Funds - Daily NAV Dashboard")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    _line(pdf, 6, f"Report date: {run_date.strftime('%d %B %Y')} (Asia/Kuala_Lumpur)")
    _line(pdf, 6, "Source: principal.com.my (public NAV history)")
    pdf.ln(4)
    pdf.set_text_color(0, 0, 0)

    for r in results:
        pdf.set_font("Helvetica", "B", 12)
        _wrapped(pdf, 7, r["name"])
        pdf.set_font("Helvetica", "", 10)

        if r["error"] or not r["latest"]:
            pdf.set_text_color(180, 0, 0)
            _line(pdf, 6, f"  Data unavailable ({r['error'] or 'no rows'})")
            pdf.set_text_color(0, 0, 0)
            pdf.ln(3)
            continue

        latest = r["latest"]
        nav_line = f"  NAV: RM {latest['nav']:.4f}   (as at {latest['date'].strftime('%d %b %Y')})"
        _line(pdf, 6, nav_line)

        if r["previous"] and r["pct_change"] is not None:
            prev = r["previous"]
            pct = r["pct_change"]
            direction = "+" if pct >= 0 else ""
            if pct >= 0:
                pdf.set_text_color(0, 128, 0)
            else:
                pdf.set_text_color(200, 0, 0)
            change_line = (
                f"  Change vs {prev['date'].strftime('%d %b %Y')} "
                f"(RM {prev['nav']:.4f}): {direction}{pct:.2f}%"
            )
            _line(pdf, 6, change_line)
            pdf.set_text_color(0, 0, 0)
        else:
            pdf.set_text_color(120, 120, 120)
            _line(pdf, 6, "  No previous NAV available for comparison yet")
            pdf.set_text_color(0, 0, 0)

        pdf.ln(3)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(130, 130, 130)
    _wrapped(
        pdf,
        5,
        "Automated report generated by a scheduled GitHub Actions workflow. "
        "NAV figures are sourced from principal.com.my's public NAV history export "
        "and reflect the most recent business day for which data has been published; "
        "this may lag today's date by one or more business days.",
    )

    return bytes(pdf.output())


def send_email(pdf_bytes: bytes, run_date: date, results: list[dict]) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    from_addr = os.environ["RESEND_FROM"]
    to_addrs = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    filename = f"principal-funds-dashboard-{run_date.isoformat()}.pdf"

    rows_html = ""
    for r in results:
        if r["error"] or not r["latest"]:
            rows_html += (
                f"<tr><td>{r['short']}</td><td colspan='3' style='color:#b00'>"
                f"Data unavailable</td></tr>"
            )
            continue
        latest = r["latest"]
        pct_html = "n/a"
        if r["pct_change"] is not None:
            color = "#008000" if r["pct_change"] >= 0 else "#c00000"
            sign = "+" if r["pct_change"] >= 0 else ""
            pct_html = f"<span style='color:{color}'>{sign}{r['pct_change']:.2f}%</span>"
        rows_html += (
            f"<tr><td>{r['short']}</td>"
            f"<td>RM {latest['nav']:.4f}</td>"
            f"<td>{latest['date'].strftime('%d %b %Y')}</td>"
            f"<td>{pct_html}</td></tr>"
        )

    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#222">
      <h2>Principal Funds - Daily NAV Dashboard</h2>
      <p>Report date: {run_date.strftime('%d %B %Y')} (Asia/Kuala_Lumpur)</p>
      <table cellpadding="6" style="border-collapse:collapse;width:100%">
        <tr style="background:#f2f2f2;text-align:left">
          <th>Fund</th><th>NAV</th><th>NAV date</th><th>Change</th>
        </tr>
        {rows_html}
      </table>
      <p style="color:#888;font-size:12px;margin-top:16px">
        Full detail attached as PDF. Source: principal.com.my public NAV history.
      </p>
    </div>
    """

    payload = {
        "from": from_addr,
        "to": to_addrs,
        "subject": f"Principal Funds NAV Dashboard - {run_date.strftime('%d %b %Y')}",
        "html": html,
        "attachments": [
            {
                "filename": filename,
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
                "content_type": "application/pdf",
            }
        ],
    }

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"[error] Resend API returned {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    print(f"Email sent: {resp.json()}")


def main() -> None:
    today = datetime.now(MYT).date()

    if os.environ.get("FORCE_RUN") != "1":
        should_run, reason = is_working_day(today)
        if not should_run:
            print(f"Skipping run: {reason} ({today.isoformat()})")
            return

    results = build_fund_results()
    pdf_bytes = build_pdf(results, today)

    out_path = f"principal-funds-dashboard-{today.isoformat()}.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")

    for r in results:
        if r["error"]:
            print(f"[warn] {r['name']}: {r['error']}", file=sys.stderr)

    send_email(pdf_bytes, today, results)


if __name__ == "__main__":
    main()
