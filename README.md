# Principal Funds Daily Pipeline (GitHub Actions)

Runs Mon-Fri (skipping Malaysian public holidays automatically), fetches the
latest NAV **once** for these three Principal Asset Management (Malaysia)
unit trust funds from principal.com.my's public NAV history, and drives
**both** of this project's outputs from that single fetch:

1. Updates the **GitHub Pages dashboard** (`index.html`) — the live
   self-updating dashboard at
   https://ckm1268-cell.github.io/principal-funds-dashboard/
2. Emails a short **narrative summary** via Resend, matching the wording
   style of the reports this project used to send from a Claude scheduled
   task, with the illustrated PDF (NAV trend chart + per-fund table,
   `reports/principal-funds-dashboard-<date>.pdf`) attached — a copy is
   also committed into the repo's `reports/` folder.

Funds tracked:

1. Principal Islamic Asia Pacific Dynamic Equity Fund
2. Principal DALI Asia Pacific Equity Growth Fund
3. Principal Greater China Equity Fund (Class MYR)

## Why one workflow instead of two

This repo used to run two independent, identically-scheduled workflows:
"Daily NAV dashboard update" (updated `index.html`) and "Principal Funds
Daily NAV Dashboard" (sent the email). Each fetched NAV independently and
each ended with its own `git commit && git push` to `main`. That meant:

- **A real git race condition.** Two workflow runs pushing to the same
  branch at the same scheduled minute — GitHub Actions does not serialize
  separate workflow files that happen to share a cron schedule, so the
  second push could be rejected as a non-fast-forward update.
- **A risk of disagreement.** Two independent fetches of the same "today's
  NAV" could, in principle, see different data (e.g. if Principal published
  an update between the two requests), so the dashboard and the email could
  report slightly different numbers for the same day.
- **Double the load** on Principal's public NAV endpoint for no benefit.

This is now a **single workflow** (`.github/workflows/principal-funds-daily.yml`)
running a **single script** (`scripts/daily_pipeline.py`) in two steps:

```
python scripts/daily_pipeline.py build     # fetch NAV once, update index.html,
                                            # build the illustrated PDF + chart
        |
        v
   git commit + push (index.html, reports/)
        |
        v
python scripts/daily_pipeline.py notify    # send the narrative email using
                                            # exactly what `build` just computed
                                            # and committed (no re-fetch)
```

Ordering matters: the dashboard and PDF are committed **before** the email
is attempted, so a Resend outage or a bad API key never blocks the dashboard
update — only the (best-effort) email notification. A `concurrency` group on
the workflow also guards against two runs (e.g. a manual "Run workflow"
overlapping the scheduled run) executing at the same time.

## What's in this bundle

```
.github/workflows/principal-funds-daily.yml   the single scheduled workflow
scripts/daily_pipeline.py                     fetch -> dashboard -> PDF -> email
requirements.txt                              Python dependencies
README.md                                     this file
```

`scripts/fetch_nav_and_report.py` (the old email-only script) and the old
`scripts/update_dashboard.py` / `.github/workflows/daily.yml` (the old
dashboard-only workflow) are retired — see "Applying this to your repo"
below for exactly what to remove.

## 1. Apply these files to your GitHub repo

Using PowerShell + git (as you've been doing):

```powershell
cd C:\Users\CKM1268\Downloads\principal-funds-dashboard
git pull

# Remove the two old scripts and the old dashboard-only workflow —
# daily_pipeline.py + principal-funds-daily.yml replace all of them.
git rm scripts\update_dashboard.py
git rm scripts\fetch_nav_and_report.py
git rm .github\workflows\daily.yml

# Copy in the new/updated files from this bundle:
#   scripts\daily_pipeline.py
#   .github\workflows\principal-funds-daily.yml
#   requirements.txt
#   README.md
# (copy them over the existing files/paths, preserving folder structure)

git add scripts\daily_pipeline.py .github\workflows\principal-funds-daily.yml requirements.txt README.md
git commit -m "Merge dashboard + email workflows into one pipeline"
git push
```

Your existing repo secrets (`RESEND_API_KEY`, `RESEND_FROM`, `EMAIL_TO`) are
unchanged and don't need to be re-entered.

## 2. Enable Actions and test it

1. Go to the **Actions** tab in your repo.
2. Select **Principal Funds Daily Pipeline** in the left sidebar (this
   replaces both "Daily NAV dashboard update" and "Principal Funds Daily NAV
   Dashboard" in that list — the old workflow entries disappear once their
   `.yml` files are removed from `main`), click **Run workflow** to trigger
   it manually right away.
3. Check the run logs — you should see the fetch, the dashboard update, one
   git commit covering both `index.html` and `reports/`, and then the email
   send, in that order.
4. Confirm the dashboard (https://ckm1268-cell.github.io/principal-funds-dashboard/)
   and the email report show the **same** NAV figures for the same day —
   that consistency is the main thing this merge guarantees.

## 3. How the schedule works

- Cron: `0 1 * * 1-5` → 01:00 UTC, Mon-Fri = **9:00 AM Malaysia time**
  (Malaysia has no daylight saving, so this stays fixed year-round).
- The script independently double-checks the date and calls the free
  [Nager.Date](https://date.nager.at) public holiday API for Malaysia; if
  today is a public holiday it skips the whole run (dashboard update, PDF,
  and email) — logged, not an error. If that holiday check itself fails
  (e.g. API down), it proceeds anyway rather than silently skipping a real
  trading day.
- Manually running the workflow via "Run workflow" defaults to
  `force_run: true`, which bypasses both the weekend and holiday checks —
  handy for testing on any day.

## 4. Where the NAV data comes from

Each fund's public factsheet page on principal.com.my exposes a CSV export
of its NAV history. The script calls that CSV endpoint directly for each
fund with an explicit date range ending today, which also naturally busts
any per-URL CDN caching on Principal's side. It keeps the most recent 15
trading sessions per fund — used both for the dashboard's trend chart data
(`realHistory` in `index.html`) and the emailed PDF's trend chart — and uses
the last two sessions to report the latest NAV, its date, and the % change
versus the previous available NAV.

Portfolio value is NAV x unit holdings (your personal holdings, hardcoded in
`FUNDS` in `daily_pipeline.py` since a GitHub Actions runner can't log into
your personal Principal account), and a fund is "flagged" if its
day-over-day move exceeds 2%. If a fund's fetch ever fails, it's shown as
"data unavailable" in both outputs and excluded from the portfolio total
rather than breaking the whole run.

## 5. Resend email setup — verified domain (resolved)

Resend's shared `onboarding@resend.dev` "from" address only delivers to the
email address on your own Resend account — sending to any second address
while `RESEND_FROM` uses that shared address either gets rejected outright,
or (as happened here) silently only delivers to the account owner while any
other address in `EMAIL_TO` is dropped. This is a restriction on the *from*
domain's verification status, not something a second "from" secret can work
around — an email only ever has one "From" address; it's the **to** field
that carries multiple recipients.

This is now fixed with a real verified domain:

- **Domain:** `myckmdomain.abrdns.com`, verified in Resend (Domains →
  verified, sending enabled). DNS records (SPF/DKIM) for it are hosted with
  **ClouDNS**, a production DNS provider — not a throwaway/free DNS
  sandbox — so the verification is stable and isn't at risk of silently
  lapsing.
- **`RESEND_FROM` repo secret** is set to an address on that domain, e.g.
  `Principal Funds Report <reports@myckmdomain.abrdns.com>`.
- **`EMAIL_TO` repo secret** is confirmed to contain both recipients,
  comma-separated: `ckm1268@gmail.com,lyn1268@gmail.com`.

With `RESEND_FROM` on a verified domain, Resend's sandbox restriction no
longer applies — both addresses in `EMAIL_TO` receive the report. Confirmed
directly against Resend's own send log (not just "should work"): the actual
delivered email now shows `From: reports@myckmdomain.abrdns.com` with both
Gmail addresses in `To`.

If a third recipient is ever needed, just add it to `EMAIL_TO` — no code or
domain changes required, since the script already splits that secret on
commas.

The email is plain text/HTML narrative with the illustrated PDF attached.
The same PDF also lands in `reports/` in the repo (committed automatically)
and as a 30-day workflow artifact, so it stays available even if an email
ever fails to send.
