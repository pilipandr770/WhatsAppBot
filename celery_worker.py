from app import create_app
from app.tasks import celery, init_celery

flask_app = create_app()
init_celery(flask_app)
