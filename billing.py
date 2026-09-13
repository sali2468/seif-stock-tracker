"""billing.py — Stripe subscription paywall for StockPal.

Design goals: secure, trusted, reliable — and SAFE to deploy dark.

• Card details NEVER touch our servers. All payment entry happens on Stripe's
  hosted Checkout page (PCI-compliant); we only ever receive a subscription id
  and ask Stripe "is it active?".
• Gated behind PAYWALL_ENABLED — with the flag off (the default) nothing here
  changes the app at all, so the code can ship long before it's switched on.
• Grandfathering: every account that exists at cutover is free forever. Only
  accounts created at/after PAYWALL_CUTOVER_TS must subscribe. Admin always
  bypasses. Legacy accounts with no `created` timestamp are treated as existing.
• Fail OPEN: if the paywall is enabled but misconfigured (missing keys / cutover),
  we allow access and log a warning — we never wrongly lock out or charge.

Required env (set in the Render dashboard, never in code):
    PAYWALL_ENABLED=1
    PAYWALL_CUTOVER_TS=<unix seconds at rollout>   # accounts before this are free
    STRIPE_SECRET_KEY=sk_live_...   (or sk_test_... while testing)
    STRIPE_PRICE_ID=price_...       (the $20/mo recurring price)
    STRIPE_TRIAL_DAYS=7             (optional, default 7)
    APP_URL=https://stockpal.org    (optional, for Checkout return URLs)
"""

import os
import time
import logging

import streamlit as st
import auth

log = logging.getLogger("billing")

_MARKER = os.path.join(os.getenv("DATA_DIR") or os.path.dirname(__file__), ".paywall_migrated")


# ── Config helpers ────────────────────────────────────────────────────────────
def _enabled() -> bool:
    return os.getenv("PAYWALL_ENABLED", "").lower() in ("1", "true", "yes")


def _cutover_ts() -> float:
    try:
        return float(os.getenv("PAYWALL_CUTOVER_TS", "0") or 0)
    except ValueError:
        return 0.0


def _price_id() -> str:
    return os.getenv("STRIPE_PRICE_ID", "").strip()


def _trial_days() -> int:
    try:
        return int(os.getenv("STRIPE_TRIAL_DAYS", "7"))
    except ValueError:
        return 7


def _app_url() -> str:
    return os.getenv("APP_URL", "https://stockpal.org").rstrip("/")


def _stripe():
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return None
    try:
        import stripe
        stripe.api_key = key
        return stripe
    except Exception as e:
        log.warning("stripe import failed: %s", e)
        return None


def _configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip() and _price_id() and _cutover_ts())


# ── Account field access (reuse auth's dual-location store) ────────────────────
def _acct(username: str) -> dict:
    return auth._load().get((username or "").lower().strip(), {})


def _set_acct(username: str, **fields) -> None:
    users = auth._load()
    u = (username or "").lower().strip()
    if u in users:
        users[u].update(fields)
        auth._save(users)


# ── Grandfathering ────────────────────────────────────────────────────────────
def is_exempt(username: str, acct: dict) -> bool:
    """True when this account never has to pay."""
    if auth.is_admin():
        return True
    if acct.get("grandfathered"):
        return True
    created = acct.get("created")
    if created is None:
        return True   # legacy account — pre-dates the paywall
    return bool(_cutover_ts()) and created < _cutover_ts()


def grandfather_all_once() -> None:
    """One-time (per disk): stamp grandfathered=True on every pre-cutover account,
    so existing users are permanently protected even if cutover config changes."""
    if not _enabled() or not _cutover_ts() or os.path.exists(_MARKER):
        return
    try:
        users = auth._load()
        ct = _cutover_ts()
        changed = False
        for _u, r in users.items():
            c = r.get("created")
            if not r.get("grandfathered") and (c is None or c < ct):
                r["grandfathered"] = True
                changed = True
        if changed:
            auth._save(users)
        with open(_MARKER, "w") as f:
            f.write(str(time.time()))
        log.info("paywall: grandfathered existing accounts (marker written)")
    except Exception as e:
        log.warning("grandfather migration failed: %s", e)


# ── Subscription status (Stripe API, lightly cached) ──────────────────────────
_ACTIVE = ("active", "trialing")


def _active(username: str) -> bool:
    acct = _acct(username)
    sub_id = acct.get("stripe_subscription_id")
    if not sub_id:
        return False
    now = time.time()
    # 5-min cache to avoid a Stripe call on every Streamlit rerun
    if acct.get("sub_status") in _ACTIVE and (now - acct.get("sub_checked_at", 0)) < 300:
        return True
    stripe = _stripe()
    if not stripe:
        return acct.get("sub_status") in _ACTIVE
    try:
        sub = stripe.Subscription.retrieve(sub_id)
        status = sub.get("status") if isinstance(sub, dict) else getattr(sub, "status", None)
        _set_acct(username, sub_status=status, sub_checked_at=now)
        return status in _ACTIVE
    except Exception as e:
        log.warning("subscription status check failed: %s", e)
        return acct.get("sub_status") in _ACTIVE


def _reconcile(username: str, acct: dict) -> bool:
    """Self-heal: find this user's subscription in Stripe and record it, even if we
    never stored the id (e.g. the Checkout redirect lost the session). Matched by the
    account email AND our metadata.username, so it never claims someone else's sub."""
    stripe = _stripe()
    email = (acct or {}).get("email")
    if not stripe or not email:
        return False
    try:
        custs = stripe.Customer.list(email=email, limit=10)
        for c in getattr(custs, "data", []) or []:
            subs = stripe.Subscription.list(customer=c.id, status="all", limit=20)
            for s in getattr(subs, "data", []) or []:
                status = getattr(s, "status", None)
                meta = getattr(s, "metadata", None) or {}
                uname = meta.get("username") if hasattr(meta, "get") else None
                if status in _ACTIVE and (uname is None or uname == username):
                    _set_acct(username, stripe_customer_id=c.id, stripe_subscription_id=s.id,
                              sub_status=status, sub_checked_at=time.time())
                    log.info("reconciled existing Stripe subscription for %s", username)
                    return True
    except Exception as e:
        log.warning("subscription reconcile failed: %s", e)
    return False


# ── Stripe hosted flows ───────────────────────────────────────────────────────
def _create_checkout(username: str, email: str) -> str:
    stripe = _stripe()
    if not stripe or not _price_id():
        return ""
    acct = _acct(username)
    kwargs = dict(
        mode="subscription",
        line_items=[{"price": _price_id(), "quantity": 1}],
        subscription_data={"trial_period_days": _trial_days(),
                           "metadata": {"username": username}},
        success_url=f"{_app_url()}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{_app_url()}/?checkout=cancel",
        client_reference_id=username,
        metadata={"username": username},
        allow_promotion_codes=True,
        # Managed Payments is on by default on some accounts and demands a product
        # tax code; opt out so a plain $20/mo subscription checkout just works.
        managed_payments={"enabled": False},
    )
    if acct.get("stripe_customer_id"):
        kwargs["customer"] = acct["stripe_customer_id"]
    elif email:
        kwargs["customer_email"] = email
    try:
        sess = stripe.checkout.Session.create(**kwargs)
        return sess.url
    except Exception as e:
        log.warning("checkout session create failed: %s", e)
        return ""


def _create_portal(username: str) -> str:
    stripe = _stripe()
    acct = _acct(username)
    if not stripe or not acct.get("stripe_customer_id"):
        return ""
    try:
        ps = stripe.billing_portal.Session.create(
            customer=acct["stripe_customer_id"], return_url=_app_url())
        return ps.url
    except Exception as e:
        log.warning("billing portal create failed: %s", e)
        return ""


def _handle_return(username: str) -> None:
    """After Stripe Checkout redirects back, record the customer + subscription."""
    try:
        qp = st.query_params
        if qp.get("checkout") == "success" and qp.get("session_id"):
            stripe = _stripe()
            if stripe:
                sess = stripe.checkout.Session.retrieve(qp["session_id"], expand=["subscription"])
                sub = sess.get("subscription") if isinstance(sess, dict) else sess.subscription
                sub_id = sub.get("id") if isinstance(sub, dict) else (getattr(sub, "id", None) or sub)
                status = (sub.get("status") if isinstance(sub, dict)
                          else getattr(sub, "status", "active")) if sub else "active"
                cust = sess.get("customer") if isinstance(sess, dict) else sess.customer
                if sub_id:
                    _set_acct(username, stripe_customer_id=cust, stripe_subscription_id=sub_id,
                              sub_status=status, sub_checked_at=time.time())
                    st.session_state["_just_subscribed"] = True
            try:
                st.query_params.clear()
            except Exception:
                pass
    except Exception as e:
        log.warning("checkout return handling failed: %s", e)


# ── The gate ──────────────────────────────────────────────────────────────────
def require_subscription() -> None:
    """Call right after login. No-op unless the paywall is enabled AND the user is
    a post-cutover account without an active subscription — then it renders the
    paywall and stops the script."""
    if not _enabled():
        return
    username = auth.current_user()
    if not username:
        return
    if not _configured():
        log.warning("PAYWALL_ENABLED but Stripe/cutover not fully configured — allowing access")
        return

    _handle_return(username)
    acct = _acct(username)
    if is_exempt(username, acct):
        return
    if _active(username):
        return
    if _reconcile(username, acct):   # self-heal a sub we didn't record (e.g. lost redirect)
        return
    _render_paywall(username, acct)
    st.stop()


def _render_paywall(username: str, acct: dict) -> None:
    email = acct.get("email", "")
    was_subscribed = bool(acct.get("stripe_subscription_id"))
    # Cache the checkout URL briefly so reruns don't create a new session each time.
    ck = st.session_state.get("_checkout_url")
    if not ck or (time.time() - st.session_state.get("_checkout_ts", 0)) > 600:
        ck = _create_checkout(username, email)
        st.session_state["_checkout_url"] = ck
        st.session_state["_checkout_ts"] = time.time()

    _c1, _c2, _c3 = st.columns([1, 2, 1])
    with _c2:
        st.markdown(
            '<div style="text-align:center;margin:36px 0 6px">'
            '<span style="font-size:1.9rem;font-weight:800;letter-spacing:-.02em">StockPal</span>'
            '<span class="brand-pro" style="font-size:.7rem">PRO</span></div>',
            unsafe_allow_html=True)
        if was_subscribed:
            st.warning("Your subscription is no longer active. Renew to get back in.")
        st.markdown(
            '<div style="text-align:center;color:var(--muted);margin-bottom:18px">'
            'Unlimited AI scans, recommendations, portfolio tracking &amp; alerts.</div>',
            unsafe_allow_html=True)
        st.markdown(
            '<div style="text-align:center;font-size:2.2rem;font-weight:800">$20'
            '<span style="font-size:1rem;font-weight:600;color:var(--muted)">/month</span></div>'
            f'<div style="text-align:center;color:#00c805;font-weight:700;margin-bottom:8px">'
            f'Start with a {_trial_days()}-day free trial — cancel anytime</div>',
            unsafe_allow_html=True)

        if ck:
            st.link_button(f"Start {_trial_days()}-day free trial →", ck,
                           type="primary", use_container_width=True)
            st.caption("Secure checkout by Stripe. We never see your card details.")
        else:
            st.error("Checkout is temporarily unavailable. Please try again shortly.")

        if was_subscribed:
            portal = _create_portal(username)
            if portal:
                st.link_button("Manage / update payment", portal, use_container_width=True)

        st.divider()
        if st.button("Log out", use_container_width=True):
            auth.logout()
            st.rerun()

    try:
        from legal import render_footer
        render_footer()
    except Exception:
        pass


# Run the one-time grandfather migration when this module is imported under an
# enabled+configured paywall (marker-guarded, so it does real work only once).
try:
    grandfather_all_once()
except Exception:
    pass
