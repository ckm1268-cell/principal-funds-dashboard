# Principal Funds Daily NAV Dashboard (GitHub Actions)

Runs Mon-Fri (skipping Malaysian public holidays automatically), fetches the
latest NAV for these three Principal Asset Management (Malaysia) unit trust
funds from principal.com.my's public NAV history, computes portfolio value
(NAV x your unit holdings) and its day-over-day change, builds an illustrated
PDF with a NAV trend chart (`reports/principal-funds-dashboard-<date>.pdf`,
committed into the repo), and emails a short narrative summary via Resend —
matching the wording style of the reports this project used to send from a
Claude scheduled task, rather than a plain data table:

1. Principal Islamic Asia Pacific Dynamic Equity Fund
2. Principal DALI Asia Pacific Equity Growth Fund
3. Principal Greater China Equity Fund (Class MYR)

This replaces a Claude-side scheduled job with a self-contained GitHub
Actions workflow — once set up, it runs entirely on GitHub's infrastructure
and doesn't depend on this (or any) Claude session being active.

## What's in this bundle

```
.github/workflows/principal-funds-daily.yml   the scheduled workflow
scripts/fetch_nav_and_report.py               fetch → compute → PDF → email
requirements.txt                              Python dependencies
README.md                                     this file
```

## 1. Put these files in a GitHub repo

I can't push to your GitHub directly from this session (no GitHub connection
is set up here), so:

- Create a new repository (can be private), or pick an existing one.
- Copy these files in, preserving the folder structure exactly as above
  (the workflow must live at `.github/workflows/principal-funds-daily.yml`).
- Commit and push.

## 2. Set up Resend (the email sender)

You chose **Resend's API** for sending. One important limitation to know
up front:

> Resend's shared `onboarding@resend.dev` address can only send to the
> email address on your own Resend account — sending to any other address
> (like a second Gmail inbox) returns a 403 error. Since you want the report
> sent to **two** addresses (ckm1268@gmail.com and lyn1268@gmail.com), you
> need to verify your own domain in Resend and send from an address on it
> (e.g. `reports@yourdomain.com`).

Steps:

1. Sign up at [resend.com](https://resend.com) if you don't have an account.
2. In the Resend dashboard, go to **Domains → Add Domain**, add a domain you
   own, and add the DNS records it gives you (SPF/DKIM). This can take a few
   minutes to a few hours to verify depending on your DNS provider.
3. Once verified, create an API key (**API Keys → Create API Key**).
4. Decide on a sending address on that domain, e.g. `funds@yourdomain.com`.

If you don't have a domain to verify, let me know and I can help you think
through alternatives (e.g. a cheap domain just for this, or switching to
Gmail SMTP instead, which sends from your existing Gmail address but has
its own setup — an App Password and slightly different code).

## 3. Add GitHub repo secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add:

| Secret name        | Value                                              |
|---------------------|-----------------------------------------------------|
| `RESEND_API_KEY`   | the API key from step 2                            |
| `RESEND_FROM`      | e.g. `Principal Funds Report <funds@yourdomain.com>` |
| `EMAIL_TO`         | `ckm1268@gmail.com,lyn1268@gmail.com`              |

## 4. Enable Actions and test it

1. Go to the **Actions** tab in your repo and enable workflows if prompted.
2. Select **Principal Funds Daily NAV Dashboard** in the left sidebar, click
   **Run workflow** (workflow_dispatch) to trigger it manually right away —
   don't wait for tomorrow's schedule to find out if something's
   misconfigured.
3. Check the run logs. If email sending fails, the log will show the exact
   error from Resend (e.g. domain not verified, bad API key).
4. Check your inbox (and spam folder, for the first send) for the report.
   The email itself is plain text/HTML only — the PDF is **not** attached,
   to keep it lightweight, matching how the old Claude-scheduled reports
   worked.
5. The illustrated PDF lands in the repo under `reports/` (the workflow
   commits it automatically), and is also attached to the workflow run
   itself as a downloadable artifact for 30 days — useful for debugging
   without needing email to work first.

## 5. How the schedule works

- Cron: `0 1 * * 1-5` → 01:00 UTC, Mon-Fri = **9:00 AM Malaysia time**
  (Malaysia has no daylight saving, so this stays fixed year-round).
- The script independently double-checks the date and calls the free
  [Nager.Date](https://date.nager.at) public holiday API for Malaysia; if
  today is a public holiday it skips sending (logged, not an error). If that
  holiday check itself fails (e.g. API down), it proceeds anyway rather than
  silently skipping a real trading day.
- Manually running the workflow via "Run workflow" defaults to
  `force_run: true`, which bypasses both the weekend and holiday checks —
  handy for testing on any day.

## 6. Where the NAV data comes from

Each fund's public factsheet page on principal.com.my exposes a CSV export
of its NAV history (used for the "NAV History" table + CSV-download button
you see on the page). The script calls that CSV endpoint directly for each
fund, e.g.:

```
https://www.principal.com.my/en/nav/1280?field_fund_nav_date_value[min]=...&field_fund_nav_date_value[max]=...&_format=csv
```

It keeps the most recent 15 sessions per fund (for the trend chart) and uses
the last two to report the latest NAV, its date, and the % change versus the
previous available NAV — which is also how the report communicates "how
current" the data is. Portfolio value is then NAV x unit holdings (same
`units` / `baseline_nav` figures used by the GitHub Pages dashboard, so both
reports agree), and a fund is "flagged" if its day-over-day move exceeds 2%.
If Principal's site structure changes and a fund's fetch ever fails, that
fund is shown as "data unavailable" and excluded from the portfolio total
rather than breaking the whole report.

Note the NAV itself is Principal's **published fund NAV** (fetched live,
same as the GitHub Pages dashboard); the `units` and `baseline_nav` figures
are the personal holdings you originally shared, hardcoded into the script
since a GitHub Actions runner can't log into your personal Principal
account.

## 7. Retiring the old approach

There's currently no active Claude-side scheduled task for this project (I
checked), so there's nothing to cancel there. If you had been running this
check manually or through another tool, you can stop once you've confirmed
a couple of successful GitHub Actions runs land in your inbox.
