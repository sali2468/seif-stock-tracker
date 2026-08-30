"""ui_components.py — pure presentational helpers (HTML/badge builders).

Extracted verbatim from app.py. Each returns an HTML string (or renders via
st.markdown) and depends only on Streamlit + stdlib — no app.py state."""

import streamlit as st


def _page_header(title: str, sub: str = ""):
    """Consistent page title + subtitle across all pages."""
    st.markdown(
        f'<div class="page-title">{title}</div>'
        + (f'<div class="page-sub">{sub}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


def _metric_tile(label: str, value: str, sub: str = "", color: str = "var(--fg)") -> str:
    """Returns HTML for a single stat tile."""
    return (
        f'<div class="tile">'
        f'<div class="tile-label">{label}</div>'
        f'<div class="tile-value" style="color:{color}">{value}</div>'
        f'<div class="tile-sub">{sub}</div>'
        f'</div>'
    )


def _status_dot(ok: bool, on_text: str, off_text: str = "") -> str:
    """Inline coloured status indicator."""
    col  = "var(--pos)" if ok else "var(--neg)"
    text = on_text if ok else (off_text or on_text)
    return f'<span style="color:{col};font-size:.75rem;font-weight:600">{"⬤" if ok else "○"} {text}</span>'


def _star_color(stars: int) -> str:
    """Star rating → hex color."""
    return "#fbbf24" if stars == 3 else "var(--muted)" if stars == 2 else "#b45309"


def _sig_type_badge(sig) -> str:
    """✅ BUY or 👁 WATCH badge span for signal cards."""
    if sig.signal_type == "WATCH":
        return ('<span style="background:rgba(245,158,11,.18);color:#fbbf24;'
                'border:1px solid rgba(245,158,11,.4);border-radius:5px;'
                'padding:2px 9px;font-size:.72rem;font-weight:800;'
                'letter-spacing:.4px;margin-left:6px">👁 WATCH</span>')
    return ('<span style="background:rgba(59,130,246,.15);color:var(--accent);'
            'border:1px solid rgba(59,130,246,.35);border-radius:5px;'
            'padding:2px 9px;font-size:.72rem;font-weight:800;'
            'letter-spacing:.4px;margin-left:6px">✅ BUY</span>')


def _watch_banner_html(sig) -> str:
    """Amber WATCH banner as HTML string, empty string for BUY signals."""
    if sig.signal_type != "WATCH" or not sig.watch_reason:
        return ""
    buy_line = (
        f'<div style="margin-top:8px;color:#fbbf24;font-size:.85rem;font-weight:700">'
        f'📍 Recommended buy price: ${sig.watch_buy_at:.2f}</div>'
        if sig.watch_buy_at else ""
    )
    return (
        f'<div style="margin:4px 0 6px;padding:10px 16px;border-radius:8px;'
        f'background:rgba(245,158,11,.08);border-left:3px solid #f59e0b;'
        f'border:1px solid rgba(245,158,11,.25)">'
        f'<div style="color:#fbbf24;font-weight:700;font-size:.78rem;'
        f'text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px">'
        f'⏳ Entry timing not ideal — wait for pullback</div>'
        f'<div style="color:var(--muted);font-size:.81rem;line-height:1.55">{sig.watch_reason}</div>'
        f'{buy_line}'
        f'</div>'
    )


def _ipo_status_badge(status: str) -> str:
    """Coloured status badge for IPO rows."""
    cfg = {
        "priced":   ("var(--pos)", "PRICED ●"),
        "expected": ("var(--accent)", "EXPECTED"),
        "filed":    ("#f59e0b", "FILED"),
    }
    col, label = cfg.get(status, ("var(--faint)", status.upper()))
    return (
        f'<span style="color:{col};background:{col}18;border:1px solid {col}44;'
        f'border-radius:4px;padding:1px 7px;font-size:.67rem;font-weight:800;'
        f'letter-spacing:.5px;margin-left:6px">{label}</span>'
    )


def _ipo_countdown(days_away: int) -> tuple:
    """Returns (label, hex_color) countdown string for IPO rows."""
    if days_away == 0:  return "Today",    "var(--neg)"
    if days_away == 1:  return "Tomorrow", "#f59e0b"
    if days_away <= 7:  return f"In {days_away}d", "#f59e0b"
    if days_away > 0:   return f"In {days_away}d", "var(--faint)"
    return f"{abs(days_away)}d ago", "var(--faint)"


def regime_plain(r: str) -> tuple:
    m = {
        "BULL_QUIET":    ("var(--pos)", "🟢  Calm Bull Market",     "Ideal conditions. Full size, lean long."),
        "BULL_VOLATILE": ("#f59e0b", "🟡  Choppy Bull Market",   "Trend up but bumpy. Trade smaller, tighter stops."),
        "BEAR_QUIET":    ("#f97316", "🟠  Quiet Bear Market",    "Trend down. Only the best setups, half size."),
        "BEAR_VOLATILE": ("var(--neg)", "🔴  Volatile Bear Market", "High danger. Minimal trades, protect capital."),
        "CRISIS":        ("#991b1b", "💀  Market Crisis",        "Stay in cash. No new longs. Wait."),
    }
    return m.get(r, ("var(--faint)", r, ""))


def action_badge(action: str, color: str) -> str:
    return (f'<span class="badge" style="background:{color}22;'
            f'color:{color};border:1px solid {color}55">{action}</span>')


def sector_badge(sector: str) -> str:
    return f'<span class="sector-badge">{sector}</span>' if sector else ""
