from __future__ import annotations

import html
from urllib.parse import quote as _urlencode

from bot.shapes import DashboardSettings

BRAND = "MarketWingMan"
ACCENT_NAME = "signal blue"
ICON_URL = "/icon.png"


_ROOT_CSS = """
:root{color-scheme:dark;--bg:#05070b;--bg2:#080b11;--panel:#0b0f16;--panel2:#0e131c;
--text:#e6ebf2;--muted:#8a97ab;--faint:#5c6a80;--border:#18202d;--border2:#243043;
--accent:#5b8cff;--accent2:#5b8cff;--spark:#f5b83d;--good:#34d399;--bad:#fb7185;
--chip:#121925;--warn:#fbbf24;--glow:rgba(91,140,255,.28);--brand:#34d399;
--shadow:none;--radius:8px;--rail:13.5rem;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
html[data-theme="light"]{color-scheme:light;--bg:#f3f5f9;--bg2:#eceff5;--panel:#ffffff;
--panel2:#f7f9fc;--text:#0f172a;--muted:#52607a;--faint:#7c8aa5;--border:#dde3ee;
--border2:#c3cfe2;--accent:#2456d6;--accent2:#2456d6;--spark:#b45309;--good:#047857;
--bad:#be123c;--chip:#e8eef7;--warn:#92400e;--glow:rgba(36,86,214,.22);--brand:#047857}
"""


_BASE_CSS = """
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:clip}
body{font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;
font-variant-numeric:tabular-nums;-webkit-text-size-adjust:100%;min-height:100vh}
.shell{display:grid;grid-template-columns:var(--rail) minmax(0,1fr);min-height:100vh}
aside.rail{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;
background:var(--bg2);border-right:1px solid var(--border);padding:.7rem .55rem 1rem;
display:flex;flex-direction:column;gap:.15rem}
a.brand{display:flex;align-items:center;gap:.5rem;padding:.35rem .45rem .7rem;color:var(--text);
text-decoration:none;font-weight:800;font-size:.95rem;letter-spacing:.01em}
a.brand img{width:22px;height:22px;border-radius:5px;flex:none}
.rail-tag{display:block;margin:-.55rem 0 .6rem 2.35rem;font-size:.6rem;font-weight:700;
letter-spacing:.14em;text-transform:uppercase;color:var(--brand)}
.nav-group{margin:0 0 .55rem}
.nav-group p{margin:.5rem .45rem .2rem;font-size:.62rem;font-weight:700;letter-spacing:.14em;
text-transform:uppercase;color:var(--faint)}
.nav-group a{display:flex;align-items:center;gap:.5rem;padding:.3rem .45rem;border-radius:6px;
color:var(--muted);text-decoration:none;font-size:.82rem;font-weight:500;border:1px solid transparent}
.nav-group a::before{content:'';width:.4rem;height:.4rem;border-radius:2px;background:var(--border2);flex:none}
.nav-group a:hover{color:var(--text);background:var(--chip)}
.nav-group a[aria-current]{color:var(--text);font-weight:700;border-color:var(--accent);
background:color-mix(in srgb,var(--accent) 14%,transparent)}
.nav-group a[aria-current]::before{background:var(--accent)}
.rail-foot{margin-top:auto;padding:.6rem .45rem 0;border-top:1px solid var(--border);
color:var(--faint);font-size:.7rem}
.col{min-width:0;display:flex;flex-direction:column}
header.top{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:.6rem;
min-height:2.9rem;padding:.4rem 1rem;background:color-mix(in srgb,var(--bg) 92%,transparent);
backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
header.top form{display:flex;align-items:center;gap:.4rem;margin:0}
header.top form.search{flex:1 1 12rem;max-width:26rem}
header.top form.search input{width:100%;font-size:.82rem;padding:.38rem .65rem;background:var(--panel2);
border-color:var(--border)}
header.top .spacer{flex:1 1 0}
header.top button{min-height:30px;padding:.25rem .65rem;font-size:.76rem;color:var(--muted);
border-color:var(--border2)}
.status{display:inline-flex;align-items:center;gap:.4rem;font-size:.72rem;font-weight:700;
letter-spacing:.06em;text-transform:uppercase;color:var(--muted);border:1px solid var(--border);
border-radius:6px;padding:.28rem .6rem;background:var(--panel2);white-space:nowrap}
.status b{color:var(--text);font-family:var(--mono);font-weight:700}
.status i{width:.45rem;height:.45rem;border-radius:99px;background:var(--good)}
.wrap{width:100%;max-width:92rem;margin:0 auto;padding:1rem 1rem 3rem}
.pagehead{margin:0 0 1rem;padding:0 0 .8rem;border-bottom:1px solid var(--border)}
h1{font-size:1.25rem;margin:.15rem 0 .2rem;font-weight:800;letter-spacing:-.01em;line-height:1.2}
h2{font-size:.72rem;margin:1.6rem 0 .6rem;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
color:var(--muted);display:flex;align-items:center;gap:.5rem}
h2::before{content:'';width:.2rem;height:.85rem;border-radius:2px;background:var(--accent);flex:none}
.sub{color:var(--muted);margin:0 0 .8rem;font-size:.82rem}
.eyebrow{font-size:.64rem;font-weight:800;letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}
.hero{border:1px solid var(--border);border-radius:var(--radius);padding:1rem 1.1rem;margin:0 0 1rem;
background:var(--panel);border-left:3px solid var(--brand)}
.hero h1{font-size:1.35rem}
.hero p{color:var(--muted);max-width:52rem;margin:.3rem 0 0}
.hero-row{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin-top:.8rem}
.pill{display:inline-flex;align-items:center;gap:.35rem;font-size:.74rem;font-weight:700;
border:1px solid var(--border2);border-radius:6px;padding:.22rem .55rem;background:var(--panel2);white-space:nowrap}
.pill.good{color:var(--good);border-color:color-mix(in srgb,var(--good) 55%,var(--border))}
.pill.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 55%,var(--border))}
.pill.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 55%,var(--border))}
.verdict{font-size:.74rem;padding:.25rem .6rem;letter-spacing:.06em}
.verdict.match{color:#052e16;background:var(--good);border-color:var(--good)}
.verdict.contradiction{color:#fff;background:var(--bad);border-color:var(--bad)}
.verdict.off,.verdict.unverified{color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.5rem;margin:0 0 1rem}
.stat{border:1px solid var(--border);border-radius:var(--radius);padding:.6rem .75rem;background:var(--panel);
display:flex;flex-direction:column-reverse;gap:.15rem}
.stat b{display:block;font-family:var(--mono);font-size:1.2rem;font-weight:700}
.stat span{color:var(--faint);font-size:.62rem;font-weight:700;letter-spacing:.13em;text-transform:uppercase}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
.grid2>*,.duel>*,.visual-grid>*{min-width:0}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
.panel{border:1px solid var(--border);border-radius:var(--radius);padding:.85rem .95rem;background:var(--panel)}
.panel h2{margin-top:0}.panel h3{margin:.1rem 0 .45rem;font-size:.9rem}
.spark-panel{border-left:3px solid var(--spark);margin:0 0 1rem}
.spark-panel h2{font-size:.95rem;letter-spacing:0;text-transform:none;color:var(--text);flex-wrap:wrap}
.spark-panel h2::before{display:none}
.duel{display:grid;grid-template-columns:1fr auto 1fr;gap:.6rem;align-items:stretch;margin:.7rem 0}
@media (max-width:640px){.duel{grid-template-columns:1fr}.duel .vs{justify-self:center}}
.duel .side{border:1px solid var(--border);border-radius:var(--radius);padding:.6rem .75rem;background:var(--bg)}
.duel .side span{display:block;font-size:.62rem;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--faint)}
.duel .side b{font-family:var(--mono);font-size:1.1rem}
.vs{align-self:center;font-weight:800;color:var(--faint);font-size:.7rem;letter-spacing:.12em}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--border);
border-radius:var(--radius);margin:0 0 1rem;background:var(--panel)}
.table-scroll table{margin:0;border:none;min-width:42rem}
table{width:100%;border-collapse:collapse;margin:0 0 1rem;font-size:.82rem}
th,td{text-align:left;padding:.42rem .7rem;border-bottom:1px solid var(--border);white-space:nowrap}
td{font-family:var(--mono)}
th{color:var(--faint);font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;
background:var(--panel2);position:sticky;top:0}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--chip)}
.long,.call{color:var(--good)}.short,.put{color:var(--bad)}
.fresh{color:var(--good);font-weight:700}.stale{color:var(--warn);font-weight:700}
ul{margin:.4rem 0;padding-left:1.15rem}li{margin:.2rem 0}
dl{display:grid;grid-template-columns:9rem 1fr;gap:.2rem 1rem;margin:.5rem 0}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
a{color:var(--accent)}
pre{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:.8rem .9rem;
overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:.78rem;line-height:1.6;margin:0 0 1rem}
details{border:1px solid var(--border);border-radius:var(--radius);padding:.6rem .85rem;background:var(--panel);margin:0 0 .7rem}
details pre{border:none;padding:.5rem 0 0;margin:0;background:transparent}
summary{cursor:pointer;font-weight:700;min-height:28px;font-size:.82rem}
article{border:1px solid var(--border);border-radius:var(--radius);padding:0;margin:0 0 1rem;overflow:hidden;
background:var(--panel);max-width:48rem}
.trade-card{max-width:48rem}article .trade-card{margin:0}
.card-visual{padding:.9rem 1rem .2rem}
.card-head{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;margin:0 0 .5rem}
.card-head h2{margin:0;font-size:1rem;flex:1 1 12rem;letter-spacing:0;text-transform:none;color:var(--text)}
.card-head h2::before{display:none}
.badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.7rem;font-weight:700;
border:1px solid var(--border2);border-radius:6px;padding:.16rem .5rem;white-space:nowrap}
.badge.long{color:var(--good);border-color:var(--good)}.badge.short{color:var(--bad);border-color:var(--bad)}
.badge.regime{color:var(--muted)}.badge.expiry{color:var(--accent);border-color:var(--accent)}
.meter{height:.4rem;background:var(--chip);border-radius:99px;overflow:hidden;min-width:8rem;flex:1 1 10rem}
.meter i{display:block;height:100%;background:var(--accent);border-radius:99px;transition:width .5s}
.meter.hot i{background:var(--good)}.meter.warm i{background:var(--warn)}
.conf-row{display:flex;align-items:center;gap:.6rem;margin:.3rem 0 .5rem;font-size:.82rem;color:var(--muted)}
.conf-row b{color:var(--text)}
.visual-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin:.6rem 0}
@media (max-width:640px){.visual-grid{grid-template-columns:1fr}}
.visual{border:1px solid var(--border);border-radius:var(--radius);padding:.55rem .65rem;background:var(--bg)}
.visual h3{margin:0 0 .35rem;font-size:.62rem;color:var(--faint);font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.visual svg{width:100%;height:auto;display:block}
.visual .cap{margin:.3rem 0 0;font-size:.74rem;color:var(--muted)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:.4rem;margin:.6rem 0}
.fact{border:1px solid var(--border);border-radius:6px;padding:.45rem .55rem;background:var(--bg)}
.fact span{display:block;font-size:.6rem;color:var(--faint);font-weight:700;text-transform:uppercase;letter-spacing:.12em}
.fact b{font-family:var(--mono);font-size:.86rem}
.card-body{padding:0 1rem 1rem}.card-body ul{margin:.4rem 0}
article h2{border-bottom:none;margin:0 0 .8rem;padding-bottom:0}
.actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(13.5rem,1fr));gap:.5rem;margin:0 0 1rem}
.action{border:1px solid var(--border);border-radius:var(--radius);padding:.65rem .75rem;background:var(--panel)}
.action:hover{border-color:var(--border2)}
.action h3{margin:0 0 .3rem;font-size:.86rem}.action h3 a{text-decoration:none}
.action p{margin:.2rem 0;color:var(--muted);font-size:.78rem}
form.block{display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-end;margin:.45rem 0}
label.field{display:flex;flex-direction:column;gap:.25rem;font-size:.66rem;font-weight:700;letter-spacing:.1em;
text-transform:uppercase;color:var(--faint);min-width:7rem;flex:1 1 9rem}
input,select,textarea{font:inherit;font-size:16px;color:var(--text);background:var(--bg);
border:1px solid var(--border2);border-radius:6px;padding:.45rem .6rem;max-width:100%;
letter-spacing:0;text-transform:none;font-weight:400}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
textarea{width:100%;min-height:5rem;font-family:var(--mono);font-size:.8rem}
button,.btn{display:inline-flex;align-items:center;justify-content:center;font:inherit;font-size:.82rem;
font-weight:700;color:var(--accent);background:transparent;border:1px solid var(--accent);border-radius:6px;
padding:.45rem .85rem;min-height:36px;text-decoration:none;cursor:pointer;transition:background .15s,border-color .15s}
button:hover,.btn:hover{background:color-mix(in srgb,var(--accent) 14%,transparent)}
button.solid{background:var(--accent);color:#fff;border-color:var(--accent)}
button.solid:hover{background:color-mix(in srgb,var(--accent) 85%,#fff)}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{
outline:2px solid var(--accent);outline-offset:2px}
.shock-btn{display:inline-flex;align-items:center;margin:0 .4rem .4rem 0;padding:.4rem .8rem;min-height:36px;
border:1px solid var(--border2);border-radius:6px;color:var(--text);background:var(--panel);
text-decoration:none;font-weight:700;font-size:.82rem}
.shock-btn:hover{border-color:var(--accent);color:var(--accent)}
.chart-panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:10px;
margin:0 0 1rem;overflow-x:auto}
.chart-panel svg{max-width:100%;height:auto;display:block}
.chatbox{border:1px solid var(--border);border-radius:var(--radius);padding:.9rem 1rem;background:var(--panel);
margin:0 0 1rem}
.chatbox h2{font-size:.95rem;letter-spacing:0;text-transform:none;color:var(--text)}
.chatbox h2::before{display:none}
.chatbox form{display:flex;gap:.5rem;flex-wrap:wrap;align-items:flex-end}
.chatbox label.field .sub{margin:0;text-transform:none;letter-spacing:0;font-weight:400}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:.5rem 0}
.chipbtn{min-height:30px;padding:.2rem .6rem;font-size:.76rem;color:var(--muted);border-color:var(--border2)}
#chat-reply{margin-top:.7rem;display:none}#chat-reply.show{display:block}
footer.foot{margin-top:2rem;padding-top:.8rem;border-top:1px solid var(--border);color:var(--faint);
font-size:.72rem;display:flex;flex-wrap:wrap;gap:.4rem 1.2rem}
.skip{position:absolute;left:-999px}
.skip:focus{left:1rem;top:1rem;position:fixed;z-index:99;background:var(--panel);padding:.6rem 1rem}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:860px){
.shell{grid-template-columns:minmax(0,1fr)}
aside.rail{position:static;height:auto;flex-direction:row;align-items:center;gap:.25rem;overflow-x:auto;
border-right:none;border-bottom:1px solid var(--border);padding:.4rem .6rem}
a.brand{padding:.3rem .4rem;white-space:nowrap}.rail-tag,.rail-foot{display:none}
aside.rail nav{display:flex;align-items:center;gap:.25rem}
.nav-group{display:flex;align-items:center;gap:.15rem;margin:0;padding-left:.5rem;border-left:1px solid var(--border)}
.nav-group p{display:none}.nav-group a{white-space:nowrap;min-height:36px}
header.top{padding:.4rem .75rem}.status.wide{display:none}
.wrap{padding:.8rem .75rem 2.5rem}dl{grid-template-columns:1fr;gap:.15rem}}
"""


_CHART_CSS = """
.cx{width:100%;height:auto;display:block;font-family:var(--mono);font-size:11px}
.chart-panel .cx{min-width:34rem}
.cx text{fill:var(--muted)}
.cx.ladder text{paint-order:stroke;stroke:var(--panel);stroke-width:3px;stroke-linejoin:round}
.cx .dim{fill:var(--faint)}
.cx .strong{fill:var(--text);font-weight:700}
.cx .pos{fill:var(--good)}.cx .neg{fill:var(--bad)}
.cx .grid{stroke:var(--border);stroke-width:1;fill:none}
.cx .zero{stroke:var(--faint);stroke-width:1.5;fill:none}
.cx .band{fill:var(--accent);fill-opacity:.1}
.cx .wall{stroke:var(--text);stroke-width:1.5}
.cx .spot{stroke:var(--accent);stroke-width:1.5;stroke-dasharray:5 3;fill:none}
.cx .flip{stroke:var(--warn);stroke-width:1.5;stroke-dasharray:2 3;fill:none}
.cx text.spot-tag{fill:var(--accent);font-weight:700}
.cx text.flip-tag{fill:var(--warn);font-weight:700}
.cx .line{fill:none;stroke:var(--accent);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
.cx .dot{fill:var(--accent)}
.cx .track{fill:var(--chip)}
.cx .ref{stroke:var(--text);stroke-width:2;stroke-dasharray:5 3;fill:none}
.tabs{display:flex;flex-wrap:wrap;gap:.35rem;margin:0 0 .8rem}
.tabs a{display:inline-flex;align-items:center;min-height:30px;padding:.2rem .75rem;border:1px solid var(--border2);
border-radius:99px;color:var(--muted);text-decoration:none;font-size:.78rem;font-weight:700;letter-spacing:.04em;background:var(--panel)}
.tabs a:hover{color:var(--text);border-color:var(--accent)}
.tabs a[aria-current]{color:var(--text);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 18%,transparent)}
.legend{display:flex;flex-wrap:wrap;gap:.3rem 1.1rem;margin:-.3rem 0 1rem;color:var(--muted);font-size:.74rem}
.legend span{display:inline-flex;align-items:center;gap:.35rem}
.legend i{display:inline-block;width:.75rem;height:.75rem;border-radius:2px;background:var(--faint)}
.legend i.pos{background:var(--good)}.legend i.neg{background:var(--bad)}
.legend i.wall{background:transparent;border:1.5px solid var(--text)}
.legend i.spot{background:transparent;height:0;border-top:2px dashed var(--accent);border-radius:0}
.legend i.flip{background:transparent;height:0;border-top:2px dotted var(--warn);border-radius:0}
.table-scroll table.heat{min-width:0}
table.heat th,table.heat td{text-align:right;padding:.32rem .6rem}
table.heat th:first-child,table.heat td.k{text-align:left}
table.heat td.k{color:var(--text);font-weight:700}
table.heat td.k .tag{margin-left:.5rem;padding:.05rem .35rem;border-radius:4px;border:1px solid var(--accent);color:var(--accent);
font-size:.6rem;letter-spacing:.1em;text-transform:uppercase}
table.heat tr.near td{box-shadow:inset 0 1px 0 var(--accent),inset 0 -1px 0 var(--accent)}
table.heat td.cell.pos{--tint:var(--good)}table.heat td.cell.neg{--tint:var(--bad)}
table.heat td.cell{background:color-mix(in srgb,var(--tint,transparent) calc(var(--i,0)*55%),transparent)}
table.heat td.zero{color:var(--faint)}
table.heat td.total,table.heat th.total{border-left:1px solid var(--border2)}
"""


def _minify(css: str) -> str:
    # One line per rule above for diffs; joined here so pages stay compact. A rule broken
    # across lines must break after a comma, semicolon, or brace: strip() eats the space
    # that would otherwise separate descendant selectors.
    return "".join(line.strip() for line in css.splitlines())


def stylesheet() -> str:
    return _minify(_ROOT_CSS + _BASE_CSS + _CHART_CSS)


def chart_stylesheet() -> str:
    """The chart classes plus the dark palette, for standalone pages that do not inline stylesheet()."""
    return _minify(_ROOT_CSS + _CHART_CSS)


# Sidebar sections. Every entry is (label, href, key): key matches the
# `current` a page passes, and marks which hrefs take the active underlying.
NAV_GROUPS = (
    ("Dashboard", (
        ("Home", "/", "home"),
        ("Symbol desk", "/dashboard", "dashboard"),
        ("Charts", "/chart", "chart"),
    )),
    ("Scanners", (
        ("Regime board", "/board", "board"),
        ("Earnings radar", "/earnings", "earnings"),
    )),
    ("Options & flow", (
        ("Trade cards", "/cards", "cards"),
        ("Strategy builder", "/builder", "builder"),
    )),
    ("Trade", (
        ("Agentic trader", "/deployments/page", "deployments"),
        ("Track record", "/trackrecord", "trackrecord"),
        ("Live ordering", "/live", "live"),
    )),
    ("Account", (
        ("Feed health", "/feeds", "feeds"),
        ("Settings", "/settings", "settings"),
    )),
)
SYMBOL_PAGES = ("dashboard", "chart", "cards", "builder")
NAV_LINKS = tuple((key, href) for _, items in NAV_GROUPS for _, href, key in items)


def _rail(underlying: str, current: str) -> str:
    escaped = _urlencode(underlying)
    groups = []
    for title, items in NAV_GROUPS:
        links = []
        for label, href, key in items:
            if key in SYMBOL_PAGES and underlying:
                href = f"{href}?underlying={escaped}"
            mark = ' aria-current="page"' if key == current else ""
            links.append(f'<a href="{href}"{mark}>{html.escape(label)}</a>')
        groups.append(
            f'<div class="nav-group"><p>{html.escape(title)}</p>{"".join(links)}</div>'
        )
    brand = (
        f"<a class=\"brand\" href=\"/\">"
        f"<img src=\"{ICON_URL}\" alt=\"\" width=\"26\" height=\"26\">{BRAND}</a>"
    )
    return (
        f"<aside class=\"rail\">{brand}<span class=\"rail-tag\">agentic options desk</span>"
        f"<nav aria-label=\"sections\">{''.join(groups)}</nav>"
        "<div class=\"rail-foot\">Paper first. Research only.</div></aside>"
    )


def nav_header(underlying: str, current: str, settings: DashboardSettings, next_path: str) -> str:
    other = "light" if settings.theme == "dark" else "dark"
    symbol = html.escape(underlying, quote=True)
    search = (
        "<form class=\"search\" method=\"get\" action=\"/dashboard\" role=\"search\">"
        "<input name=\"underlying\" placeholder=\"Search symbol…\" aria-label=\"symbol\" "
        "autocomplete=\"off\" autocapitalize=\"characters\"></form>"
    )
    status = f"<span class=\"status\"><i></i>symbol <b>{symbol}</b></span>" if underlying else ""
    toggle = (
        '<form method="post" action="/settings">'
        f'<input type="hidden" name="theme" value="{other}">'
        f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">'
        f'<button type="submit" aria-label="switch to {other} mode">{other} mode</button></form>'
    )
    return (
        f"<header class=\"top\">{search}<span class=\"spacer\"></span>{status}"
        f"<span class=\"status wide\">paper default</span>{toggle}</header>"
    )


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
    refresh: bool = False,
) -> str:
    return (
        "<!doctype html>"
        f"<html lang=\"en\" data-theme=\"{html.escape(settings.theme, quote=True)}\">"
        "<head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"description\" content=\"MarketWingMan: Claude proposes, the exposure engine grades it. Paper-first options research with a journaled audit trail.\">"
        f"<link rel=\"icon\" type=\"image/png\" href=\"{ICON_URL}\">"
        f"<link rel=\"apple-touch-icon\" href=\"{ICON_URL}\">"
        f"{refresh_meta(settings) if refresh else ''}"
        f"<title>{html.escape(title)} | {BRAND}</title><style>{stylesheet()}</style></head><body>"
        "<a class=\"skip\" href=\"#main\">skip to content</a>"
        f"<div class=\"shell\">{_rail(underlying, current)}<div class=\"col\">"
        f"{nav_header(underlying, current, settings, next_path)}"
        "<div class=\"wrap\"><div class=\"pagehead\">"
        f"<h1>{html.escape(heading)}</h1>"
        f"<p class=\"sub\">{html.escape(sub)}</p></div>"
        f"<main id=\"main\">{body}</main>"
        "<footer class=\"foot\"><span>MarketWingMan — research only, not investment advice. Paper trading is the default; live ordering stays off until armed on /live.</span>"
        "<span>Every signal, intent, fill, and model call is journaled.</span></footer>"
        "</div></div></div></body></html>"
    )
