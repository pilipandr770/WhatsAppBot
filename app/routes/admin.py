from functools import wraps
from datetime import datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import User, Subscription, WhatsAppInstance, Conversation, Message, SiteConfig, TRIAL_DAYS

admin_bp = Blueprint('admin', __name__)

INSTANCES_BY_PLAN = {'solo': 1, 'business': 3, 'agentur': 15}
PLAN_LABELS = {'solo': 'Solo-Assistent', 'business': 'Business-Assistent', 'agentur': 'Agentur'}


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Zugriff verweigert.', 'error')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/')
@login_required
@admin_required
def index():
    total_users = User.query.count()
    active_subs = Subscription.query.filter_by(status='active').count()
    trial_users = User.query.filter(
        User.trial_ends_at > datetime.utcnow(),
        ~User.subscription.has(status='active')
    ).count()
    total_instances = WhatsAppInstance.query.count()
    total_messages = db.session.query(db.func.count(Message.id)).scalar() or 0
    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()

    return render_template('admin/dashboard.html',
        total_users=total_users,
        active_subs=active_subs,
        trial_users=trial_users,
        total_instances=total_instances,
        total_messages=total_messages,
        recent_users=recent_users,
    )


@admin_bp.route('/users')
@login_required
@admin_required
def users():
    q = request.args.get('q', '').strip()
    query = User.query
    if q:
        query = query.filter(
            db.or_(User.email.ilike(f'%{q}%'), User.name.ilike(f'%{q}%'), User.company.ilike(f'%{q}%'))
        )
    users_list = query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users_list, q=q)


@admin_bp.route('/users/<int:user_id>')
@login_required
@admin_required
def user_detail(user_id):
    user = db.get_or_404(User, user_id)
    instances = WhatsAppInstance.query.filter_by(user_id=user_id).all()
    return render_template('admin/user_detail.html',
        user=user, instances=instances,
        plans=PLAN_LABELS, instances_by_plan=INSTANCES_BY_PLAN
    )


@admin_bp.route('/users/<int:user_id>/set-plan', methods=['POST'])
@login_required
@admin_required
def set_plan(user_id):
    user = db.get_or_404(User, user_id)
    plan = request.form.get('plan', 'starter')
    action = request.form.get('action', 'activate')

    if not user.subscription:
        sub = Subscription(user_id=user.id)
        db.session.add(sub)
        user.subscription = sub

    if action == 'deactivate':
        user.subscription.status = 'inactive'
        user.subscription.instances_limit = 0
        flash(f'Zugang für {user.email} deaktiviert.', 'info')
    elif action == 'extend_trial':
        try:
            days = max(1, min(int(request.form.get('trial_days', TRIAL_DAYS)), 365))
        except (ValueError, TypeError):
            days = TRIAL_DAYS
        user.trial_ends_at = datetime.utcnow() + timedelta(days=days)
        flash(f'Trial für {user.email} um {days} Tage verlängert.', 'success')
    else:
        user.subscription.plan = plan
        user.subscription.status = 'active'
        user.subscription.instances_limit = INSTANCES_BY_PLAN.get(plan, 1)
        flash(f'Plan für {user.email} auf {PLAN_LABELS.get(plan)} gesetzt.', 'success')

    db.session.commit()
    return redirect(url_for('admin.user_detail', user_id=user_id))


@admin_bp.route('/demo-bot', methods=['GET', 'POST'])
@login_required
@admin_required
def demo_bot():
    if request.method == 'POST':
        phone_raw = ''.join(c for c in request.form.get('demo_wa_phone', '') if c.isdigit())
        message   = request.form.get('demo_wa_message', '').strip()
        enabled   = '1' if request.form.get('demo_wa_enabled') else '0'

        SiteConfig.set('demo_wa_phone',   phone_raw)
        SiteConfig.set('demo_wa_message', message)
        SiteConfig.set('demo_wa_enabled', enabled)
        db.session.commit()
        flash('Demo-Bot Einstellungen gespeichert.', 'success')
        return redirect(url_for('admin.demo_bot'))

    cfg = {
        'phone':   SiteConfig.get('demo_wa_phone',   ''),
        'message': SiteConfig.get('demo_wa_message',
                                  'Hallo! Ich möchte euren WhatsApp Bot ausprobieren 👋'),
        'enabled': SiteConfig.get('demo_wa_enabled', '0') == '1',
    }
    return render_template('admin/demo_bot.html', cfg=cfg)


@admin_bp.route('/users/<int:user_id>/toggle-admin', methods=['POST'])
@login_required
@admin_required
def toggle_admin(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash('Du kannst deinen eigenen Admin-Status nicht ändern.', 'error')
        return redirect(url_for('admin.user_detail', user_id=user_id))
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f'Admin-Status für {user.email} geändert.', 'success')
    return redirect(url_for('admin.user_detail', user_id=user_id))
