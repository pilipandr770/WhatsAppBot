import os
import stripe
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Subscription

billing_bp = Blueprint('billing', __name__)
logger = logging.getLogger(__name__)

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

# Price IDs from Stripe Dashboard - set in .env
PLANS = {
    'starter': {
        'name': 'Starter',
        'price': '€29',
        'period': '/Monat',
        'instances': 1,
        'features': ['1 WhatsApp-Nummer', 'KI-Antworten', 'Basis-Konfiguration', 'E-Mail Support'],
        'price_id': os.environ.get('STRIPE_PRICE_STARTER', ''),
        'highlight': False,
    },
    'pro': {
        'name': 'Pro',
        'price': '€79',
        'period': '/Monat',
        'instances': 5,
        'features': ['5 WhatsApp-Nummern', 'KI-Antworten', 'Dokument-Upload (RAG)', 'Gesprächshistorie', 'Prioritäts-Support'],
        'price_id': os.environ.get('STRIPE_PRICE_PRO', ''),
        'highlight': True,
    },
    'business': {
        'name': 'Business',
        'price': '€199',
        'period': '/Monat',
        'instances': 20,
        'features': ['20 WhatsApp-Nummern', 'Alle Pro-Features', 'API-Zugang', 'Dedicated Support', 'Custom Branding'],
        'price_id': os.environ.get('STRIPE_PRICE_BUSINESS', ''),
        'highlight': False,
    }
}

INSTANCES_BY_PLAN = {'starter': 1, 'pro': 5, 'business': 20}


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
        flash('Stripe nicht konfiguriert. Bitte .env prüfen.', 'error')
        return redirect(url_for('billing.plans'))

    # Create or get Stripe customer
    user = current_user
    if not user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=user.email,
            name=user.name,
            metadata={'user_id': user.id}
        )
        user.stripe_customer_id = customer.id
        db.session.commit()

    try:
        session = stripe.checkout.Session.create(
            customer=user.stripe_customer_id,
            payment_method_types=['card'],
            line_items=[{'price': plan['price_id'], 'quantity': 1}],
            mode='subscription',
            success_url=url_for('billing.success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('billing.plans', _external=True),
            metadata={'user_id': user.id, 'plan': plan_key}
        )
        return redirect(session.url)
    except stripe.error.StripeError as e:
        flash(f'Stripe-Fehler: {e.user_message}', 'error')
        return redirect(url_for('billing.plans'))


@billing_bp.route('/success')
@login_required
def success():
    flash('Zahlung erfolgreich! Dein Abo ist jetzt aktiv.', 'success')
    return redirect(url_for('dashboard.index'))


@billing_bp.route('/portal')
@login_required
def portal():
    """Stripe customer portal for managing subscription."""
    if not current_user.stripe_customer_id:
        flash('Kein Stripe-Konto gefunden.', 'error')
        return redirect(url_for('billing.plans'))

    try:
        session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=url_for('dashboard.index', _external=True)
        )
        return redirect(session.url)
    except stripe.error.StripeError as e:
        flash(f'Fehler: {e.user_message}', 'error')
        return redirect(url_for('dashboard.index'))


@billing_bp.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events."""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'checkout.session.completed':
        _handle_checkout_completed(event['data']['object'])
    elif event['type'] in ('customer.subscription.updated', 'customer.subscription.deleted'):
        _handle_subscription_change(event['data']['object'])

    return jsonify({'status': 'ok'})


def _handle_checkout_completed(session):
    user_id = int(session.get('metadata', {}).get('user_id', 0))
    plan_key = session.get('metadata', {}).get('plan', 'starter')

    if not user_id:
        return

    from app.models import User
    user = User.query.get(user_id)
    if not user:
        return

    sub_id = session.get('subscription')
    stripe_sub = stripe.Subscription.retrieve(sub_id) if sub_id else None

    sub = user.subscription or Subscription(user_id=user.id)
    sub.stripe_subscription_id = sub_id
    sub.stripe_price_id = stripe_sub['items']['data'][0]['price']['id'] if stripe_sub else None
    sub.status = 'active'
    sub.plan = plan_key
    sub.instances_limit = INSTANCES_BY_PLAN.get(plan_key, 1)

    if stripe_sub:
        from datetime import datetime
        sub.current_period_end = datetime.fromtimestamp(stripe_sub['current_period_end'])

    if not sub.id:
        db.session.add(sub)
    db.session.commit()
    logger.info(f"Subscription activated for user {user_id}, plan {plan_key}")


def _handle_subscription_change(stripe_sub):
    sub = Subscription.query.filter_by(
        stripe_subscription_id=stripe_sub['id']
    ).first()

    if not sub:
        return

    sub.status = stripe_sub['status']
    if stripe_sub['status'] == 'canceled':
        sub.instances_limit = 0
    db.session.commit()


def _current_plan():
    if current_user.subscription and current_user.subscription.is_active:
        return current_user.subscription.plan
    return None
