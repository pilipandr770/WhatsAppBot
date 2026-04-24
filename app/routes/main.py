from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    return render_template('landing.html')


@main_bp.route('/impressum')
def impressum():
    return render_template('legal/impressum.html')


@main_bp.route('/agb')
def agb():
    return render_template('legal/agb.html')


@main_bp.route('/datenschutz')
def datenschutz():
    return render_template('legal/datenschutz.html')
