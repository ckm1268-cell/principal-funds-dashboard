#!/usr/bin/env python3
"""
Principal Funds daily pipeline — single source of truth for NAV data used by
BOTH the GitHub Pages dashboard (index.html) and the narrative email report.

Why this file exists: this repo used to run two independent workflows that
each fetched NAV from principal.com.my and each committed to `main`. Same
schedule + two independent git pushes = a real race condition (second push
gets rejected), and two independent fetches could in principle disagree with
each other on the same day. Merging into one fetch -> two consumers removes
both problems and halves the load on Principal's public endpoint.

Run as two subcommands from the workflow, with the git commit happening in
between them:

    python scripts/daily_pipeline.py build
        Fetch NAV for all 3 funds (once), update index.html, build the
        illustrated PDF + trend chart into reports/, and cache the narrative
        text + run date to .pipeline_state.json for the notify step. Writes
        `skip=true|false` to $GITHUB_OUTPUT so the workflow can skip the
        commit/email steps on a non-trading day without treating it as an
        error.

    python scripts/daily_pipeline.py notify
        Read .pipeline_state.json (written by `build`) and send the
        narrative email via Resend. Deliberately does NOT redo the fetch —
        it reports exactly what `build` already computed and committed.

This ordering matters: the dashboard/PDF are committed to the repo BEFORE
the email is attempted, so a Resend outage or bad API key never blocks the
dashboard update — only the (best-effort) notification.
"""

import base64
import csv
import io
import json
import os
import re
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

# Single fund configuration shared by the dashboard (index.html) and the
# email report. units / baseline_nav are your personal holdings (shared
# across both outputs so they can never drift apart from each other).
FUNDS = [
    {
        "key": "islamic",
        "name": "Principal Islamic Asia Pacific Dynamic Equity Fund",
        "short": "Islamic Asia Pacific Dynamic Equity",
        "isin": "MYU1000AA007",
        "nav_node": 1280,
        "units": 74474.83,
        "baseline_nav": 1.0396,
    },
    {
        "key": "dali",
        "name": "Principal DALI Asia Pacific Equity Growth Fund",
        "short": "DALI Asia Pacific Equity Growth",
        "isin": "MYU1000BD009",
        "nav_node": 1270,
        "units": 76816.72,
        "baseline_nav": 0.9549,
    },
    {
        "key": "greaterChina",
        "name": "Principal Greater China Equity Fund (Class MYR)",
        "short": "Greater China Equity (Class MYR)",
        "isin": "MYU1000CB001",
        "nav_node": 6677,
        "units": 42026.20,
        "baseline_nav": 1.2895,
    },
]

NAV_CSV_URL = "https://www.principal.com.my/en/nav/{node}"
HOLIDAY_API_URL = "https://date.nager.at/api/v3/publicholidays/{year}/MY"
DASHBOARD_URL = "https://ckm1268-cell.github.io/principal-funds-dashboard/"
FLAG_THRESHOLD_PCT = 2.0
HISTORY_POINTS = 15  # trading sessions kept for the trend chart / index.html
HTML_PATH = "index.html"
STATE_PATH = ".pipeline_state.json"


def set_github_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"[output] {key}={value}")


# ---------------------------------------------------------------------------
# Fetch (single source of truth for both outputs)
# ---------------------------------------------------------------------------

def is_working_day(today: date) -> tuple[bool, str]:
    if today.weekday() >= 5:  # 5=Sat, 6=Sun
        return False, "weekend"
    try:
        resp = requests.get(HOLIDAY_API_URL.format(year=today.year), timeout=15)
        resp.raise_for_status()
        holidays = {h["date"] for h in resp.json()}
        if today.isoformat() in holidays:
            return False, "Malaysian public holiday"
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] holiday check failed ({exc}); proceeding anyway", file=sys.stderr)
    return True, ""


def fetch_nav_history(node_id: int, days_back: int = 35) -> list[dict]:
    """Fetch recent NAV rows for one fund, oldest first. The date-range params
    put today's date in the URL, so the request naturally busts any
    per-URL CDN caching on Principal's side (a real issue we hit with the
    old no-date-filter fetch, which could return the same cached response
    for days)."""
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
    if not rows:
        resp = requests.get(NAV_CSV_URL.format(node=node_id), params={"_format": "csv"}, timeout=30)
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
                prev_nav = rows[-2]["nav"]
                if prev_nav:
                    entry["pct_change"] = (rows[-1]["nav"] - prev_nav) / prev_nav * 100
            if not rows:
                entry["error"] = "No NAV data returned"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        results.append(entry)
    return results


def build_portfolio_summary(results: list[dict]) -> dict:
    ok = [r for r in results if r["latest"] and not r["error"]]
    unavailable = [r for r in results if r["error"] or not r["latest"]]

    total_today = sum(r["latest"]["nav"] * r["units"] for r in ok)
    total_prev = sum((r["previous"]["nav"] if r["previous"] else r["latest"]["nav"]) * r["units"] for r in ok)
    diff = total_today - total_prev
    pct = (diff / total_prev * 100) if total_prev else 0.0

    max_date = max((r["latest"]["date"] for r in ok), default=None)
    lagging = [r for r in ok if r["latest"]["date"] != max_date]

    flagged = [r for r in ok if r["pct_change"] is not None and abs(r["pct_change"]) > FLAG_THRESHOLD_PCT]
    movers = [r for r in ok if r["pct_change"] is not None]
    largest_mover = max(movers, key=lambda r: abs(r["pct_change"])) if movers else None

    return {
        "total_today": total_today, "total_prev": total_prev, "diff": diff, "pct": pct,
        "max_date": max_date, "lagging": lagging, "flagged": flagged,
        "largest_mover": largest_mover, "unavailable": unavailable,
    }


# ---------------------------------------------------------------------------
# Consumer 1: index.html (GitHub Pages dashboard)
# ---------------------------------------------------------------------------

def update_dashboard_html(results: list[dict], today: date) -> bool:
    """Rewrite index.html in place. Returns True if it changed."""
    if not os.path.exists(HTML_PATH):
        print(f"[warn] {HTML_PATH} not found — skipping dashboard update", file=sys.stderr)
        return False

    with open(HTML_PATH, "r", encoding="utf-8") as fh:
        html = read_html = fh.read()

    def safe_sub(pattern, repl, html, label, flags=0):
        new_html, n = re.subn(pattern, repl, html, count=1, flags=flags)
        if n == 0:
            print(f"[warn] template pattern not found for '{label}' — site structure may have "
                  f"drifted; that section was left unchanged.", file=sys.stderr)
            return html
        return new_html

    # 1. realHistory object
    def _history_array(history):
        points = []
        for h in history:
            day_label = h["date"].strftime("%d-%m")
            points.append('["%s",%.4f]' % (day_label, h["nav"]))
        return "[" + ",".join(points) + "]"

    history_entries = ",\n  ".join(
        "%s: %s" % (r["key"], _history_array(r["history"]))
        for r in results if r["history"]
    )
    html = safe_sub(r"const realHistory = \{.*?\};",
                     f"const realHistory = {{\n  {history_entries}\n}};", html, "realHistory", flags=re.S)

    # 2. fundsSnapshot() literal
    fund_lines = []
    for r in results:
        if not r["latest"]:
            continue
        fund_lines.append(
            '    { name: "%s", isin: "%s", units: %s, baselineNav: %s, '
            'nav: %.4f, dailyChangePct: %.2f, history: realHistory.%s }'
            % (r["name"], r["isin"], r["units"], r["baseline_nav"],
               r["latest"]["nav"], r["pct_change"] or 0.0, r["key"])
        )
    snapshot_body = ",\n".join(fund_lines)
    html = safe_sub(r"function fundsSnapshot\(\) \{\s*return \[.*?\];\s*\}",
                     f"function fundsSnapshot() {{\n  return [\n{snapshot_body}\n  ];\n}}",
                     html, "fundsSnapshot", flags=re.S)

    # 3. Per-tab subtitles (today/yesterday/2days), using the Islamic fund's
    # own history as the reference session list (same convention as before).
    ref = next((r for r in results if r["key"] == "islamic" and r["history"]), None)
    if ref:
        yesterday, two_days_ago = today - timedelta(days=1), today - timedelta(days=2)
        for tab_id, checked, offset in (("today", today, 0), ("yesterday", yesterday, 1), ("2days", two_days_ago, 2)):
            idx = max(len(ref["history"]) - 1 - offset, 0)
            published = ref["history"][idx]["date"]
            subtitle = (f"Published NAV as of {published.strftime('%-d %b %Y')} "
                        f"· checked {checked.strftime('%-d %b %Y')}")
            html = safe_sub(rf'(id="subtitle-{tab_id}">)[^<]*(</div>)',
                             lambda m, s=subtitle: m.group(1) + s + m.group(2),
                             html, f"subtitle-{tab_id}")

    # 4. Footer "last updated" stamp. Pattern tolerates any attributes
    # between id="last-updated" and the closing '>' (a previous version of
    # this regex required an exact match and silently stopped updating for
    # weeks once a style attribute was added to that div).
    stamp = f"Auto-updated by GitHub Actions · last run {today.strftime('%-d %b %Y')} (Asia/Kuala_Lumpur)"
    if re.search(r'id="last-updated"', html):
        html = safe_sub(r'(id="last-updated"[^>]*>)[^<]*(</div>)',
                         lambda m: m.group(1) + stamp + m.group(2), html, "last-updated")
    else:
        html = html.replace(
            "</body>",
            f'<div id="last-updated" style="text-align:center;font-size:11px;'
            f'color:#a8a8a4;padding-bottom:16px;">{stamp}</div>\n</body>',
        )

    if html == read_html:
        print("No changes to index.html this run.")
        return False
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("index.html updated.")
    return True


# ---------------------------------------------------------------------------
# Consumer 2: narrative email + illustrated PDF
# ---------------------------------------------------------------------------

def build_narrative(summary: dict) -> str:
    diff, pct = summary["diff"], summary["pct"]
    if diff >= 0:
        value_line = (f"Total portfolio value is RM {summary['total_today']:,.2f}, "
                       f"up +RM {diff:,.2f} (+{pct:.2f}%) vs yesterday.")
    else:
        value_line = (f"Total portfolio value is RM {summary['total_today']:,.2f}, "
                       f"down RM {abs(diff):,.2f} ({pct:.2f}%) vs yesterday.")

    if summary["max_date"] is None:
        freshness_line = "NAV data could not be retrieved for any fund this run."
    elif not summary["lagging"]:
        freshness_line = f"All three funds are now published through {summary['max_date'].strftime('%-d %b %Y')}."
    else:
        lag_names = ", ".join(f"{r['short']} ({r['latest']['date'].strftime('%-d %b %Y')})" for r in summary["lagging"])
        freshness_line = (f"Published through {summary['max_date'].strftime('%-d %b %Y')} for most funds; "
                           f"still lagging: {lag_names}.")

    if summary["unavailable"]:
        names = ", ".join(r["name"] for r in summary["unavailable"])
        freshness_line += f" Note: could not fetch data for {names} this run — excluded from the total above."

    if summary["flagged"]:
        names = ", ".join(f"{r['short']} {'+' if r['pct_change'] >= 0 else ''}{r['pct_change']:.2f}%"
                           for r in summary["flagged"])
        flag_line = f"{len(summary['flagged'])} fund(s) flagged this session (>{FLAG_THRESHOLD_PCT:.0f}% move): {names}."
    elif summary["largest_mover"]:
        m = summary["largest_mover"]
        flag_line = (f"No funds flagged this session (largest move: {m['short']} "
                     f"{'+' if m['pct_change'] >= 0 else ''}{m['pct_change']:.2f}%).")
    else:
        flag_line = "No day-over-day comparison available yet for any fund."

    return (
        f"{value_line} {freshness_line} {flag_line}\n\n"
        f"The illustrated dashboard-style PDF (with charts) is attached to this email, "
        f"and also saved to the repo's reports/ folder on GitHub.\n\n"
        f"Live self-updating dashboard: {DASHBOARD_URL}"
    )


def render_trend_chart(results: list[dict], out_path: str) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=150)
    colors = ["#2f6fed", "#e08a2c", "#1f7a4d"]
    # Stagger each fund's value labels above/below its markers (alternating,
    # with growing offset) so 3 overlapping lines don't stack labels on top
    # of one another.
    label_offsets = [8, -11, 14]
    for idx, (r, color) in enumerate(zip(results, colors)):
        if not r["history"]:
            continue
        xs = [h["date"] for h in r["history"]]
        ys = [h["nav"] for h in r["history"]]
        ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.6, label=r["short"], color=color)
        dy = label_offsets[idx % len(label_offsets)]
        for x, y in zip(xs, ys):
            ax.annotate(
                f"{y:.4f}", (x, y), textcoords="offset points", xytext=(0, dy),
                ha="center", va=("bottom" if dy >= 0 else "top"),
                fontsize=6, color=color,
            )
    ax.set_ylabel("NAV (RM)")
    ax.tick_params(axis="x", rotation=40, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", linestyle="-", linewidth=0.5, color="#eeece7")
    ax.margins(y=0.22)  # extra vertical headroom so the value labels aren't clipped
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

    pdf.set_font("Helvetica", "B", 12)
    diff, pct = summary["diff"], summary["pct"]
    color = (0, 128, 0) if diff >= 0 else (200, 0, 0)
    sign = "+" if diff >= 0 else ""
    _line(pdf, 7, f"Total portfolio value: RM {summary['total_today']:,.2f}")
    pdf.set_text_color(*color)
    _line(pdf, 7, f"Change vs yesterday: {sign}RM {diff:,.2f} ({sign}{pct:.2f}%)")
    pdf.set_text_color(0, 0, 0)
    _line(pdf, 7, f"Funds flagged (>{FLAG_THRESHOLD_PCT:.0f}% move): {len(summary['flagged'])} of {len(results)}")
    pdf.ln(4)

    if os.path.exists(chart_path):
        pdf.image(chart_path, x=15, w=180)
        pdf.ln(3)

    pdf.set_font("Helvetica", "B", 10)
    col_w = [70, 22, 28, 24, 24, 22]
    for w, h in zip(col_w, ["Fund", "NAV", "Est. Value", "1-day", "NAV date", "Flag"]):
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
        pct_txt, flag_txt = "n/a", ""
        if r["pct_change"] is not None:
            pct_txt = f"{'+' if r['pct_change'] >= 0 else ''}{r['pct_change']:.2f}%"
            flag_txt = "FLAG" if abs(r["pct_change"]) > FLAG_THRESHOLD_PCT else "OK"
        row = [r["short"], f"{latest['nav']:.4f}", f"{value:,.0f}", pct_txt, latest["date"].strftime("%d %b"), flag_txt]
        for w, val in zip(col_w, row):
            pdf.cell(w, 7, val, border=1)
        pdf.ln(7)

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(130, 130, 130)
    _wrapped(pdf, 5,
        "Automated report generated by a scheduled GitHub Actions workflow. NAV figures are "
        "sourced from principal.com.my's public NAV history export and reflect the most recent "
        "business day for which data has been published; this may lag today's date by one or "
        "more business days. Estimated value = published NAV x unit holdings.")
    return bytes(pdf.output())


def send_email(narrative: str, run_date: date, pdf_path: str | None = None) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    from_addr = os.environ["RESEND_FROM"]
    to_addrs = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    html_body = "".join(f"<p>{para}</p>" for para in narrative.split("\n\n"))
    html_body = html_body.replace(DASHBOARD_URL, f'<a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>')

    payload = {
        "from": from_addr,
        "to": to_addrs,
        "subject": f"Principal Funds NAV Dashboard - {run_date.strftime('%d %b %Y')}",
        "html": f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">{html_body}</div>',
        "text": narrative,
    }

    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        payload["attachments"] = [{
            "filename": os.path.basename(pdf_path),
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "content_type": "application/pdf",
        }]
    elif pdf_path:
        print(f"[warn] PDF not found at {pdf_path} — sending email without attachment", file=sys.stderr)

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    if resp.status_code >= 300:
        print(f"[error] Resend API returned {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    print(f"Email sent: {resp.json()}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_build() -> None:
    today = datetime.now(MYT).date()

    if os.environ.get("FORCE_RUN") != "1":
        should_run, reason = is_working_day(today)
        if not should_run:
            print(f"Skipping run: {reason} ({today.isoformat()})")
            set_github_output("skip", "true")
            return

    results = build_fund_results()
    summary = build_portfolio_summary(results)

    for r in results:
        if r["error"]:
            print(f"[warn] {r['name']}: {r['error']}", file=sys.stderr)
        elif r["latest"]:
            pct = f"{r['pct_change']:+.2f}%" if r["pct_change"] is not None else "n/a"
            print(f"  {r['short']}: NAV {r['latest']['nav']:.4f} as of {r['latest']['date']} ({pct})")

    update_dashboard_html(results, today)

    chart_path = "nav_trend_chart.png"
    render_trend_chart(results, chart_path)
    pdf_bytes = build_pdf(results, summary, today, chart_path)
    os.makedirs("reports", exist_ok=True)
    out_path = f"reports/principal-funds-dashboard-{today.isoformat()}.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")

    narrative = build_narrative(summary)
    print("---- narrative ----")
    print(narrative)
    print("--------------------")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": today.isoformat(), "narrative": narrative, "pdf_path": out_path}, f)
    set_github_output("skip", "false")


def cmd_notify() -> None:
    if not os.path.exists(STATE_PATH):
        print(f"[error] {STATE_PATH} not found — did the build step run first?", file=sys.stderr)
        sys.exit(1)
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    run_date = date.fromisoformat(state["date"])
    send_email(state["narrative"], run_date, pdf_path=state.get("pdf_path"))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("build", "notify"):
        print("Usage: python daily_pipeline.py [build|notify]", file=sys.stderr)
        sys.exit(2)
    {"build": cmd_build, "notify": cmd_notify}[sys.argv[1]]()
