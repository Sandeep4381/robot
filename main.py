"""Shared Ceipal / LinkedIn selectors and helpers for the harvesting flow.

`recruit.py` imports this module for the DOM selectors of both sites, the Ceipal
endpoints and source label, and the dummy email/mobile generators, so those
definitions live in one place:

    import main as existing

Run `recruit.py`, not this file. It is the current entry point: it works from
Ceipal job codes and keeps its queue in the `resume_harvesting` table.

The `main()` entry point further down is the earlier flow, which walked a list of
LinkedIn profile URLs from its own table. It is superseded and its table
(`DB_TABLE`, default `harvesting`) is no longer configured in .env, so running it
will not find a queue.

How the scraping helpers work:
- The main profile page only carries the headline and About text. Work history,
  education, skills and certifications live on separate /details/... routes.
- LinkedIn's markup uses rotating hashed class names and the detail pages have no
  stable ids, so extraction is anchored on the rendered text layout, which is
  much more stable than the DOM.

Notes:
- Automated access to LinkedIn is against their User Agreement and heavy usage
  can get an account restricted.
- Ceipal's Add Applicant page parses the PDF into an applicant record, so the
  placeholders below are only ever written into fields the PDF left blank.
"""

import base64
import html
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
SESSION_DIR = BASE_DIR / "browser-data"
OUTPUT_DIR = BASE_DIR / "resumes"

CEIPAL_SIGNIN_URL = "https://talenthirecls12.ceipal.com/pages/signin"
CEIPAL_ADD_APPLICANT_URL = "https://talenthirecls12.ceipal.com/applicant_profiles/add_applicant"
DUMMY_EMAIL_PREFIX = "dummy"
DUMMY_EMAIL_DOMAIN = "resumehavesting.bot"
CEIPAL_DEFAULT_SOURCE = "LinkedIn Resume Harvesting"

# Queue table: url rows start at status 0 and flip to 1 once Ceipal has them.
# Rows that are not LinkedIn profiles are marked 2 so they are never retried.
DB_NAME = "resume_harvesting"
DB_TABLE = "harvesting"
DB_PENDING_STATUS = 0
DB_DONE_STATUS = 1
DB_INVALID_STATUS = 2

LINKEDIN_PROFILE_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*linkedin\.com/in/[^\s/?#]+", re.I
)

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"

EMAIL_SELECTORS = ('input[type="email"]:visible', "#username", 'input[autocomplete="username"]')
PASSWORD_SELECTORS = ('input[type="password"]:visible', "#password", 'input[autocomplete="current-password"]')

CEIPAL_EMAIL_SELECTORS = (
    "#UserLoginUsername",
    'input[name="data[UserLogin][username]"]',
    'input[placeholder="User Name"]',
)
CEIPAL_PASSWORD_SELECTORS = (
    "#UserLoginPassword",
    'input[name="data[UserLogin][password]"]',
)
CEIPAL_SUBMIT_SELECTORS = (
    "#signin_btn:visible",
    'button[type="submit"]:visible',
    'input[type="submit"]:visible',
    'button:has-text("Sign In"):visible',
)

# Fields the parsed form leaves blank; filled in before the save click.
CEIPAL_SOURCE_SELECTOR = "#referred_by"
CEIPAL_EMAIL_SELECTOR = "#email"
CEIPAL_MOBILE_SELECTOR = "#mobile_number"

# Ceipal's own "Please confirm to proceed" step before it posts the resume.
CEIPAL_CONFIRM_SELECTOR = "#confirmation button.confirm"
CEIPAL_PARSED_FORM_SELECTOR = ".applicant-form-details #form_all"
CEIPAL_DUPLICATE_MODAL_SELECTOR = "#SingleEmailDuplicateModal"
CEIPAL_SAVE_SELECTOR = ".applicant-form-details button.save_all"
CEIPAL_SNAPSHOT_MARKER = "/applicant_profiles/snapshot/"

DETAIL_ROUTES = (
    ("experience", "details/experience/", "Experience"),
    ("education", "details/education/", "Education"),
    ("skills", "details/skills/", "Skills"),
    ("certifications", "details/certifications/", "Licenses & certifications"),
)

SECTIONS = (
    "About",
    "Activity",
    "Experience",
    "Education",
    "Skills",
    "Licenses & certifications",
    "Projects",
    "Courses",
    "Honors & awards",
    "Languages",
    "Volunteering",
    "Publications",
    "Patents",
    "Organizations",
    "Recommendations",
)

FOOTER_MARKERS = (
    "More profiles for you",
    "Explore Premium profiles",
    "People you may know",
    "You might like",
    "Select language",
    "LinkedIn Corporation",
)

DATE_RANGE_RE = re.compile(
    r"^(?:[A-Z][a-z]{2}\s+)?\d{4}\s*[-\u2013\u2014]\s*(?:Present|(?:[A-Z][a-z]{2}\s+)?\d{4})"
)
EMPLOYMENT_TYPES = (
    "Full-time|Part-time|Self-employed|Freelance|Contract|Internship|Apprenticeship|Seasonal"
)
# "Ginesys One · Full-time"
COMPANY_TYPE_RE = re.compile(rf"^(?P<company>.+?)\s*\u00b7\s*(?:{EMPLOYMENT_TYPES})$")
# "Full-time · 1 yr 7 mos" - the header of a company holding several roles
COMPANY_DURATION_RE = re.compile(rf"^(?:{EMPLOYMENT_TYPES})\s*\u00b7\s*\d+\s*(?:yr|mo)")
SKILLS_META_RE = re.compile(r"((^|\s)skills?:)|(\band \+\d+ skills)|(^\+?\d+ skills$)", re.I)
DETAIL_NOISE_RE = re.compile(r"^(Show all|Endorse|LinkedIn helped me get this job|helped me get this job)", re.I)
EDU_EXTRA_RE = re.compile(r"^(Activities and societies|Grade|Score)\s*:", re.I)
CERT_EXTRA_RE = re.compile(r"^(Credential ID|Show credential|Skills)\b", re.I)
# "Nothing to see for now" / "...will appear here." are LinkedIn's empty-section
# placeholders and must never be mistaken for profile content.
NOISE_LINE_RE = re.compile(
    r"^(\u2026 more|Show all|Show more|Follow|Connect|Message)$"
    r"|^Nothing to see for now"
    r"|will appear here",
    re.I,
)
SKILL_NOISE_RE = re.compile(
    r"(endorsement|linkedin skill assessment|^show |^endorse$|^endorsed by|^all$|^industry knowledge$"
    r"|^tools & technologies$|\s+at\s+)",
    re.I,
)

HEADLESS = False
PAGE_TIMEOUT_MS = 60_000
LOGIN_TIMEOUT_S = 180
CEIPAL_PARSE_TIMEOUT_S = 180
DELAY_BETWEEN_PROFILES_S = 5.0
DELAY_BETWEEN_PAGES_S = 2.0
DELAY_BETWEEN_UPLOADS_S = 3.0


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()


def profile_slug(url):
    parts = [part for part in urlsplit(url).path.split("/") if part]
    slug = parts[-1] if parts else "profile"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-") or "profile"


def valid_linkedin_url(url):
    return bool(LINKEDIN_PROFILE_URL_RE.match((url or "").strip()))


# --------------------------------------------------------------------------
# browser / login
# --------------------------------------------------------------------------

def is_logged_in(page):
    url = page.url
    if any(marker in url for marker in ("/login", "/checkpoint", "/authwall", "/uas/")):
        return False
    return "/feed" in url or "/mynetwork" in url or "/in/" in url


def wait_for_login(page, timeout=LOGIN_TIMEOUT_S):
    deadline = time.time() + timeout
    warned = False
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if is_logged_in(page):
            return True
        if not warned and any(marker in page.url for marker in ("/checkpoint", "/uas/", "/authwall")):
            print("LinkedIn is asking for verification - finish it in the browser window.", flush=True)
            warned = True
    return False


def first_visible(page, candidates, timeout_s=30):
    """LinkedIn's login markup changes often; try each candidate until one shows up."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for candidate in candidates:
            locator = page.locator(candidate).first if isinstance(candidate, str) else candidate.first
            try:
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                continue
        page.wait_for_timeout(500)
    return None


def login(page, email, password):
    page.goto(FEED_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    if is_logged_in(page):
        print("Existing session reused, no login needed.", flush=True)
        return True

    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    email_field = first_visible(page, EMAIL_SELECTORS)
    password_field = first_visible(page, PASSWORD_SELECTORS)
    submit_button = first_visible(
        page,
        (
            # exact name, otherwise "Sign in with Microsoft" / "Sign in with Apple" win the match
            page.get_by_role("button", name="Sign in", exact=True),
            'button[type="submit"]',
            "#login-submit",
        ),
    )
    if not email_field or not password_field or not submit_button:
        print("Could not find the login form. LinkedIn may have changed the page or be blocking this browser.", flush=True)
        return False

    email_field.fill(email)
    password_field.fill(password)
    submit_button.click()

    if wait_for_login(page):
        print("Logged in.", flush=True)
        return True
    print("Login did not complete.", flush=True)
    return False


def wait_for_any(page, titles, timeout_s=25):
    """Wait until one of these section titles is rendered as a line of its own."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if any(title in lines_from_text(page.evaluate("document.body.innerText") or "") for title in titles):
            return True
        page.wait_for_timeout(500)
    return False


def settle(page, quiet_ms=1200, max_rounds=12):
    """LinkedIn hydrates profile sections progressively; wait for the text to stop growing."""
    previous = -1
    for _ in range(max_rounds):
        length = len(page.evaluate("document.body.innerText") or "")
        if length == previous:
            return
        previous = length
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(quiet_ms)


def page_lines(page):
    return lines_from_text(page.evaluate("document.body.innerText") or "")


# A skill row reads "<skill> / <role> / <company|project> / <endorsements...>". The
# row is the only structure that separates skill names from the company and project
# names they are attached to, so collect every line that sits after the first line
# of such a row and treat those as context, not skills.
SKILL_CONTEXT_JS = r"""() => {
  const CONTEXT = /(\sat\s|endorsement|LinkedIn Skill Assessment|^Show all|^Show \d|^Endorsed by)/i;
  const NOISE = /(notification|^Home$|^My Network$|^Jobs$|^Messaging$|^Me$|^For Business$|^Learning$)/i;
  const lines = el => (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  const isRow = el => {
    const ls = lines(el);
    if (!ls.length || ls.length > 8) return false;
    if (NOISE.test(ls[0])) return false;
    if (/\s(at|\u00b7)\s/.test(ls[0]) || /^(Connect|Message|Follow)$/.test(ls[0])) return false;
    return ls.slice(1).some(line => CONTEXT.test(line));
  };
  const found = new Set();
  for (const el of document.querySelectorAll('*')) {
    const text = (el.innerText || '').trim();
    if (!text || text.length > 500 || !isRow(el)) continue;
    for (const line of lines(el).slice(1)) found.add(line);
  }
  return Array.from(found);
}"""


def skill_context_names(page):
    try:
        return set(page.evaluate(SKILL_CONTEXT_JS))
    except Exception:
        return set()


def lines_from_text(text):
    """Rendered text trimmed to the content above the site footer, as useful lines."""
    cut = len(text)
    for marker in FOOTER_MARKERS:
        index = text.find(marker)
        if index != -1:
            cut = min(cut, index)
    body = text[:cut]

    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line in ("\u00b7", "\u2022"):
            continue
        if any(line.startswith(prefix) for prefix in ("Skip to ",)):
            continue
        lines.append(line)
    return lines


def section_lines(lines, title):
    """Content of one profile section, stopping at the next known section title."""
    try:
        start = lines.index(title)
    except ValueError:
        return []

    end = len(lines)
    for other in SECTIONS:
        if other == title:
            continue
        try:
            index = lines.index(other, start + 1)
        except ValueError:
            continue
        end = min(end, index)

    # search(), not match(): some placeholders only give themselves away mid-line
    return [line for line in lines[start + 1:end] if not NOISE_LINE_RE.search(line)]


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

def split_location(location):
    """LinkedIn writes "City, State, Country" (sometimes just "Country")."""
    parts = [part.strip() for part in (location or "").split(",") if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[-1]


def parse_header(lines):
    """Name, headline, location and company from the profile top card."""
    start = 0
    for index, line in enumerate(lines[:40]):
        if line in ("Learning", "For Business"):
            start = index + 1

    name = lines[start] if start < len(lines) else ""
    headline = lines[start + 1] if start + 1 < len(lines) else ""

    location = ""
    if "Contact info" in lines:
        contact = lines.index("Contact info")
        for index in range(contact - 1, max(-1, contact - 6), -1):
            candidate = lines[index]
            if candidate.isdigit():
                continue
            location = candidate
            break

    company = ""
    if "Contact info" in lines:
        contact = lines.index("Contact info")
        for index in range(contact + 1, min(len(lines), contact + 8)):
            candidate = lines[index]
            if candidate.isdigit() or candidate == "connections" or "mutual connection" in candidate.lower():
                continue
            company = candidate
            break

    city, country = split_location(location)
    return {
        "name": name,
        "headline": headline,
        "location": location,
        "city": city,
        "country": country,
        "company": company,
    }


def parse_about(lines):
    about = section_lines(lines, "About")
    return " ".join(about).strip()


def parse_experience(lines):
    """Entries come in two shapes.

    Usual:        title / "Company · Full-time" / dates / location / bullets
    Grouped role: company holds several roles, so the company is declared once as
                  "Company" + "Full-time · total duration", and each role below it
                  is just title / dates / bullets with no company of its own.
    """
    section = section_lines(lines, "Experience")

    company_at = {}
    for index, line in enumerate(section):
        match = COMPANY_TYPE_RE.match(line)
        if match:
            company_at[index] = match.group("company")
        elif index > 0 and COMPANY_DURATION_RE.match(line):
            company_at[index - 1] = section[index - 1]

    # a role title is the line directly above a date range
    anchors = [
        index
        for index in range(len(section) - 1)
        if DATE_RANGE_RE.match(section[index + 1])
    ]

    entries = []
    for position, title_index in enumerate(anchors):
        above = section[title_index]
        match = COMPANY_TYPE_RE.match(above)
        if match:
            title = section[title_index - 1] if title_index >= 1 else ""
            company = match.group("company")
        else:
            title = above
            company = ""
            for index in sorted(company_at, reverse=True):
                if index < title_index:
                    company = company_at[index]
                    break

        if not title or DATE_RANGE_RE.match(title) or SKILLS_META_RE.search(title):
            continue

        next_title = anchors[position + 1] if position + 1 < len(anchors) else None
        if next_title is None:
            body_end = len(section)
        elif COMPANY_TYPE_RE.match(section[next_title]):
            body_end = next_title - 1
        else:
            body_end = next_title
        body = section[title_index + 2:max(title_index + 2, body_end)]

        location = ""
        bullets = []
        for line in body:
            if SKILLS_META_RE.search(line) or DETAIL_NOISE_RE.match(line):
                continue
            if line.startswith(("\u2022", "-", "\u2013")):
                bullets.append(line.lstrip("\u2022-\u2013 ").strip())
                continue
            if not bullets and not location:
                location = line
                continue
            # a wrapped bullet continues the sentence; anything else is trailing metadata
            if bullets and not bullets[-1].endswith((".", ":", ";", "!", "?")):
                joiner = "" if bullets[-1].endswith("-") else " "
                bullets[-1] = f"{bullets[-1]}{joiner}{line}"

        dates_line = section[title_index + 1]
        entries.append(
            {
                "title": title,
                "company": company,
                "dates": dates_line.split(" \u00b7 ")[0].strip(),
                "location": location,
                "bullets": bullets,
            }
        )
    return entries


def parse_education(lines):
    section = section_lines(lines, "Education")
    entries = []
    current = {}

    for line in section:
        is_date = bool(DATE_RANGE_RE.match(line))
        is_score = line.lower().startswith("score:")

        if is_date or is_score:
            current["dates" if is_date else "score"] = line
            entries.append(current)
            current = {}
            continue

        if not current and entries and EDU_EXTRA_RE.match(line):
            # "Activities and societies:" with nothing after the colon is not worth keeping
            if line.split(":", 1)[1].strip():
                entries[-1]["extra"] = line
            continue

        if "school" not in current:
            current["school"] = line
        elif "degree" not in current:
            current["degree"] = line
        else:
            entries.append(current)
            current = {"school": line}

    if current.get("school"):
        entries.append(current)
    return entries


def parse_certifications(lines):
    section = section_lines(lines, "Licenses & certifications")
    entries = []
    current = {}

    for line in section:
        if line.startswith("Issued "):
            current["issued"] = line
            entries.append(current)
            current = {}
            continue

        if CERT_EXTRA_RE.match(line) or line == "Show credential":
            if entries:
                entries[-1].setdefault("extra", []).append(line)
            continue

        if "name" not in current:
            current["name"] = line
        elif "issuer" not in current:
            current["issuer"] = line
        else:
            entries.append(current)
            current = {"name": line}

    if current.get("name"):
        entries.append(current)
    return entries


def parse_skills(lines, context_names=()):
    """Skill names and their company/project contexts render as identical text.

    The DOM does group them into rows though, so the caller passes the context
    lines found in those rows and they are subtracted here.
    """
    section = section_lines(lines, "Skills")
    skills = []
    for line in section:
        if SKILL_NOISE_RE.search(line) or line in context_names:
            continue
        if line not in skills:
            skills.append(line)
    return skills


def fetch_profile(page, url):
    slug = profile_slug(url)

    page.goto(url, wait_until="domcontentloaded")
    wait_for_any(page, ("Contact info", "About"))
    settle(page)
    main_lines = page_lines(page)
    profile = parse_header(main_lines)
    profile["about"] = parse_about(main_lines)

    for key, route, title in DETAIL_ROUTES:
        page.goto(f"https://www.linkedin.com/in/{slug}/{route}", wait_until="domcontentloaded")
        if not wait_for_any(page, (title,)):
            print(f"    note: no {title!r} section found", flush=True)
        settle(page)
        profile[key] = page_lines(page)
        if key == "skills":
            profile["skill_context"] = skill_context_names(page)
        time.sleep(DELAY_BETWEEN_PAGES_S)

    profile["experience"] = parse_experience(profile["experience"])
    # The header blurb is not always filled in; the roles usually carry a location.
    for entry in profile["experience"]:
        city, country = split_location(entry.get("location"))
        if not profile["city"] and city:
            profile["city"] = city
        if not profile["country"] and country:
            profile["country"] = country
        if profile["city"] and profile["country"]:
            break
    profile["education"] = parse_education(profile["education"])
    profile["skills"] = parse_skills(profile["skills"], profile.get("skill_context", ()))
    profile["certifications"] = parse_certifications(profile["certifications"])
    profile["url"] = url
    return profile


# --------------------------------------------------------------------------
# ceipal ats
# --------------------------------------------------------------------------

def dummy_email(slug):
    timestamp = time.strftime("%d%m%y%H%M%S")
    return f"{DUMMY_EMAIL_PREFIX}{timestamp}@{DUMMY_EMAIL_DOMAIN}"


def dummy_mobile():
    return str(random.randint(6, 9)) + "".join(random.choice("0123456789") for _ in range(9))


def ceipal_is_logged_in(page):
    return "ceipal.com" in page.url and "/pages/signin" not in page.url


def ceipal_login(page, email, password, signin_url):
    page.goto(signin_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if ceipal_is_logged_in(page):
        print("Existing Ceipal session reused, no login needed.", flush=True)
        return True

    email_field = first_visible(page, CEIPAL_EMAIL_SELECTORS)
    password_field = first_visible(page, CEIPAL_PASSWORD_SELECTORS)
    submit_button = first_visible(page, CEIPAL_SUBMIT_SELECTORS)
    if not email_field or not password_field or not submit_button:
        print("Could not find the Ceipal login form.", flush=True)
        return False

    email_field.fill(email)
    password_field.fill(password)
    submit_button.click()

    deadline = time.time() + LOGIN_TIMEOUT_S
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if ceipal_is_logged_in(page):
            print("Logged in to Ceipal.", flush=True)
            return True
    print("Ceipal login did not complete.", flush=True)
    return False


def fill_if_empty(page, selector, value):
    """Placeholder details are only added where the parsed resume left a field blank."""
    field = page.locator(selector).first
    if not field.count() or field.input_value().strip():
        return False
    field.fill(value)
    field.blur()
    return True


def fill_dummy_details(page, slug):
    """Email and mobile are never on a LinkedIn profile, so Ceipal gets placeholders."""
    email = dummy_email(slug)
    if fill_if_empty(page, CEIPAL_EMAIL_SELECTOR, email):
        print(f"    filled dummy email {email}", flush=True)
    if fill_if_empty(page, CEIPAL_MOBILE_SELECTOR, dummy_mobile()):
        print("    filled dummy mobile number", flush=True)


def choose_option(page, selector, label):
    """Pick a dropdown option by its visible text, as the recruiter would."""
    field = page.locator(selector).first
    if not field.count():
        return False
    try:
        field.select_option(label=label)
        return True
    except Exception:
        pass
    return bool(
        page.evaluate(
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
            [selector, label],
        )
    )


def ceipal_upload_resume(page, pdf_path, add_applicant_url, source):
    """Parse one resume PDF into Ceipal and save the applicant it produces."""
    page.bring_to_front()
    page.goto(add_applicant_url, wait_until="domcontentloaded")
    page.wait_for_selector("#parse_resume", state="attached", timeout=PAGE_TIMEOUT_MS)

    page.set_input_files("#parse_resume", str(pdf_path))

    # Ceipal asks "Please confirm to proceed" before it posts the resume.
    confirm = page.locator(CEIPAL_CONFIRM_SELECTOR)
    confirm.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    confirm.click()

    # Parsing is async: the applicant form is injected into the page when it lands.
    parsed = page.locator(CEIPAL_PARSED_FORM_SELECTOR)
    duplicate = page.locator(CEIPAL_DUPLICATE_MODAL_SELECTOR)
    deadline = time.time() + CEIPAL_PARSE_TIMEOUT_S
    while time.time() < deadline:
        if duplicate.count() and duplicate.is_visible():
            print("    Ceipal already knows this applicant, skipping.", flush=True)
            return False
        if parsed.count():
            break
        page.wait_for_timeout(1000)
    else:
        raise TimeoutError("Ceipal did not return the parsed applicant form")

    fill_dummy_details(page, pdf_path.stem)
    if choose_option(page, CEIPAL_SOURCE_SELECTOR, source):
        print(f"    set source to {source}", flush=True)

    # Save is intentionally left to the recruiter: the parsed form stays open on
    # screen with the details filled in. Uncomment the block below to auto-save.
    # save = page.locator(CEIPAL_SAVE_SELECTOR).first
    # save.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)
    # save.click()
    #
    # deadline = time.time() + CEIPAL_PARSE_TIMEOUT_S
    # while time.time() < deadline:
    #     page.wait_for_timeout(1000)
    #     if CEIPAL_SNAPSHOT_MARKER in page.url:
    #         return True

    print("    form is ready in Ceipal - save it manually", flush=True)
    return False


# --------------------------------------------------------------------------
# resume rendering
# --------------------------------------------------------------------------

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
       margin: 14px 0 7px; color: #1f2328;
       break-after: avoid; page-break-after: avoid; }
  .entry { margin-bottom: 10px; break-inside: avoid; page-break-inside: avoid; }
  .entry-head { display: flex; justify-content: space-between; gap: 12px; }
  .title { font-weight: 600; }
  .dates { color: #57606a; font-size: 9.5pt; white-space: nowrap; }
  .sub { color: #444d56; font-size: 10pt; }
  ul { margin: 5px 0 0; padding-left: 16px; }
  li { margin-bottom: 3px; }
  p.summary { margin: 0; text-align: justify; }
  .skills { margin: 0; }
  .chip { display: inline-block; border: 1px solid #d0d7de; border-radius: 3px;
          padding: 1px 7px; margin: 0 5px 5px 0; font-size: 9.5pt; }
"""


def escape(value):
    return html.escape(value or "", quote=True)


def render_resume(profile):
    parts = []

    meta = [item for item in (profile.get("location"), profile.get("company"), profile.get("url")) if item]
    parts.append("<header>")
    parts.append(f"<h1>{escape(profile.get('name'))}</h1>")
    if profile.get("headline"):
        parts.append(f"<div class='headline'>{escape(profile['headline'])}</div>")
    if meta:
        parts.append("<div class='meta'>" + "".join(f"<span>{escape(item)}</span>" for item in meta) + "</div>")
    parts.append("</header>")

    if profile.get("city") or profile.get("country"):
        parts.append("<h2>Location</h2>")
        if profile.get("city"):
            parts.append(f"<div class='sub'>City: {escape(profile['city'])}</div>")
        if profile.get("country"):
            parts.append(f"<div class='sub'>Country: {escape(profile['country'])}</div>")

    if profile.get("about"):
        parts.append("<h2>Summary</h2>")
        parts.append(f"<p class='summary'>{escape(profile['about'])}</p>")

    if profile.get("experience"):
        parts.append("<h2>Experience</h2>")
        for entry in profile["experience"]:
            parts.append("<div class='entry'>")
            parts.append("<div class='entry-head'>")
            parts.append(f"<div><span class='title'>{escape(entry['title'])}</span>")
            if entry.get("company"):
                parts.append(f" <span class='sub'>&mdash; {escape(entry['company'])}</span>")
            parts.append("</div>")
            if entry.get("dates"):
                parts.append(f"<div class='dates'>{escape(entry['dates'])}</div>")
            parts.append("</div>")
            if entry.get("location"):
                parts.append(f"<div class='sub'>{escape(entry['location'])}</div>")
            if entry.get("bullets"):
                parts.append("<ul>" + "".join(f"<li>{escape(bullet)}</li>" for bullet in entry["bullets"]) + "</ul>")
            parts.append("</div>")

    if profile.get("education"):
        parts.append("<h2>Education</h2>")
        for entry in profile["education"]:
            parts.append("<div class='entry'>")
            parts.append("<div class='entry-head'>")
            parts.append(f"<div><span class='title'>{escape(entry.get('degree') or entry.get('school'))}</span>")
            if entry.get("degree") and entry.get("school"):
                parts.append(f" <span class='sub'>&mdash; {escape(entry['school'])}</span>")
            parts.append("</div>")
            if entry.get("dates"):
                parts.append(f"<div class='dates'>{escape(entry['dates'])}</div>")
            parts.append("</div>")
            if entry.get("score"):
                parts.append(f"<div class='sub'>{escape(entry['score'])}</div>")
            if entry.get("extra"):
                parts.append(f"<div class='sub'>{escape(entry['extra'])}</div>")
            parts.append("</div>")

    if profile.get("skills"):
        parts.append("<h2>Skills</h2>")
        parts.append(
            "<div class='skills'>"
            + "".join(f"<span class='chip'>{escape(skill)}</span>" for skill in profile["skills"])
            + "</div>"
        )

    if profile.get("certifications"):
        parts.append("<h2>Certifications</h2>")
        for entry in profile["certifications"]:
            parts.append("<div class='entry'>")
            parts.append("<div class='entry-head'>")
            parts.append(f"<div><span class='title'>{escape(entry.get('name'))}</span>")
            if entry.get("issuer"):
                parts.append(f" <span class='sub'>&mdash; {escape(entry['issuer'])}</span>")
            parts.append("</div>")
            if entry.get("issued"):
                parts.append(f"<div class='dates'>{escape(entry['issued'].replace('Issued ', ''))}</div>")
            parts.append("</div>")
            extras = [item for item in entry.get("extra", []) if item.startswith("Skills:")]
            if extras:
                parts.append(f"<div class='sub'>{escape(extras[0])}</div>")
            parts.append("</div>")

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(profile.get('name'))} - Resume</title>"
        f"<style>{RESUME_CSS}</style></head><body>{''.join(parts)}</body></html>"
    )


def write_pdf(context, html_doc, path):
    page = context.new_page()
    try:
        page.set_content(html_doc, wait_until="load")
        client = context.new_cdp_session(page)
        try:
            client.send("Page.enable")
            result = client.send(
                "Page.printToPDF",
                {
                    "printBackground": True,
                    "paperWidth": 8.27,
                    "paperHeight": 11.69,
                    "marginTop": 0.55,
                    "marginBottom": 0.55,
                    "marginLeft": 0.51,
                    "marginRight": 0.51,
                },
            )
        finally:
            client.detach()
    finally:
        page.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(result["data"]))


# --------------------------------------------------------------------------
# queue database (MySQL)
# --------------------------------------------------------------------------

def db_settings():
    return {
        "host": os.getenv("DB_HOST", "").strip(),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", DB_NAME).strip(),
        "table": os.getenv("DB_TABLE", DB_TABLE).strip(),
    }


def db_connect(settings):
    import pymysql

    return pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=settings["database"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=15,
    )


def db_table_name(settings):
    """Table names cannot be bound as parameters, so keep them to a safe shape."""
    table = re.sub(r"[^A-Za-z0-9_]", "", settings["table"])
    if not table:
        raise ValueError("DB_TABLE is empty")
    return table


def db_ensure_table(conn, table):
    with conn.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS `{table}` ("
            "  id INT AUTO_INCREMENT PRIMARY KEY,"
            "  url VARCHAR(512) NOT NULL UNIQUE,"
            f"  status TINYINT NOT NULL DEFAULT {DB_PENDING_STATUS}"
            ")"
        )


def db_pending_urls(conn, table):
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT url FROM `{table}` WHERE status = %s ORDER BY date_posted, url",
            (DB_PENDING_STATUS,),
        )
        return [row[0].strip() for row in cursor.fetchall() if row[0] and row[0].strip()]


def db_set_status(conn, table, url, status):
    with conn.cursor() as cursor:
        cursor.execute(f"UPDATE `{table}` SET status = %s WHERE url = %s", (status, url))
        return cursor.rowcount


def load_db_urls(settings):
    conn = db_connect(settings)
    try:
        table = db_table_name(settings)
        db_ensure_table(conn, table)
        return conn, table, db_pending_urls(conn, table)
    except Exception:
        conn.close()
        raise


def main():
    email = os.getenv("LINKEDIN_EMAIL", "")
    password = os.getenv("LINKEDIN_PASSWORD", "")
    if not email or not password:
        print(f"Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in {ENV_FILE} and run again.")
        return 1

    ceipal_email = os.getenv("CEIPAL_EMAIL", "")
    ceipal_password = os.getenv("CEIPAL_PASSWORD", "")
    ceipal_signin_url = os.getenv("CEIPAL_SIGNIN_URL", CEIPAL_SIGNIN_URL)
    ceipal_add_url = os.getenv("CEIPAL_ADD_APPLICANT_URL", CEIPAL_ADD_APPLICANT_URL)
    ceipal_source = os.getenv("CEIPAL_SOURCE", CEIPAL_DEFAULT_SOURCE)
    upload_to_ceipal = bool(ceipal_email and ceipal_password)

    db = db_settings()
    if not db["host"]:
        print("Set DB_HOST (with DB_USER, DB_PASSWORD, DB_NAME, DB_TABLE) in .env and run again.")
        return 1
    try:
        conn, table, urls = load_db_urls(db)
    except Exception as exc:
        print(f"Could not read the MySQL queue at {db['host']}/{db['database']}.")
        print(f"  {type(exc).__name__}: {exc}")
        print("  Check DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME and DB_TABLE in .env.")
        return 1

    pending = urls
    urls = []
    print(f"{len(pending)} pending row(s) in {db['database']}.{table} (status=0)", flush=True)
    for url in pending:
        if valid_linkedin_url(url):
            urls.append(url)
        else:
            db_set_status(conn, table, url, DB_INVALID_STATUS)
            print(f"  not a LinkedIn profile, marked status=2: {url}", flush=True)

    if not urls:
        print("Nothing to do - no usable LinkedIn URLs left.")
        conn.close()
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{len(urls)} profile(s) queued, writing resumes to {OUTPUT_DIR}", flush=True)
    if upload_to_ceipal:
        print("Ceipal credentials found - each resume is parsed into the ATS, then deleted.", flush=True)
    else:
        print("No Ceipal credentials set - resumes are only downloaded.", flush=True)

    saved = []
    uploaded = []
    failed = []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=HEADLESS,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        linkedin_page = context.pages[0] if context.pages else context.new_page()
        linkedin_page.set_default_timeout(PAGE_TIMEOUT_MS)
        ceipal_page = context.new_page()
        ceipal_page.set_default_timeout(PAGE_TIMEOUT_MS)

        try:
            linkedin_page.bring_to_front()
            if not login(linkedin_page, email, password):
                return 1

            if upload_to_ceipal:
                ceipal_page.bring_to_front()
                if not ceipal_login(ceipal_page, ceipal_email, ceipal_password, ceipal_signin_url):
                    return 1

            for index, url in enumerate(urls, start=1):
                slug = profile_slug(url)
                target = OUTPUT_DIR / f"{slug}.pdf"
                print(f"[{index}/{len(urls)}] {url}", flush=True)
                try:
                    linkedin_page.bring_to_front()
                    profile = fetch_profile(linkedin_page, url)
                    if not profile.get("name"):
                        raise ValueError("no profile data found (wrong URL or page blocked)")
                    write_pdf(context, render_resume(profile), target)
                    saved.append(target)
                    print(
                        f"    saved {target.name} | experience={len(profile['experience'])} "
                        f"education={len(profile['education'])} skills={len(profile['skills'])} "
                        f"certs={len(profile['certifications'])}",
                        flush=True,
                    )

                    if upload_to_ceipal:
                        if ceipal_upload_resume(ceipal_page, target, ceipal_add_url, ceipal_source):
                            target.unlink()
                            uploaded.append(slug)
                            db_set_status(conn, table, url, DB_DONE_STATUS)
                            print(
                                f"    parsed into Ceipal, deleted {target.name} locally, status=1",
                                flush=True,
                            )
                        else:
                            failed.append(url)
                            print(f"    left {target.name} in the folder", flush=True)
                except Exception as exc:
                    if url not in failed:
                        failed.append(url)
                    print(f"    failed: {type(exc).__name__}: {exc}", flush=True)

                if index < len(urls):
                    time.sleep(DELAY_BETWEEN_PROFILES_S)
                    if upload_to_ceipal:
                        time.sleep(DELAY_BETWEEN_UPLOADS_S)
        finally:
            context.close()
            conn.close()

    print(f"\nDone. {len(saved)}/{len(urls)} resumes saved in {OUTPUT_DIR}")
    if upload_to_ceipal:
        print(f"      {len(uploaded)}/{len(urls)} parsed into Ceipal and deleted locally")
    for url in failed:
        print(f"  failed: {url}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
