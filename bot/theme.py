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
        "font-variant-numeric:tabular-nums;line-height:1.5;-webkit-text-size-adjust:100%}"
        ".wrap{max-width:72rem;margin:0 auto;padding:1.25rem 1rem 3rem}"
        "h1{font-size:1.5rem;margin:0 0 .25rem;font-weight:600}"
        "h2{font-size:1.15rem;margin:2rem 0 .75rem;border-bottom:1px solid var(--border);padding-bottom:.25rem}"
        ".sub{color:var(--muted);margin:0 0 1rem}"
        "header.top{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;align-items:center;"
        "gap:.5rem 1rem;background:var(--bg);"
        "border-bottom:1px solid var(--border);padding:.75rem 0;margin-bottom:1.5rem}"
        "header.top nav{display:flex;flex-wrap:wrap;gap:.25rem}"
        "header.top nav a{color:var(--accent);text-decoration:none;padding:.55rem .6rem;"
        "border-radius:6px;min-height:44px;display:inline-flex;align-items:center}"
        "header.top nav a[aria-current]{font-weight:600;text-decoration:underline}"
        "header.top form{margin-left:auto;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}"
        ".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));"
        "gap:.75rem;margin:1.5rem 0}"
        ".stat{border:1px solid var(--border);border-radius:8px;padding:.75rem 1rem;background:var(--panel)}"
        ".stat b{display:block;font-size:1.5rem;font-weight:600}"
        ".stat span{color:var(--muted);font-size:.85rem}"
        ".table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;"
        "border:1px solid var(--border);border-radius:8px;margin:0 0 1rem}"
        ".table-scroll table{margin:0;border:none;min-width:42rem}"
        "table{width:100%;border-collapse:collapse;margin:0 0 1rem}"
        "th,td{text-align:left;padding:.6rem .75rem;border-bottom:1px solid var(--border);white-space:nowrap}"
        "th{color:var(--muted);font-size:.85rem;font-weight:600}"
        ".long,.call{color:var(--good)}.short,.put{color:var(--bad)}"
        ".fresh{color:var(--good)}.stale{color:var(--warn)}"
        "ul{margin:.5rem 0;padding-left:1.25rem}li{margin:.25rem 0}"
        "dl{display:grid;grid-template-columns:9rem 1fr;gap:.25rem 1rem;margin:.5rem 0}"
        "dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}"
        "a{color:var(--accent)}"
        "pre{background:var(--panel);border:1px solid var(--border);border-radius:8px;"
        "padding:1rem;overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:.85rem}"
        "article{border:1px solid var(--border);border-radius:8px;padding:1.25rem;margin:0 0 1.5rem;"
        "background:var(--panel);max-width:42rem}"
        ".trade-card{max-width:46rem}article .trade-card{margin:-.25rem 0 0}"
        ".card-visual{padding:1.1rem 1.25rem .25rem}"
        ".card-head{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;margin:0 0 .6rem}"
        ".card-head h2{margin:0;font-size:1.15rem;flex:1 1 12rem}"
        ".badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.8rem;font-weight:700;"
        "border:1px solid var(--border);border-radius:999px;padding:.2rem .65rem;white-space:nowrap}"
        ".badge.long{color:var(--good);border-color:var(--good)}"
        ".badge.short{color:var(--bad);border-color:var(--bad)}"
        ".badge.regime{color:var(--muted)}"
        ".badge.expiry{color:var(--accent);border-color:var(--accent)}"
        ".meter{height:.55rem;background:var(--chip);border-radius:999px;overflow:hidden;min-width:8rem;"
        "flex:1 1 10rem}"
        ".meter i{display:block;height:100%;background:var(--accent);border-radius:999px}"
        ".meter.hot i{background:var(--good)}.meter.warm i{background:var(--warn)}"
        ".conf-row{display:flex;align-items:center;gap:.6rem;margin:.35rem 0 .6rem;font-size:.9rem;"
        "color:var(--muted)}"
        ".conf-row b{color:var(--text)}"
        ".visual-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin:.75rem 0}"
        "@media (max-width:640px){.visual-grid{grid-template-columns:1fr}}"
        ".visual{border:1px solid var(--border);border-radius:8px;padding:.6rem .7rem;background:var(--bg)}"
        ".visual h3{margin:0 0 .4rem;font-size:.85rem;color:var(--muted);font-weight:600}"
        ".visual svg{width:100%;height:auto;display:block}"
        ".visual .cap{margin:.35rem 0 0;font-size:.8rem;color:var(--muted)}"
        ".facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.5rem;margin:.75rem 0}"
        ".fact{border:1px solid var(--border);border-radius:8px;padding:.5rem .65rem;background:var(--bg)}"
        ".fact span{display:block;font-size:.75rem;color:var(--muted)}"
        ".fact b{font-size:.95rem}"
        ".card-body{padding:0 1.25rem 1.25rem}"
        ".card-body ul{margin:.5rem 0}"
        "article h2{border-bottom:none;margin:0 0 1rem;padding-bottom:0}"
        ".actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));"
        "gap:.75rem;margin:1rem 0}"
        ".action{border:1px solid var(--border);border-radius:8px;padding:.9rem 1rem;background:var(--panel)}"
        ".action h3{margin:0 0 .5rem;font-size:1rem}"
        ".action p{margin:.25rem 0;color:var(--muted);font-size:.9rem}"
        "form.block{display:flex;flex-wrap:wrap;gap:.6rem;align-items:flex-end;margin:.5rem 0}"
        "label.field{display:flex;flex-direction:column;gap:.3rem;font-size:.85rem;color:var(--muted);"
        "min-width:7rem;flex:1 1 9rem}"
        "input,select,textarea{font:inherit;font-size:16px;color:var(--text);background:var(--bg);"
        "border:1px solid var(--border);border-radius:8px;padding:.55rem .7rem;max-width:100%}"
        "textarea{width:100%;min-height:5rem;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85rem}"
        "button,.btn{display:inline-flex;align-items:center;justify-content:center;font:inherit;"
        "font-size:1rem;font-weight:600;color:var(--accent);"
        "background:transparent;border:1px solid var(--accent);border-radius:8px;"
        "padding:.6rem 1rem;min-height:44px;text-decoration:none;cursor:pointer}"
        "button.solid{background:var(--accent);color:#fff;border-color:var(--accent)}"
        "button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{"
        "outline:2px solid var(--accent);outline-offset:2px}"
        ".shock-btn{display:inline-flex;align-items:center;margin-right:.5rem;padding:.6rem 1rem;min-height:44px;"
        "border:1px solid var(--good);border-radius:8px;color:var(--good);"
        "text-decoration:none;font-weight:600}"
        ".chart-panel{background:#fff;border-radius:8px;padding:12px;margin:1rem 0;overflow-x:auto}"
        ".chart-panel svg{max-width:100%;height:auto;display:block}"
        "@media (max-width:640px){.wrap{padding:1rem .75rem 2.5rem}h1{font-size:1.25rem}"
        "dl{grid-template-columns:1fr;gap:.15rem}.stat b{font-size:1.25rem}"
        "header.top nav a{padding:.55rem .5rem}pre{font-size:.8rem}}"
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
    ("live", "/live"),
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
        f"<html lang=\"en\" data-theme=\"{html.escape(settings.theme, quote=True)}\">"
        "<head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"{refresh_meta(settings)}"
        f"<title>{html.escape(title)} | {BRAND}</title><style>{stylesheet()}</style></head><body>"
        "<div class=\"wrap\">"
        f"<h1>{html.escape(heading)}</h1>"
        f"<p class=\"sub\">{html.escape(sub)}</p>"
        f"{nav_header(underlying, current, settings, next_path)}"
        f"{body}"
        "</div></body></html>"
    )
