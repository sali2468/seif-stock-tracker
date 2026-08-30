"""legal.py — disclaimer / terms text + reusable UI helpers.

IMPORTANT (to the operator, not shown to users): this is standard protective
boilerplate, NOT legal advice. Have a licensed attorney review it before relying
on it — an app that outputs buy/sell stock rankings can implicate securities /
investment-adviser law that disclaimers alone may not fully cover.
"""

import streamlit as st

DISCLAIMER_SHORT = (
    "For educational & informational purposes only — **not financial advice**. "
    "Trading involves substantial risk of loss; you are solely responsible for your own decisions."
)

TERMS_TEXT = """
### Terms of Use & Risk Disclaimer

**Please read carefully. By creating an account or using this application ("the App"), you agree to all of the following.**

**1. Not financial advice.** The App and everything it produces — scores, star ratings, rankings, scans, signals, watchlists, and "recommendations" — are for **educational and informational purposes only**. They are **not** financial, investment, trading, legal, or tax advice, and are **not** an offer, solicitation, or recommendation to buy or sell any security.

**2. No adviser relationship.** The App and its operator are **not** a registered investment adviser, broker-dealer, or financial planner and do **not** provide personalized investment advice. Nothing here creates a fiduciary or advisory relationship.

**3. Your decisions are your own.** Any trade or investment decision you make is made **solely at your own discretion and risk**. Always do your own research and consult a licensed financial professional before investing.

**4. Trading risk.** Trading and investing involve **substantial risk, including the possible loss of your entire investment**. Past performance and any signals, ratings, or projections are **not** indicative of future results. No strategy or ranking is guaranteed to be profitable.

**5. No warranty.** The App is provided **"as is" and "as available," without warranties of any kind**, express or implied, including accuracy, completeness, timeliness, or fitness for a particular purpose. Data may be delayed, inaccurate, or incomplete.

**6. Limitation of liability.** To the maximum extent permitted by law, the operator and its affiliates shall **not be liable for any losses or damages** — direct, indirect, incidental, consequential, or financial — arising from your use of or reliance on the App. **You use the App entirely at your own risk and agree not to hold the operator liable for any trading or investment losses.**

**7. Third-party data.** Market data comes from third parties (e.g., Finnhub) under their terms; the operator does not guarantee its accuracy.

**8. Acceptance.** By checking "I agree" and using the App, you confirm you have read, understood, and accepted these terms.
"""


def render_terms_expander(expanded: bool = False):
    """Full terms inside an expander — used on the signup screen."""
    with st.expander("📄 Terms of Use & Risk Disclaimer (please read)", expanded=expanded):
        st.markdown(TERMS_TEXT)


def render_footer():
    """A persistent, unobtrusive disclaimer shown at the bottom of every page."""
    st.markdown(
        '<div style="margin:26px 0 6px;padding-top:12px;border-top:1px solid var(--border);'
        'color:var(--faint);font-size:.72rem;line-height:1.5;text-align:center">'
        '⚠️ For educational &amp; informational purposes only — <b>not financial advice</b>. '
        'Trading involves substantial risk of loss; you are solely responsible for your own decisions. '
        'This app is not a registered investment adviser or broker-dealer. '
        'No guarantee of accuracy or profit; use at your own risk.'
        '</div>',
        unsafe_allow_html=True,
    )
