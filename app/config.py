"""Site, workbook and checklist configuration.

Everything the checks depend on lives here so the tool can be pointed at a
different site, or the checklist reordered, without touching check logic.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Site under test -------------------------------------------------------
SITE_NAME = "Positka"
BASE_URL = os.environ.get("HYGIENE_BASE_URL", "https://positka.com").rstrip("/")
USER_AGENT = "Mozilla/5.0 (compatible; PositkaHygieneCheck/1.0)"

# Response-time thresholds, seconds. Above WARN the item is reported as Fail.
DESKTOP_LOAD_WARN = 5.0
MOBILE_LOAD_WARN = 8.0
HTTP_TIMEOUT = 30

# Pages the landing-page and responsiveness checks sample.
CRITICAL_PAGES = [
    "/",
    "/contact-us",
    "/platforms",
    "/services/managed-security-services",
    "/siem-sizing-calculator",
]

LANDING_PAGES = [
    "/siem-sizing-calculator",
    "/resources/radio-equipment-directive-red-checklist",
    "/services/cert-in",
    "/services/cra",
    "/contact-us",
]

# --- Workbook --------------------------------------------------------------
# Fixed by design: there is no upload feature. The checklist workbook always
# lives at this exact path, under a fixed name, inside the "input" folder next
# to the project. If the file is renamed or moved, the app fails with a clear
# error at startup/dashboard-load rather than silently picking something else.
INPUT_DIR = BASE_DIR / "input"
WORKBOOK_FILENAME = "Website Hygiene Checklist 2026 v1.0.xlsx"
WORKBOOK_PATH = INPUT_DIR / WORKBOOK_FILENAME

HEADER_ROW = 2          # "Category | Checklist Item | Status | Checked By | Notes"
FIRST_ITEM_ROW = 3
LAST_ITEM_ROW = 16
DATE_ROW = 1
BLOCK_WIDTH = 3         # Status, Checked By, Notes


# Status vocabulary written to the sheet. The existing workbook uses only
# Pass and Fail, so the tool stays inside that vocabulary.
STATUS_PASS = "Pass"
STATUS_FAIL = "Fail"
VALID_STATUSES = (STATUS_PASS, STATUS_FAIL)

# Authentication. With REQUIRE_LOGIN False the tool opens straight on the
# dashboard and the Checked By name is set from a field there instead of from
# an account. Turn it back on before putting this anywhere other than this
# machine — without it, anyone who can reach the port can write to the workbook.
REQUIRE_LOGIN = False

# Name used for Checked By when login is off and nothing has been set yet.
DEFAULT_CHECKED_BY = "Website Health Check"

# Self-service sign-up, used only when REQUIRE_LOGIN is True. Safe while the
# server binds to 127.0.0.1, since only someone already on this machine can
# reach it. Set to False before exposing the tool on any network interface,
# and create accounts with manage.py instead.
ALLOW_SIGNUP = True

USERS_FILE = BASE_DIR / "users.json"
RUNS_DIR = BASE_DIR / "runs"
