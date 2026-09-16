"""The 14 hygiene checks.

Every check is read-only. Nothing here submits a form, books an appointment,
sends mail, creates a record, or modifies the site in any way. Where a
checklist item genuinely requires one of those actions to verify end to end,
the check reports what it *could* confirm and says plainly what it could not.

Each check returns a Result. `status` is Pass or Fail for automated checks and
None for manual ones, which a person resolves in the UI before saving.
"""
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

from .config import (BASE_URL, CRITICAL_PAGES, DESKTOP_LOAD_WARN, HTTP_TIMEOUT,
                     LANDING_PAGES, MOBILE_LOAD_WARN, STATUS_FAIL, STATUS_PASS,
                     USER_AGENT)


@dataclass
class Result:
    status: str | None          # "Pass" | "Fail" | None (manual)
    note: str = ""              # written to the Notes column
    evidence: list = field(default_factory=list)   # shown in the UI only
    automated: bool = True
    error: str | None = None    # set when the check itself failed to run
    urls: list = field(default_factory=list)


def _same_site(url: str, base_url: str) -> bool:
    try:
        target = urlparse(url)
        base = urlparse(base_url)
        return target.scheme in {"http", "https"} and target.netloc.lower() == base.netloc.lower()
    except Exception:
        return False


def bad(text: str) -> dict:
    """Mark an evidence line as a failure, so the UI can show it in red."""
    return {"text": text, "bad": True}


def _fmt_secs(value: float) -> str:
    """Match the sheet's existing style: 'Desktop:01 Sec', 'Mobile:03 Sec'."""
    if value < 1:
        return f"{value:.2f} msec"
    return f"{int(round(value)):02d} Sec"


class Context:
    """Shared HTTP session, page cache and browser for one run."""

    def __init__(self, base_url=BASE_URL, log=None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._pages = {}        # path -> (status, seconds, text)
        self._rendered = {}     # (path, width, script) -> dict
        self._contexts = {}     # (width, height) -> browser context
        self._status = {}       # url -> status code
        self._browser = None
        self._pw = None
        self.browser_error = None
        self.log = log or (lambda msg: None)

    # -- plain HTTP ---------------------------------------------------------
    def get(self, path):
        if path in self._pages:
            return self._pages[path]
        url = path if path.startswith("http") else self.base_url + path
        started = time.perf_counter()
        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            out = (resp.status_code, time.perf_counter() - started, resp.text)
        except requests.RequestException as exc:
            out = (0, time.perf_counter() - started, f"__ERROR__ {exc}")
        self._pages[path] = out
        return out

    def status_of(self, path):
        url = path if path.startswith("http") else self.base_url + path
        if url in self._status:
            return self._status[url]
        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            code = resp.status_code
        except requests.RequestException:
            code = 0
        self._status[url] = code
        return code

    # -- rendered browser ---------------------------------------------------
    def _ensure_browser(self):
        if self._browser or self.browser_error:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()

            # Prefer Playwright-managed Chromium, then fall back to a locally
            # installed Chrome/Edge so users do not get false website failures
            # merely because Playwright's bundled browser is missing.
            try:
                self._browser = self._pw.chromium.launch(args=["--disable-dev-shm-usage"])
                return self._browser
            except Exception:
                pass

            for channel in ("chrome", "msedge"):
                try:
                    self._browser = self._pw.chromium.launch(
                        channel=channel, args=["--disable-dev-shm-usage"])
                    return self._browser
                except Exception:
                    pass

            candidates = []
            if os.name == "nt":
                candidates.extend([
                    os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                ])
            else:
                candidates.extend([
                    shutil.which("google-chrome"), shutil.which("chromium"),
                    shutil.which("chromium-browser"), shutil.which("microsoft-edge")
                ])

            for executable in filter(None, candidates):
                if not os.path.exists(executable):
                    continue
                try:
                    self._browser = self._pw.chromium.launch(
                        executable_path=executable, args=["--disable-dev-shm-usage"])
                    return self._browser
                except Exception:
                    pass

            raise RuntimeError(
                "Browser automation is unavailable. Run `python -m playwright install chromium`, "
                "or make sure Google Chrome/Microsoft Edge is installed. Render-dependent checks were not executed."
            )
        except Exception as exc:
            import traceback
            self.browser_error = str(exc)
            self.browser_traceback = traceback.format_exc()
            try:
                from .config import BASE_DIR
                (BASE_DIR / "browser-error.log").write_text(self.browser_traceback, encoding="utf-8")
            except Exception:
                pass
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._browser = None
        return self._browser

    def _context_for(self, width, height):
        """One browser context per viewport, reused across checks.

        Creating a context per call made a full run take minutes and pushed
        later pages past their navigation timeout.
        """
        key = (width, height)
        if key in self._contexts:
            return self._contexts[key]
        browser = self._ensure_browser()
        if browser is None:
            return None
        ctx = browser.new_context(
            viewport={"width": width, "height": height},
            user_agent=USER_AGENT,
            is_mobile=width < 768,
            has_touch=width < 768,
        )
        ctx.set_default_navigation_timeout(35000)
        self._contexts[key] = ctx
        return ctx

    def render(self, path, width=1280, height=900, script=None, wait_ms=2000):
        """Load a page in Chromium and evaluate `script` against it.

        Navigation waits for DOMContentLoaded, then gives the load event a
        short capped window. The site embeds several third-party widgets that
        can keep `load` pending far longer than the page is actually usable,
        so waiting on `load` alone is not a reliable signal here.
        """
        key = (path, width, script or "")
        if key in self._rendered:
            return self._rendered[key]

        bctx = self._context_for(width, height)
        if bctx is None:
            return {"__error__": self.browser_error or "browser unavailable"}

        url = path if path.startswith("http") else self.base_url + path
        page = None
        try:
            page = bctx.new_page()
            page.goto(url, timeout=35000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("load", timeout=12000)
            except Exception:                         # noqa: BLE001
                pass                                  # usable without every third-party asset
            page.wait_for_timeout(wait_ms)

            timing = page.evaluate("""() => {
                const n = performance.getEntriesByType('navigation')[0];
                if (n) return {load: n.loadEventEnd/1000, dom: n.domContentLoadedEventEnd/1000};
                const t = performance.timing;
                return {load: (t.loadEventEnd - t.navigationStart)/1000,
                        dom: (t.domContentLoadedEventEnd - t.navigationStart)/1000};
            }""") or {}

            data = page.evaluate(script) if script else {}
            if isinstance(data, dict):
                load = timing.get("load") or 0
                dom = timing.get("dom") or 0
                data["__load_secs__"] = load if load > 0 else dom
                data["__dom_secs__"] = dom
                data["__title__"] = page.title()
            self._rendered[key] = data
            return data
        except Exception as exc:                      # noqa: BLE001
            return {"__error__": str(exc)}
        finally:
            if page:
                try: page.close()
                except Exception: pass

    def close(self):
        for ctx in self._contexts.values():
            try: ctx.close()
            except Exception: pass
        if self._browser:
            try: self._browser.close()
            except Exception: pass
        if self._pw:
            try: self._pw.stop()
            except Exception: pass
        self.session.close()


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

def check_siem_calculator(ctx: Context) -> Result:
    path = "/siem-sizing-calculator"
    code, _, html = ctx.get(path)
    if code != 200:
        return Result(STATUS_FAIL,
                      f"SIEM Sizing Calculator page returned HTTP {code}. Page is not reachable.",
                      [f"GET {path} -> {code}"])

    data = ctx.render(path, script="""() => {
        const forms=[...document.forms].map(f=>({
          id:f.id||null, visible:f.offsetHeight>0,
          fields:[...f.elements].filter(e=>e.type!=='hidden'&&e.name).map(e=>({name:e.name,type:e.type,required:e.required})),
          hasCaptcha: !!f.querySelector('[data-sitekey],[class*=captcha],[id*=captcha],textarea[name=g-recaptcha-response]'),
          hasToken: !!f.querySelector('input[name=_token]')
        }));
        return {formCount:forms.length, forms,
                recaptcha: !!document.querySelector('.g-recaptcha,[data-sitekey],script[src*=recaptcha]'),
                calcInputs: document.querySelectorAll('input[type=number],select').length};
    }""")

    if "__error__" in data:
        return Result(None, "", [f"Browser render failed: {data['__error__']}"],
                      automated=False, error=data["__error__"])

    forms = [f for f in data.get("forms", []) if f.get("visible")]
    inputs = data.get("calcInputs", 0)
    ev = [f"GET {path} -> 200",
          f"visible forms: {len(forms)}",
          f"numeric/select inputs: {inputs}",
          f"reCAPTCHA present: {data.get('recaptcha')}"]

    if not forms:
        return Result(STATUS_FAIL,
                      "SIEM Sizing Calculator page loads but no usable form is rendered. "
                      "Calculator input could not be entered.", ev)
    if inputs == 0:
        return Result(STATUS_FAIL,
                      "SIEM Sizing Calculator form renders but exposes no numeric or select "
                      "inputs, so sizing values cannot be entered.", ev)

    required = sum(1 for f in forms for fl in f["fields"] if fl.get("required"))
    missing_captcha = not data.get("recaptcha")
    ev.append(f"required fields: {required}")

    if missing_captcha:
        return Result(STATUS_FAIL,
                      "SIEM Sizing Calculator form renders and accepts input, but no reCAPTCHA "
                      "is present on the page. Submission path not verified.", ev)

    return Result(STATUS_PASS,
                  "",
                  ev + ["Note: form structure and validation verified; the calculator was not "
                        "submitted, so output values and lead delivery are untested."])


def check_click_to_call(ctx: Context) -> Result:
    found, bad = [], []
    for path in ("/", "/contact-us"):
        code, _, html = ctx.get(path)
        if code != 200:
            return Result(STATUS_FAIL, f"{path} returned HTTP {code}; click-to-call links could not be checked.",
                          [f"GET {path} -> {code}"])
        for href in re.findall(r'href="(tel:[^"]+)"', html):
            found.append((path, href))
            digits = re.sub(r"[^\d+]", "", href[4:])
            if len(re.sub(r"\D", "", digits)) < 8:
                bad.append((path, href))

    ev = [f"{p}  {h}" for p, h in found] or ["no tel: links found"]
    if not found:
        return Result(STATUS_FAIL,
                      "No click-to-call (tel:) links found on the homepage or contact page. "
                      "Phone numbers are not tappable on mobile.", ev)
    if bad:
        listed = ", ".join(h for _, h in bad)
        return Result(STATUS_FAIL,
                      f"Malformed click-to-call link(s): {listed}. Number is too short to dial.", ev)
    return Result(STATUS_PASS, "", ev + [f"{len(found)} tel: links, all well-formed"])


def check_calendly(ctx: Context) -> Result:
    hits = []
    for path in ("/", "/contact-us"):
        code, _, html = ctx.get(path)
        if code == 200 and "calendly" in html.lower():
            hits.append(path)

    ev = [f"calendly reference found on: {', '.join(hits)}" if hits else "no calendly reference found"]

    if not hits:
        return Result(STATUS_FAIL,
                      "Calendly scheduling widget not found on the homepage or contact page.", ev)

    data = ctx.render(hits[0], script="""() => ({
        widget: !!document.querySelector('[class*=calendly],[data-url*=calendly],iframe[src*=calendly]'),
        scriptLoaded: typeof window.Calendly !== 'undefined',
        links: [...document.querySelectorAll('a[href*=calendly]')].map(a=>a.href).slice(0,5)
    })""")

    if "__error__" in data:
        return Result(None, "", [f"Browser render failed: {data['__error__']}"],
                      automated=False, error=data["__error__"], urls=[ctx.base_url + hits[0]])

    ev.append(f"widget element: {data.get('widget')}, Calendly JS loaded: {data.get('scriptLoaded')}")
    if data.get("links"):
        ev.append("links: " + ", ".join(data["links"]))
    if not (data.get("widget") or data.get("scriptLoaded") or data.get("links")):
        return Result(STATUS_FAIL,
                      "Calendly is referenced in the page source but no booking widget, link or "
                      "script initialises in the browser. Scheduling is likely unavailable.", ev,
                      urls=[ctx.base_url + hits[0]])

    return Result(STATUS_PASS, "",
                  ev + ["Note: widget presence verified only. No booking was made, so the "
                        "confirmation email path is untested and needs a manual booking to confirm."],
                  urls=[ctx.base_url + p for p in hits])


def _inspect_form(ctx, path, want_keywords=None):
    return ctx.render(path, script="""() => {
        const forms=[...document.forms].map(f=>({
          id:f.id||null,
          action:f.getAttribute('action'), method:f.getAttribute('method'),
          visible:f.offsetHeight>0,
          text:(f.innerText||'').slice(0,200).toLowerCase(),
          fields:[...f.elements].filter(e=>e.type!=='hidden'&&e.name)
                 .map(e=>({name:e.name,type:e.type,required:e.required})),
          hasCaptcha: !!f.querySelector('[data-sitekey],textarea[name=g-recaptcha-response],[class*=captcha]'),
          hasToken: !!f.querySelector('input[name=_token]')
        }));
        return {forms, recaptcha: !!document.querySelector('.g-recaptcha,[data-sitekey],script[src*=recaptcha]')};
    }""")


def _form_verdict(data, label, path, keywords=None):
    if "__error__" in data:
        return Result(None, "", [f"Browser render failed: {data['__error__']}"],
                      automated=False, error=data["__error__"])

    forms = [f for f in data.get("forms", []) if f.get("visible")]
    if keywords:
        matched = [f for f in forms
                   if any(k in (f.get("text") or "") for k in keywords)
                   or any(k in (f.get("id") or "").lower() for k in keywords)]
        forms = matched or forms

    ev = [f"page: {path}", f"visible forms: {len(forms)}"]
    if not forms:
        return Result(STATUS_FAIL, f"{label} is not rendered on {path}. Visitors cannot submit an enquiry.", ev)

    form = forms[0]
    required = [f["name"] for f in form["fields"] if f.get("required")]
    ev += [f"form id: {form.get('id')}",
           f"fields: {', '.join(f['name'] for f in form['fields']) or 'none'}",
           f"required: {', '.join(required) or 'none'}",
           f"captcha: {form.get('hasCaptcha') or data.get('recaptcha')}",
           f"CSRF token: {form.get('hasToken')}"]

    if not form["fields"]:
        return Result(STATUS_FAIL, f"{label} renders but exposes no input fields.", ev)
    if not required:
        return Result(STATUS_FAIL,
                      f"{label} renders but no field is marked required, so empty submissions "
                      f"are not blocked client-side.", ev)

    return Result(STATUS_PASS, "",
                  ev + ["Note: structure and required-field validation verified; the form was "
                        "not submitted, so delivery to the inbox is untested."])


def check_contact_form(ctx: Context) -> Result:
    path = "/contact-us"
    code, _, _ = ctx.get(path)
    if code != 200:
        return Result(STATUS_FAIL, f"Contact us page returned HTTP {code}.", [f"GET {path} -> {code}"])
    return _form_verdict(_inspect_form(ctx, path), "Contact us form", path)


def check_get_in_touch_form(ctx: Context) -> Result:
    for path in ("/contact-us", "/"):
        code, _, _ = ctx.get(path)
        if code != 200:
            continue
        data = _inspect_form(ctx, path)
        if "__error__" in data:
            continue
        res = _form_verdict(data, "Get in touch form", path,
                            keywords=["get in touch", "enquire", "enquiry", "touch"])
        if res.status == STATUS_PASS:
            return res
    return Result(STATUS_FAIL,
                  "Get in touch form could not be located on the contact page or homepage.",
                  ["searched /contact-us and / for an enquiry form"])


def check_red_checklist(ctx: Context) -> Result:
    path = "/resources/radio-equipment-directive-red-checklist"
    code, _, html = ctx.get(path)
    ev = [f"GET {path} -> {code}"]
    if code != 200:
        return Result(STATUS_FAIL, f"RED checklist page returned HTTP {code}.", ev)
    if len(html) < 2000:
        return Result(STATUS_FAIL, "RED checklist page returns 200 but the response body is nearly empty.", ev)
    ev.append(f"body: {len(html):,} bytes")
    return Result(STATUS_PASS, "", ev)


def _collect_links(html, base):
    out = set()
    for href in re.findall(r'href="([^"#][^"]*)"', html):
        if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        url = urljoin(base + "/", href)
        if _same_site(url, base):
            out.add(url)
    return sorted(out)


def check_cta_buttons(ctx: Context) -> Result:
    code, _, html = ctx.get("/")
    if code != 200:
        return Result(STATUS_FAIL, f"Homepage returned HTTP {code}; CTAs could not be checked.",
                      [f"GET / -> {code}"])

    ctas = set()
    for m in re.finditer(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        blob = (m.group(0) or "").lower()
        text = re.sub(r"<[^>]+>", " ", inner).strip().lower()
        looks_cta = any(c in blob for c in ("btn", "cta", "button")) or any(
            k in text for k in ("contact", "get in touch", "book", "demo", "enquire",
                                "talk to", "get started", "learn more", "read more"))
        if looks_cta and not href.startswith(("mailto:", "tel:", "javascript:")):
            url = urljoin(ctx.base_url + "/", href)
            if _same_site(url, ctx.base_url):
                ctas.add(url)

    if not ctas:
        return Result(STATUS_FAIL, "No call-to-action links could be identified on the homepage.",
                      ["CTA pattern matched 0 anchors"])

    broken, working = [], []
    for url in sorted(ctas):
        st = ctx.status_of(url)
        path = url.replace(ctx.base_url, "") or "/"
        if st >= 400 or st == 0:
            broken.append((url, st))
        else:
            working.append(f"{st}  {path}")

    ev = []
    if broken:
        ev.append(bad(f"--- {len(broken)} BROKEN of {len(ctas)} checked ---"))
        ev += [bad(f"{st}  {u.replace(ctx.base_url,'') or '/'}") for u, st in broken]
        ev.append(f"--- {len(working)} working CTAs below ---")
    ev.extend(working)

    if broken:
        listed = "; ".join(f"{u.replace(ctx.base_url,'')} -> {s}" for u, s in broken)
        return Result(STATUS_FAIL,
                      f"{len(broken)} of {len(ctas)} CTA links do not resolve: {listed}", ev)
    return Result(STATUS_PASS, "", ev + [f"all {len(ctas)} CTA links resolve"])


def check_landing_pages(ctx: Context) -> Result:
    failures, ev = [], []
    for path in LANDING_PAGES:
        code, _, html = ctx.get(path)
        if code != 200:
            ev.append(bad(f"{code}  {path}  <- page did not load"))
            failures.append(f"{path} -> HTTP {code}")
            continue

        # Forms on these pages are often injected by script, so the rendered
        # DOM is the only trustworthy signal. Not every landing page embeds a
        # form — some route to the contact page instead — so a page passes on
        # either a working form or a working route to one.
        data = ctx.render(path, wait_ms=2500, script="""() => ({
            forms:[...document.forms].filter(f=>f.offsetHeight>0).length,
            fields:document.querySelectorAll('form input:not([type=hidden]),form select,form textarea').length,
            ctas:[...document.querySelectorAll('a[href*=contact],a[href*=enquire],a[href*=touch]')]
                 .filter(a=>a.offsetHeight>0).map(a=>a.href)
        })""")
        if "__error__" in data:
            ev.append(bad(f"{code}  render blocked  {path}: {data['__error__']}"))
            continue

        forms, fields = data.get("forms", 0), data.get("fields", 0)
        ctas = data.get("ctas", [])
        summary = f"{code}  forms={forms} fields={fields} conversion-links={len(ctas)}  {path}"

        if forms and fields:
            ev.append(summary)
            continue
        if forms and not fields:
            ev.append(bad(summary + "  <- form has no usable fields"))
            failures.append(f"{path} has a form with no usable fields")
            continue

        if not ctas:
            ev.append(bad(summary + "  <- no form and no link to one"))
            failures.append(f"{path} offers neither a form nor a link to one")
            continue

        broken = [u for u in {*ctas} if ctx.status_of(u) >= 400]
        if broken:
            ev.append(bad(summary + f"  <- conversion link broken: "
                                    f"{broken[0].replace(ctx.base_url,'')}"))
            failures.append(f"{path} has no form and its conversion link is broken "
                            f"({broken[0].replace(ctx.base_url,'')})")
        else:
            ev.append(summary + "  (no form; converts via link)")

    browser_blocked = any("render blocked" in (e.get("text", "") if isinstance(e, dict) else str(e)) for e in ev)
    if failures:
        return Result(STATUS_FAIL, "Landing page issues: " + "; ".join(failures), ev, urls=[ctx.base_url + p for p in LANDING_PAGES])
    if browser_blocked:
        return Result(None, "Browser checks were blocked; HTTP landing-page checks completed.", ev,
                      automated=False, error=ctx.browser_error, urls=[ctx.base_url + p for p in LANDING_PAGES])
    return Result(STATUS_PASS, "",
                  ev + [f"all {len(LANDING_PAGES)} landing pages convert, via a form or a working link"],
                  urls=[ctx.base_url + p for p in LANDING_PAGES])


def check_page_speed(ctx: Context) -> Result:
    # Timings come from the browser's own Navigation Timing entries, so they
    # measure the page rather than this tool's wait strategy.
    desktop = ctx.render("/", width=1280, height=900, wait_ms=300,
                         script="() => ({ok:true})")
    mobile = ctx.render("/", width=390, height=844, wait_ms=300,
                        script="() => ({ok:true})")

    if "__error__" in desktop or "__error__" in mobile:
        err = desktop.get("__error__") or mobile.get("__error__")
        return Result(None, "", [f"Browser render failed: {err}"], automated=False, error=err,
                      urls=[ctx.base_url + "/"])

    d = desktop.get("__load_secs__", 0.0)
    m = mobile.get("__load_secs__", 0.0)
    note = f"Desktop:{_fmt_secs(d)} and Mobile:{_fmt_secs(m)}"
    ev = [f"desktop load: {d:.2f}s (threshold {DESKTOP_LOAD_WARN}s)",
          f"mobile load: {m:.2f}s (threshold {MOBILE_LOAD_WARN}s)"]

    if d > DESKTOP_LOAD_WARN or m > MOBILE_LOAD_WARN:
        return Result(STATUS_FAIL, note + " - above the agreed threshold.", ev)
    return Result(STATUS_PASS, note, ev)


def check_mobile_responsive(ctx: Context) -> Result:
    script = """() => {
        const de=document.documentElement;
        const overflow = de.scrollWidth - window.innerWidth;
        const wide=[...document.querySelectorAll('body *')].filter(e=>{
            const r=e.getBoundingClientRect();
            return r.width>0 && r.right > window.innerWidth+4 && !e.closest('[class*=swiper],[class*=carousel],[class*=owl],[class*=slider]');
        }).slice(0,4).map(e=>e.tagName+'.'+String(e.className||'').split(' ')[0]);
        const tiny=[...document.querySelectorAll('p,li,a')].filter(e=>{
            const fs=parseFloat(getComputedStyle(e).fontSize);
            return e.textContent.trim().length>15 && fs>0 && fs<11;
        }).length;
        return {overflow, hasScroll: overflow>4, wide, tiny, vw:window.innerWidth};
    }"""

    problems, ev, browser_blocked = [], [], []
    for path in CRITICAL_PAGES:
        data = ctx.render(path, width=390, height=844, script=script)
        if "__error__" in data:
            ev.append(bad(f"{path}: render blocked - {data['__error__']}"))
            browser_blocked.append(path)
            continue

        line = f"{path}: overflow={data.get('overflow')}px tiny-text={data.get('tiny')}"
        ev.append(bad(line) if (data.get("hasScroll") or data.get("tiny", 0) > 0) else line)

        if data.get("hasScroll"):
            problems.append(f"{path} scrolls horizontally by {data.get('overflow')}px "
                            f"({', '.join(data.get('wide') or []) or 'source unclear'})")
        if data.get("tiny", 0) > 0:
            problems.append(f"{path} has {data['tiny']} text elements under 11px")

    if problems:
        return Result(STATUS_FAIL, "Mobile rendering issues: " + "; ".join(problems), ev,
                      urls=[ctx.base_url + p for p in CRITICAL_PAGES])
    if browser_blocked:
        return Result(None, "Browser checks were blocked; mobile rendering was not executed.", ev,
                      automated=False, error=ctx.browser_error, urls=[ctx.base_url + p for p in CRITICAL_PAGES])
    return Result(STATUS_PASS, "", ev + [f"{len(CRITICAL_PAGES)} critical pages render cleanly at 390px"],
                  urls=[ctx.base_url + p for p in CRITICAL_PAGES])


def check_nav_menus(ctx: Context) -> Result:
    # Menu markup is not confined to <nav>/<header> on this site — the services
    # dropdown sits outside them — so collect from the rendered DOM *and* the
    # raw HTML, then verify every internal destination.
    data = ctx.render("/", script="""() => {
        const links=new Set();
        [...document.querySelectorAll('a[href]')].forEach(a=>{
            const h=a.getAttribute('href')||'';
            if(h && !h.startsWith('#') && !/^(mailto|tel|javascript):/i.test(h)) links.add(a.href);
        });
        return {links:[...links]};
    }""")

    collected = set()
    if "__error__" not in data:
        collected.update(data.get("links", []))

    code, _, html = ctx.get("/")
    if code == 200:
        collected.update(_collect_links(html, ctx.base_url))

    if not collected and "__error__" in data:
        return Result(None, "", [f"Browser render failed: {data['__error__']}"],
                      automated=False, error=data["__error__"], urls=[ctx.base_url + "/"])

    links = [u for u in collected
             if _same_site(u, ctx.base_url)
             and not re.search(r"/assets/|/cdn-cgi/", u)]
    if not links:
        return Result(STATUS_FAIL, "No navigation links could be extracted from the page.", [], urls=[ctx.base_url + "/"])

    unique = sorted(set(links))
    broken, working = [], []
    for url in unique:
        st = ctx.status_of(url)
        path = url.replace(ctx.base_url, "") or "/"
        if st >= 400 or st == 0:
            broken.append((url, st))
            working.append(None)  # placeholder, replaced below
        else:
            working.append(f"{st}  {path}")
    working = [w for w in working if w]

    # Broken links first and in red, so they are not lost in a long list.
    ev = []
    if broken:
        ev.append(bad(f"--- {len(broken)} BROKEN of {len(unique)} checked ---"))
        for url, st in broken:
            ev.append(bad(f"{st}  {url.replace(ctx.base_url,'') or '/'}"))
        ev.append(f"--- {len(working)} working links below ---")
    ev.extend(working)

    if broken:
        listed = "; ".join(f"{u.replace(ctx.base_url,'')} -> {s}" for u, s in broken)
        return Result(STATUS_FAIL,
                      f"{len(broken)} navigation link(s) broken out of {len(unique)}: {listed}", ev,
                      urls=unique)
    return Result(STATUS_PASS, "", ev + [f"all {len(unique)} navigation links resolve"], urls=unique)


def check_cookie_banner(ctx: Context) -> Result:
    data = ctx.render("/", wait_ms=3500, script="""() => {
        const sel = '[class*=cookie],[id*=cookie],[class*=consent],[id*=consent],[class*=gdpr],[id*=gdpr]';
        const nodes=[...document.querySelectorAll(sel)].filter(e=>e.offsetHeight>0&&e.offsetWidth>0);
        const texts=nodes.map(e=>(e.innerText||'').trim().slice(0,120)).filter(Boolean);
        const buttons=[...document.querySelectorAll('button,a')].filter(b=>{
            const t=(b.innerText||'').trim().toLowerCase();
            return b.offsetHeight>0 && /accept|agree|allow|decline|reject|consent|cookie/.test(t);
        }).map(b=>(b.innerText||'').trim().slice(0,40));
        const scripts=[...document.scripts].map(s=>s.src).filter(s=>/cookie|consent|osano|onetrust|cookieyes|termly|iubenda/i.test(s));
        return {visibleNodes:nodes.length, texts, buttons, scripts,
                privacyLink: !!document.querySelector('a[href*="privacy"]')};
    }""")

    if "__error__" in data:
        return Result(None, "", [f"Browser render failed: {data['__error__']}"],
                      automated=False, error=data["__error__"], urls=[ctx.base_url + "/"])

    ev = [f"visible consent elements: {data.get('visibleNodes')}",
          f"consent buttons: {data.get('buttons')}",
          f"consent scripts: {data.get('scripts')}",
          f"privacy policy link present: {data.get('privacyLink')}"]

    has_banner = bool(data.get("visibleNodes") or data.get("buttons") or data.get("scripts"))
    if not has_banner:
        return Result(STATUS_FAIL,
                      "No cookie or privacy consent banner was detected on the homepage. "
                      "Visitors are not offered a consent choice.", ev)
    return Result(STATUS_PASS, "", ev)


def check_images_media(ctx: Context) -> Result:
    script = """() => {
        const imgs=[...document.images];
        const broken=imgs.filter(i=>i.complete&&i.naturalWidth===0&&(i.currentSrc||i.getAttribute('src')))
                         .map(i=>(i.currentSrc||i.src).split('/').pop()).slice(0,8);
        const neverLoaded=imgs.filter(i=>!i.getAttribute('src')&&(i.getAttribute('data-src')||i.getAttribute('data-lazy')))
                         .map(i=>(i.getAttribute('data-src')||i.getAttribute('data-lazy')).split('/').pop()).slice(0,12);
        return {total:imgs.length, broken, brokenCount:broken.length,
                neverLoaded, neverLoadedCount:neverLoaded.length};
    }"""

    problems, ev, rendered = [], [], []
    for path in ("/", "/contact-us"):
        data = ctx.render(path, wait_ms=4000, script=script)
        if "__error__" in data:
            ev.append(bad(f"{path}: render blocked - {data['__error__']}"))
            continue
        rendered.append(path)
        line = (f"{path}: {data.get('total')} images, "
                f"{data.get('brokenCount')} broken, {data.get('neverLoadedCount')} never loaded")
        ev.append(bad(line) if (data.get("brokenCount") or data.get("neverLoadedCount")) else line)

        if data.get("brokenCount"):
            problems.append(f"{path}: {data['brokenCount']} broken image(s) "
                            f"({', '.join(data['broken'])})")
            ev += [bad(f"    broken: {n}") for n in data["broken"]]
        if data.get("neverLoadedCount"):
            problems.append(f"{path}: {data['neverLoadedCount']} image(s) never load - they carry "
                            f"data-src with no src ({', '.join(data['neverLoaded'][:4])}...)")
            ev += [bad(f"    never loads: {n}") for n in data["neverLoaded"]]

    if problems:
        return Result(STATUS_FAIL, "; ".join(problems), ev, urls=[ctx.base_url + p for p in ("/", "/contact-us")])
    if not rendered:
        return Result(None, "Browser image checks were blocked; no rendered pages were available.", ev,
                      automated=False, error=ctx.browser_error, urls=[ctx.base_url + p for p in ("/", "/contact-us")])
    return Result(STATUS_PASS, "", ev, urls=[ctx.base_url + p for p in rendered])


def check_spelling(ctx: Context) -> Result:
    """Not automatable. Gather nothing; a person decides."""
    return Result(None, "",
                  ["Spelling and grammar have no reliable automated signal.",
                   "Set Pass or Fail yourself after reviewing the pages you normally scan."],
                  automated=False)


CHECKS = {
    "siem_calculator":   check_siem_calculator,
    "click_to_call":     check_click_to_call,
    "calendly":          check_calendly,
    "contact_form":      check_contact_form,
    "get_in_touch_form": check_get_in_touch_form,
    "red_checklist":     check_red_checklist,
    "cta_buttons":       check_cta_buttons,
    "landing_pages":     check_landing_pages,
    "page_speed":        check_page_speed,
    "mobile_responsive": check_mobile_responsive,
    "nav_menus":         check_nav_menus,
    "cookie_banner":     check_cookie_banner,
    "spelling":          check_spelling,
    "images_media":      check_images_media,
}
