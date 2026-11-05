"""Synthetic test data builders. EVERYTHING here is SYNTHETIC (generated in code) except the optional
real resume path, which is only ever read."""
from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import httpx

from jobagent.config import AppConfig
from jobagent.db import iso
from jobagent.sources import RawJob, SourceResult

REAL_RESUME = Path(r"C:\Users\tledt\OneDrive\Desktop\hrithik\Hrithik_Dwivedi_Resume_India.pdf")

# ----------------------------------------------------------------------------- resumes (SYNTHETIC)

SYNTHETIC_RESUME_LINES = [
    ("Alex Example", "name"),
    ("Mumbai, India | alex.example@example.com | +91 90000 00000 | linkedin.com/in/alex-example", "n"),
    ("Software Engineer (synthetic test resume)", "n"),
    ("SUMMARY", "h"),
    ("Software engineer with 2+ years building C# and .NET services and Python automation.", "n"),
    ("SKILLS", "h"),
    ("Languages: C#, Python, SQL, JavaScript", "n"),
    ("Frameworks: ASP.NET Core, Entity Framework, FastAPI", "n"),
    ("Tools: Docker, Git, AWS", "n"),
    ("EXPERIENCE", "h"),
    ("Software Engineer   Jan 2023 - Present", "b"),
    ("Acme Synthetic Ltd | Mumbai, India", "n"),
    ("\u2022 Built ASP.NET Core REST APIs in C# serving 5000 users backed by SQL Server.", "n"),
    ("\u2022 Reduced report generation time by 40% with Python and FastAPI automation.", "n"),
    ("\u2022 Shipped Docker based CI/CD pipelines on AWS for 3 services.", "n"),
    ("Junior Developer   Jun 2022 - Dec 2022", "b"),
    ("Beta Synthetic Pvt | Pune, India", "n"),
    ("\u2022 Developed Entity Framework data access for 12 reporting screens.", "n"),
    ("EDUCATION", "h"),
    ("B.E. Computer Engineering", "n"),
    ("Mumbai University | 2018 - 2022 | CGPA: 8.1/10", "n"),
]


def build_pdf(path: Path, lines=SYNTHETIC_RESUME_LINES) -> Path:
    """SYNTHETIC text PDF via reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for text, style in lines:
        font = "Helvetica-Bold" if style in ("h", "b", "name") else "Helvetica"
        size = 14 if style == "name" else 11 if style == "h" else 9.5
        c.setFont(font, size)
        c.drawString(50, y, text)
        y -= 16
    c.save()
    return path


def pdf_bytes(lines=SYNTHETIC_RESUME_LINES) -> bytes:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = build_pdf(Path(d) / "r.pdf", lines)
        return p.read_bytes()


def build_encrypted_pdf(path: Path) -> Path:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "SYNTHETIC protected resume EXPERIENCE")
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    doc.close()
    return path


def build_image_only_pdf(path: Path) -> Path:
    import pymupdf
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
    pix.clear_with(180)
    png = pix.tobytes("png")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(72, 72, 400, 400), stream=png)
    doc.save(str(path))
    doc.close()
    return path


def build_blank_pdf(path: Path) -> Path:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def build_docx(path: Path) -> Path:
    import docx
    d = docx.Document()
    for text, style in SYNTHETIC_RESUME_LINES:
        p = d.add_paragraph()
        r = p.add_run(text)
        r.bold = style in ("h", "b", "name")
    d.save(str(path))
    return path


def png_bytes() -> bytes:
    import pymupdf
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(100)
    return pix.tobytes("png")


# ----------------------------------------------------------------------------- jobs (SYNTHETIC)

DOTNET_JD = (
    "We are hiring a Junior .NET Developer to join our Dublin team.\n"
    "Requirements\n"
    "\u2022 1+ years of experience with C# and .NET\n"
    "\u2022 ASP.NET Core Web API and SQL\n"
    "\u2022 Entity Framework\n"
    "Nice to have\n"
    "\u2022 Docker\n"
    "We offer visa sponsorship and relocation support for the right candidate.\n"
)


def make_raw(**kw) -> RawJob:
    """SYNTHETIC RawJob factory."""
    n = kw.pop("n", 1)
    d = dict(source="greenhouse", source_job_id=f"acme:{n}", title="Software Engineer", company="Acme",
             location_raw="Dublin, Ireland", url=f"https://boards.greenhouse.io/acme/jobs/{n}",
             apply_url=f"https://boards.greenhouse.io/acme/jobs/{n}",
             description_text="We build C# and .NET services.\nRequirements\n\u2022 C#, .NET and SQL\n")
    d.update(kw)
    return RawJob(**d)


class FakeSource:
    """In-memory source adapter for agent runs (no network)."""

    def __init__(self, name="greenhouse", jobs=None, complete=True, error=None, raise_exc=None,
                 on_fetch=None):
        self.name = name
        self.jobs = jobs or []
        self.complete = complete
        self.error = error
        self.raise_exc = raise_exc
        self.on_fetch = on_fetch
        self.calls = 0

    def fetch(self, board=None):
        self.calls += 1
        if self.on_fetch:
            self.on_fetch()
        if self.raise_exc:
            raise self.raise_exc
        return SourceResult(self.name, board, list(self.jobs), self.complete, self.error)


def no_network_transport():
    def handler(request):
        return httpx.Response(599, text="network disabled in tests")
    return httpx.MockTransport(handler)


def json_transport(routes: dict, default_status=404):
    """MockTransport mapping URL path -> (status, body or callable)."""
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        item = routes.get(request.url.path)
        if item is None:
            return httpx.Response(default_status, json={"error": "not found"})
        if callable(item):
            return item(request)
        status, body = item
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)
    t = httpx.MockTransport(handler)
    t.calls = calls
    return t


def agent_config(db_url: str, **kw) -> AppConfig:
    cfg = AppConfig(data_dir=Path("."), database_url=db_url, secret_key="k" * 40, resume_path=None,
                    ai_provider="none", openai_api_key=None, anthropic_api_key=None)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# ----------------------------------------------------------------------------- accounts (SYNTHETIC)

ADMIN = 1                       # the first account is always the administrator (id 1 on a fresh database)
ADMIN_NAME = "alex"
TEST_PW = "Correct-Horse-42x"


def ensure_admin(db) -> int:
    """Create the administrator account on a fresh test database (no-op if it exists)."""
    from jobagent.web import security
    row = db.one("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
    if row:
        return int(row["id"])
    uid = security.create_user(db, ADMIN_NAME, TEST_PW, actor="test", email="admin@example.com",
                               full_name="Alex Admin")
    assert uid == ADMIN, uid
    return uid


def make_user(db, username="bea", *, status="approved", email=None, full_name=None) -> int:
    """A second (non-admin) account, approved by default."""
    from jobagent.web import security
    return security.create_user(db, username, TEST_PW, actor="test", email=email or f"{username}@example.com",
                                full_name=full_name or username.title(), role="user", status=status)
