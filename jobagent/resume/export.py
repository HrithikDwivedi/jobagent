"""Professional, ATS-friendly ONE-column PDF and DOCX export of a structured resume.

Design: large name, contact line, targeting line, section headings with a thin rule, role lines with the title /
company / location on the left and dates RIGHT-ALIGNED on the same line, project sub-headings with the tech stack
in italics, tight standard bullets, and "Category: items" skill lines. Built-in Helvetica only (no embedded fonts),
so every ATS can extract the text. Layout parameters (font size, leading, margins, spacing, page size) come from
the `layout` dict produced by the tailoring one-page fitter; without one, the roomiest preset is used."""
from __future__ import annotations

import io
import re

from .styles import styles
from .variants import ATS_HEADINGS, DEFAULT_ORDER

ACCENT = "#1F3A5F"
TEXT = "#1A1A1A"
MUTED = "#4A4A4A"
RULE = "#8C99A6"


def resolve_layout(s: dict, layout: dict | None = None) -> dict:
    lay = dict(styles()["fit_ladder"][0])
    lay["page_size"] = s.get("page_size") or "A4"
    lay.update({k: v for k, v in (layout or {}).items() if v is not None})
    return lay


def _display_name(s: dict) -> str:
    name = s.get("name") or ""
    return name.title() if name.isupper() else name


def _contact_line(s: dict) -> str:
    c = s.get("contact", {}) or {}
    fields = [c.get("email"), c.get("phone"), c.get("linkedin"), c.get("github")]
    if not s.get("tagline"):
        fields.insert(0, c.get("location"))
    return "  |  ".join(x for x in fields if x)


def _blocks(s: dict) -> list[tuple]:
    """Semantic rows in reading order, shared by the PDF and DOCX writers.
    ('name', text) ('contact', text) ('tagline', text) ('heading', text) ('para', text) ('skill', cat, items)
    ('entry', [(text, 'b'|'r'|'i')...], right_text, [tail segments]) ('sub', name, tech) ('bullet', text)"""
    h = dict(ATS_HEADINGS)
    h.update(s.get("headings") or {})
    rows: list[tuple] = [("name", _display_name(s))]
    if s.get("headline") and s.get("show_headline", True):
        rows.append(("headline", s["headline"]))
    rows.append(("contact", _contact_line(s)))
    if s.get("tagline"):
        rows.append(("tagline", s["tagline"]))
    for key in s.get("section_order") or DEFAULT_ORDER:
        if key == "summary" and s.get("summary"):
            rows += [("heading", h["summary"]), ("para", s["summary"])]
        elif key == "skills" and s.get("skills"):
            rows.append(("heading", h["skills"]))
            for cat, items in s["skills"].items():
                if items:
                    rows.append(("skill", cat, ", ".join(items)))
        elif key == "experience" and s.get("experience"):
            rows.append(("heading", h["experience"]))
            for r in s["experience"]:
                left = [(r.get("title") or "", "b")]
                if r.get("company"):
                    left.append((f"  |  {r['company']}", "r"))
                tail = [(f"  |  {r['location']}", "i")] if r.get("location") else []
                dates = " – ".join(x for x in [r.get("start"), r.get("end")] if x)
                rows.append(("entry", left, dates, tail))
                for b in r.get("bullets", []):
                    if "|" not in b:
                        rows.append(("bullet", b))
                for p in r.get("projects", []):
                    rows.append(("sub", p.get("name") or "", ", ".join(p.get("tech") or [])))
                    for b in p.get("bullets", []):
                        rows.append(("bullet", b))
        elif key == "projects" and s.get("projects"):
            rows.append(("heading", h["projects"]))
            for p in s["projects"]:
                tail = [(f"  |  {', '.join(p['tech'])}", "i")] if p.get("tech") else []
                rows.append(("entry", [(p.get("name") or "", "b")], p.get("date") or "", tail))
                for b in p.get("bullets", []):
                    rows.append(("bullet", b))
        elif key == "education" and s.get("education"):
            rows.append(("heading", h["education"]))
            for e in s["education"]:
                left = [(e.get("degree") or "", "b")]
                if e.get("institution"):
                    left.append((f"  |  {e['institution']}", "r"))
                tail = [(f"  |  CGPA: {e['gpa']}", "i")] if e.get("gpa") else []
                dates = " – ".join(x for x in [e.get("start_year"), e.get("end_year")] if x)
                rows.append(("entry", left, dates, tail))
        elif key == "achievements" and s.get("achievements"):
            rows.append(("heading", h["achievements"]))
            for a in s["achievements"]:
                rows.append(("bullet", a))
    return rows


# ------------------------------------------------------------------ text safety

_MAP = {"▸": "", "★": "", "☆": "", "▪": "", "●": "", "→": "->", "−": "-",
        "‐": "-", "‑": "-", " ": " ", " ": " ", " ": " "}


def safe(t: str) -> str:
    """Keep characters Helvetica (WinAnsi) can show; map or drop the rest (e.g. decorative glyphs)."""
    t = "".join(_MAP.get(ch, ch) for ch in (t or ""))
    out = []
    for ch in t:
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            continue
    return "".join(out)


def esc(t: str) -> str:
    return safe(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ PDF

def _fonts():
    return {"b": "Helvetica-Bold", "r": "Helvetica", "i": "Helvetica-Oblique"}


def _entry_flowable(left, right, tail, size, leading, colors):
    """Role/project/education line: left segments + right-aligned dates on the same baseline. When everything fits
    on one line it is drawn as ONE text object, the gap filled with (invisible) spaces, so text extraction keeps the
    title and dates on a single line. Tail segments (location / tech stack / grade) move to a second line if they
    do not fit."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable, Paragraph

    fonts = _fonts()
    color_of = {"b": HexColor(colors["text"]), "r": HexColor(colors["text"]), "i": HexColor(colors["muted"])}

    class Entry(Flowable):
        def __init__(self):
            super().__init__()
            self.left = [(safe(t), st) for t, st in left if t]
            self.tail = [(safe(t), st) for t, st in tail if t]
            self.right = safe(right or "")
            self.extra = None

        def _w(self, segs):
            return sum(stringWidth(t, fonts[st], size) for t, st in segs)

        def wrap(self, availW, availH):
            self.availW = availW
            rw = stringWidth(self.right, fonts["r"], size) if self.right else 0
            gap = size * 1.2 if self.right else 0
            self.line = list(self.left)
            if self._w(self.left + self.tail) + rw + gap <= availW:
                self.line += self.tail
                self.extra = None
            else:
                tail_txt = "".join(t for t, _ in self.tail).strip().lstrip("|").strip()
                self.extra = Paragraph(f"<font color='{colors['muted']}'><i>{esc(tail_txt)}</i></font>",
                                       ParagraphStyle("t", fontName=fonts["r"], fontSize=size - 0.5,
                                                      leading=leading - 0.5)) if tail_txt else None
            # If even the left part is too wide, shrink it by wrapping into a paragraph.
            self.para = None
            if self._w(self.line) + rw + gap > availW:
                markup = "".join(f"<b>{esc(t)}</b>" if st == "b" else f"<i>{esc(t)}</i>" if st == "i" else esc(t)
                                 for t, st in self.line)
                self.para = Paragraph(markup, ParagraphStyle("e", fontName=fonts["r"], fontSize=size,
                                                             leading=leading, textColor=HexColor(colors["text"])))
                _, self.ph = self.para.wrap(availW - rw - gap, availH)
            else:
                self.ph = leading
            self.eh = self.extra.wrap(availW, availH)[1] if self.extra else 0
            self.height = self.ph + self.eh
            return availW, self.height

        def draw(self):
            c = self.canv
            base = self.height - size
            if self.para is not None:
                self.para.drawOn(c, 0, self.eh)
                if self.right:
                    c.setFont(fonts["r"], size)
                    c.setFillColor(HexColor(colors["text"]))
                    c.drawRightString(self.availW, base, self.right)
            else:
                tx = c.beginText(0, base)
                for t, st in self.line:
                    tx.setFont(fonts[st], size)
                    tx.setFillColor(color_of[st])
                    tx.textOut(t)
                if self.right:
                    used = self._w(self.line)
                    rw = stringWidth(self.right, fonts["r"], size)
                    gap = self.availW - used - rw
                    sp = stringWidth(" ", fonts["r"], size)
                    n = max(1, int(gap // sp))
                    tx.setFont(fonts["r"], size)
                    tx.setCharSpace((gap - n * sp) / n)
                    tx.textOut(" " * n)
                    tx.setCharSpace(0)
                    tx.setFillColor(HexColor(colors["text"]))
                    tx.textOut(self.right)
                c.drawText(tx)
            if self.extra is not None:
                self.extra.drawOn(c, 0, 0)

    return Entry()


def export_pdf(s: dict, layout: dict | None = None) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    L = resolve_layout(s, layout)
    fs, ld = float(L["font_size"]), float(L["leading"])
    colors = {"text": TEXT, "muted": MUTED}
    base = ParagraphStyle("body", fontName="Helvetica", fontSize=fs, leading=ld, textColor=HexColor(TEXT))
    st = {
        "name": ParagraphStyle("name", parent=base, fontName="Helvetica-Bold", fontSize=L["name_size"],
                               leading=L["name_size"] * 1.15, alignment=TA_CENTER, textColor=HexColor(ACCENT),
                               spaceAfter=2),
        "headline": ParagraphStyle("headline", parent=base, fontName="Helvetica-Bold", fontSize=fs + 1,
                                   leading=(fs + 1) * 1.3, alignment=TA_CENTER, textColor=HexColor(TEXT),
                                   spaceAfter=1),
        "contact": ParagraphStyle("contact", parent=base, fontSize=fs - 0.5, leading=ld - 0.5,
                                  alignment=TA_CENTER, textColor=HexColor(MUTED)),
        "tagline": ParagraphStyle("tagline", parent=base, fontSize=fs - 0.5, leading=ld - 0.5,
                                  alignment=TA_CENTER, textColor=HexColor(TEXT)),
        "heading": ParagraphStyle("heading", parent=base, fontName="Helvetica-Bold", fontSize=L["heading_size"],
                                  leading=L["heading_size"] * 1.2, textColor=HexColor(ACCENT),
                                  spaceBefore=L["section_gap"], spaceAfter=1.2),
        "para": ParagraphStyle("para", parent=base, spaceAfter=0),
        "sub": ParagraphStyle("sub", parent=base, spaceBefore=L["item_gap"] * 0.6, spaceAfter=0.6),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=fs * 1.15, bulletIndent=fs * 0.25,
                                 bulletFontName="Helvetica", bulletFontSize=fs * 0.85,
                                 spaceBefore=L["bullet_gap"], spaceAfter=0),
        "skill": ParagraphStyle("skill", parent=base, spaceBefore=L["bullet_gap"], leftIndent=0),
    }
    story = []
    first_in_section = False
    for row in _blocks(s):
        kind = row[0]
        if kind == "name":
            story.append(Paragraph(esc(row[1]), st["name"]))
        elif kind in ("contact", "tagline", "headline"):
            if row[1]:
                story.append(Paragraph(esc(row[1]), st[kind]))
        elif kind == "heading":
            story.append(Paragraph(esc(row[1].upper()), st["heading"]))
            story.append(HRFlowable(width="100%", thickness=0.6, color=HexColor(RULE), spaceBefore=0.5,
                                    spaceAfter=2.2))
            first_in_section = True
            continue
        elif kind == "para":
            story.append(Paragraph(esc(row[1]), st["para"]))
        elif kind == "skill":
            story.append(Paragraph(f"<b>{esc(row[1])}:</b> {esc(row[2])}", st["skill"]))
        elif kind == "entry":
            if not first_in_section:
                story.append(Spacer(1, L["item_gap"]))
            story.append(_entry_flowable(row[1], row[2], row[3], fs, ld, colors))
        elif kind == "sub":
            tech = f"<font color='{MUTED}'><i>  |  {esc(row[2])}</i></font>" if row[2] else ""
            story.append(Paragraph(f"<b>{esc(row[1])}</b>{tech}", st["sub"]))
        elif kind == "bullet":
            story.append(Paragraph(esc(row[1]), st["bullet"], bulletText="•"))
        first_in_section = False
    buf = io.BytesIO()
    page = LETTER if str(L.get("page_size", "A4")).lower() == "letter" else A4
    mx, my = float(L["margin_x_cm"]) * cm, float(L["margin_y_cm"]) * cm
    name = _display_name(s) or "Resume"
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=mx, rightMargin=mx, topMargin=my, bottomMargin=my,
                            title=f"{name} - {s.get('doc_title') or 'Resume'}", author=name,
                            subject=s.get("headline") or "", creator="JobAgent")
    doc.build(story)
    return buf.getvalue()


def pdf_metrics(data: bytes) -> dict:
    """Page count and how full the last page is (0..1 of the printable height)."""
    import pymupdf
    d = pymupdf.open(stream=data, filetype="pdf")
    try:
        pages = d.page_count
        last = d[pages - 1]
        ys = [b[3] for b in last.get_text("blocks")]
        fill = (max(ys) / last.rect.height) if ys else 0.0
        return {"pages": pages, "fill": round(fill, 3)}
    finally:
        d.close()


def page_count(data: bytes) -> int:
    return pdf_metrics(data)["pages"]


# ------------------------------------------------------------------ DOCX

def export_docx(s: dict, layout: dict | None = None) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    L = resolve_layout(s, layout)
    fs = float(L["font_size"]) + 0.5      # Calibri runs smaller than Helvetica at the same point size
    accent = RGBColor.from_string(ACCENT[1:])
    muted = RGBColor.from_string(MUTED[1:])
    d = docx.Document()
    sec = d.sections[0]
    if str(L.get("page_size", "A4")).lower() == "letter":
        sec.page_width, sec.page_height = Cm(21.59), Cm(27.94)
    else:
        sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(float(L["margin_x_cm"]))
    sec.top_margin = sec.bottom_margin = Cm(float(L["margin_y_cm"]))
    text_width = sec.page_width - sec.left_margin - sec.right_margin
    normal = d.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(fs)
    pf = normal.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(0), 1.0
    lb = d.styles["List Bullet"]
    lb.font.size = Pt(fs)
    lb.paragraph_format.space_before = Pt(L["bullet_gap"])
    lb.paragraph_format.space_after = Pt(0)

    def rule_below(p):
        ppr = p._p.get_or_add_pPr()
        bdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in (("w:val", "single"), ("w:sz", "4"), ("w:space", "1"), ("w:color", RULE[1:])):
            bottom.set(qn(k), v)
        bdr.append(bottom)
        ppr.append(bdr)

    def run(p, text, style="r", size=None, color=None):
        r = p.add_run(text)
        r.bold, r.italic = style == "b", style == "i"
        if size:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color
        return r

    first_in_section = False
    for row in _blocks(s):
        kind = row[0]
        if kind == "name":
            p = d.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run(p, row[1], "b", L["name_size"], accent)
        elif kind == "headline":
            p = d.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run(p, row[1], "b", fs + 1)
        elif kind in ("contact", "tagline"):
            if row[1]:
                p = d.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run(p, row[1], "r", fs - 0.5, muted if kind == "contact" else None)
        elif kind == "heading":
            p = d.add_paragraph()
            p.paragraph_format.space_before = Pt(L["section_gap"])
            p.paragraph_format.space_after = Pt(2)
            run(p, row[1].upper(), "b", L["heading_size"] + 0.5, accent)
            rule_below(p)
            first_in_section = True
            continue
        elif kind == "para":
            d.add_paragraph(row[1])
        elif kind == "skill":
            p = d.add_paragraph()
            p.paragraph_format.space_before = Pt(L["bullet_gap"])
            run(p, row[1] + ": ", "b")
            run(p, row[2])
        elif kind == "entry":
            p = d.add_paragraph()
            if not first_in_section:
                p.paragraph_format.space_before = Pt(L["item_gap"])
            p.paragraph_format.tab_stops.add_tab_stop(text_width, WD_TAB_ALIGNMENT.RIGHT)
            for t, stl in row[1] + row[3]:
                run(p, t, stl, color=muted if stl == "i" else None)
            if row[2]:
                run(p, "\t" + row[2])
        elif kind == "sub":
            p = d.add_paragraph()
            p.paragraph_format.space_before = Pt(L["item_gap"] * 0.6)
            run(p, row[1], "b")
            if row[2]:
                run(p, "  |  " + row[2], "i", color=muted)
        elif kind == "bullet":
            d.add_paragraph(row[1], style="List Bullet")
        first_in_section = False
    d.core_properties.title = f"{_display_name(s)} - {s.get('doc_title') or 'Resume'}"
    d.core_properties.author = _display_name(s)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
