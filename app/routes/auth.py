from datetime import datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Subscription, TRIAL_DAYS

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        company = request.form.get('company', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        if not all([name, email, password]):
            flash('Bitte alle Pflichtfelder ausfüllen.', 'error')
            return render_template('auth/register.html')

        if password != password2:
            flash('Passwörter stimmen nicht überein.', 'error')
            return render_template('auth/register.html')

        if len(password) < 8:
            flash('Passwort muss mindestens 8 Zeichen haben.', 'error')
            return render_template('auth/register.html')

        if User.query.filter_by(email=email).first():
            flash('Diese E-Mail ist bereits registriert.', 'error')
            return render_template('auth/register.html')

        user = User(
            name=name, email=email, company=company,
            trial_ends_at=datetime.utcnow() + timedelta(days=TRIAL_DAYS)
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        sub = Subscription(user_id=user.id, status='inactive')
        db.session.add(sub)
        db.session.commit()

        login_user(user)
        flash(f'Willkommen, {name}! Du hast {TRIAL_DAYS} Tage kostenlos.', 'success')
        return redirect(url_for('dashboard.index'))

    return render_template('auth/register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard.index'))
        else:
            flash('Ungültige E-Mail oder Passwort.', 'error')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))


@auth_bp.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    user = current_user._get_current_object()

    # Cancel Stripe subscription if active
    if user.subscription and user.subscription.stripe_subscription_id:
        import stripe, os
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
        try:
            stripe.Subscription.cancel(user.subscription.stripe_subscription_id)
        except Exception:
            pass

    # Delete Evolution API instances
    from app.services.evolution import evolution_client
    from app.models import WhatsAppInstance
    for inst in WhatsAppInstance.query.filter_by(user_id=user.id).all():
        try:
            evolution_client.delete_instance(inst.instance_name, inst.api_token)
        except Exception:
            pass

    logout_user()
    db.session.delete(user)
    db.session.commit()
    flash('Dein Konto wurde gelöscht.', 'info')
    return redirect(url_for('main.index'))
