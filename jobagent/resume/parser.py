"""Locate and parse a resume into structured data. The original file is only ever opened read-only."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pymupdf

from ..skills import find_skills

SUPPORTED = {".pdf", ".docx", ".txt"}
BULLET_GLYPHS = "▸•●▪◦‣∙★-–*"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

HEADINGS = {
    "summary": ["professionalsummary", "summary", "profile", "about", "careerobjective", "objective"],
    "skills": ["technicalskills", "skills", "coreskills", "keyskills", "technologies"],
    "experience": ["professionalexperience", "experience", "workexperience", "employment",
                   "employmenthistory", "workhistory"],
    "projects": ["keyprojects", "projects", "personalprojects", "selectedprojects"],
    "education": ["education", "academics", "qualifications"],
    "achievements": ["certificationsachievements", "certificationsandachievements", "achievements",
                     "certifications", "awards", "honours", "honors", "awardsandachievements",
                     # standard headings recommended by analysis/tailoring ("Awards & Recognition") were missing
                     "awardsrecognition", "awardsandrecognition", "certificationsawards", "certificationsandawards",
                     "honoursawards", "honorsawards"],
}
_HEADING_LOOKUP = {alias: key for key, aliases in HEADINGS.items() for alias in aliases}

DATE_RANGE = re.compile(
    r"(?P<start>(?:[A-Za-z]{3,9}\.?\s+)?\d{4})\s*(?:–|-|—|to)\s*(?P<end>(?:[A-Za-z]{3,9}\.?\s+)?\d{4}|present|current|now)",
    re.IGNORECASE)
SINGLE_DATE = re.compile(r"^(?:[A-Za-z]{3,9}\.?\s+)?\d{4}$")


# ---------------------------------------------------------------- errors

class ResumeError(Exception):
    """User-facing resume problem. `code` lets the UI and tests react precisely."""

    def __init__(self, code: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []


def discover_resume(path: Path | str | None) -> Path:
    """Resolve a resume path that may be a file or a directory. Never guesses between several files."""
    if not path:
        raise ResumeError("not_configured", "No resume path configured. Set JOBAGENT_RESUME_PATH in .env.")
    p = Path(path)
    if p.is_file():
        if p.suffix.lower() not in SUPPORTED:
            raise ResumeError("unsupported_format",
                              f"'{p.name}' is a {p.suffix or 'extensionless'} file. Supported: PDF, DOCX, TXT.")
        return p
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED)
        if not files:
            raise ResumeError("not_found", f"No PDF, DOCX or TXT resume found in folder '{p}'.")
        if len(files) > 1:
            raise ResumeError("multiple_candidates",
                              f"Found {len(files)} possible resumes in '{p}'. Choose one explicitly.",
                              [str(f) for f in files])
        return files[0]
    if p.parent.is_dir():
        near = sorted(f.name for f in p.parent.iterdir() if f.suffix.lower() in SUPPORTED)[:10]
        hint = f" Files in that folder: {', '.join(near)}" if near else ""
        raise ResumeError("not_found", f"Resume '{p}' does not exist.{hint}", [str(p.parent / n) for n in near])
    raise ResumeError("not_found", f"Resume location '{p}' does not exist (folder missing).")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- line extraction

@dataclass
class Line:
    page: int
    y: float
    x: float
    size: float
    bold: bool
    text: str

    @property
    def is_marker(self) -> bool:
        return len(self.text) <= 2 and all(c in BULLET_GLYPHS for c in self.text)


def _pdf_lines(path: Path) -> list[Line]:
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # pymupdf raises various types for broken files
        raise ResumeError("corrupted", f"'{path.name}' could not be opened as a PDF (file may be corrupted): {e}")
    try:
        if doc.needs_pass:
            raise ResumeError("password_protected",
                              f"'{path.name}' is password-protected. Export an unprotected copy and try again.")
        if doc.page_count == 0:
            raise ResumeError("empty", f"'{path.name}' has no pages.")
        lines: list[Line] = []
        has_images = False
        for pno, page in enumerate(doc):
            if page.get_images():
                has_images = True
            for block in page.get_text("dict")["blocks"]:
                for ln in block.get("lines", []):
                    spans = [s for s in ln["spans"] if s["text"].strip()]
                    if not spans:
                        continue
                    text = " ".join("".join(s["text"] for s in ln["spans"]).replace("\xa0", " ").split())
                    lines.append(Line(pno, ln["bbox"][1], ln["bbox"][0], spans[0]["size"],
                                      bool(spans[0]["flags"] & 16) or "Bold" in spans[0]["font"], text))
    finally:
        doc.close()
    if not lines:
        if has_images:
            raise ResumeError("scanned_no_text",
                              f"'{path.name}' contains only images (a scanned or image-exported PDF). OCR is not "
                              "installed, so its text cannot be read. Use a text-based PDF or DOCX export.")
        raise ResumeError("empty", f"'{path.name}' contains no extractable text.")
    lines.sort(key=lambda l: (l.page, round(l.y), l.x))
    return lines


def _docx_lines(path: Path) -> list[Line]:
    try:
        import docx
        d = docx.Document(str(path))
    except Exception as e:
        raise ResumeError("corrupted", f"'{path.name}' could not be opened as a Word document: {e}")
    lines = []
    for i, para in enumerate(d.paragraphs):
        t = " ".join(para.text.split())
        if t:
            bold = any(r.bold for r in para.runs if r.text.strip())
            lines.append(Line(0, float(i * 12), 0.0, 10.0, bold, t))
    if not lines:
        raise ResumeError("empty", f"'{path.name}' contains no text.")
    return lines


def _txt_lines(path: Path) -> list[Line]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")
    lines = [Line(0, float(i * 12), 0.0, 10.0, False, " ".join(t.split()))
             for i, t in enumerate(raw.splitlines()) if t.strip()]
    if not lines:
        raise ResumeError("empty", f"'{path.name}' is empty.")
    return lines


def extract_lines(path: Path) -> list[Line]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_lines(path)
    if suffix == ".docx":
        return _docx_lines(path)
    if suffix == ".txt":
        return _txt_lines(path)
    raise ResumeError("unsupported_format", f"Unsupported file type '{suffix}'.")


# ---------------------------------------------------------------- structure

def _heading_key(text: str) -> str | None:
    norm = re.sub(r"[^a-z]", "", text.lower())
    return _HEADING_LOOKUP.get(norm)


def _looks_like_heading(line: Line) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", line.text)
    return bool(letters) and (line.text.isupper() or line.bold or line.text.endswith(":")) and len(line.text) < 45


def parse_month_year(s: str) -> date | None:
    s = s.strip().lower().rstrip(".")
    if s in ("present", "current", "now"):
        return None
    m = re.match(r"(?:([a-z]{3,9})\.?\s+)?(\d{4})$", s)
    if not m:
        return None
    month = MONTHS.get((m.group(1) or "jan")[:3], 1)
    return date(int(m.group(2)), month, 1)


def _split_tech(text: str) -> list[str]:
    inner = text
    m = re.search(r"\[(.*)\]", text)
    if m:
        inner = m.group(1)
    return [t.strip() for t in re.split(r"·|•|\||,", inner) if t.strip()]


def _group_bullets(lines: list[Line]) -> list[tuple[Line, list[str]]]:
    """Turn a run of lines into (first_line, [bullet texts]) using marker glyphs or indentation."""
    markers = [l for l in lines if l.is_marker]
    content = [l for l in lines if not l.is_marker]
    items: list[list[str]] = []
    for l in content:
        starts = any(0 <= m.y - l.y <= 5 and m.x <= l.x for m in markers)
        text = l.text
        lead = re.match(r"^[" + re.escape(BULLET_GLYPHS) + r"]\s*", text)
        if lead:
            starts, text = True, text[lead.end():]
        if starts or not items:
            items.append([text])
        else:
            items[-1].append(text)
    return [(content[0] if content else lines[0], [" ".join(i) for i in items])]


def parse(path: Path | str) -> dict:
    """Parse a resume file into a structured dict. Raises ResumeError for unusable files."""
    p = Path(path)
    lines = extract_lines(p)
    text = "\n".join(l.text for l in lines if not l.is_marker)
    warnings: list[str] = []

    # Find headings and their column (two-column regions like EDUCATION | CERTIFICATIONS).
    page_mid = 280.0
    heads = []
    for i, l in enumerate(lines):
        key = _heading_key(l.text)
        if key and _looks_like_heading(l):
            col = 1 if l.x > page_mid and l.page == lines[i].page else 0
            heads.append((i, key, l, col))
    if not heads:
        warnings.append("No standard section headings found (e.g. EXPERIENCE, SKILLS, EDUCATION).")
    two_column = any(h[3] == 1 for h in heads)
    if two_column:
        warnings.append("Two-column layout detected in at least one section; some ATS parsers read across "
                        "columns and mix the content.")

    # The two-column region starts at the first right-column heading (per page).
    col_start = {}
    for _, _, hl, col in heads:
        if col == 1:
            col_start[hl.page] = min(col_start.get(hl.page, float("inf")), hl.y - 2)

    def in_two_col_region(l: Line) -> bool:
        return l.y >= col_start.get(l.page, float("inf"))

    sections: dict[str, list[Line]] = {}
    raw_headings: dict[str, str] = {}
    for n, (idx, key, hl, col) in enumerate(heads):
        later = [h for h in heads[n + 1:] if h[3] == col or (col == 1 and h[3] == 0 and h[2].y > hl.y + 5)]
        end_y = later[0][2].y if later else float("inf")
        end_page = later[0][2].page if later else 10 ** 6
        body = []
        for l in lines:
            if (l.page, l.y) <= (hl.page, hl.y + 0.5) or l is hl:
                continue
            if (l.page, l.y) >= (end_page, end_y - 0.5):
                break
            if col == 1:
                in_col = l.x > page_mid
            else:
                in_col = l.x <= page_mid or not in_two_col_region(l)
            if in_col:
                body.append(l)
        sections[key] = sections.get(key, []) + body
        raw_headings[key] = hl.text

    first_head_idx = heads[0][0] if heads else len(lines)
    header = [l for l in lines[:first_head_idx] if not l.is_marker]

    result = {
        "source_file": p.name,
        "name": header[0].text if header else None,
        "contact": _parse_contact(" | ".join(l.text for l in header[1:])),
        "tagline": header[2].text if len(header) > 2 else None,
        "summary": " ".join(l.text for l in sections.get("summary", []) if not l.is_marker),
        "skills": _parse_skills(sections.get("skills", [])),
        "experience": _parse_experience(sections.get("experience", [])),
        "projects": _parse_projects(sections.get("projects", [])),
        "education": _parse_education(sections.get("education", [])),
        "achievements": _group_bullets(sections["achievements"])[0][1] if sections.get("achievements") else [],
        "raw_headings": raw_headings,
        "layout": {"two_column": two_column,
                   "bullet_glyphs": sorted({l.text for l in lines if l.is_marker}),
                   "line_count": len(lines)},
        "warnings": warnings,
        "text": text,
    }
    result["detected_skills"] = sorted(find_skills(text).keys())
    return result


def _parse_contact(s: str) -> dict:
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", s)
    phone = re.search(r"\+?\d[\d ()-]{7,}\d", s)
    linkedin = re.search(r"linkedin\.com/in/[A-Za-z0-9_-]+", s, re.IGNORECASE)
    github = re.search(r"github\.com/[A-Za-z0-9_-]+", s, re.IGNORECASE)
    location = s.split("|")[0].strip() if "|" in s else None
    return {
        "email": email.group(0) if email else None,
        "phone": phone.group(0).strip() if phone else None,
        "linkedin": linkedin.group(0) if linkedin else None,
        "github": github.group(0) if github else None,
        "location": location,
    }


def _parse_skills(lines: list[Line]) -> dict[str, list[str]]:
    raw: dict[str, list[str]] = {}
    current = None
    for l in lines:
        if l.is_marker:
            continue
        if ":" in l.text and len(l.text.split(":")[0]) < 30:
            current, rest = l.text.split(":", 1)
            current = current.strip()
            raw[current] = [rest]
            continue
        if l.bold and len(l.text) < 30:
            current = l.text.strip()
            raw[current] = []
            continue
        if current is None:
            current = "Skills"
            raw.setdefault(current, [])
        raw[current].append(l.text)  # wrapped continuation lines are joined before splitting
    out: dict[str, list[str]] = {}
    for cat, chunks in raw.items():
        joined = " ".join(c.strip() for c in chunks)
        joined = re.sub(r"&\s+", "& ", joined)
        out[cat] = [p.strip() for p in re.split(r",(?![^()]*\))|\|", joined) if p.strip()]
    return out


def _parse_experience(lines: list[Line]) -> list[dict]:
    roles: list[dict] = []
    i = 0
    content = [l for l in lines if not l.is_marker]
    markers = [l for l in lines if l.is_marker]
    current_proj = None
    while i < len(content):
        l = content[i]
        same_row = [c for c in content if abs(c.y - l.y) < 3 and c is not l and c.page == l.page]
        date_m = DATE_RANGE.search(l.text) or next((DATE_RANGE.search(c.text) for c in same_row
                                                    if DATE_RANGE.search(c.text)), None)
        is_role = date_m is not None and (l.bold or l.size >= 8.8 or not l.bold) and "[" not in l.text \
            and not any(0 <= m.y - l.y <= 5 for m in markers)
        if is_role:
            title = DATE_RANGE.sub("", l.text).strip(" |–-")
            skip = {id(c) for c in same_row}
            nxt = next((c for c in content[i + 1:] if id(c) not in skip), None)
            if "|" in title:
                # single-line format "Title | Company | Location   Dates": do NOT consume the next line
                title, _, rest = (p.strip() for p in title.partition("|"))
                company, _, loc = rest.partition("|")
                nxt = None
            else:
                company_line = nxt.text if nxt else ""
                company, _, loc = company_line.partition("|")
            roles.append({
                "title": title, "company": company.strip(), "location": loc.strip() or None,
                "start": date_m.group("start"), "end": date_m.group("end"),
                "projects": [], "bullets": [],
            })
            current_proj = None
            i += 1
            while i < len(content) and id(content[i]) in skip:
                i += 1
            if nxt is not None and i < len(content) and content[i] is nxt:
                i += 1
            continue
        if roles and "[" in l.text and "]" in l.text and l.bold:
            name = l.text.split("[")[0].strip(" –-")
            current_proj = {"name": name, "tech": _split_tech(l.text), "bullets": []}
            roles[-1]["projects"].append(current_proj)
            i += 1
            continue
        if roles:
            starts = any(0 <= m.y - l.y <= 5 for m in markers) or l.text[:1] in BULLET_GLYPHS
            text = l.text.lstrip(BULLET_GLYPHS + " ")
            target = current_proj["bullets"] if current_proj else roles[-1]["bullets"]
            if starts or not target:
                target.append(text)
            else:
                target[-1] += " " + text
        i += 1
    return roles


def _parse_projects(lines: list[Line]) -> list[dict]:
    projects: list[dict] = []
    content = [l for l in lines if not l.is_marker]
    markers = [l for l in lines if l.is_marker]
    for l in content:
        starts_bullet = any(0 <= m.y - l.y <= 5 for m in markers) or l.text[:1] in BULLET_GLYPHS
        same_row_date = next((c.text for c in content if c is not l and abs(c.y - l.y) < 3
                              and SINGLE_DATE.match(c.text)), None)
        if SINGLE_DATE.match(l.text) and any(abs(p_.y - l.y) < 3 for p_ in content if p_.bold):
            continue  # date column, consumed with its title
        if (l.bold or same_row_date) and not starts_bullet:
            projects.append({"name": l.text.strip(), "date": same_row_date, "tech": [], "bullets": []})
        elif projects and not projects[-1]["tech"] and not projects[-1]["bullets"] and "·" in l.text:
            projects[-1]["tech"] = _split_tech(l.text)
        elif projects:
            text = l.text.lstrip(BULLET_GLYPHS + " ")
            if starts_bullet or not projects[-1]["bullets"]:
                projects[-1]["bullets"].append(text)
            else:
                projects[-1]["bullets"][-1] += " " + text
    return projects


def _parse_education(lines: list[Line]) -> list[dict]:
    content = [l.text for l in lines if not l.is_marker]
    if not content:
        return []
    blob = " | ".join(content)
    years = re.search(r"(\d{4})\s*(?:–|-|—|to)\s*(\d{4}|present)", blob, re.IGNORECASE)
    gpa = re.search(r"(?:cgpa|gpa)\s*:?\s*([\d.]+\s*/\s*[\d.]+)", blob, re.IGNORECASE)
    inst = content[1].split("|")[0].strip() if len(content) > 1 else None
    return [{
        "degree": content[0],
        "institution": inst,
        "start_year": years.group(1) if years else None,
        "end_year": years.group(2) if years else None,
        "gpa": gpa.group(1).replace(" ", "") if gpa else None,
    }]


def experience_months(structured: dict, today: date | None = None) -> int:
    """Total months across roles, merging overlaps."""
    today = today or date.today()
    spans = []
    for r in structured.get("experience", []):
        s = parse_month_year(r.get("start") or "")
        e_raw = (r.get("end") or "").strip().lower()
        e = today if e_raw in ("present", "current", "now") else parse_month_year(e_raw)
        if s and e and e >= s:
            spans.append((s, e))
    spans.sort()
    total, cur_s, cur_e = 0, None, None
    for s, e in spans:
        if cur_e and s <= cur_e:
            cur_e = max(cur_e, e)
            continue
        if cur_s:
            total += (cur_e.year - cur_s.year) * 12 + cur_e.month - cur_s.month
        cur_s, cur_e = s, e
    if cur_s:
        total += (cur_e.year - cur_s.year) * 12 + cur_e.month - cur_s.month
    return total
