"""Flask application: sign in, run the checks, review, write to the workbook."""
import datetime
import json
import secrets
import shutil
import subprocess
import sys
import threading
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

from . import auth, workbook
from .config import (ALLOW_SIGNUP, BASE_DIR, BASE_URL, DEFAULT_CHECKED_BY,
                     REQUIRE_LOGIN, RUNS_DIR, SITE_NAME, STATUS_FAIL, STATUS_PASS,
                     VALID_STATUSES, WORKBOOK_PATH)


def _tail(path, lines=3):
    """Last few lines of a log, for surfacing a runner crash to the user."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tail = [ln.strip() for ln in fh.readlines()[-lines:] if ln.strip()]
        return " / ".join(tail) if tail else "No output was captured."
    except OSError:
        return "No output was captured."

app = Flask(__name__)

# Persist the session key so signing in survives a restart.
_key_file = BASE_DIR / ".secret_key"
if not _key_file.exists():
    _key_file.write_bytes(secrets.token_bytes(32))
    try:
        _key_file.chmod(0o600)
    except OSError:
        pass
app.secret_key = _key_file.read_bytes()

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# job_id -> state. In-memory: a run is a short-lived, single-user operation.
JOBS = {}
JOBS_LOCK = threading.Lock()


# --- helpers ---------------------------------------------------------------

def login_required(view):
    """Gate a view behind sign-in, unless REQUIRE_LOGIN is off.

    With login disabled the tool is a single-operator local utility: there is
    no account, so an anonymous identity is established instead and the
    Checked By name comes from a field on the dashboard.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not REQUIRE_LOGIN:
            session.setdefault("username", "local")
            session.setdefault("display_name", DEFAULT_CHECKED_BY)
            return view(*args, **kwargs)
        if not session.get("username"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf", "")
    if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Session expired or form token mismatch. Reload and try again.")


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["require_login"] = REQUIRE_LOGIN


def workbook_path():
    """The checklist workbook always lives at the fixed WORKBOOK_PATH.

    There is no upload feature and no per-session override: the input
    folder holds exactly one file, at a fixed name, and that is what runs.
    """
    return WORKBOOK_PATH


RUNS_DIR.mkdir(parents=True, exist_ok=True)


@app.route("/download")
@login_required
def download_workbook():
    """Send the workbook, results and all, as a download."""
    path = workbook_path()
    if not path.exists():
        flash(f"Workbook not found at {path}", "error")
        return redirect(url_for("dashboard"))

    stamp = datetime.date.today().isoformat()
    return send_file(path, as_attachment=True,
                     download_name=f"{path.stem} ({stamp}){path.suffix}")


@app.route("/download/<job_id>")
@login_required
def download_result(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["owner"] != session["username"]:
        abort(404)
    result_path = job["dir"] / "result.xlsx"
    if not result_path.exists():
        flash("Result workbook is not available yet. Save the review first.", "error")
        return redirect(url_for("review", job_id=job_id))
    name = job.get("result_download_name", result_path.name)
    return send_file(result_path, as_attachment=True, download_name=name)


# --- auth ------------------------------------------------------------------

@app.route("/whoami", methods=["POST"])
@login_required
def whoami():
    """Set the Checked By name when running without accounts.

    Leaving the box empty is a valid choice: it resets to the default
    Checked By name rather than being rejected.
    """
    check_csrf()
    name = (request.form.get("display_name") or "").strip()
    if not name:
        session["display_name"] = DEFAULT_CHECKED_BY
        flash(f"No name entered — checks will be recorded as {DEFAULT_CHECKED_BY}.", "ok")
    elif len(name) > 60:
        flash("Name must be 60 characters or fewer.", "error")
    else:
        session["display_name"] = name
        flash(f"Checks will be recorded as {name}.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not REQUIRE_LOGIN:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        check_csrf()
        user = auth.verify(request.form.get("username", ""), request.form.get("password", ""))
        if user:
            session.clear()
            session.permanent = True
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            csrf_token()
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
        flash("Incorrect username or password.", "error")

    return render_template("login.html",
                           site=SITE_NAME,
                           allow_signup=ALLOW_SIGNUP,
                           no_accounts=auth.user_count() == 0)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if not REQUIRE_LOGIN:
        return redirect(url_for("dashboard"))
    if not ALLOW_SIGNUP:
        flash("Sign-up is disabled. Ask an administrator to create your account.", "error")
        return redirect(url_for("login"))

    form = {"username": "", "display_name": ""}

    if request.method == "POST":
        check_csrf()
        form["username"] = (request.form.get("username") or "").strip()
        form["display_name"] = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if password != confirm:
            flash("The two passwords do not match.", "error")
        else:
            try:
                auth.add_user(form["username"], password, form["display_name"])
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                # Sign the new account straight in.
                session.clear()
                session.permanent = True
                session["username"] = form["username"].strip().lower()
                session["display_name"] = form["display_name"]
                csrf_token()
                flash(f"Account created. Checks you save will be recorded as "
                      f"{form['display_name']}.", "ok")
                return redirect(url_for("dashboard"))

    return render_template("signup.html", site=SITE_NAME, form=form,
                           min_length=auth.MIN_PASSWORD_LENGTH)


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("login"))


# --- dashboard -------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    path = WORKBOOK_PATH
    info = error = None
    if path.exists():
        try:
            info = workbook.describe(path)
        except Exception as exc:                        # noqa: BLE001
            error = f"Could not read the workbook: {exc}"
    else:
        error = (f"Checklist workbook not found at {path}. Place the workbook "
                 f"at that exact path and filename, then reload.")

    today = datetime.date.today()
    existing = None
    if info and not error:
        try:
            existing = workbook.last_run_for(path, today)
        except Exception:                               # noqa: BLE001
            existing = None

    checklist = []
    if info and not error:
        try:
            checklist = workbook.read_checklist(path)
        except Exception as exc:
            error = str(exc)

    return render_template("dashboard.html",
                           site=SITE_NAME, base_url=BASE_URL,
                           checklist=checklist, info=info, error=error,
                           workbook_path=str(path), today=today,
                           already_run=existing is not None,
                           require_login=REQUIRE_LOGIN,
                           default_checked_by=DEFAULT_CHECKED_BY,
                           display_name=session["display_name"])


# --- running the checks ----------------------------------------------------

def _job_dir(job_id):
    return RUNS_DIR / job_id


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


@app.route("/run", methods=["POST"])
@login_required
def run():
    check_csrf()
    job_id = secrets.token_urlsafe(12)
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    selected_workbook = workbook_path()
    if not selected_workbook.exists():
        flash(f"Checklist workbook not found at {selected_workbook}.", "error")
        return redirect(url_for("dashboard"))
    try:
        checklist = workbook.read_checklist(selected_workbook)
    except Exception as exc:
        flash(f"Cannot run this workbook: {exc}", "error")
        return redirect(url_for("dashboard"))
    (job_dir / "run_config.json").write_text(
        json.dumps({"workbook_path": str(selected_workbook), "checklist": checklist}),
        encoding="utf-8")

    # Each run gets its own process so Playwright always starts in a clean
    # interpreter. Sharing one long-lived server process proved flaky.
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "app.runner", str(job_dir)],
        cwd=str(BASE_DIR),
        stdout=open(job_dir / "runner.log", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    with JOBS_LOCK:
        JOBS[job_id] = {
            "owner": session["username"],
            "proc": proc,
            "dir": job_dir,
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    session["job_id"] = job_id
    return redirect(url_for("progress", job_id=job_id))


@app.route("/run/<job_id>")
@login_required
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job["owner"] != session["username"]:
            abort(404)
    cfg = _read_json(job["dir"] / "run_config.json", {})
    return render_template("progress.html", job_id=job_id, total=len(cfg.get("checklist", [])),
                           site=SITE_NAME, display_name=session["display_name"])


@app.route("/run/<job_id>/status")
@login_required
def run_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["owner"] != session["username"]:
        return jsonify({"error": "not found"}), 404

    cfg = _read_json(job["dir"] / "run_config.json", {})
    total = len(cfg.get("checklist", []))
    state = _read_json(job["dir"] / "progress.json",
                       {"state": "running", "done": 0,
                        "total": total, "current": "starting"})

    # A crashed runner would otherwise leave the page polling forever.
    proc = job.get("proc")
    if state.get("state") != "finished" and proc is not None and proc.poll() is not None:
        if not (job["dir"] / "results.json").exists():
            state = {"state": "failed", "done": state.get("done", 0),
                     "total": total, "current": None,
                     "error": f"the check process exited with code {proc.returncode}"}
    return jsonify(state)


@app.route("/review/<job_id>")
@login_required
def review(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["owner"] != session["username"]:
        abort(404)

    payload = _read_json(job["dir"] / "results.json")
    if payload is None:
        proc = job.get("proc")
        if proc is not None and proc.poll() is not None:
            log = _tail(job["dir"] / "runner.log")
            flash(f"The check process stopped before finishing. {log}", "error")
            return redirect(url_for("dashboard"))
        return redirect(url_for("progress", job_id=job_id))

    by_row = payload.get("results", {})
    results = list(by_row.values())
    results.sort(key=lambda r: r.get("row", 0))
    browser_error = payload.get("browser_error")

    auto_pass = sum(1 for r in results if r["status"] == STATUS_PASS)
    auto_fail = sum(1 for r in results if r["status"] == STATUS_FAIL)
    needs_you = sum(1 for r in results if r["status"] is None)

    return render_template("review.html",
                           job_id=job_id, results=results, site=SITE_NAME,
                           display_name=session["display_name"],
                           today=datetime.date.today(),
                           workbook_path=str(workbook_path()),
                           auto_pass=auto_pass, auto_fail=auto_fail,
                           needs_you=needs_you, browser_error=browser_error,
                           statuses=VALID_STATUSES)


@app.route("/save/<job_id>", methods=["POST"])
@login_required
def save(job_id):
    check_csrf()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job["owner"] != session["username"]:
            abort(404)

    payload = _read_json(job["dir"] / "results.json", {})
    entries = sorted(payload.get("results", {}).values(), key=lambda r: r.get("row", 0))
    rows, skipped = {}, []
    for entry in entries:
        row = entry["row"]
        status = (request.form.get(f"status_{row}") or "").strip()
        notes = (request.form.get(f"notes_{row}") or "").strip()
        if status not in VALID_STATUSES:
            skipped.append(entry["item"].strip())
            continue
        rows[row] = {"status": status, "notes": notes}

    if not rows:
        flash("Nothing to save — every row still needs a Pass or Fail.", "error")
        return redirect(url_for("review", job_id=job_id))

    try:
        source_path = workbook_path()
        if not source_path.exists():
            raise FileNotFoundError(f"Workbook not found at {source_path}")

        details = []
        for entry in entries:
            row = entry["row"]
            final_status = rows[row]["status"] if row in rows else (entry.get("status") or "Not Tested")
            details.append({
                "run_date": datetime.date.today().isoformat(),
                "row": row,
                "category": entry.get("category"),
                "item": entry.get("item"),
                "status": final_status,
                "automated": entry.get("automated", False),
                "scenario_time_sec": entry.get("scenario_time_sec"),
                "page_load_time_sec": entry.get("page_load_time_sec"),
                "process_time_sec": entry.get("process_time_sec"),
                "total_time_sec": entry.get("total_time_sec"),
                "urls": " | ".join(entry.get("urls") or []) or None,
                "action": entry.get("item"),
                "expected": "Checklist scenario is satisfied on the target website.",
                "actual": (rows[row]["notes"] if row in rows else "") or entry.get("note") or entry.get("status") or "Not Tested",
                "evidence": " | ".join(
                    e.get("text", "") if isinstance(e, dict) else str(e)
                    for e in entry.get("evidence", [])),
                "error": entry.get("error"),
            })

        # Write Status / Checked By / Notes straight into today's date block in
        # the real workbook. If today has no block yet, one is inserted in
        # chronological order (after the nearest earlier date, before the
        # nearest later one). A timestamped backup is taken first.
        today = datetime.date.today()
        write_result = workbook.write_run(
            source_path, today, rows,
            checked_by=session["display_name"] or DEFAULT_CHECKED_BY,
            details=details, backup=True,
        )

        # The download is a copy of the now-updated workbook, taken at save
        # time so it stays stable even if the workbook changes again later.
        result_path = job["dir"] / "result.xlsx"
        shutil.copy2(source_path, result_path)

        result_name = f"{source_path.stem} - {today.isoformat()}{source_path.suffix}"
        job["result_download_name"] = result_name
        outcome = {
            "result_path": str(result_path),
            "result_download_name": result_name,
            "rows_written": write_result["rows_written"],
            "column_letter": write_result["column_letter"],
            "created_block": write_result["created_block"],
            "backup": write_result["backup"],
        }
    except PermissionError:
        flash("The workbook is open in Excel. Close it and save again.", "error")
        return redirect(url_for("review", job_id=job_id))
    except Exception as exc:                            # noqa: BLE001
        flash(f"Could not write the workbook: {exc}", "error")
        return redirect(url_for("review", job_id=job_id))

    return render_template("saved.html",
                           site=SITE_NAME, outcome=outcome, skipped=skipped,
                           saved_count=len(rows), today=datetime.date.today(),
                           display_name=session["display_name"],
                           workbook_path=str(workbook_path()),
                           result_download_url=url_for("download_result", job_id=job_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
