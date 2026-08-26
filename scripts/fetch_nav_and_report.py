#!/usr/bin/env python3
"""
Principal Funds Daily NAV Tracker
==================================
Fetches the latest NAV for three Principal Asset Management (Malaysia) unit
trust funds, computes portfolio value and day-over-day change, builds an
illustrated PDF (with a NAV trend chart), and emails a short narrative
summary via the Resend API — matching the style of the reports this project
used to send from a Claude scheduled task.

Runs Mon-Fri only, skipping Malaysian public holidays (via the Nager.Date
public holiday API). Designed to run inside GitHub Actions.

Environment variables required:
    RESEND_API_KEY   - Resend API key (repo secret)
    RESEND_FROM      - verified "from" address, e.g. onboarding@resend.dev
    EMAIL_TO         - comma-separated recipient list

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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests
from fpdf import FPDF
from fpdf.enums import XPos, YPos

MYT = ZoneInfo("Asia/Kuala_Lumpur")

# Fund configuration. units / baseline_nav mirror the values used by the
# GitHub Pages dashboard (scripts/update_dashboard.py) so both reports agree
# on portfolio value. nav_node is the Principal Malaysia website "node" ID
# behind each fund's public NAV-history CSV export.
FUNDS = [
    {
        "key": "islamic",
        "name": "Principal Islamic Asia Pacific Dynamic Equity Fund",
        "short": "Islamic Asia Pacific Dynamic Equity",
        "nav_node": 1280,
        "units": 74474.83,
        "baseline_nav": 1.0396,
    },
    {
        "key": "dali",
        "name": "Principal DALI Asia Pacific Equity Growth Fund",
        "short": "DALI Asia Pacific Equity Growth",
        "nav_node": 1270,
        "units": 76816.72,
        "baseline_nav": 0.9549,
    },
    {
        "key": "greaterChina",
        "name": "Principal Greater China Equity Fund (Class MYR)",
        "short": "Greater China Equity (Class MYR)",
        "nav_node": 6677,
        "units": 42026.20,
        "baseline_nav": 1.2895,
    },
]

NAV_CSV_URL = "https://www.principal.com.my/en/nav/{node}"
HOLIDAY_API_URL = "https://date.nager.at/api/v3/publicholidays/{year}/MY"
DASHBOARD_URL = "https://ckm1268-cell.github.io/principal-funds-dashboard/"
FLAG_THRESHOLD_PCT = 2.0
HISTORY_POINTS = 15  # trading sessions kept for the trend chart


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


def fetch_nav_history(node_id: int, days_back: int = 35) -> list[dict]:
    """Fetch recent NAV rows for one fund as a list of dicts, oldest first."""
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

    def parse(text):
        rows = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            try:
                d = datetime.strptime(row["Date"].strip(), "%d-%m-%Y").date()
                nav = float(row["NAV"].strip())
            except (KeyError, ValueError):
                continue
            rows.append({"date": d, "nav": nav})
        rows.sort(key=lambda r: r["date"])
        return rows

    rows = parse(resp.text)

    # If the windowed request came back empty (site quirks, holidays, etc.)
    # retry once without a date filter as a fallback.
    if not rows:
        resp = requests.get(
            NAV_CSV_URL.format(node=node_id), params={"_format": "csv"}, timeout=30
        )
        resp.raise_for_status()
        rows = parse(resp.text)

    return rows[-HISTORY_POINTS:]


def build_fund_results() -> list[dict]:
    results = []
    for fund in FUNDS:
        entry = {**fund, "error": None, "history": [], "latest": None, "previous": None, "pct_change": None}
        try:
            rows = fetch_nav_history(fund["nav_node"])
            entry["history"] = rows
            if len(rows) >= 1:
                entry["latest"] = rows[-1]
            if len(rows) >= 2:
                entry["previous"] = rows[-2]
                latest_nav = rows[-1]["nav"]
                prev_nav = rows[-2]["nav"]
                if prev_nav:
                    entry["pct_change"] = (latest_nav - prev_nav) / prev_nav * 100
            if not rows:
                entry["error"] = "No NAV data returned"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        results.append(entry)
    return results


def build_portfolio_summary(results: list[dict]) -> dict:
    """Aggregate portfolio value, day-over-day change, freshness, and flags."""
    ok = [r for r in results if r["latest"] and not r["error"]]
    unavailable = [r for r in results if r["error"] or not r["latest"]]

    total_today = sum(r["latest"]["nav"] * r["units"] for r in ok)
    # Use previous NAV where available; fall back to latest (no day-over-day
    # move assumed) so one missing "previous" doesn't wreck the whole total.
    total_prev = sum((r["previous"]["nav"] if r["previous"] else r["latest"]["nav"]) * r["units"] for r in ok)
    diff = total_today - total_prev
    pct = (diff / total_prev * 100) if total_prev else 0.0

    dated = [r for r in ok]
    max_date = max((r["latest"]["date"] for r in dated), default=None)
    lagging = [r for r in dated if r["latest"]["date"] != max_date]

    flagged = [r for r in ok if r["pct_change"] is not None and abs(r["pct_change"]) > FLAG_THRESHOLD_PCT]
    movers = [r for r in ok if r["pct_change"] is not None]
    largest_mover = max(movers, key=lambda r: abs(r["pct_change"])) if movers else None

    return {
        "total_today": total_today,
        "total_prev": total_prev,
        "diff": diff,
        "pct": pct,
        "max_date": max_date,
        "lagging": lagging,
        "flagged": flagged,
        "largest_mover": largest_mover,
        "unavailable": unavailable,
    }


def build_narrative(results: list[dict], summary: dict, run_date: date) -> str:
    diff, pct = summary["diff"], summary["pct"]
    if diff >= 0:
        value_line = (
            f"Total portfolio value is RM {summary['total_today']:,.2f}, "
            f"up +RM {diff:,.2f} (+{pct:.2f}%) vs yesterday."
        )
    else:
        value_line = (
            f"Total portfolio value is RM {summary['total_today']:,.2f}, "
            f"down RM {abs(diff):,.2f} ({pct:.2f}%) vs yesterday."
        )

    if summary["max_date"] is None:
        freshness_line = "NAV data could not be retrieved for any fund this run."
    elif not summary["lagging"]:
        freshness_line = f"All three funds are now published through {summary['max_date'].strftime('%-d %b %Y')}."
    else:
        lag_names = ", ".join(
            f"{r['short']} ({r['latest']['date'].strftime('%-d %b %Y')})" for r in summary["lagging"]
        )
        freshness_line = (
            f"Published through {summary['max_date'].strftime('%-d %b %Y')} for most funds; "
            f"still lagging: {lag_names}."
        )

    if summary["unavailable"]:
        names = ", ".join(r["name"] for r in summary["unavailable"])
        freshness_line += f" Note: could not fetch data for {names} this run — excluded from the total above."

    if summary["flagged"]:
        names = ", ".join(
            f"{r['short']} {'+' if r['pct_change'] >= 0 else ''}{r['pct_change']:.2f}%" for r in summary["flagged"]
        )
        flag_line = f"{len(summary['flagged'])} fund(s) flagged this session (>{FLAG_THRESHOLD_PCT:.0f}% move): {names}."
    elif summary["largest_mover"]:
        m = summary["largest_mover"]
        flag_line = (
            f"No funds flagged this session (largest move: {m['short']} "
            f"{'+' if m['pct_change'] >= 0 else ''}{m['pct_change']:.2f}%)."
        )
    else:
        flag_line = "No day-over-day comparison available yet for any fund."

    return (
        f"{value_line} {freshness_line} {flag_line}\n\n"
        f"The illustrated dashboard-style PDF (with charts) is saved to the repo's "
        f"reports/ folder on GitHub — not attached here to keep this email lightweight.\n\n"
        f"Live self-updating dashboard: {DASHBOARD_URL}"
    )


def render_trend_chart(results: list[dict], out_path: str) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=150)
    colors = ["#2f6fed", "#e08a2c", "#1f7a4d"]
    for r, color in zip(results, colors):
        if not r["history"]:
            continue
        xs = [h["date"] for h in r["history"]]
        ys = [h["nav"] for h in r["history"]]
        ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.6, label=r["short"], color=color)
    ax.set_ylabel("NAV (RM)")
    ax.tick_params(axis="x", rotation=40, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", linestyle="-", linewidth=0.5, color="#eeece7")
    fig.subplots_adjust(bottom=0.42, top=0.95, left=0.1, right=0.97)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=1, fontsize=8, frameon=False)
    fig.savefig(out_path, facecolor="white")
    plt.close(fig)


def _line(pdf: FPDF, h: float, text: str) -> None:
    pdf.cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _wrapped(pdf: FPDF, h: float, text: str) -> None:
    pdf.multi_cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def build_pdf(results: list[dict], summary: dict, run_date: date, chart_path: str) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    _line(pdf, 10, "Principal Funds - Daily NAV Dashboard")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    _line(pdf, 6, f"Report date: {run_date.strftime('%d %B %Y')} (Asia/Kuala_Lumpur)")
    _line(pdf, 6, "Source: principal.com.my (public NAV history)")
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)

    # Summary cards
    pdf.set_font("Helvetica", "B", 12)
    diff, pct = summary["diff"], summary["pct"]
    color = (0, 128, 0) if diff >= 0 else (200, 0, 0)
    sign = "+" if diff >= 0 else ""
    _line(pdf, 7, f"Total portfolio value: RM {summary['total_today']:,.2f}")
    pdf.set_text_color(*color)
    _line(pdf, 7, f"Change vs yesterday: {sign}RM {diff:,.2f} ({sign}{pct:.2f}%)")
    pdf.set_text_color(0, 0, 0)
    flagged_n = len(summary["flagged"])
    _line(pdf, 7, f"Funds flagged (>{FLAG_THRESHOLD_PCT:.0f}% move): {flagged_n} of {len(results)}")
    pdf.ln(4)

    # Trend chart
    if os.path.exists(chart_path):
        pdf.image(chart_path, x=15, w=180)
        pdf.ln(3)

    # Per-fund table
    pdf.set_font("Helvetica", "B", 10)
    col_w = [70, 22, 28, 24, 24, 22]
    headers = ["Fund", "NAV", "Est. Value", "1-day", "NAV date", "Flag"]
    for w, h in zip(col_w, headers):
        pdf.cell(w, 7, h, border=1)
    pdf.ln(7)
    pdf.set_font("Helvetica", "", 9)
    for r in results:
        if r["error"] or not r["latest"]:
            pdf.cell(sum(col_w), 7, f"{r['short']}: data unavailable ({r['error'] or 'no rows'})", border=1)
            pdf.ln(7)
            continue
        latest = r["latest"]
        value = latest["nav"] * r["units"]
        pct_txt = "n/a"
        flag_txt = ""
        if r["pct_change"] is not None:
            pct_txt = f"{'+' if r['pct_change'] >= 0 else ''}{r['pct_change']:.2f}%"
            flag_txt = "FLAG" if abs(r["pct_change"]) > FLAG_THRESHOLD_PCT else "OK"
        row = [
            r["short"],
            f"{latest['nav']:.4f}",
            f"{value:,.0f}",
            pct_txt,
            latest["date"].strftime("%d %b"),
            flag_txt,
        ]
        for w, val in zip(col_w, row):
            pdf.cell(w, 7, val, border=1)
        pdf.ln(7)

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(130, 130, 130)
    _wrapped(
        pdf,
        5,
        "Automated report generated by a scheduled GitHub Actions workflow. NAV figures are "
        "sourced from principal.com.my's public NAV history export and reflect the most recent "
        "business day for which data has been published; this may lag today's date by one or "
        "more business days. Estimated value = published NAV x unit holdings.",
    )

    return bytes(pdf.output())


def send_email(narrative: str, run_date: date) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    from_addr = os.environ["RESEND_FROM"]
    to_addrs = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    html_body = "".join(f"<p>{para}</p>" for para in narrative.split("\n\n"))
    html_body = html_body.replace(
        DASHBOARD_URL, f'<a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>'
    )

    payload = {
        "from": from_addr,
        "to": to_addrs,
        "subject": f"Principal Funds NAV Dashboard - {run_date.strftime('%d %b %Y')}",
        "html": f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">{html_body}</div>',
        "text": narrative,
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
    summary = build_portfolio_summary(results)

    for r in results:
        if r["error"]:
            print(f"[warn] {r['name']}: {r['error']}", file=sys.stderr)
        elif r["latest"]:
            print(f"  {r['short']}: NAV {r['latest']['nav']:.4f} as of {r['latest']['date']} "
                  f"({r['pct_change']:+.2f}%)" if r["pct_change"] is not None
                  else f"  {r['short']}: NAV {r['latest']['nav']:.4f} as of {r['latest']['date']}")

    chart_path = "nav_trend_chart.png"
    render_trend_chart(results, chart_path)

    pdf_bytes = build_pdf(results, summary, today, chart_path)
    os.makedirs("reports", exist_ok=True)
    out_path = f"reports/principal-funds-dashboard-{today.isoformat()}.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")

    narrative = build_narrative(results, summary, today)
    print("---- narrative ----")
    print(narrative)
    print("--------------------")

    send_email(narrative, today)


if __name__ == "__main__":
    main()
