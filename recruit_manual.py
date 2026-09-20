"""Cached job producer and LinkedIn-to-Ceipal manual applicant pipeline."""

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace

# Statuses never processed again: saved/duplicate are Ceipal's own verdicts, and
# form_ready means the parsed form is waiting for the recruiter to save it.
FINAL = {"saved", "duplicate", "form_ready"}
UNCERTAIN = {"saving", "review_required"}

# The public member URL a talent profile links to under Personal Information.
PUBLIC_PROFILE_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*linkedin\.com/in/[^\s?#]+", re.I)


def is_applicant_snapshot(url):
    return bool(re.match(r"^/(?:applicant_profiles|applicantprofiles)/snapshot/[^/]+", urlsplit(url).path, re.I))


def read_store(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


PARSE_RESUME_SELECTOR = "#parse_resume"
PARSE_TIMEOUT_S = 180


async def fill_if_empty(page, selector, value):
    """Placeholder contact details go only where the parsed resume left a blank."""
    field = page.locator(selector).first
    if not await field.count() or (await field.input_value()).strip():
        return False
    await field.fill(value)
    await field.blur()
    return True


async def choose_option(page, selector, label):
    """Pick a dropdown option by its visible text, as the recruiter would."""
    field = page.locator(selector).first
    if not await field.count():
        return False
    try:
        await field.select_option(label=label)
        return True
    except Exception:
        pass
    return bool(await page.evaluate(
        """([selector, label]) => {
            const field = document.querySelector(selector);
            if (!field) return false;
            const wanted = label.trim().toLowerCase();
            const option = Array.from(field.options).find(o => o.text.trim().toLowerCase() === wanted);
            if (!option) return false;
            field.value = option.value;
            field.dispatchEvent(new Event('input', {bubbles: true}));
            field.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""",
        [selector, label]))


async def create_applicant(page, row, api, persist):
    """Parse the generated resume into Ceipal and save the applicant it produces.

    The PDF is handed to the Parsing Resume input, which builds the applicant
    record; placeholders are only written into fields the parse left blank.
    """
    path = Path(row.get("resume_path", ""))
    if not path.is_file():
        raise ValueError(
            f"Resume {path} is missing; clear this profile's 'profile' entry to rebuild it.")

    await page.goto(os.getenv("CEIPAL_ADD_APPLICANT_URL", api.existing.CEIPAL_ADD_APPLICANT_URL),
                    wait_until="domcontentloaded", timeout=180000)
    upload = page.locator(os.getenv("CEIPAL_PARSE_RESUME_SELECTOR", PARSE_RESUME_SELECTOR)).first
    await upload.wait_for(state="attached", timeout=60000)
    await upload.set_input_files(str(path))

    # Ceipal asks "Please confirm to proceed" before it posts the resume.
    confirm = page.locator(api.existing.CEIPAL_CONFIRM_SELECTOR).first
    await confirm.wait_for(state="visible", timeout=60000)
    await confirm.click()

    # Parsing is asynchronous: the applicant form is injected when it lands.
    parsed = page.locator(api.existing.CEIPAL_PARSED_FORM_SELECTOR)
    duplicate = page.locator(api.existing.CEIPAL_DUPLICATE_MODAL_SELECTOR)
    deadline = time.time() + PARSE_TIMEOUT_S
    while time.time() < deadline:
        if await duplicate.count() and await duplicate.is_visible():
            row["status"] = "duplicate"
            persist()
            print("    Ceipal already knows this applicant.", flush=True)
            return
        if await parsed.count():
            break
        await asyncio.sleep(1)
    else:
        raise TimeoutError("Ceipal did not return the parsed applicant form")

    # Persist placeholder identity before the first save attempt for stable retries.
    if "dummy_email" not in row:
        row["dummy_email"] = api.existing.dummy_email(
            hashlib.sha256(row["url"].encode()).hexdigest()[:16])
        row["dummy_mobile"] = api.existing.dummy_mobile()
        persist()
    if await fill_if_empty(page, api.existing.CEIPAL_EMAIL_SELECTOR, row["dummy_email"]):
        print(f"    filled placeholder email {row['dummy_email']}", flush=True)
    if await fill_if_empty(page, api.existing.CEIPAL_MOBILE_SELECTOR, row["dummy_mobile"]):
        print("    filled placeholder mobile number", flush=True)
    source = os.getenv("CEIPAL_SOURCE", api.existing.CEIPAL_DEFAULT_SOURCE)
    if await choose_option(page, api.existing.CEIPAL_SOURCE_SELECTOR, source):
        print(f"    set source to {source}", flush=True)

    # The parsed form is left open for the recruiter to complete and save.
    # Uncomment the block below to save it automatically instead.
    #
    # save = page.locator(os.getenv("CEIPAL_PARSE_SAVE_SELECTOR",
    #                               api.existing.CEIPAL_SAVE_SELECTOR + ":visible")).first
    # await save.wait_for(state="visible", timeout=60000)
    # row["status"] = "saving"
    # persist()
    # await save.click()
    # for _ in range(60):
    #     if is_applicant_snapshot(page.url):
    #         row["status"] = "saved"
    #         row["applicant_url"] = page.url
    #         persist()
    #         return
    #     if await page.locator(api.existing.CEIPAL_DUPLICATE_MODAL_SELECTOR).is_visible():
    #         row["status"] = "duplicate"
    #         persist()
    #         return
    #     await asyncio.sleep(1)
    # raise RuntimeError("Ceipal save not confirmed; reconcile this applicant before retrying.")

    row["status"] = "form_ready"
    persist()
    print("    parsed form is ready in Ceipal - save it manually", flush=True)


def find_public_profile_url(text, name):
    """The talent page hides the member's own public /in/ URL; prefer its slug.

    Other people's URLs (sidebars, "people also viewed") can appear in the same
    text, so the owner's name is matched against each candidate slug first.
    """
    candidates = []
    for match in PUBLIC_PROFILE_RE.finditer(text or ""):
        segments = [segment for segment in urlsplit(match.group(0)).path.split("/") if segment]
        if len(segments) != 2 or segments[0] != "in":
            continue
        url = f"https://www.linkedin.com/in/{segments[1]}"
        if url not in candidates:
            candidates.append(url)
    if not candidates:
        return ""

    words = re.findall(r"[A-Za-z]{3,}", name or "")
    tokens = {words[0].casefold(), words[-1].casefold()} if words else set()
    for url in candidates:
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].casefold()
        if tokens and all(token in slug for token in tokens):
            return url
    return candidates[0]


async def settled_text(content, quiet_rounds=3, max_rounds=24):
    """LinkedIn hydrates a profile progressively; read once its text stops growing."""
    text = ""
    previous = -1
    stable = 0
    for _ in range(max_rounds):
        text = (await content.inner_text()).strip()
        if text and len(text) == previous:
            stable += 1
            if stable >= quiet_rounds:
                break
        else:
            stable = 0
            previous = len(text)
        await asyncio.sleep(0.5)
    return text


async def read_profile_text(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    if any(marker in page.url for marker in ("/login", "/authwall", "/checkpoint")):
        raise RuntimeError("LinkedIn profile requires login/verification.")
    override = os.getenv("LINKEDIN_PROFILE_CONTENT_SELECTOR")
    content = page.locator(override or 'main, [role="main"]').first
    await content.wait_for(state="visible", timeout=30000)
    # The container is visible before it is filled, so wait for the text to settle.
    text = await settled_text(content)
    # Only expand text inside the profile content, never navigate to other people.
    expanded = False
    for button in await content.get_by_role("button", name=re.compile(r"^(see more|show more|show all)$", re.I)).all():
        if await button.is_visible():
            await button.click()
            expanded = True
    if expanded:
        text = await settled_text(content)
    if len(text) < 50:
        raise ValueError("No usable profile content found.")
    return text


async def extract_profile(page, url):
    """Read the talent profile only. The public /in/ page is never opened.

    Browsing linkedin.com member profiles from a Recruiter session risks the
    account, so the public URL is only recorded (for the resume), never visited.
    """
    text = await read_profile_text(page, url)
    raw = {"url": url, "public_sections": [text]}
    name = text.splitlines()[0].strip()
    public_url = find_public_profile_url(text, name)
    if public_url:
        raw["public_url"] = public_url
    return raw


RESUME_CSS = """
  @page { size: A4; margin: 14mm 13mm; }
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", Calibri, Arial, sans-serif; color: #1f2328;
         font-size: 10.5pt; line-height: 1.45; margin: 0; }
  header { border-bottom: 2px solid #1f2328; padding-bottom: 8px; margin-bottom: 14px; }
  h1 { font-size: 21pt; margin: 0 0 4px; letter-spacing: 0.3px; }
  .headline { font-size: 11.5pt; color: #444d56; margin-bottom: 5px; }
  .meta { font-size: 9.5pt; color: #57606a; }
  .meta span + span::before { content: "  |  "; color: #b0b7bf; }
  h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: 0.9px;
       border-bottom: 1px solid #d0d7de; padding-bottom: 3px;
       margin: 14px 0 7px; color: #1f2328; }
  .entry { margin-bottom: 10px; break-inside: avoid; }
  .sub { color: #444d56; font-size: 10pt; }
  .chip { display: inline-block; border: 1px solid #d0d7de; border-radius: 3px;
          padding: 1px 7px; margin: 0 5px 5px 0; font-size: 9.5pt; }
"""


def escape(value):
    return html.escape(value or "", quote=True)


def resume_name(profile):
    return " ".join(part.strip() for part in (profile.get("first_name"), profile.get("last_name"))
                    if part and part.strip())


def resume_filename(profile, url):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", resume_name(profile)).strip("-") or "profile"
    source = re.sub(r"[^A-Za-z0-9._-]+", "-", urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return f"{name}-{source or 'profile'}.pdf"


def render_resume_html(profile, url, public_url=""):
    """Resume for the applicant: name, location, public profile URL and skills.

    The public /in/ URL is the candidate's own profile link; the talent URL is
    only a fallback for the rare profile that does not expose one.
    """
    name = resume_name(profile) or "Candidate"
    parts = ["<header>", f"<h1>{escape(name)}</h1>"]
    if profile.get("job_title"):
        parts.append(f"<div class='headline'>{escape(profile['job_title'])}</div>")
    location = ", ".join(part.strip() for part in (profile.get("city"), profile.get("state"))
                         if part and part.strip())
    meta = [item for item in (location, public_url or url) if item]
    if meta:
        parts.append("<div class='meta'>"
                     + "".join(f"<span>{escape(item)}</span>" for item in meta) + "</div>")
    parts.append("</header>")

    skills = [item.get("skill", "").strip() for item in profile.get("skills", []) if item.get("skill")]
    if skills:
        parts.append("<h2>Skills</h2>")
        parts.append("<div class='skills'>"
                     + "".join(f"<span class='chip'>{escape(skill)}</span>" for skill in skills) + "</div>")

    if profile.get("certifications"):
        parts.append("<h2>Certifications</h2>")
        for block in profile["certifications"]:
            parts.append("<div class='entry'>"
                         + "".join(f"<div class='sub'>{escape(line)}</div>" for line in block.splitlines())
                         + "</div>")

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{escape(name)} - Resume</title><style>{RESUME_CSS}</style></head>"
            f"<body>{''.join(parts)}</body></html>")


async def write_resume_pdf(context, html_doc, path):
    page = await context.new_page()
    try:
        await page.set_content(html_doc, wait_until="load")
        client = await context.new_cdp_session(page)
        try:
            await client.send("Page.enable")
            result = await client.send("Page.printToPDF", {
                "printBackground": True,
                "paperWidth": 8.27, "paperHeight": 11.69,
                "marginTop": 0.55, "marginBottom": 0.55,
                "marginLeft": 0.51, "marginRight": 0.51,
            })
        finally:
            await client.detach()
    finally:
        await page.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(result["data"]))


async def produce_jobs(api, args, ceipal, jobs, history, job_path, queue, failures):
    """Persist jobs independently of profile processing; never await a profile."""
    stored = 0
    try:
        urls = [args.job_url] if args.job_url else await api.discover_jobs(ceipal, args.jobs_limit)
        for url in urls:
            code = api.job_code_for(url)
            try:
                if not args.search_only and history.get(code, {}).get("status") == "completed":
                    print(f"Job {code} already completed; skipping.", flush=True)
                    continue
                cached = jobs.get(code)
                if not cached:
                    job_args = SimpleNamespace(job_url=url, jd_file=None, location=None)
                    title = await api.job_title(ceipal, job_args)
                    location = await api.job_location(ceipal, job_args)
                    cached = {"url": url, "code": code, "title": title,
                              "location": location, "extracted_at": time.time()}
                    jobs[code] = cached
                    api.save_state(job_path, jobs)
                    print(f"Stored new job: {code} / {title}", flush=True)
                else:
                    print(f"Using cached job: {code}", flush=True)
                queue.put_nowait(dict(cached))
                stored += 1
                print(f"Job collector: {stored} queued; continuing without waiting for profiles.", flush=True)
            except Exception as exc:
                failures.append(code)
                print(f"Job collection failed for {code}: {type(exc).__name__}: {exc}; continuing.", flush=True)
    finally:
        queue.put_nowait(None)
        print(f"Job collection finished: {stored} queued. Profile processing is independent.", flush=True)


async def run(api, args):
    from url import extraction_settings, check_local_model
    provider, model = extraction_settings()
    needs_ai = provider != "none" and not args.search_only and not args.jobs_only
    if needs_ai and provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY in .env for profile skill extraction.")
    if needs_ai:
        # Fail before login if AI dependencies are missing.
        import pydantic
        if provider == "openai":
            if os.getenv("AI_PROVIDER", "auto").strip().lower() != "auto":
                import openai
        else:
            await asyncio.to_thread(check_local_model, model)
    job_path = api.ROOT / "extract_job.json"
    profile_path = api.ROOT / "profile_data.json"
    history_path = api.ROOT / "job_history.json"
    jobs = read_store(job_path)
    profiles = read_store(profile_path)
    history = read_store(history_path)
    for row in profiles.values():
        if row.get("status") == "saving":
            row["status"] = "review_required"
    def persist():
        api.save_state(profile_path, profiles)
    persist()
    async with api.async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(api.ROOT / "browser-data-recruit"), headless=False,
            accept_downloads=False, viewport={"width": 1440, "height": 900})
        context.set_default_timeout(30000)
        try:
            ceipal = context.pages[0] if context.pages else await context.new_page()
            if args.jobs_only:
                await api.login(ceipal, True)
                await ceipal.wait_for_function(
                    "() => !/\\/pages\\/signin|\\/users\\/autologout/.test(location.pathname)",
                    timeout=240000)
                failures = []
                await produce_jobs(api, args, ceipal, jobs, history, job_path,
                                   asyncio.Queue(), failures)
                return 1 if failures else 0
            linkedin = await context.new_page()
            await asyncio.gather(api.login(ceipal, True), api.login(linkedin))
            await api.wait_for_sites_ready(ceipal, linkedin)
            queue = asyncio.Queue()
            failures = []

            async def consume_jobs():
                profile_page = await context.new_page()
                applicant_page = await context.new_page()
                while True:
                    job = await queue.get()
                    if job is None:
                        return
                    try:
                        await process_job(api, args, linkedin, profile_page, applicant_page,
                                          job, profiles, history, history_path, persist)
                    except Exception as exc:
                        failures.append(job["code"])
                        print(f"Job {job['code']} incomplete: {type(exc).__name__}: {exc}", flush=True)

            producer = asyncio.create_task(produce_jobs(
                api, args, ceipal, jobs, history, job_path, queue, failures))
            consumer = asyncio.create_task(consume_jobs())
            try:
                await asyncio.gather(producer, consumer)
            finally:
                for task in (producer, consumer):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(producer, consumer, return_exceptions=True)
                api.save_state(job_path, jobs)
            return 1 if failures else 0
        finally:
            await context.close()


async def process_job(api, args, linkedin, profile_page, applicant_page,
                      job, profiles, history, history_path, persist):
    from url import build_skill_set

    code = job["code"]
    saved_job = history.get(code, {})
    urls = saved_job.get("profiles")
    if not urls or args.search_only:
        await linkedin.goto(api.SEARCH_URL, wait_until="domcontentloaded")
        await api.apply_title(linkedin, job["title"])
        await api.apply_location(linkedin, args.location or job["location"])
        await api.wait_for_search_results(linkedin)
        urls = await api.collect_profiles(linkedin, args.limit)
        if not urls:
            raise ValueError("No LinkedIn profiles found; job remains pending.")
        if args.search_only:
            print(f"Search-only: {len(urls)} profile URLs for {code}", flush=True)
            return
        history[code] = {"status": "pending", "profiles": urls, "job_url": job["url"]}
        api.save_state(history_path, history)
    for url in urls[:args.limit]:
        row = profiles.setdefault(url, {"url": url, "status": "pending", "job_codes": []})
        if code not in row["job_codes"]:
            row["job_codes"].append(code)
            persist()
        if row["status"] in FINAL | UNCERTAIN:
            continue
        try:
            if not row.get("raw"):
                row["raw"] = await extract_profile(profile_page, url)
                row["extracted_at"] = time.time()
                persist()
            if not row.get("profile"):
                row["profile"] = await asyncio.to_thread(
                    build_skill_set, row["raw"])
                row["status"] = "extracted"
                persist()
            # The PDF is derived from the profile, so rebuild it when the file is
            # gone instead of leaving the profile permanently unprocessable.
            if not Path(row.get("resume_path", "")).is_file():
                target = api.ROOT / "resumes" / resume_filename(row["profile"], url)
                await write_resume_pdf(
                    profile_page.context,
                    render_resume_html(row["profile"], url, row["raw"].get("public_url")),
                    target)
                row["resume_path"] = str(target)
                persist()
                print(f"    resume written to {target}", flush=True)
            profile = row["profile"]
            print(
                f"    profile: {profile.get('first_name')} {profile.get('last_name')}"
                f" | city={profile.get('city')!r} state={profile.get('state')!r}"
                f" | skills={len(profile.get('skills', []))}", flush=True)
            await create_applicant(applicant_page, row, api, persist)
            row.pop("error", None)
            persist()
        except Exception as exc:
            row["status"] = "review_required" if row["status"] in UNCERTAIN else "failed"
            error = str(exc)
            key = os.getenv("OPENAI_API_KEY")
            row["error"] = error.replace(key, "[REDACTED]") if key else error
            persist()
            print(f"Profile {url}: {row['status']}: {row['error']}", flush=True)
            if applicant_page is not None:
                try:
                    fields = await applicant_page.locator('input:not([type="hidden"]), select, textarea').evaluate_all(
                        "els => els.map(e => ({tag:e.tagName,id:e.id,name:e.name,type:e.type,placeholder:e.getAttribute('placeholder'),visible:!!e.getClientRects().length}))")
                    buttons = await applicant_page.locator('button, input[type="submit"], a[role="button"]').evaluate_all(
                        "els => els.filter(e => e.getClientRects().length).map(e => ({tag:e.tagName,id:e.id,classes:e.className,text:(e.innerText || e.value || '').trim()}))")
                    errors = await applicant_page.locator('.error:visible, .invalid-feedback:visible, .help-block:visible').all_inner_texts()
                    api.save_state(api.ROOT / "ceipal_form_dump.json", {"url": applicant_page.url, "fields": fields, "buttons": buttons, "errors": errors})
                except Exception:
                    pass
    if not all(profiles.get(url, {}).get("status") in FINAL for url in urls):
        raise RuntimeError("Some profiles failed or require save reconciliation; see profile_data.json.")
    history[code]["status"] = "completed"
    history[code]["completed_at"] = time.time()
    api.save_state(history_path, history)
