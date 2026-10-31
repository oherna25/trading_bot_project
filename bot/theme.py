from __future__ import annotations

import html
from urllib.parse import quote as _urlencode

from bot.shapes import DashboardSettings

BRAND = "MarketwingmanBot"
ACCENT_NAME = "signal blue"


def stylesheet() -> str:
    return (
        ":root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--text:#e6edf3;--muted:#8b949e;"
        "--border:#30363d;--accent:#4493f8;--good:#3fb950;--bad:#f85149;--chip:#21262d;"
        "--warn:#d29922}"
        'html[data-theme="light"]{color-scheme:light;--bg:#ffffff;--panel:#f6f8fa;--text:#1f2328;'
        "--muted:#59636e;--border:#d0d7de;--accent:#0969da;--good:#1a7f37;--bad:#cf222e;--chip:#eef1f4;"
        "--warn:#9a6700}"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;background:#0d1117;background:var(--bg);color:var(--text);"
        "font-variant-numeric:tabular-nums;line-height:1.5}"
        ".wrap{max-width:72rem;margin:0 auto;padding:2rem 2rem 4rem}"
        "h1{font-size:1.5rem;margin:0 0 .25rem;font-weight:600}"
        "h2{font-size:1.15rem;margin:2rem 0 .75rem;border-bottom:1px solid var(--border);padding-bottom:.25rem}"
        ".sub{color:var(--muted);margin:0 0 1rem}"
        "header.top{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem 1rem;"
        "border-bottom:1px solid var(--border);padding-bottom:.75rem;margin-bottom:1.5rem}"
        "header.top nav{display:flex;flex-wrap:wrap;gap:.25rem 1rem}"
        "header.top nav a{color:var(--accent);text-decoration:none}"
        "header.top nav a[aria-current]{font-weight:600;text-decoration:underline}"
        "header.top form{margin-left:auto;display:flex;gap:.5rem;align-items:center}"
        ".stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin:1.5rem 0}"
        ".stat{border:1px solid var(--border);border-radius:6px;padding:.75rem 1rem;background:var(--panel)}"
        ".stat b{display:block;font-size:1.5rem;font-weight:600}"
        ".stat span{color:var(--muted);font-size:.85rem}"
        "table{width:100%;border-collapse:collapse;margin:0 0 1rem}"
        "th,td{text-align:left;padding:.5rem .75rem;border-bottom:1px solid var(--border)}"
        "th{color:var(--muted);font-size:.85rem;font-weight:600}"
        ".long,.call{color:var(--good)}.short,.put{color:var(--bad)}"
        ".fresh{color:var(--good)}.stale{color:var(--warn)}"
        "ul{margin:.5rem 0;padding-left:1.25rem}li{margin:.25rem 0}"
        "dl{display:grid;grid-template-columns:9rem 1fr;gap:.25rem 1rem;margin:.5rem 0}"
        "dt{color:var(--muted)}dd{margin:0}"
        "a{color:var(--accent)}"
        "pre{background:var(--panel);border:1px solid var(--border);border-radius:6px;"
        "padding:1rem;overflow-x:auto;white-space:pre-wrap}"
        "article{border:1px solid var(--border);border-radius:6px;padding:1.5rem;margin:0 0 1.5rem;"
        "background:var(--panel);max-width:42rem}"
        "article h2{border-bottom:none;margin:0 0 1rem;padding-bottom:0}"
        ".actions{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin:1rem 0}"
        ".action{border:1px solid var(--border);border-radius:6px;padding:.75rem 1rem;background:var(--panel)}"
        ".action h3{margin:0 0 .5rem;font-size:1rem}"
        ".action p{margin:.25rem 0;color:var(--muted);font-size:.9rem}"
        "form.block{display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-end;margin:.5rem 0}"
        "label.field{display:flex;flex-direction:column;gap:.25rem;font-size:.85rem;color:var(--muted)}"
        "input,select,textarea{font:inherit;color:var(--text);background:var(--bg);"
        "border:1px solid var(--border);border-radius:6px;padding:.4rem .6rem}"
        "textarea{width:100%;min-height:5rem;font-family:ui-monospace,Menlo,Consolas,monospace}"
        "button,.btn{display:inline-block;font:inherit;font-weight:600;color:var(--accent);"
        "background:transparent;border:1px solid var(--accent);border-radius:6px;"
        "padding:.35rem .9rem;text-decoration:none;cursor:pointer}"
        "button.solid{background:var(--accent);color:#fff;border-color:var(--accent)}"
        ".shock-btn{display:inline-block;margin-right:.5rem;padding:.35rem .9rem;"
        "border:1px solid var(--good);border-radius:6px;color:var(--good);"
        "text-decoration:none;font-weight:600}"
        ".chart-panel{background:#fff;border-radius:8px;padding:16px;margin:1rem 0}"
        ".chart-panel svg{max-width:100%;height:auto}"
    )


NAV_LINKS = (
    ("dashboard", "/dashboard"),
    ("board", "/board"),
    ("chart", "/chart"),
    ("cards", "/cards"),
    ("trackrecord", "/trackrecord"),
    ("earnings", "/earnings"),
    ("demo", "/demo"),
    ("video", "/video"),
    ("settings", "/settings"),
)


def nav_header(underlying: str, current: str, settings: DashboardSettings, next_path: str) -> str:
    escaped = _urlencode(underlying)
    links = []
    for label, href in NAV_LINKS:
        if label in ("dashboard", "chart", "cards", "demo", "video"):
            href = f"{href}?underlying={escaped}"
        mark = ' aria-current="page"' if label == current else ""
        links.append(f'<a href="{href}"{mark}>{label}</a>')
    other = "light" if settings.theme == "dark" else "dark"
    toggle = (
        '<form method="post" action="/settings">'
        f'<input type="hidden" name="theme" value="{other}">'
        f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">'
        f'<button type="submit">use {other} mode</button></form>'
    )
    brand = f"<a href=\"/dashboard?underlying={escaped}\">{BRAND}</a>"
    return f"<header class=\"top\"><nav>{brand}{''.join(links)}</nav>{toggle}</header>"


def refresh_meta(settings: DashboardSettings) -> str:
    if settings.refresh_seconds <= 0:
        return ""
    return f"<meta http-equiv=\"refresh\" content=\"{settings.refresh_seconds}\">"


def page(
    title: str,
    heading: str,
    sub: str,
    body: str,
    underlying: str,
    current: str,
    settings: DashboardSettings,
    next_path: str,
) -> str:
    return (
        "<!doctype html>"
        f"<html data-theme=\"{html.escape(settings.theme, quote=True)}\">"
        "<head><meta charset=\"utf-8\">"
        f"{refresh_meta(settings)}"
        f"<title>{html.escape(title)} | {BRAND}</title><style>{stylesheet()}</style></head><body>"
        "<div class=\"wrap\">"
        f"<h1>{html.escape(heading)}</h1>"
        f"<p class=\"sub\">{html.escape(sub)}</p>"
        f"{nav_header(underlying, current, settings, next_path)}"
        f"{body}"
        "</div></body></html>"
    )
