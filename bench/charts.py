"""Dependency-free SVG bar charts."""
from __future__ import annotations

import html

COLORS = {
    "wf-cli": "#2a9d8f",
    "wf-cli-display": "#52b788",
    "wf-repl": "#1d6f5f",
    "pw-chromium-cold": "#e76f51",
    "pw-chromium-warm": "#f4a261",
    "pw-webkit-cold": "#6d597a",
    "pw-webkit-warm": "#b56576",
}


def color_for(label: str) -> str:
    for k, v in COLORS.items():
        if label.startswith(k):
            return v
    return "#888"


def hbar_chart(title: str, rows: list[tuple[str, float | None, str]], unit: str, subtitle: str = "", width: int = 860) -> str:
    """rows: (label, value, annotation). None value renders as 'n/a'."""
    row_h = 28
    left = 230
    right = 250
    top = 56 if subtitle else 40
    height = top + row_h * max(1, len(rows)) + 24
    vals = [v for _, v, _ in rows if v is not None]
    vmax = max(vals) if vals else 1.0
    scale = (width - left - right) / (vmax if vmax > 0 else 1.0)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif" font-size="13">',
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
        f'<text x="12" y="24" font-size="16" font-weight="600">{html.escape(title)}</text>',
    ]
    if subtitle:
        out.append(f'<text x="12" y="44" fill="#666" font-size="12">{html.escape(subtitle)}</text>')
    y = top
    for label, value, note in rows:
        out.append(f'<text x="{left - 8}" y="{y + 18}" text-anchor="end" fill="#222">{html.escape(label)}</text>')
        if value is None:
            out.append(f'<text x="{left + 4}" y="{y + 18}" fill="#999">n/a{(" - " + html.escape(note)) if note else ""}</text>')
        else:
            w = max(1.0, value * scale)
            out.append(f'<rect x="{left}" y="{y + 4}" width="{w:.1f}" height="{row_h - 8}" rx="3" fill="{color_for(label)}"/>')
            txt = f"{value:,.0f} {unit}" if value >= 100 else f"{value:,.1f} {unit}"
            if note:
                txt += f"  ({note})"
            out.append(f'<text x="{left + w + 6:.1f}" y="{y + 18}" fill="#333">{html.escape(txt)}</text>')
        y += row_h
    out.append("</svg>")
    return "\n".join(out)
