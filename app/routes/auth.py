from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Subscription

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

        user = User(name=name, email=email, company=company)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        # Create empty subscription record
        sub = Subscription(user_id=user.id, status='inactive')
        db.session.add(sub)
        db.session.commit()

        login_user(user)
        flash(f'Willkommen, {name}! Wähle jetzt deinen Plan.', 'success')
        return redirect(url_for('billing.plans'))

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
