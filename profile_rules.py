"""Conservative, vocabulary-free extraction of visible profile text."""
import re


def build_profile(raw):
    sections = raw.get("public_sections", [])
    if not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
        raise ValueError("public_sections must be a list of strings")
    lines = [line.strip() for section in sections for line in section.splitlines() if line.strip()]
    if not lines:
        raise ValueError("No profile text available")
    name = raw.get("name") or lines[0]
    if re.search(r"sign in|join linkedin|search results", name, re.I):
        raise ValueError("Profile identity unavailable")
    parts = name.split()
    result = {"first_name": " ".join(parts[:-1]) if len(parts) > 1 else name,
              "last_name": parts[-1] if len(parts) > 1 else None,
              "city": None, "state": None, "job_title": None,
              "skills": [], "certifications": [], "extraction_method": "rules"}
    # Only the profile header can supply personal location, never job addresses.
    header = []
    for line in lines:
        if re.fullmatch(r"Experience|Education|About|Summary|Skills(?:\s*\(\d+\))?", line, re.I):
            break
        header.append(line)
    for line in header:
        for segment in re.split(r"\s*[·•]\s*", line):
            location = segment.split("Contact Info")[0].strip()
            fields = [part.strip() for part in location.split(",")]
            if len(fields) != 3 or not all(fields):
                continue
            if any(x in location for x in ("|", "&")):
                continue
            # A school or award segment ("..., Global Campus (Cum Laude, 2009)")
            # also splits into three parts; a real location has no digits or notes.
            if any(re.search(r"[()\[\]\d]", field) for field in fields):
                continue
            result["city"], result["state"] = fields[:2]
            break
        if result["city"]:
            break
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == "position title":
            result["job_title"] = lines[index + 1]
            break

    seen = set()
    def add(skill, evidence, source):
        skill = skill.strip(" \t•,;:.")
        if not skill or len(skill) > 100 or len(skill.split()) > 12:
            return
        if "…" in skill or "..." in skill:
            return
        key = skill.casefold()
        if key not in seen and len(result["skills"]) < 8:
            result["skills"].append({"skill": skill, "basis": "explicit",
                                     "source": source, "evidence": evidence})
            seen.add(key)

    in_skills = False
    for line in lines:
        if re.fullmatch(r"(?:Top\s+)?Skills(?:\s*\(\d+\))?", line, re.I):
            in_skills = True
            continue
        if in_skills:
            if re.match(r"^(Accomplishments|Certifications|Licenses|Interests|Education|Experience|Recommendations|Languages|Personal Information|Show all|Show more)\b", line, re.I):
                in_skills = False
            elif not re.search(r"endorse|^\d+$|see more", line, re.I):
                add(line, line, "skills")

    # Language cues select arbitrary skill phrases, without a skill-name dictionary.
    cue = re.compile(r"\b(?:speciali[sz]ing in|expertise in|proficient in|skilled in|focusing on|work across)\s+(.+)", re.I)
    section_kind = "summary"
    for line in lines:
        if line.casefold() == "experience":
            section_kind = "experience"
        if re.match(r"^(Education|Skills|Accomplishments|Certifications|Interests|Personal Information)\b", line, re.I):
            section_kind = "other"
        if line.casefold() in ("about", "summary"):
            section_kind = "summary"
        if section_kind == "other":
            continue
        match = cue.search(line)
        if match:
            topics = re.split(r"[.!?](?:\s|$)|…|\.{3}|\bsee more\b", match.group(1), maxsplit=1)[0]
            for topic in re.split(r",\s*(?:and\s+)?|;|\s+and\s+|\s+&\s+", topics):
                add(topic, line, section_kind)

    # Preserve certification source text separately instead of guessing competence.
    for index, line in enumerate(lines):
        if re.fullmatch(r"(?:Licenses\s*&\s*)?Certifications", line, re.I):
            block = []
            for following in lines[index + 1:]:
                if re.match(r"^(Interests|Personal Information|Education|Experience|Skills|Show more|Show all)\b", following, re.I):
                    break
                block.append(following)
            if block:
                result["certifications"].append("\n".join(block))
    return result
