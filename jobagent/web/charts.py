"""Tiny server-side SVG charts (no JavaScript, CSP-friendly, accessible with <title>/aria labels)."""
from __future__ import annotations

from html import escape

PALETTE = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)", "var(--c7)", "var(--c8)"]


def empty(label: str) -> str:
    return f'<div class="chart-empty" role="img" aria-label="{escape(label)}: no data yet">No data yet</div>'


def hbar(data: list[tuple[str, int]], label: str, labels: dict | None = None) -> str:
    data = [(k, v) for k, v in data if v]
    if not data:
        return empty(label)
    labels = labels or {}
    mx = max(v for _, v in data) or 1
    row_h, w, lw = 26, 420, 130
    h = row_h * len(data) + 6
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{escape(label)}"><title>{escape(label)}</title>']
    for i, (k, v) in enumerate(data):
        y = i * row_h + 4
        bw = max(2, (w - lw - 50) * v / mx)
        name = escape(str(labels.get(k, k)))
        parts.append(f'<text x="{lw - 8}" y="{y + 15}" text-anchor="end" class="chart-label">{name}</text>'
                     f'<rect x="{lw}" y="{y + 3}" width="{bw:.1f}" height="16" rx="3" fill="{PALETTE[i % len(PALETTE)]}">'
                     f'<title>{name}: {v}</title></rect>'
                     f'<text x="{lw + bw + 6:.1f}" y="{y + 15}" class="chart-value">{v}</text>')
    parts.append("</svg>")
    return "".join(parts)


def columns(data: list[tuple[str, int]], label: str) -> str:
    if not any(v for _, v in data):
        return empty(label)
    mx = max(v for _, v in data) or 1
    w, h, pad = 460, 180, 24
    bw = (w - pad) / len(data)
    parts = [f'<svg viewBox="0 0 {w} {h + 30}" class="chart" role="img" aria-label="{escape(label)}"><title>{escape(label)}</title>']
    for i, (k, v) in enumerate(data):
        bh = (h - 20) * v / mx
        x = pad / 2 + i * bw
        parts.append(f'<rect x="{x + 4:.1f}" y="{h - bh:.1f}" width="{bw - 8:.1f}" height="{bh:.1f}" rx="3" fill="var(--c1)">'
                     f'<title>{escape(k)}: {v}</title></rect>')
        if v:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - bh - 4:.1f}" text-anchor="middle" class="chart-value">{v}</text>')
        if i % 2 == 0:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{h + 16}" text-anchor="middle" class="chart-label">{escape(k)}</text>')
    parts.append(f'<line x1="0" y1="{h}" x2="{w}" y2="{h}" class="chart-axis"/></svg>')
    return "".join(parts)
