from __future__ import annotations

import html
from urllib.parse import quote as _urlencode

from bot.shapes import DashboardSettings

BRAND = "MarketWingMan"
ACCENT_NAME = "signal blue"
ICON_URL = "/icon.png"


def stylesheet() -> str:
    return (
        ":root{color-scheme:dark;--bg:#070b14;--bg2:#0b1220;--panel:#101827;--panel2:#0d1524;"
        "--text:#eef2f8;--muted:#93a1b5;--faint:#64748f;--border:#22304a;--border2:#2c3f5f;"
        "--accent:#5b8cff;--accent2:#8b5cf6;--spark:#f5b83d;--good:#34d399;--bad:#fb7185;"
        "--chip:#182338;--warn:#fbbf24;--glow:rgba(91,140,255,.35);"
        "--shadow:0 18px 50px rgba(0,0,0,.45);--radius:16px}"
        'html[data-theme="light"]{color-scheme:light;--bg:#f4f6fb;--bg2:#eef2f9;--panel:#ffffff;'
        "--panel2:#f8fafc;--text:#0f172a;--muted:#52607a;--faint:#7c8aa5;--border:#d6deeb;"
        "--border2:#bccbe4;--accent:#2456d6;--accent2:#6d28d9;--spark:#b45309;--good:#047857;"
        "--bad:#be123c;--chip:#e8eef7;--warn:#92400e;--glow:rgba(36,86,214,.25);"
        "--shadow:0 18px 40px rgba(15,23,42,.12)}"
        "*{box-sizing:border-box}html,body{max-width:100%;overflow-x:clip}"
        "body{font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;background:radial-gradient(1100px 500px at 15% -10%,rgba(91,140,255,.20),transparent 60%),"
        "radial-gradient(900px 480px at 90% -10%,rgba(139,92,246,.16),transparent 55%),"
        "linear-gradient(180deg,var(--bg2),var(--bg) 320px);color:var(--text);"
        "font-variant-numeric:tabular-nums;line-height:1.55;-webkit-text-size-adjust:100%;min-height:100vh}"
        ".wrap{max-width:76rem;margin:0 auto;padding:1.25rem 1rem 3.5rem}"
        "h1{font-size:clamp(1.6rem,3.4vw,2.3rem);margin:0 0 .35rem;font-weight:800;letter-spacing:-.02em;line-height:1.12}"
        "h2{font-size:1.12rem;margin:2.2rem 0 .8rem;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:.55rem}"
        "h2::before{content:'';width:.55rem;height:.55rem;border-radius:99px;background:linear-gradient(135deg,var(--accent),var(--accent2));flex:none}"
        ".sub{color:var(--muted);margin:0 0 1rem}"
        ".eyebrow{font-size:.72rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}"
        "header.top{position:sticky;top:.6rem;z-index:30;display:flex;flex-wrap:wrap;align-items:center;"
        "gap:.5rem .75rem;background:color-mix(in srgb,var(--panel) 88%,transparent);backdrop-filter:blur(12px);"
        "border:1px solid var(--border);border-radius:14px;padding:.55rem .7rem;margin-bottom:1.4rem;box-shadow:var(--shadow)}"
        "header.top nav{display:flex;flex-wrap:wrap;gap:.2rem;align-items:center}"
        "header.top nav a{color:var(--muted);text-decoration:none;padding:.5rem .62rem;font-size:.92rem;font-weight:600;"
        "border-radius:9px;min-height:40px;display:inline-flex;align-items:center;transition:all .15s}"
        "header.top nav a:hover{color:var(--text);background:var(--chip)}"
        "header.top nav a[aria-current]{font-weight:700;color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 6px 18px var(--glow)}"
        "header.top nav a.brand{color:var(--text);font-weight:800;letter-spacing:-.01em;padding-left:.4rem}"
        "header.top nav a.brand img{width:26px;height:26px;border-radius:7px;vertical-align:-7px;margin-right:.45rem}"
        "header.top form{margin-left:auto;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}"
        ".hero{border:1px solid var(--border2);border-radius:20px;padding:1.4rem 1.4rem 1.2rem;margin:0 0 1.2rem;"
        "background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 16%,var(--panel)),var(--panel) 55%,color-mix(in srgb,var(--accent2) 12%,var(--panel)));"
        "box-shadow:var(--shadow);position:relative;overflow:hidden}"
        ".hero::after{content:'';position:absolute;inset:auto -60px -90px auto;width:280px;height:280px;border-radius:50%;"
        "background:radial-gradient(circle,var(--glow),transparent 65%);pointer-events:none}"
        ".hero h1{max-width:46rem}.hero p{color:var(--muted);max-width:44rem;margin:.4rem 0 0}"
        ".hero-row{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin-top:1rem;position:relative;z-index:1}"
        ".pill{display:inline-flex;align-items:center;gap:.4rem;font-size:.82rem;font-weight:700;border:1px solid var(--border2);"
        "border-radius:999px;padding:.32rem .7rem;background:var(--panel2);white-space:nowrap}"
        ".pill.good{color:var(--good);border-color:var(--good)}.pill.bad{color:var(--bad);border-color:var(--bad)}"
        ".pill.spark{color:var(--spark);border-color:var(--spark)}.pill.warn{color:var(--warn);border-color:var(--warn)}"
        ".verdict{font-size:1rem;padding:.5rem .9rem}"
        ".verdict.match{color:#052e16;background:var(--good);border-color:var(--good)}"
        ".verdict.contradiction{color:#fff;background:var(--bad);border-color:var(--bad)}"
        ".verdict.off,.verdict.unverified{color:var(--muted)}"
        ".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:.75rem;margin:1.2rem 0}"
        ".stat{border:1px solid var(--border);border-radius:var(--radius);padding:.9rem 1rem;background:linear-gradient(180deg,var(--panel),var(--panel2))}"
        ".stat b{display:block;font-size:1.55rem;font-weight:800;letter-spacing:-.02em}"
        ".stat span{color:var(--muted);font-size:.83rem;font-weight:600}"
        ".grid2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.grid2>*,.steps>*,.duel>*,.visual-grid>*{min-width:0}@media (max-width:820px){.grid2{grid-template-columns:1fr}}"
        ".panel{border:1px solid var(--border);border-radius:var(--radius);padding:1.1rem 1.15rem;background:linear-gradient(180deg,var(--panel),var(--panel2));box-shadow:0 8px 28px rgba(0,0,0,.18)}"
        ".panel h2{margin-top:0}.panel h3{margin:.2rem 0 .5rem;font-size:1rem}"
        ".spark-panel{border-color:color-mix(in srgb,var(--spark) 45%,var(--border));"
        "background:linear-gradient(135deg,color-mix(in srgb,var(--spark) 9%,var(--panel)),var(--panel2) 60%)}"
        ".duel{display:grid;grid-template-columns:1fr auto 1fr;gap:.7rem;align-items:stretch;margin:.8rem 0}"
        "@media (max-width:640px){.duel{grid-template-columns:1fr}.duel .vs{justify-self:center}}"
        ".duel .side{border:1px solid var(--border);border-radius:12px;padding:.75rem .85rem;background:var(--bg)}"
        ".duel .side span{display:block;font-size:.72rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}"
        ".duel .side b{font-size:1.25rem}.vs{align-self:center;font-weight:800;color:var(--faint);font-size:.8rem;letter-spacing:.1em}"
        ".table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--border);border-radius:12px;margin:0 0 1rem;background:var(--panel)}"
        ".table-scroll table{margin:0;border:none;min-width:42rem}"
        "table{width:100%;border-collapse:collapse;margin:0 0 1rem}"
        "th,td{text-align:left;padding:.62rem .75rem;border-bottom:1px solid var(--border);white-space:nowrap}"
        "th{color:var(--muted);font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}"
        "tbody tr{transition:background .12s}tbody tr:hover{background:var(--chip)}"
        ".long,.call{color:var(--good)}.short,.put{color:var(--bad)}"
        ".fresh{color:var(--good);font-weight:700}.stale{color:var(--warn);font-weight:700}"
        "ul{margin:.5rem 0;padding-left:1.25rem}li{margin:.25rem 0}"
        "dl{display:grid;grid-template-columns:9rem 1fr;gap:.25rem 1rem;margin:.5rem 0}"
        "dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}"
        "a{color:var(--accent)}"
        "pre{background:var(--panel2);border:1px solid var(--border);border-radius:12px;"
        "padding:1rem;overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:.85rem;line-height:1.6}"
        "details{border:1px solid var(--border);border-radius:12px;padding:.8rem 1rem;background:var(--panel);margin:0 0 .8rem}"
        "summary{cursor:pointer;font-weight:700;min-height:32px}"
        "article{border:1px solid var(--border2);border-radius:18px;padding:0;margin:0 0 1.4rem;overflow:hidden;"
        "background:linear-gradient(180deg,var(--panel),var(--panel2));max-width:48rem;box-shadow:var(--shadow)}"
        ".trade-card{max-width:48rem}article .trade-card{margin:0}"
        ".card-visual{padding:1.15rem 1.25rem .25rem}"
        ".card-head{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;margin:0 0 .6rem}"
        ".card-head h2{margin:0;font-size:1.18rem;flex:1 1 12rem}.card-head h2::before{display:none}"
        ".badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.78rem;font-weight:800;"
        "border:1px solid var(--border);border-radius:999px;padding:.22rem .65rem;white-space:nowrap}"
        ".badge.long{color:var(--good);border-color:var(--good)}.badge.short{color:var(--bad);border-color:var(--bad)}"
        ".badge.regime{color:var(--muted)}.badge.expiry{color:var(--accent);border-color:var(--accent)}"
        ".meter{height:.6rem;background:var(--chip);border-radius:999px;overflow:hidden;min-width:8rem;flex:1 1 10rem}"
        ".meter i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:999px;transition:width .5s}"
        ".meter.hot i{background:linear-gradient(90deg,var(--good),#22d3ee)}.meter.warm i{background:linear-gradient(90deg,var(--warn),var(--spark))}"
        ".conf-row{display:flex;align-items:center;gap:.6rem;margin:.35rem 0 .6rem;font-size:.9rem;color:var(--muted)}"
        ".conf-row b{color:var(--text)}"
        ".visual-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin:.75rem 0}"
        "@media (max-width:640px){.visual-grid{grid-template-columns:1fr}}"
        ".visual{border:1px solid var(--border);border-radius:12px;padding:.65rem .75rem;background:var(--bg)}"
        ".visual h3{margin:0 0 .4rem;font-size:.82rem;color:var(--muted);font-weight:700}"
        ".visual svg{width:100%;height:auto;display:block}"
        ".visual .cap{margin:.35rem 0 0;font-size:.8rem;color:var(--muted)}"
        ".facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.5rem;margin:.75rem 0}"
        ".fact{border:1px solid var(--border);border-radius:10px;padding:.55rem .65rem;background:var(--bg)}"
        ".fact span{display:block;font-size:.72rem;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.05em}"
        ".fact b{font-size:.95rem}"
        ".card-body{padding:0 1.25rem 1.25rem}.card-body ul{margin:.5rem 0}"
        "article h2{border-bottom:none;margin:0 0 1rem;padding-bottom:0}"
        ".actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.75rem;margin:1rem 0}"
        ".action{border:1px solid var(--border);border-radius:14px;padding:.95rem 1rem;background:linear-gradient(180deg,var(--panel),var(--panel2));transition:transform .15s,border-color .15s}"
        ".action:hover{transform:translateY(-2px);border-color:var(--border2)}"
        ".action h3{margin:0 0 .5rem;font-size:1rem}.action p{margin:.25rem 0;color:var(--muted);font-size:.9rem}"
        "form.block{display:flex;flex-wrap:wrap;gap:.6rem;align-items:flex-end;margin:.5rem 0}"
        "label.field{display:flex;flex-direction:column;gap:.3rem;font-size:.85rem;color:var(--muted);min-width:7rem;flex:1 1 9rem}"
        "input,select,textarea{font:inherit;font-size:16px;color:var(--text);background:var(--bg);"
        "border:1px solid var(--border2);border-radius:10px;padding:.58rem .72rem;max-width:100%}"
        "input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}"
        "textarea{width:100%;min-height:5rem;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85rem}"
        "button,.btn{display:inline-flex;align-items:center;justify-content:center;font:inherit;"
        "font-size:.95rem;font-weight:700;color:var(--accent);"
        "background:transparent;border:1px solid var(--accent);border-radius:10px;"
        "padding:.6rem 1rem;min-height:44px;text-decoration:none;cursor:pointer;transition:all .15s}"
        "button:hover,.btn:hover{transform:translateY(-1px);box-shadow:0 8px 20px var(--glow)}"
        "button.solid{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-color:transparent;box-shadow:0 8px 22px var(--glow)}"
        "button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{"
        "outline:2px solid var(--accent);outline-offset:2px}"
        ".shock-btn{display:inline-flex;align-items:center;margin:0 .5rem .5rem 0;padding:.6rem 1rem;min-height:44px;"
        "border:1px solid var(--border2);border-radius:10px;color:var(--text);background:var(--panel);"
        "text-decoration:none;font-weight:700}.shock-btn:hover{border-color:var(--accent);color:var(--accent)}"
        ".chart-panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:12px;margin:1rem 0;overflow-x:auto}"
        ".chart-panel svg{max-width:100%;height:auto;display:block}"
        ".chatbox{border:1px solid var(--border2);border-radius:16px;padding:1rem;background:linear-gradient(180deg,var(--panel),var(--panel2));box-shadow:var(--shadow)}"
        ".chatbox form{display:flex;gap:.55rem;flex-wrap:wrap}.chatbox input[type=text]{flex:1 1 16rem}"
        ".chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:.6rem 0}"
        ".chipbtn{min-height:36px;padding:.35rem .7rem;font-size:.85rem;border-radius:999px}"
        "#chat-reply{margin-top:.8rem;display:none}#chat-reply.show{display:block}"
        ".steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.75rem;margin:1rem 0}"
        ".step{border:1px solid var(--border);border-radius:14px;padding:.9rem 1rem;background:var(--panel)}"
        ".step b{display:block;margin-bottom:.25rem}.step p{margin:0;color:var(--muted);font-size:.9rem}"
        ".step .n{display:inline-flex;width:1.6rem;height:1.6rem;border-radius:99px;align-items:center;justify-content:center;"
        "background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;font-size:.85rem;margin-bottom:.5rem}"
        "footer.foot{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--border);color:var(--faint);font-size:.85rem;"
        "display:flex;flex-wrap:wrap;gap:.5rem 1.2rem}"
        ".skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;position:fixed;z-index:99;background:var(--panel);padding:.6rem 1rem}"
        "@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}"
        ".hero,.panel,article,.stat{animation:rise .01s ease both}"
        "@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}"
        "@media (max-width:640px){.wrap{padding:1rem .75rem 2.5rem}body{overflow-x:hidden}.steps{grid-template-columns:1fr}header.top form{margin-left:0}.hero h1{font-size:1.45rem}"
        "dl{grid-template-columns:1fr;gap:.15rem}.stat b{font-size:1.25rem}"
        "header.top{top:0;border-radius:0;margin-left:-.75rem;margin-right:-.75rem}"
        "header.top nav a{padding:.5rem .45rem;font-size:.88rem}pre{font-size:.8rem}.hero{padding:1.1rem}}"
    )


NAV_LINKS = (
    ("demo", "/demo"),
    ("dashboard", "/dashboard"),
    ("cards", "/cards"),
    ("board", "/board"),
    ("chart", "/chart"),
    ("trackrecord", "/trackrecord"),
    ("earnings", "/earnings"),
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
        f'<button type="submit" aria-label="switch to {other} mode">{other} mode</button></form>'
    )
    brand = (
        f"<a class=\"brand\" href=\"/dashboard?underlying={escaped}\">"
        f"<img src=\"{ICON_URL}\" alt=\"\" width=\"26\" height=\"26\">{BRAND}</a>"
    )
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
        "<meta name=\"description\" content=\"MarketWingMan: Muse Spark proposes, the exposure engine grades it. Paper-first options research with a journaled audit trail.\">"
        f"<link rel=\"icon\" type=\"image/png\" href=\"{ICON_URL}\">"
        f"<link rel=\"apple-touch-icon\" href=\"{ICON_URL}\">"
        f"{refresh_meta(settings)}"
        f"<title>{html.escape(title)} | {BRAND}</title><style>{stylesheet()}</style></head><body>"
        "<a class=\"skip\" href=\"#main\">skip to content</a>"
        "<div class=\"wrap\">"
        f"<h1>{html.escape(heading)}</h1>"
        f"<p class=\"sub\">{html.escape(sub)}</p>"
        f"{nav_header(underlying, current, settings, next_path)}"
        f"<main id=\"main\">{body}</main>"
        "<footer class=\"foot\"><span>MarketWingMan — research only, not investment advice. Paper trading is the default; live ordering stays off until armed on /live.</span>"
        "<span>Every signal, intent, fill, and model call is journaled.</span></footer>"
        "</div></body></html>"
    )


def hero_html(eyebrow: str, heading: str, lede: str, pills: str = "") -> str:
    row = f"<div class=\"hero-row\">{pills}</div>" if pills else ""
    return (
        "<section class=\"hero\">"
        f"<span class=\"eyebrow\">{html.escape(eyebrow)}</span>"
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(lede)}</p>{row}</section>"
    )
