"""styles.py — themeable app CSS (Robinhood-style dark + Fidelity-clean light).

get_css(theme) returns the full <style> block; only the :root token block
changes between "dark" and "light" — every rule below uses CSS variables, so
both themes share one source of truth. Robinhood green accent, big tabular
numbers, airy rounded cards. All class/selector names are preserved.
"""

_FONT = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=Geist:wght@400;500;600;700;800&family=Geist+Mono:wght@400;500;600&display=swap');"
)

# ── Theme token blocks ────────────────────────────────────────────────────────
_DARK = """:root {
    --bg:            #0d0f10;
    --surface:       rgba(255,255,255,.035);
    --surface-hover: rgba(255,255,255,.06);
    --border:        rgba(255,255,255,.09);
    --border-strong: rgba(255,255,255,.16);
    --fg:            #f5f7f8;
    --muted:         #9ca3a8;
    --faint:         #6b7280;
    --dim:           #4b5257;
    --pos:           #00c805;
    --neg:           #ff5000;
    --accent:        #22a3e6;
    --accent-ink:    #ffffff;
    --accent-soft:   rgba(34,163,230,.14);
    --ring:          rgba(34,163,230,.32);
    --scroll:        #2a2f2c;
    --radius:        10px;
    --radius-lg:     16px;
    --sans: 'Geist', ui-sans-serif, system-ui, -apple-system, sans-serif;
    --mono: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}"""

_LIGHT = """:root {
    --bg:            #ffffff;
    --surface:       #f6f7f9;
    --surface-hover: #eceef1;
    --border:        rgba(0,0,0,.10);
    --border-strong: rgba(0,0,0,.20);
    --fg:            #0c1013;
    --muted:         #4b5560;
    --faint:         #5c646e;
    --dim:           #6d7681;
    --pos:           #00a406;
    --neg:           #e5352b;
    --accent:        #0e86c9;
    --accent-ink:    #ffffff;
    --accent-soft:   rgba(14,134,201,.12);
    --ring:          rgba(14,134,201,.22);
    --scroll:        #c9ced4;
    --radius:        10px;
    --radius-lg:     16px;
    --sans: 'Geist', ui-sans-serif, system-ui, -apple-system, sans-serif;
    --mono: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}"""

# ── Shared rules (theme-agnostic via var()) ───────────────────────────────────
_RULES = """
/* ── 1. Base ──────────────────────────────────────────────────────────────── */
[data-testid="stAppViewContainer"], section[data-testid="stSidebar"] { font-family: var(--sans); color: var(--fg); }
[data-testid="stAppViewContainer"] { background: var(--bg) !important; }
[data-testid="stAppViewContainer"] > .main, [data-testid="stAppViewContainer"] .block-container { background: var(--bg) !important; }
[data-testid="stAppViewContainer"] h1, [data-testid="stAppViewContainer"] h2,
[data-testid="stAppViewContainer"] h3, [data-testid="stAppViewContainer"] h4,
[data-testid="stAppViewContainer"] .stMarkdown p { color: var(--fg); }
section[data-testid="stSidebar"]           { background: var(--bg) !important; border-right: 1px solid var(--border) !important; }
section[data-testid="stSidebar"] > div     { padding-top: 0 !important; }
.block-container { padding: 0.5rem 2rem 2rem !important; max-width: 1200px; }
iframe { display: block; }
/* Hide Streamlit's default top toolbar chrome (⋮ menu, Deploy, status widget).
   Collapse the header to zero height rather than display:none on the whole
   container — the collapsed-sidebar reopen control can live inside it, so we
   keep the container in layout and only hide its chrome children. */
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"], #MainMenu { display: none !important; }
[data-testid="stHeader"] { background: transparent !important; height: 0 !important; min-height: 0 !important; border: none !important; box-shadow: none !important; }
/* Lock the navigation sidebar OPEN — users cannot hide/collapse the nav panel. */
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="stExpandSidebarButton"] { display: none !important; }
section[data-testid="stSidebar"] { transform: none !important; visibility: visible !important; min-width: 244px !important; }
section[data-testid="stSidebar"][aria-expanded="false"] { margin-left: 0 !important; }
/* Tighter vertical rhythm (dense Robinhood/Fidelity feel) */
[data-testid="stVerticalBlock"]   { gap: 0.6rem !important; }
[data-testid="stAppViewContainer"] .element-container { margin-bottom: 0 !important; }
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: 0.45rem !important; }
hr { margin: 0.6rem 0 !important; }

::-webkit-scrollbar       { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--scroll); border-radius: 4px; }

/* ── 2. Typography ────────────────────────────────────────────────────────── */
.page-title    { font-size: 1.75rem; font-weight: 700; letter-spacing: -.02em; color: var(--fg); line-height: 1.15; margin: 0 0 4px; }
.page-sub      { color: var(--faint); font-size: .85rem; margin: 0 0 18px; line-height: 1.5; }
.section-label { font-size: .67rem; font-weight: 600; color: var(--faint); text-transform: uppercase; letter-spacing: .8px; margin: 0 0 10px; }

/* ── 3. Card system ───────────────────────────────────────────────────────── */
.card {
    border-radius: var(--radius-lg); padding: 18px 20px; margin: 8px 0;
    background: var(--surface); border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
    transition: background .18s, border-color .18s;
}
.card:hover { background: var(--surface-hover); border-left-color: var(--dim); }
.card-buy    { border-left-color: var(--accent); }
.card-watch  { border-left-color: var(--dim); }
.card-raise  { border-left-color: var(--border-strong); }
.card-hold   { border-left-color: var(--border-strong); }
.card-exit   { border-left-color: var(--neg); }
.card-profit { border-left-color: var(--pos); }

/* ── 4. Stat tiles ────────────────────────────────────────────────────────── */
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 18px; text-align: center; transition: background .18s; }
.tile:hover  { background: var(--surface-hover); }
.tile-label  { font-size: .66rem; font-weight: 600; color: var(--faint); text-transform: uppercase; letter-spacing: .7px; }
.tile-value  { font-family: var(--mono); font-size: 1.7rem; font-weight: 600; color: var(--fg); margin: 6px 0 3px; line-height: 1; letter-spacing: -.01em; }
.tile-sub    { font-size: .72rem; color: var(--faint); }

/* ── 5. Pills & badges ────────────────────────────────────────────────────── */
.pill { display: inline-block; background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 3px 11px; margin: 2px 2px; font-size: .77rem; color: var(--muted); }
.pill b { color: var(--fg); }
.badge { display: inline-block; padding: 3px 12px; border-radius: 999px; font-weight: 600; font-size: .76rem; letter-spacing: .2px; }
.sector-badge { display: inline-block; background: var(--surface); border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 1px 8px; font-size: .7rem; font-weight: 500; letter-spacing: .3px; margin-left: 6px; vertical-align: middle; }

/* ── 6. Text helpers ──────────────────────────────────────────────────────── */
.ticker-big   { font-size: 1.4rem; font-weight: 700; color: var(--fg); letter-spacing: -.02em; }
.price-tag    { font-family: var(--mono); font-size: .92rem; color: var(--faint); margin-left: 4px; }
.company-meta { color: var(--faint); font-size: .8rem; margin: 2px 0 10px; line-height: 1.5; }
.company-name { color: var(--muted); font-weight: 500; }
.thin-div     { border: none; border-top: 1px solid var(--border); margin: 10px 0; }
.mono         { font-family: var(--mono); }

/* ── 7. Stats (inline) ────────────────────────────────────────────────────── */
.stat       { display: inline-block; margin-right: 20px; }
.stat-label { color: var(--faint); font-size: .68rem; text-transform: uppercase; letter-spacing: .5px; display: block; }
.stat-value { font-family: var(--mono); color: var(--fg); font-size: 1rem; font-weight: 600; }

/* ── 8. Regime banner ─────────────────────────────────────────────────────── */
.regime-banner { border-radius: var(--radius-lg); padding: 14px 20px; margin-bottom: 18px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; border: 1px solid var(--border); background: var(--surface); }

/* ── 9. Sidebar / brand ───────────────────────────────────────────────────── */
.brand-title { font-size: 1.15rem; font-weight: 700; letter-spacing: -.02em; color: var(--fg); }
.brand-pro { display: inline-block; background: var(--accent); color: var(--accent-ink); border-radius: 5px; padding: 1px 5px; font-size: .6rem; font-weight: 700; letter-spacing: 1px; margin-left: 5px; vertical-align: middle; }
.brand-sub { color: var(--dim); font-size: .68rem; text-transform: uppercase; letter-spacing: 1px; }
section[data-testid="stSidebar"] [data-testid="stRadio"]       { overflow: visible !important; }
section[data-testid="stSidebar"] [data-testid="stRadio"] > div { overflow: visible !important; gap: 1px !important; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label { padding: 8px 10px 8px 8px !important; border-radius: var(--radius) !important; transition: background .15s !important; font-size: .88rem !important; color: var(--muted) !important; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background: var(--surface-hover) !important; color: var(--fg) !important; }
/* The nav text is a <p> inside stMarkdownContainer that Streamlit paints with its
   base theme color (#fafafa) — override so it follows our theme and stays readable
   in light mode. */
section[data-testid="stSidebar"] [data-testid="stRadio"] label [data-testid="stMarkdownContainer"] p { color: var(--muted) !important; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label:hover [data-testid="stMarkdownContainer"] p { color: var(--fg) !important; }
/* Widget labels (text/number/select/slider/checkbox…) — Streamlit paints these
   with its base theme color (#fafafa); force the theme token so every form label
   stays readable in light mode as well as dark. */
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label { color: var(--muted) !important; }
/* Sidebar collapse/expand chrome icon (was near-white in light mode) */
[data-testid="stSidebarHeader"] [data-testid="stIconMaterial"],
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"] { color: var(--muted) !important; }

/* ── 10. Streamlit overrides ─────────────────────────────────────────────── */
div[data-testid="stMetricValue"]    { font-family: var(--mono) !important; font-size: 1.25rem !important; font-weight: 600 !important; color: var(--fg) !important; }
div[data-testid="metric-container"] { background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: var(--radius) !important; padding: 12px !important; }
div[data-testid="stMetricLabel"] p  { font-size: .68rem !important; text-transform: uppercase; letter-spacing: .6px; color: var(--faint) !important; }

.stTabs [data-baseweb="tab-list"] { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 4px; gap: 3px; }
.stTabs [data-baseweb="tab"] { border-radius: var(--radius); color: var(--muted); padding: 9px 20px; font-weight: 500; font-size: .9rem; transition: all .15s; }
.stTabs [data-baseweb="tab"]:hover { background: var(--surface-hover) !important; color: var(--fg) !important; }
.stTabs [aria-selected="true"] { background: var(--accent-soft) !important; color: var(--accent) !important; font-weight: 600 !important; }

button[kind="primary"] { background: var(--accent) !important; color: var(--accent-ink) !important; border: none !important; border-radius: 999px !important; font-weight: 600 !important; letter-spacing: 0 !important; }
button[kind="primary"]:hover { filter: brightness(1.08) !important; }
button[kind="secondary"] { background: transparent !important; border: 1px solid var(--border-strong) !important; border-radius: 999px !important; color: var(--fg) !important; font-weight: 500 !important; }
button[kind="secondary"]:hover { background: var(--surface-hover) !important; }

[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stTextArea"] textarea { background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: var(--radius) !important; color: var(--fg) !important; -webkit-text-fill-color: var(--fg) !important; caret-color: var(--fg) !important; font-family: var(--sans) !important; }
[data-testid="stTextInput"] input:focus, [data-testid="stNumberInput"] input:focus { border-color: var(--accent) !important; box-shadow: 0 0 0 2px var(--ring) !important; }
/* Placeholder + browser-autofilled credentials must stay readable in BOTH themes */
[data-testid="stTextInput"] input::placeholder, [data-testid="stNumberInput"] input::placeholder { color: var(--faint) !important; -webkit-text-fill-color: var(--faint) !important; opacity: 1 !important; }
[data-testid="stTextInput"] input:-webkit-autofill { -webkit-text-fill-color: var(--fg) !important; -webkit-box-shadow: 0 0 0 1000px var(--surface) inset !important; caret-color: var(--fg) !important; }

[data-testid="stExpander"] { background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: var(--radius-lg) !important; margin: 4px 0 !important; overflow: hidden; }
[data-testid="stExpander"] summary { background: transparent !important; color: var(--muted) !important; font-weight: 500 !important; font-size: .88rem !important; padding: 12px 16px !important; }
[data-testid="stExpander"] summary:hover { background: var(--surface-hover) !important; }

[data-testid="stProgress"] > div       { background: var(--surface-hover) !important; border-radius: 4px !important; }
[data-testid="stProgress"] > div > div { background: var(--accent) !important; border-radius: 4px !important; }

[data-testid="stAlert"]     { border-radius: var(--radius) !important; }
[data-testid="stDataFrame"] { border-radius: var(--radius) !important; overflow: hidden; }

/* Fidelity-style holdings table */
.holdings { width: 100%; border-collapse: collapse; font-size: .85rem; margin: 2px 0 6px; }
.holdings th { text-align: right; color: var(--faint); font-weight: 600; font-size: .66rem; text-transform: uppercase; letter-spacing: .5px; padding: 6px 12px; border-bottom: 1px solid var(--border); }
.holdings td { text-align: right; padding: 11px 12px; border-bottom: 1px solid var(--border); font-family: var(--mono); color: var(--muted); white-space: nowrap; }
.holdings tbody tr:hover { background: var(--surface); }
.holdings .sym { text-align: left; font-family: var(--sans); font-weight: 600; color: var(--fg); }

/* ── 11. AI card ──────────────────────────────────────────────────────────── */
.ai-card { border-radius: var(--radius-lg); padding: 20px 22px; margin: 12px 0; border: 1px solid var(--border); background: var(--surface); border-left: 2px solid var(--accent); }

/* ── 12. Moomoo cards ─────────────────────────────────────────────────────── */
.mm-plan-card { border-radius: var(--radius-lg); padding: 14px 18px; margin: 6px 0; border: 1px solid var(--border); border-left: 2px solid var(--border-strong); background: var(--surface); }
.mm-plan-card.mm-partial { border-left-color: var(--dim); }
.mm-plan-card.mm-closed  { border-left-color: var(--pos); opacity: .7; }
.mm-badge        { display: inline-block; font-size: .67rem; font-weight: 600; letter-spacing: .06em; padding: 2px 7px; border-radius: 999px; text-transform: uppercase; }
.mm-badge-sim    { background: var(--surface-hover); color: var(--muted); }
.mm-badge-real   { background: rgba(255,80,0,.15);  color: var(--neg); }
.mm-badge-active { background: var(--accent-soft);  color: var(--accent); }
.mm-badge-part   { background: var(--surface-hover); color: var(--muted); }
.mm-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 18px; margin: 6px 0; }

/* ── 13. Animations ───────────────────────────────────────────────────────── */
@keyframes pulse  { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: .4; transform: scale(.75); } }
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.fade-in { animation: fadeIn .3s ease; }
"""


def get_css(theme: str = "dark") -> str:
    root = _LIGHT if theme == "light" else _DARK
    return f"<style>\n{_FONT}\n{root}\n{_RULES}\n</style>"


APP_CSS = get_css("dark")   # back-compat for any importer
