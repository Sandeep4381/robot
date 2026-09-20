# LinkedIn → Ceipal applicant pipeline

Current documentation for the harvesting tool.

Entry point: **`recruit.py`**. Everything else is a module it imports.

```
python recruit.py --jobs-only --jobs-limit 25     # step 1: store the job list
python recruit.py --jobs-limit 25 --limit 20      # step 2: profiles -> resumes -> Ceipal
```

---

## 1. What it does

For every Active/Open job in Ceipal it searches LinkedIn **Talent** by title and
location, collects up to 20 matching profiles, reads each **talent** profile,
renders a resume PDF, and hands that PDF to Ceipal's **Parsing Resume** input,
which builds the applicant record.

Two hard rules protect the LinkedIn account:

* **Only `linkedin.com/talent/profile/...` is ever opened.** Public `/in/` pages
  are never visited from the Recruiter session — a public profile URL is only
  read out of the talent page text and printed on the resume.
* The job list is read from Ceipal; a job's own page is never opened.

## 2. Files

| File | Role |
| --- | --- |
| `recruit.py` | **Entry point.** CLI, Ceipal job discovery, LinkedIn search/collection, job title/location/history handling. |
| `recruit_manual.py` | The pipeline: job producer, profile extraction, resume rendering, Ceipal resume parsing and save. |
| `profile_rules.py` | Default vocabulary-free extraction (name, location, job title, skills, certifications) from profile text. |
| `url.py` | AI providers (Ollama/OpenAI) and the standalone public-profile fetcher used by `python url.py`. |
| `main.py` | Shared selectors/constants, imported by `recruit.py` as `existing`. Its own `main()` is the superseded PDF/MySQL flow — do not run it. |
| `.env` | Credentials and settings. Never commit. |
| `recruit.env.example` | Template listing every optional selector/override. |
| `test_recruit_manual.py`, `test_profile_rules.py` | Offline tests (23). No browser, no network. |
| `.docs/README.md` | This document. |

## 3. Flow

### Step 1 — store the job list (`--jobs-only`)

* Opens the Ceipal job list and reads the **visible Active/Open rows only**.
* Reads code, title and location **from the list row**; a job is never opened.
  The list cell clips long titles (`Non-Clinical - Finance/Account..`); the
  trailing ellipsis is a display artefact and is trimmed before storing.
* Location comes from the bracketed cell (`[Richmond, VA, 23218]`) plus the
  following state cell. A row with no bracketed city stores an empty location.
* Writes `extract_job.json`. **A stored job is never re-extracted** — later runs
  reuse it and report `Using cached job: <code>`.
* Jobs already marked `completed` in `job_history.json` are skipped.

### Step 2 — jobs → profiles → resumes → Ceipal (normal run)

Jobs are produced and profiles processed **concurrently**: the collector keeps
storing jobs while the consumer works on profiles.

For each job:

1. If `job_history.json` already holds the profile URLs for that job they are
   reused. Otherwise LinkedIn Talent is searched: the **Job titles** filter gets
   the stored title, the **Locations** filter gets the stored location (omitted
   when empty), then the results pane is scrolled and up to `--limit` talent
   profile URLs are collected.
2. For each profile URL, in order:
   * **Extract** — the talent profile text is read once it stops growing. The
     member's public `/in/` URL is recorded from that text but never fetched.
   * **Build** — name, city, state, job title, skills and certifications are
     derived with rules or the configured AI provider.
   * **Resume** — written to `resumes/<Name>-<profile id>.pdf`, carrying the
     name, city/state, public profile URL and skills.
   * **Create** — the PDF is given to the **Parsing Resume** input, Ceipal's
     "confirm to proceed" step is accepted, and the parsed applicant form is
     awaited. Only fields the parse left blank are given placeholders (email,
     mobile); Source is set. **The form is then left open — saving is manual.**
3. The job is marked `completed` when every profile is `saved`, `duplicate` or
   `form_ready`. Otherwise it stays `pending` and the reason is recorded.

Each profile logs the name, city and state that were extracted, so the values
are visible while completing the Ceipal form.

## 4. Commands

| Flag | Effect |
| --- | --- |
| `--jobs-only` | Step 1 only. Populate the job cache; no LinkedIn work. |
| `--jobs-limit N` | 1–25. How many visible open jobs to inspect. |
| `--limit N` | 1–20. Max profiles per job. Retries reuse the stored list. |
| `--search-only` | Search and collect profile URLs; never extracts, renders or parses. Safe dry run. |
| `--location TEXT` | Override the location used for the LinkedIn filter. |
| `--job-url URL` | Process one Ceipal job URL. **Currently broken — see Limitations.** |

## 5. Configuration (`.env`)

| Key | Purpose |
| --- | --- |
| `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` | LinkedIn Talent login. |
| `Linkedin_tanlent` | Talent home URL used to detect a live session. |
| `Code` | TOTP secret for LinkedIn 2FA, entered locally if prompted. |
| `CEIPAL_EMAIL`, `CEIPAL_PASSWORD` | Ceipal login. |
| `CEIPAL_SIGNIN_URL` | Ceipal sign-in URL. |
| `CEIPAL_ADD_APPLICANT_URL` | Page hosting the Parsing Resume input. |
| `CEIPAL_SOURCE` | Source dropdown label. Default `LinkedIn Resume Harvesting`. |
| `AI_PROVIDER` | `none` (default, rules only), `auto`, `ollama`, `openai`. |
| `OLLAMA_MODEL`, `OLLAMA_URL` | Local model for `AI_PROVIDER=ollama`. |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Used only when OpenAI is selected. |

Optional CSS overrides, blank by default and listed in `recruit.env.example`:
`CEIPAL_JOB_LINK_SELECTOR`, `CEIPAL_JOB_TITLE_SELECTOR`,
`CEIPAL_JOB_LOCATION_SELECTOR`, `LINKEDIN_JOB_TITLE_SELECTOR`,
`LINKEDIN_LOCATION_SELECTOR`, `LINKEDIN_RESULT_SELECTOR`,
`LINKEDIN_NEXT_PAGE_SELECTOR`, `LINKEDIN_PROFILE_CONTENT_SELECTOR`,
`CEIPAL_PARSE_RESUME_SELECTOR`, `CEIPAL_PARSE_SAVE_SELECTOR`.

Not used by this flow: `SERPAPI_KEY`, `2Fa`, and the `DB_*` MySQL keys (legacy
`main.py` queue).

## 6. State files

| File | Contents |
| --- | --- |
| `extract_job.json` | Job cache: code, url, title, location, `extracted_at`. Durable — never expires. Delete an entry to force it to be re-read from Ceipal. |
| `profile_data.json` | Per profile: `raw` text, derived `profile`, `status`, `resume_path`, placeholder email/mobile, `applicant_url`. |
| `job_history.json` | Per job: `status`, collected profile URLs, `completed_at`. |
| `ceipal_form_dump.json` | Written **only** when a Ceipal form step fails: field, button and error dump for selector work. |
| `resumes/*.pdf` | One resume per extracted profile. |

Profile status lifecycle: `pending → extracted → form_ready`. `form_ready` means
the parsed form is sitting in Ceipal waiting for you to save it, so it is never
parsed again. `failed` retries from the stored `raw`, and the resume PDF is
rebuilt automatically if its file is gone. Leftover `saving` records (from the
optional auto-save block) become `review_required` on the next start and are
**not** retried.

## 7. Retry and duplicate rules

* `saved` / `duplicate` / `form_ready` → never processed again.
* `review_required` → not retried. Reconcile in Ceipal, then set the status by hand.
* `failed` → retried; the stored raw text and derived profile are reused.
* The same saved profile URL is skipped across jobs, but an alternate URL for
  the same person can still reach Ceipal's duplicate check.
* A job is only `completed` when every profile is in a final status.

## 8. Extraction modes

`AI_PROVIDER=none` (default) uses `profile_rules.py`: no key, no service, fully
offline. It takes name, city/state and job title from the profile header, the
listed skills, explicitly stated expertise, and keeps certifications as source
text without guessing competence.

`auto` tries OpenAI when a key is present and falls back to local Ollama;
`ollama` and `openai` force one provider. AI output must quote real evidence
from the profile text or the run is rejected.

## 9. Offline tests

```bat
python -m unittest test_recruit_manual test_profile_rules -v
```

23 tests covering job-cache reuse, the talent-only URL rule, public-URL
selection for the resume, resume rendering/filenames, cache/store integrity,
provider selection, evidence validation and duplicate/uncertain save handling.
No browser, network or submissions.

## 10. Limitations

1. **`--job-url` is broken.** It is the only path that opens a job snapshot
   page, and the `Job Title` label is not found there, so it falls into an
   interactive prompt that receives no input and fails. Use list mode.
2. **Empty locations.** A job whose list row has no bracketed city stores an
   empty location and is searched by title only, which widens results.
3. **Clipped titles.** The full title is only on the job page, which is
   deliberately not opened, so long titles are stored as the list shows them
   (minus the `..`).
4. **Skill fragmentation.** Lists inside parentheses can split into fragments
   such as `federal frameworks (NIST` and `FISMA)`.
5. **The parse flow depends on Ceipal's parser.** If it does not return the
   applicant form within 180 s the profile fails; the confirm dialog, parsed
   form and duplicate modal selectors come from `main.py` and can be tuned via
   `CEIPAL_PARSE_RESUME_SELECTOR`. **Saving is deliberately manual:** the parsed
   form is left open and the profile is recorded as `form_ready`, because
   Ceipal's own validation (a required State dropdown that the parse may leave
   empty) rejects an automatic submit. An auto-save block is commented out in
   `create_applicant` if that is ever wanted.
6. **Placeholders are never invented for identity fields.** Only email and
   mobile are filled, and only where the parse left them blank.
7. **English status text.** The results-loaded check looks for
   `Search results loaded`; it is no longer fatal (collection proceeds) but a
   different locale would slow things down.
8. **One instance only.** The browser profile and JSON state are shared.
9. **Dead code.** `generate_boolean`, `boolean_from_groups` and
   `job_description` in `recruit.py` are not reachable from the CLI, and most of
   `main.py` (its `main()`, PDF rendering, MySQL queue) belongs to the
   superseded flow.

## 11. Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Select a valid Ceipal job title (1–200 characters)` for many jobs | A clipped list title caused the job page to be reopened and the prompt to return nothing. Fixed: the list title is used and the ellipsis trimmed. |
| `No usable profile content found.` | The profile container was read before LinkedIn filled it. Fixed: extraction waits for the text to settle. |
| `No exact state option for 'Global Campus (Cum Laude'` | A school segment in the header matched the 3-part location rule. Fixed: location fields containing digits or parentheses are rejected. |
| Only 1 profile collected for a job with many results | The results pane scrolled the wrong container. Fixed: the pane is the scrollable ancestor holding the most talent-profile links. |
| Stored jobs disappeared and were re-extracted | The job cache had a 48-hour expiry. Fixed: the cache is durable. |
| `No console input available; continuing without the manual step.` | An interactive prompt was reached in an unattended run. Steps 1 and 2 no longer prompt. |

Caveats: automated access to LinkedIn is against their User Agreement and heavy
usage can get an account restricted. Live selectors should be re-verified in
your tenant after either site changes its markup.
