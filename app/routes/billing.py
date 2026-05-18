import os
import stripe
import logging
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import User, Subscription, AffiliateCode, AffiliateUsage

billing_bp = Blueprint('billing', __name__)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plans config — matches AGB (Solo 1, Business 3, Agentur 10)
# ---------------------------------------------------------------------------

PLANS = {
    'solo': {
        'name': 'Solo',
        'price': '€59',
        'period': '/Monat',
        'instances': 1,
        'features': [
            '1 WhatsApp-Nummer',
            'KI-Antworten 24/7 (Claude AI)',
            'Wissensdatenbank (PDF/DOCX)',
            'Sprachnachrichten (STT)',
            'Unbegrenzte Gespräche',
            'E-Mail Support',
        ],
        'price_id': os.environ.get('STRIPE_PRICE_SOLO', ''),
        'highlight': False,
    },
    'business': {
        'name': 'Business',
        'price': '€149',
        'period': '/Monat',
        'instances': 3,
        'features': [
            '3 WhatsApp-Nummern',
            'KI-Antworten 24/7 (Claude AI)',
            'Wissensdatenbank (PDF/DOCX)',
            'Sprachnachrichten (STT)',
            'Google Kalender & Sheets',
            'Prioritäts-Support',
        ],
        'price_id': os.environ.get('STRIPE_PRICE_BUSINESS', ''),
        'highlight': True,
    },
    'agentur': {
        'name': 'Agentur',
        'price': '€349',
        'period': '/Monat',
        'instances': 15,
        'features': [
            '15 WhatsApp-Nummern',
            'Alle Business-Features',
            'Google Kalender & Sheets',
            'White-Label-Option',
            'Dedicated Support',
            'Für Agenturen & Wiederverkauf',
        ],
        'price_id': os.environ.get('STRIPE_PRICE_AGENTUR', ''),
        'highlight': False,
    },
}

# Maps Stripe Price ID → plan key (populated at first use)
def _price_to_plan():
    return {v['price_id']: k for k, v in PLANS.items() if v['price_id']}


def _instances_for_plan(plan_key: str) -> int:
    return PLANS.get(plan_key, {}).get('instances', 1)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@billing_bp.route('/plans')
@login_required
def plans():
    return render_template('billing/plans.html', plans=PLANS, current_plan=_current_plan())


@billing_bp.route('/validate-promo', methods=['POST'])
@login_required
def validate_promo():
    """AJAX: validate a promo code and return discount info."""
    code_str = (request.json or {}).get('code', '').strip().upper()
    if not code_str:
        return jsonify({'valid': False, 'error': 'Kein Code eingegeben.'})

    code = AffiliateCode.query.filter_by(code=code_str).first()
    if not code or not code.is_valid():
        return jsonify({'valid': False, 'error': 'Ungültiger oder abgelaufener Promocode.'})

    discount_percent = code.commission_percent / 2   # user gets half of the commission
    return jsonify({
        'valid': True,
        'discount_percent': discount_percent,
        'message': f'✅ {int(discount_percent)}% Rabatt aktiviert!',
    })


@billing_bp.route('/checkout/<plan_key>')
@login_required
def checkout(plan_key):
    if plan_key not in PLANS:
        return redirect(url_for('billing.plans'))

    plan = PLANS[plan_key]
    if not plan['price_id']:
        flash('Stripe-Preise sind nicht konfiguriert. Bitte STRIPE_PRICE_* in Umgebungsvariablen setzen.', 'error')
        return redirect(url_for('billing.plans'))

    # Optional affiliate promo code passed as ?promo=CODE
    promo_code_str = request.args.get('promo', '').strip().upper()
    aff_code       = None
    stripe_coupon_id = None

    if promo_code_str:
        aff_code = AffiliateCode.query.filter_by(code=promo_code_str).first()
        if not (aff_code and aff_code.is_valid()):
            aff_code = None
            promo_code_str = ''

    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

    # If valid promo — create (or reuse) a Stripe coupon for the user discount
    if aff_code:
        discount_percent = aff_code.commission_percent / 2   # half for user, half for affiliate
        stripe_coupon_id = _get_or_create_stripe_coupon(
            code=aff_code.code,
            percent_off=discount_percent,
        )

    # Create or reuse Stripe customer
    user = current_user
    if not user.stripe_customer_id:
        try:
            customer = stripe.Customer.create(
                email=user.email,
                name=user.name or user.email,
                metadata={'user_id': user.id}
            )
            user.stripe_customer_id = customer.id
            db.session.commit()
        except stripe.error.StripeError as e:
            flash(f'Stripe-Fehler: {e.user_message}', 'error')
            return redirect(url_for('billing.plans'))

    # Build checkout session params
    session_params = dict(
        customer=user.stripe_customer_id,
        payment_method_types=['card'],
        line_items=[{'price': plan['price_id'], 'quantity': 1}],
        mode='subscription',
        billing_address_collection='auto',
        success_url=url_for('billing.success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=url_for('billing.plans', _external=True),
        metadata={
            'user_id': str(user.id),
            'plan': plan_key,
            'promo_code': promo_code_str,
            # Store commission_percent so webhook can calculate affiliate payout correctly
            'commission_percent': str(aff_code.commission_percent) if aff_code else '',
        },
        subscription_data={
            'metadata': {'user_id': str(user.id), 'plan': plan_key}
        }
    )

    if stripe_coupon_id:
        # Apply our affiliate discount — NOTE: can't combine with allow_promotion_codes
        session_params['discounts'] = [{'coupon': stripe_coupon_id}]
    else:
        # No affiliate code — allow Stripe native promo codes
        session_params['allow_promotion_codes'] = True

    try:
        session = stripe.checkout.Session.create(**session_params)
        return redirect(session.url, 303)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe checkout error for user {user.id}: {e}")
        flash(f'Stripe-Fehler: {e.user_message}', 'error')
        return redirect(url_for('billing.plans'))


@billing_bp.route('/success')
@login_required
def success():
    flash('✅ Zahlung erfolgreich! Dein Abonnement ist jetzt aktiv.', 'success')
    return redirect(url_for('dashboard.index'))


@billing_bp.route('/portal')
@login_required
def portal():
    """Stripe Customer Portal — manage subscription, invoices, payment method."""
    if not current_user.stripe_customer_id:
        flash('Kein Stripe-Konto gefunden. Bitte zuerst ein Abo buchen.', 'error')
        return redirect(url_for('billing.plans'))

    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
    try:
        session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=url_for('dashboard.index', _external=True)
        )
        return redirect(session.url, 303)
    except stripe.error.StripeError as e:
        flash(f'Portal-Fehler: {e.user_message}', 'error')
        return redirect(url_for('dashboard.index'))


@billing_bp.route('/cancel', methods=['POST'])
@login_required
def cancel_subscription():
    """Cancel at period end (not immediately)."""
    sub = current_user.subscription
    if not sub or not sub.stripe_subscription_id:
        flash('Kein aktives Abo gefunden.', 'error')
        return redirect(url_for('billing.plans'))

    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
    try:
        # cancel_at_period_end = True → user keeps access until billing period ends
        stripe.Subscription.modify(
            sub.stripe_subscription_id,
            cancel_at_period_end=True
        )
        flash('Abo wird zum Ende der Laufzeit gekündigt. Du kannst bis dahin weiternutzen.', 'info')
    except stripe.error.StripeError as e:
        flash(f'Fehler bei der Kündigung: {e.user_message}', 'error')
    return redirect(url_for('billing.plans'))


# ---------------------------------------------------------------------------
# Stripe Webhook
# ---------------------------------------------------------------------------

@billing_bp.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Receive and verify Stripe webhook events."""
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    secret     = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    if not secret:
        # STRIPE_WEBHOOK_SECRET not configured — log and accept without verification
        # (safe only in dev; in prod you MUST set this variable)
        logger.error(
            "STRIPE_WEBHOOK_SECRET is not set! "
            "Accepting webhook without signature verification — set this variable NOW."
        )
        try:
            event = stripe.Event.construct_from(
                __import__('json').loads(payload), stripe.api_key
            )
        except Exception as e:
            logger.error(f"Stripe webhook: failed to parse payload without secret: {e}")
            return jsonify({'error': 'bad payload'}), 400
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
        except ValueError as e:
            logger.warning(f"Stripe webhook: invalid payload — {e}")
            return jsonify({'error': 'Invalid payload'}), 400
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"Stripe webhook: signature mismatch — {e}")
            return jsonify({'error': 'Invalid signature'}), 400
        except stripe.error.StripeError as e:
            # Catches any other stripe-level error during verification
            logger.error(f"Stripe webhook: StripeError during construct_event — {e}", exc_info=True)
            return jsonify({'error': 'Stripe error'}), 400
        except Exception as e:
            logger.error(f"Stripe webhook: unexpected error during construct_event — {e}", exc_info=True)
            return jsonify({'error': 'Server error'}), 400

    etype = event.get('type', '')
    obj   = event.get('data', {}).get('object', {})

    logger.info(f"Stripe webhook: {etype}")

    try:
        if etype == 'checkout.session.completed':
            _handle_checkout_completed(obj)

        elif etype in ('customer.subscription.created',
                       'customer.subscription.updated'):
            _handle_subscription_upsert(obj)

        elif etype == 'customer.subscription.deleted':
            _handle_subscription_deleted(obj)

        elif etype == 'customer.subscription.paused':
            _handle_subscription_paused(obj)

        elif etype == 'customer.subscription.resumed':
            _handle_subscription_resumed(obj)

        elif etype == 'invoice.payment_succeeded':
            _handle_invoice_paid(obj)

        elif etype == 'invoice.payment_failed':
            _handle_invoice_failed(obj)

        else:
            logger.debug(f"Stripe webhook: unhandled event type {etype!r} — ignored")

    except Exception as e:
        logger.error(f"Webhook handler error ({etype}): {e}", exc_info=True)
        # Return 200 so Stripe doesn't keep retrying on our application bugs
        return jsonify({'status': 'handler_error'}), 200

    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------

def _handle_checkout_completed(session):
    """Checkout session paid — activate subscription."""
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

    user_id  = int(session.get('metadata', {}).get('user_id', 0))
    plan_key = session.get('metadata', {}).get('plan', '')
    sub_id   = session.get('subscription', '')

    if not user_id or not sub_id:
        logger.warning(f"checkout.session.completed: missing user_id or subscription")
        return

    user = db.session.get(User, user_id)
    if not user:
        logger.warning(f"checkout.session.completed: user {user_id} not found")
        return

    # Fetch full subscription from Stripe
    stripe_sub = stripe.Subscription.retrieve(sub_id)

    # Determine plan from price_id if metadata didn't carry it
    if not plan_key:
        price_id = stripe_sub['items']['data'][0]['price']['id']
        plan_key = _price_to_plan().get(price_id, 'solo')

    _upsert_subscription(
        user=user,
        stripe_sub_id=sub_id,
        price_id=stripe_sub['items']['data'][0]['price']['id'],
        status='active',
        plan_key=plan_key,
        period_end=stripe_sub.get('current_period_end'),
    )
    logger.info(f"Subscription activated: user={user_id} plan={plan_key}")

    # Record affiliate commission if promo code was used
    meta = session.get('metadata', {})
    promo_code_str   = meta.get('promo_code', '').strip().upper()
    commission_pct   = float(meta.get('commission_percent', '0') or '0')
    if promo_code_str:
        amount_paid = session.get('amount_total') or 0   # after discount (cents)
        _record_affiliate_usage(
            code_str=promo_code_str,
            user_id=user_id,
            stripe_session_id=session.get('id', ''),
            stripe_sub_id=sub_id,
            amount_paid_cents=amount_paid,
            commission_percent=commission_pct,
            currency=session.get('currency', 'eur'),
        )


def _handle_subscription_upsert(stripe_sub):
    """Subscription created or updated — sync status."""
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

    # Find user by Stripe customer ID
    customer_id = stripe_sub.get('customer')
    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if not user:
        logger.warning(f"subscription upsert: no user for customer {customer_id}")
        return

    price_id = stripe_sub['items']['data'][0]['price']['id']
    plan_key = (
        stripe_sub.get('metadata', {}).get('plan') or
        _price_to_plan().get(price_id, 'solo')
    )
    status = stripe_sub['status']  # active, trialing, past_due, canceled, etc.

    _upsert_subscription(
        user=user,
        stripe_sub_id=stripe_sub['id'],
        price_id=price_id,
        status=status,
        plan_key=plan_key,
        period_end=stripe_sub.get('current_period_end'),
    )
    logger.info(f"Subscription upserted: user={user.id} plan={plan_key} status={status}")


def _handle_subscription_deleted(stripe_sub):
    """Subscription cancelled — revoke access."""
    sub = Subscription.query.filter_by(
        stripe_subscription_id=stripe_sub['id']
    ).first()
    if sub:
        sub.status = 'canceled'
        sub.instances_limit = 0
        db.session.commit()
        logger.info(f"Subscription canceled: sub_id={stripe_sub['id']}")


def _handle_subscription_paused(stripe_sub):
    """Subscription paused — remove access but keep record."""
    sub = Subscription.query.filter_by(
        stripe_subscription_id=stripe_sub['id']
    ).first()
    if sub:
        sub.status = 'paused'
        sub.instances_limit = 0
        db.session.commit()
        logger.info(f"Subscription paused: sub_id={stripe_sub['id']}")


def _handle_subscription_resumed(stripe_sub):
    """Subscription resumed — restore access."""
    sub = Subscription.query.filter_by(
        stripe_subscription_id=stripe_sub['id']
    ).first()
    if sub:
        sub.status = 'active'
        sub.instances_limit = _instances_for_plan(sub.plan)
        if stripe_sub.get('current_period_end'):
            sub.current_period_end = datetime.utcfromtimestamp(int(stripe_sub['current_period_end']))
        db.session.commit()
        logger.info(f"Subscription resumed: sub_id={stripe_sub['id']}")


def _handle_invoice_paid(invoice):
    """Invoice paid — ensure subscription is active (handles renewals)."""
    sub_id = invoice.get('subscription')
    if not sub_id:
        return
    sub = Subscription.query.filter_by(stripe_subscription_id=sub_id).first()
    if sub and sub.status != 'active':
        sub.status = 'active'
        sub.instances_limit = _instances_for_plan(sub.plan)
        db.session.commit()
        logger.info(f"Subscription reactivated after payment: sub_id={sub_id}")


def _handle_invoice_failed(invoice):
    """Invoice payment failed — mark as past_due."""
    sub_id = invoice.get('subscription')
    if not sub_id:
        return
    sub = Subscription.query.filter_by(stripe_subscription_id=sub_id).first()
    if sub:
        sub.status = 'past_due'
        db.session.commit()
        logger.warning(f"Invoice payment failed: sub_id={sub_id}")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _upsert_subscription(user, stripe_sub_id, price_id, status, plan_key, period_end):
    sub = user.subscription
    if not sub:
        sub = Subscription(user_id=user.id)
        db.session.add(sub)

    sub.stripe_subscription_id = stripe_sub_id
    sub.stripe_price_id        = price_id
    sub.status                 = status
    sub.plan                   = plan_key
    sub.instances_limit        = _instances_for_plan(plan_key) if status in ('active', 'trialing') else 0
    if period_end:
        sub.current_period_end = datetime.utcfromtimestamp(int(period_end))

    db.session.commit()


def _get_or_create_stripe_coupon(code: str, percent_off: float) -> str:
    """
    Create a Stripe coupon for the affiliate discount, or reuse it if it already exists.
    Uses 'aff_<CODE>' as the coupon ID so it's idempotent — safe to call on every checkout.
    """
    coupon_id = f"aff_{code}"
    try:
        stripe.Coupon.create(
            id=coupon_id,
            percent_off=percent_off,
            duration='once',       # only first invoice of the subscription
            name=f"Partnerrabatt {code} ({int(percent_off)}%)",
        )
        logger.info(f"Stripe coupon created: {coupon_id} ({int(percent_off)}% off)")
    except stripe.error.InvalidRequestError as e:
        if 'already exists' in str(e).lower() or 'resource_already_exists' in str(getattr(e, 'code', '')).lower():
            logger.debug(f"Stripe coupon {coupon_id} already exists — reusing")
        else:
            logger.error(f"Stripe coupon creation error for {coupon_id}: {e}")
            raise
    return coupon_id


def _current_plan():
    if current_user.subscription and current_user.subscription.is_active:
        return current_user.subscription.plan
    return None


def _record_affiliate_usage(
    code_str: str,
    user_id: int,
    stripe_session_id: str,
    stripe_sub_id: str,
    amount_paid_cents: int,
    commission_percent: float,
    currency: str,
):
    """
    Create AffiliateUsage record after a successful checkout.

    Commission is calculated on the ORIGINAL price (before user discount):
      user_discount = commission_percent / 2
      original_price = amount_paid / (1 - user_discount/100)
      affiliate_commission = original_price * (commission_percent / 2) / 100

    This way user saves and affiliate earns the exact same € amount.
    """
    try:
        aff = AffiliateCode.query.filter_by(code=code_str).first()
        if not aff:
            logger.warning(f"Affiliate code not found at payout time: {code_str!r}")
            return

        # Use commission_percent from metadata (passed at checkout time);
        # fall back to what's stored on the code row if not in metadata
        pct = commission_percent or aff.commission_percent
        user_discount_pct = pct / 2

        # Reconstruct original price before the discount was applied
        if user_discount_pct < 100:
            original_cents = int(amount_paid_cents / (1 - user_discount_pct / 100))
        else:
            original_cents = amount_paid_cents

        # Affiliate gets half of the total commission_percent on the original price
        commission = int(original_cents * user_discount_pct / 100)

        usage = AffiliateUsage(
            code_id=aff.id,
            user_id=user_id,
            stripe_session_id=stripe_session_id,
            stripe_subscription_id=stripe_sub_id,
            gross_amount_cents=original_cents,   # store original (pre-discount) price
            commission_cents=commission,
            currency=currency,
        )
        db.session.add(usage)
        db.session.commit()
        logger.info(
            f"AffiliateUsage recorded: code={code_str} user={user_id} "
            f"original={original_cents}¢ paid={amount_paid_cents}¢ "
            f"commission={commission}¢ ({int(user_discount_pct)}%) {currency.upper()}"
        )
    except Exception as e:
        logger.error(f"_record_affiliate_usage failed: {e}", exc_info=True)
