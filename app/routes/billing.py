import os
import stripe
import logging
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import User, Subscription

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
        'instances': 10,
        'features': [
            '10 WhatsApp-Nummern',
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


@billing_bp.route('/checkout/<plan_key>')
@login_required
def checkout(plan_key):
    if plan_key not in PLANS:
        return redirect(url_for('billing.plans'))

    plan = PLANS[plan_key]
    if not plan['price_id']:
        flash('Stripe-Preise sind nicht konfiguriert. Bitte STRIPE_PRICE_* in Umgebungsvariablen setzen.', 'error')
        return redirect(url_for('billing.plans'))

    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

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

    try:
        session = stripe.checkout.Session.create(
            customer=user.stripe_customer_id,
            payment_method_types=['card'],
            line_items=[{'price': plan['price_id'], 'quantity': 1}],
            mode='subscription',
            allow_promotion_codes=True,
            billing_address_collection='auto',
            success_url=url_for('billing.success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('billing.plans', _external=True),
            metadata={'user_id': str(user.id), 'plan': plan_key},
            subscription_data={
                'metadata': {'user_id': str(user.id), 'plan': plan_key}
            }
        )
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
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    secret     = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except ValueError:
        logger.warning("Stripe webhook: invalid payload")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook: invalid signature")
        return jsonify({'error': 'Invalid signature'}), 400

    etype = event['type']
    obj   = event['data']['object']

    logger.info(f"Stripe webhook: {etype}")

    try:
        if etype == 'checkout.session.completed':
            _handle_checkout_completed(obj)

        elif etype in ('customer.subscription.created',
                       'customer.subscription.updated'):
            _handle_subscription_upsert(obj)

        elif etype == 'customer.subscription.deleted':
            _handle_subscription_deleted(obj)

        elif etype == 'invoice.payment_succeeded':
            _handle_invoice_paid(obj)

        elif etype == 'invoice.payment_failed':
            _handle_invoice_failed(obj)

    except Exception as e:
        logger.error(f"Webhook handler error ({etype}): {e}", exc_info=True)
        # Return 200 so Stripe doesn't keep retrying on our bugs
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

    user = User.query.get(user_id)
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
        sub.current_period_end = datetime.fromtimestamp(int(period_end))

    db.session.commit()


def _current_plan():
    if current_user.subscription and current_user.subscription.is_active:
        return current_user.subscription.plan
    return None
