"""Run one check job in a dedicated process.

    python -m app.runner <job_dir>

Playwright's sync API keeps per-thread greenlet state, and starting and
stopping it repeatedly inside a long-lived web server proved unreliable —
it intermittently failed with "'PlaywrightContextManager' object has no
attribute '_playwright'". Giving every run its own process sidesteps that
entirely: the browser is always started once, in a fresh interpreter, and
the process exits when the run ends.

Progress is written to progress.json after each check so the web app can
poll it, and the full result set to results.json at the end.
"""
import json
import os
import sys
import traceback
from pathlib import Path


def _write(path: Path, payload: dict) -> None:
    """Write JSON atomically so a poller never reads a half-written file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def main(job_dir: str) -> int:
    from .checks import CHECKS, Context
    from .config import BASE_URL
    from .workbook import read_checklist

    job = Path(job_dir)
    run_config = json.loads((job / "run_config.json").read_text(encoding="utf-8"))
    checklist = run_config["checklist"]
    base_url = run_config.get("base_url") or BASE_URL
    job.mkdir(parents=True, exist_ok=True)
    progress_file = job / "progress.json"
    results_file = job / "results.json"

    total = len(checklist)
    results = {}
    state = {"state": "running", "done": 0, "total": total, "current": None}
    _write(progress_file, state)

    ctx = Context(base_url=base_url)
    try:
        for entry in checklist:
            state["current"] = entry["item"].strip()
            _write(progress_file, state)

            fn = CHECKS.get(entry["id"])
            base = {"row": entry["row"], "id": entry["id"],
                    "category": entry["category"], "item": entry["item"].strip(),
                    "mode": entry["mode"]}
            try:
                if fn is None:
                    # Unknown checklist text must never be guessed. The review
                    # screen receives an explicit manual result with timing.
                    import time
                    started = time.perf_counter()
                    result = None
                    elapsed = time.perf_counter() - started
                    base.update({"status": None,
                                 "note": "No specialized automated check is mapped to this Checklist Item. Review and test this scenario manually.",
                                 "evidence": [f"Scenario: {entry['item']}",
                                              f"Automated execution: not mapped",
                                              f"Runner processing time: {elapsed:.4f} sec"],
                                 "automated": False, "error": None,
                                 "scenario_time_sec": round(elapsed, 4),
                                 "page_load_time_sec": None, "process_time_sec": round(elapsed, 4),
                                 "total_time_sec": round(elapsed, 4), "urls": []})
                else:
                    import time
                    started = time.perf_counter()
                    rendered_before = set(ctx._rendered.keys())
                    result = fn(ctx)
                    elapsed = time.perf_counter() - started
                    rendered_after = [v for k, v in ctx._rendered.items()
                                      if k not in rendered_before and isinstance(v, dict)]
                    load_times = [float(v.get("__load_secs__") or 0) for v in rendered_after
                                  if v.get("__load_secs__") is not None]
                    page_load = max(load_times) if load_times else None
                    evidence = list(result.evidence)
                    evidence.append(f"Scenario processing time: {elapsed:.3f} sec")
                    if page_load is not None:
                        evidence.append(f"Measured browser page load time: {page_load:.3f} sec")
                    base.update({"status": result.status, "note": result.note,
                             "scenario_time_sec": round(elapsed, 4),
                             "page_load_time_sec": round(page_load, 4) if page_load is not None else None,
                             "process_time_sec": round(elapsed, 4),
                             "total_time_sec": round(elapsed, 4),
                             "evidence": evidence,
                             "automated": result.automated, "error": result.error,
                             "urls": getattr(result, "urls", [])})
            except Exception as exc:                    # noqa: BLE001
                base.update({"status": None, "note": "",
                             "evidence": [f"Check raised an error: {exc.__class__.__name__}: {exc}",
                                          traceback.format_exc(limit=3)],
                             "automated": False,
                             "error": f"{exc.__class__.__name__}: {exc}",
                             "scenario_time_sec": None, "page_load_time_sec": None,
                             "process_time_sec": None, "total_time_sec": None, "urls": []})

            results[str(entry["row"])] = base
            state["done"] += 1
            _write(progress_file, state)
    finally:
        browser_error = ctx.browser_error
        browser_traceback = getattr(ctx, "browser_traceback", None)
        ctx.close()

        _write(results_file, {"results": results,
                              "browser_error": browser_error,
                              "browser_traceback": browser_traceback})
        state["state"] = "finished"
        state["current"] = None
        state["browser_error"] = browser_error
        _write(progress_file, state)

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m app.runner <job_dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
