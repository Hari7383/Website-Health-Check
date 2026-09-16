# Website Hygiene Check

A local tool that runs website-health scenarios from an uploaded Excel workbook,
fills in Status and Notes after review, and keeps a detailed timing/evidence audit log.

It replaces the manual pass over 14 checklist items. It does not replace the person: the
agent proposes a verdict for each row, you review and adjust, and only then is anything
written to the sheet.

## Quick start

**On a PC with nothing installed**, one command does everything — it installs Python if
missing, then the dependencies and browser:

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

**If Python is already installed**, skip straight to setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

`setup.ps1` runs once: it finds Python, builds an isolated `.venv`, installs the
dependencies and downloads Chromium. `run.ps1` starts the server and opens the browser.
Leave that window open — closing it stops the tool.

**Manual equivalent**, if you would rather not use the scripts:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe manage.py runserver
```

Requires **Python 3.10 or newer** (the code uses `X | None` type syntax). Chromium is
about 150 MB and downloads once.

Then open <http://127.0.0.1:5000>. It opens straight on the dashboard — there is no
sign-in step by default.

Set the **Checked by** name at the top of the dashboard. That name is written into the
**Checked By** column of the sheet and is remembered until you change it.

To put the tool behind accounts instead, set `REQUIRE_LOGIN = True` in `app/config.py`.
Sign-in and sign-up pages come back, and the Checked By name is taken from the signed-in
account rather than the dashboard field. Create accounts from the sign-up page or with
`python manage.py adduser`.

**Turn login on before running this anywhere other than your own machine.** Unauthenticated
is fine on `127.0.0.1`, where only you can reach it; on a network it lets anyone write to
your workbook.

## How a run works

1. **Upload the workbook.** The workbook must contain a `Checklist Item` header.
2. **Checklist Item is the source of truth.** Every non-empty cell below that header becomes
   one test scenario. Nothing is hardcoded by row count or by the old 14-item checklist.
3. **Run health check.** Known scenario types are executed in Chromium/HTTP. Unknown
   scenarios are explicitly marked for manual review rather than guessed.
4. **Review.** Every scenario shows the verdict, evidence and measured timing. Change any
   verdict or note before saving.
5. **Save to workbook.** The original checklist/status layout is preserved and a `Health Check Details`
   sheet is appended with scenario, status, load time, processing time, total time, expected/actual
   result, evidence and errors.
6. **Download result XLSX.** The original upload is never overwritten; the app creates a new result workbook containing the dated checklist block and a detailed `Health Check Details` sheet with timing, status and URL(s).

Each run executes in its own process. Playwright's sync API keeps per-thread state that
proved unreliable across repeated runs inside one long-lived server, so every run gets a
fresh interpreter. If a run dies, the progress page reports it instead of spinning.

The uploaded source workbook is never overwritten. The result is written to a new XLSX in the run folder, and re-running on the same date reuses that date's column inside the new result workbook.

## What is checked, and how honestly

| # | Checklist item | How it is verified |
|---|---|---|
| 1 | SIEM Sizing Calculator | Page loads; a form with usable inputs renders; reCAPTCHA present |
| 2 | Phone click-to-call | `tel:` links exist on homepage and contact page and are dialable |
| 3 | Calendly scheduling | Widget, script or link initialises — **booking is not performed** |
| 4 | Contact us form | Form renders with required fields, captcha and CSRF token |
| 5 | Get in touch form | Same, located by form content |
| 6 | RED checklist | Page returns 200 with real content |
| 7 | CTA buttons | Every call-to-action link on the homepage resolves |
| 8 | Landing pages | Each converts, via a working form or a working link to one |
| 9 | Page load speed | Navigation Timing at desktop and mobile viewports |
| 10 | Mobile responsiveness | 5 critical pages at 390px: horizontal overflow, tiny text |
| 11 | Navigation menus | Every internal menu link resolves |
| 12 | Cookie consent banner | Consent element, button or script present |
| 13 | Spelling/grammar | **Not automated** — you set this one |
| 14 | Images/media | Rendered image check: broken images and images that never load |

Three items are honest about their limits rather than guessing:

- **Calendly** confirms the widget loads. It does not book an appointment, so the
  confirmation email path stays untested. Book one manually now and then.
- **Forms** are verified for structure and validation, never submitted. Delivery to the
  inbox is not proven by this tool.
- **Spelling and grammar** has no reliable automated signal, so it arrives blank for you
  to set.

Nothing here submits a form, books an appointment, sends mail, creates a record, or
modifies the site.

## Configuration

`app/config.py` holds the site URL, load-time thresholds, the pages sampled for the
landing-page and responsiveness checks, and the row-to-check mapping.

The normal workflow is upload-first: no fixed Windows path is required. The target site defaults to `https://positka.com` and can be overridden with the `HYGIENE_BASE_URL` environment variable. The uploaded workbook
is copied into `uploaded_workbooks/` and selected for the current session. The parser locates
`Checklist Item` case-insensitively and reads every non-empty cell below it. An optional
`Category` column is also read. If a checklist item does not match a supported automated
scenario, it is marked `Needs you` instead of producing a false Pass.

## Accounts

```bash
python manage.py adduser        # create or replace an account
python manage.py listusers      # list accounts
python manage.py deluser NAME   # remove one
```

Self-service sign-up is on by default and controlled by `ALLOW_SIGNUP` in `app/config.py`.
It is safe while the server binds to `127.0.0.1`, since only someone already on this
machine can reach the page. **Set it to `False` before exposing the tool on any network
interface**, and create accounts with `adduser` instead — otherwise anyone who can reach
the port can give themselves an account that writes to your workbook.

Sign-up will not overwrite an existing username; taking over an account requires
`adduser` at the terminal.

Passwords are hashed with scrypt and a per-user salt. Plaintext is never stored or logged.
`users.json` and `.secret_key` are gitignored — do not commit them.

The server binds to `127.0.0.1` only, so it is reachable from this machine alone. It runs
on Flask's development server, which is right for a local tool and not suitable for exposing
to a network.

## Reports

Standalone health-check reports, separate from the checklist runs:

| Date | Cycle | Result | File |
|------|-------|--------|------|
| 2026-08-27 | 001 | 31 pass · 3 fail · 13 warning · 4 unverified | [`reports/2026-08-27-health-check.html`](reports/2026-08-27-health-check.html) |

## Confidentiality

Runs record unremediated weaknesses on a live production site. Keep this repository
private and share individual reports deliberately.

## Browser troubleshooting

The runner first tries Playwright-managed Chromium, then installed Google Chrome or Microsoft Edge. If none is available, browser-dependent checks are marked Needs you instead of being reported as website failures. On a new machine run `python -m playwright install chromium` once.
