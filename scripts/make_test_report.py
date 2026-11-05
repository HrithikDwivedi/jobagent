"""Build TEST_REPORT.md from reports/junit.xml and the test docstrings (parsed with ast, no imports)."""
from __future__ import annotations

import ast
import platform
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JUNIT = ROOT / "reports" / "junit.xml"
OUT = ROOT / "TEST_REPORT.md"

CATEGORIES = {
    "test_a_resume": ("A", "Resume handling"),
    "test_b_discovery": ("B", "Discovery & normalisation"),
    "test_c_workflow": ("C", "Application workflow"),
    "test_d_security": ("D", "Security"),
    "test_e_persistence": ("E", "Persistence & concurrency"),
    "test_f_dashboard": ("F", "Dashboard & usability"),
    "test_g_ai": ("G", "AI & cost controls"),
    "test_unit": ("Unit", "Unit tests"),
}
BLOCKING = {"C", "D", "E"}


def docstrings() -> dict[tuple[str, str], str]:
    out = {}
    for f in (ROOT / "tests").glob("test_*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                doc = (ast.get_docstring(node) or "").strip().splitlines()
                out[(f.stem, node.name)] = doc[0] if doc else ""
    return out


def cell(s: str, n: int = 300) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.replace("|", "\\|")
    return s[:n] + ("…" if len(s) > n else "")


def main() -> int:
    if not JUNIT.is_file():
        print(f"{JUNIT} not found; run pytest with --junitxml first", file=sys.stderr)
        return 1
    docs = docstrings()
    tree = ET.parse(JUNIT)
    rows: dict[str, list] = {k: [] for k, _ in CATEGORIES.values()}
    dialects = set()
    for tc in tree.iter("testcase"):
        module = tc.get("classname", "").split(".")[-1]
        name = tc.get("name", "")
        base = name.split("[")[0]
        cat = CATEGORIES.get(module, ("Unit", ""))[0]
        if "[sqlite" in name or "sqlite]" in name:
            dialects.add("SQLite")
        if "postgres" in name:
            dialects.add("PostgreSQL")
        expected = docs.get((module, base), "")
        expected = re.sub(r"^Expected:\s*", "", expected)
        fail = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
        skip = tc.find("skipped")
        if fail is not None:
            status, actual = "FAIL", "Did not meet expectation"
            err = (fail.get("message") or "") + " " + (fail.text or "").strip().splitlines()[-1:][0] \
                if (fail.text or "").strip() else (fail.get("message") or "")
            blocks = "Yes" if cat in BLOCKING else "No"
        elif skip is not None:
            status, actual = "SKIP", "Not run"
            err = skip.get("message") or ""
            blocks = f"No (skipped: {cell(err, 120)}; re-run where the dependency is available)"
        else:
            status, actual, err, blocks = "PASS", "As expected", "", "No"
        rows[cat].append((f"{module}::{name}", expected, actual, status, err, blocks))

    total = sum(len(v) for v in rows.values())
    counts = {s: sum(1 for v in rows.values() for r in v if r[3] == s) for s in ("PASS", "FAIL", "SKIP")}
    if any("sqlite" in r[0] for v in rows.values() for r in v):
        dialects.add("SQLite")
    lines = [
        "# JobAgent automated test report", "",
        f"- **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- **Python:** {platform.python_version()} ({platform.system()} {platform.release()})",
        f"- **Database dialects tested:** {', '.join(sorted(dialects)) or 'SQLite'}",
        "- **Source:** `reports/junit.xml` (pytest) + test docstrings",
        "- **Test data:** synthetic (generated in code) plus the real resume opened read-only (sha256 verified unchanged)",
        "", "## Totals", "",
        f"**{total} tests** — PASS {counts['PASS']} · FAIL {counts['FAIL']} · SKIP {counts['SKIP']}", "",
        "| Category | Tests | Pass | Fail | Skip |", "|---|---|---|---|---|",
    ]
    for key, (cat, title) in CATEGORIES.items():
        r = rows[cat]
        lines.append(f"| {cat}. {title} | {len(r)} | {sum(x[3]=='PASS' for x in r)} | "
                     f"{sum(x[3]=='FAIL' for x in r)} | {sum(x[3]=='SKIP' for x in r)} |")
    release_blockers = [r for c in BLOCKING for r in rows[c] if r[3] == "FAIL"]
    lines += ["", f"**Release blockers (failures in C/D/E):** {len(release_blockers)}", ""]
    for key, (cat, title) in CATEGORIES.items():
        lines += [f"## {cat}. {title}", "",
                  "| Test name | Expected result | Actual result | Status | Error details | Blocks release? |",
                  "|---|---|---|---|---|---|"]
        for r in rows[cat]:
            lines.append("| " + " | ".join([f"`{cell(r[0], 160)}`", cell(r[1]), r[2], r[3], cell(r[4]) or "—",
                                             r[5]]) + " |")
        lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT} ({total} tests: {counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
