#!/usr/bin/env python3
"""
Daily NAV updater for the Principal Malaysia Funds dashboard (index.html).

Fetches the official NAV History CSV export for each of the three funds
directly from principal.com.my, rebuilds the dashboard's embedded data
(realHistory / fundsSnapshot), and rewrites index.html in place.

Run by .github/workflows/daily.yml on a schedule. No API keys or secrets
required — the CSV endpoints are public.

Data-integrity rules (do not relax these):
  - Never fabricate a NAV point. If a fund has not posted a new NAV since
    the last run, its latest history point simply repeats — that's correct,
    not a bug (Principal typically publishes NAV with a 1-2 business day lag).
  - The "nav" and "dailyChangePct" shown in the summary cards/table must
    always equal the LAST point in that fund's own history array.
  - Today / Yesterday / Past Two Days tabs each show a different real
    historical NAV session per fund (offsets 0/1/2 back from the latest
    point in that fund's history array) via snapshotAtOffset() in
    index.html's script. If two consecutive sessions happen to share the
    same NAV (no new print published), that's a real coincidence, not a
    bug -- but the tabs must never all point at the identical index.
"""

import csv
import io
import re
import sys
import urllib.request
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Fund configuration
# ---------------------------------------------------------------------------

FUNDS = [
    {
        "key": "islamic",
        "name": "Islamic Asia Pacific Dynamic Equity",
        "isin": "MYU1000AA007",
        "units": 74474.83,
        "baseline_nav": 1.0396,
        "csv_url": "https://www.principal.com.my/en/nav/1280?page&_format=csv",
    },
    {
        "key": "dali",
        "name": "DALI Asia Pacific Equity Growth",
        "isin": "MYU1000BD009",
        "units": 76816.72,
        "baseline_nav": 0.9549,
        "csv_url": "https://www.principal.com.my/en/nav/1270?page&_format=csv",
    },
    {
        "key": "greaterChina",
        "name": "Greater China Equity (Class MYR)",
        "isin": "MYU1000CB001",
        "units": 42026.20,
        "baseline_nav": 1.2895,
        "csv_url": "https://www.principal.com.my/en/nav/6677?page&_format=csv",
    },
]

HISTORY_POINTS = 15  # how many trading sessions to keep for the trend chart
HTML_PATH = "index.html"
USER_AGENT = (
    "Mozilla/5.0 (compatible; PrincipalFundsDashboardBot/1.0; "
    "+https://github.com/) NAV-tracker for personal portfolio monitoring"
)


def fetch_csv_rows(url):
    """Download a Principal NAV-history CSV and return (date, nav) pairs,
    oldest first, as (datetime.date, float)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig", errors="replace")

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        raise RuntimeError(f"Empty CSV from {url}")

    header = [h.strip().lower() for h in rows[0]]
    date_idx = next((i for i, h in enumerate(header) if "date" in h), 0)
    nav_idx = next((i for i, h in enumerate(header) if "nav" in h), 1)

    out = []
    for row in rows[1:]:
        if len(row) <= max(date_idx, nav_idx):
            continue
        date_str = row[date_idx].strip()
        nav_str = row[nav_idx].strip()
        if not date_str or not nav_str:
            continue
        try:
            d = datetime.strptime(date_str, "%d-%m-%Y").date()
            nav = float(nav_str)
        except ValueError:
            continue
        out.append((d, nav))

    out.sort(key=lambda x: x[0])
    if not out:
        raise RuntimeError(f"No parseable NAV rows from {url}")
    return out


def build_fund_data(fund):
    rows = fetch_csv_rows(fund["csv_url"])
    recent = rows[-HISTORY_POINTS:]
    history_js = ",".join(f'["{d.strftime("%d-%m")}",{nav:.4f}]' for d, nav in recent)

    latest_date, latest_nav = recent[-1]
    if len(recent) >= 2:
        prev_nav = recent[-2][1]
        daily_change_pct = (latest_nav - prev_nav) / prev_nav * 100 if prev_nav else 0.0
    else:
        daily_change_pct = 0.0

    return {
        **fund,
        "history_js": f"[{history_js}]",
        "latest_date": latest_date,
        "nav": latest_nav,
        "daily_change_pct": daily_change_pct,
        "recent": recent,
    }


def fmt_subtitle(latest_date, checked_date):
    return (
        f"Published NAV as of {latest_date.strftime('%-d %b %Y')} "
        f"· checked {checked_date.strftime('%-d %b %Y')}"
    )


def inject(html, replacements):
    for pattern, value in replacements:
        new_html, n = re.subn(pattern, value, html, count=1, flags=re.S)
        if n == 0:
            raise RuntimeError(f"Template pattern not found (site structure may have "
                                f"changed): {pattern[:80]}...")
        html = new_html
    return html


def main():
    today = datetime.utcnow() + timedelta(hours=8)  # Asia/Kuala_Lumpur is UTC+8
    today = today.date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    print(f"Run date (MYT): {today}")
    funds = []
    for f in FUNDS:
        print(f"Fetching {f['name']} ...")
        data = build_fund_data(f)
        print(f"  latest NAV {data['nav']:.4f} as of {data['latest_date']} "
              f"({data['daily_change_pct']:+.2f}%)")
        funds.append(data)

    with open(HTML_PATH, "r", encoding="utf-8") as fh:
        html = read_html = fh.read()

    # 1. realHistory object
    history_entries = ",\n  ".join(
        f'{f["key"]}: {f["history_js"]}' for f in funds
    )
    html = re.sub(
        r"const realHistory = \{.*?\};",
        f"const realHistory = {{\n  {history_entries}\n}};",
        html, count=1, flags=re.S,
    )

    # 2. fundsSnapshot() literal (name/isin/units/baselineNav preserved as
    #    constants; nav + dailyChangePct come from the freshest fetch)
    fund_lines = []
    for f in funds:
        fund_lines.append(
            '    { name: "%s", isin: "%s", units: %s, baselineNav: %s, '
            'nav: %.4f, dailyChangePct: %.2f, history: realHistory.%s }'
            % (f["name"], f["isin"], f["units"], f["baseline_nav"],
               f["nav"], f["daily_change_pct"], f["key"])
        )
    snapshot_body = ",\n".join(fund_lines)
    html = re.sub(
        r"function fundsSnapshot\(\) \{\s*return \[.*?\];\s*\}",
        f"function fundsSnapshot() {{\n  return [\n{snapshot_body}\n  ];\n}}",
        html, count=1, flags=re.S,
    )

    # 3. Subtitles for each tab -- each tab shows the actual historical NAV
    # date for that offset (0 = latest/"today", 1 = "yesterday", 2 = "2 days
    # ago"), matching the snapshotAtOffset() logic in index.html's script.
    islamic_recent = next(f for f in funds if f["key"] == "islamic")["recent"]
    def published_date_at(offset):
        idx = max(len(islamic_recent) - 1 - offset, 0)
        return islamic_recent[idx][0]
    for tab_id, checked, offset in (("today", today, 0), ("yesterday", yesterday, 1), ("2days", two_days_ago, 2)):
        published = published_date_at(offset)
        html = re.sub(
            rf'(id="subtitle-{tab_id}">)[^<]*(</div>)',
            lambda m, p=published, c=checked: m.group(1) + fmt_subtitle(p, c) + m.group(2),
            html, count=1,
        )

    # 4. Footer "last updated" stamp (added just before </body> the first
    #    time; afterwards, replaced in place)
    stamp = f"Auto-updated by GitHub Actions · last run {today.strftime('%-d %b %Y')} (Asia/Kuala_Lumpur)"
    if 'id="last-updated"' in html:
        html = re.sub(
            r'(id="last-updated">)[^<]*(</div>)',
            lambda m: m.group(1) + stamp + m.group(2),
            html, count=1,
        )
    else:
        html = html.replace(
            "</body>",
            f'<div id="last-updated" style="text-align:center;font-size:11px;'
            f'color:#a8a8a4;padding-bottom:16px;">{stamp}</div>\n</body>',
        )

    if html == read_html:
        print("WARNING: no changes were made to index.html — check template patterns.")
    else:
        with open(HTML_PATH, "w", encoding="utf-8") as fh:
            fh.write(html)
        print("index.html updated.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
