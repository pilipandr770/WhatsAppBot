from celery import Celery
import os

# Init Celery
celery = Celery(
    'whatsapp_saas',
    broker=os.environ.get('REDIS_URL', 'redis://redis:6379/0'),
    backend=os.environ.get('REDIS_URL', 'redis://redis:6379/0')
)

celery.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='Europe/Berlin',
    task_routes={
        'app.tasks.process_document': {'queue': 'documents'},
    },
    # On Render.com (no shared disk between web/worker) set CELERY_TASK_ALWAYS_EAGER=true
    # so document processing runs synchronously inside the web process
    task_always_eager=os.environ.get('CELERY_TASK_ALWAYS_EAGER', 'false').lower() == 'true',
)


def init_celery(app):
    """Bind Celery to Flask app context."""
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    celery.Task = ContextTask
    return celery


@celery.task(bind=True, max_retries=3)
def process_document(self, document_id: int):
    """Extract text from uploaded file and create chunks."""
    import logging
    from app import db
    from app.models import Document, DocumentChunk
    from app.services.rag import extract_text, chunk_text

    logger = logging.getLogger(__name__)

    try:
        doc = Document.query.get(document_id)
        if not doc:
            return

        doc.status = 'processing'
        db.session.commit()

        # Extract text
        text = extract_text(doc.filename, doc.file_type)

        if not text.strip():
            doc.status = 'error'
            doc.error_message = 'No text could be extracted from this file.'
            db.session.commit()
            return

        # Delete existing chunks
        DocumentChunk.query.filter_by(document_id=document_id).delete()

        # Create chunks
        chunks = chunk_text(text)
        for idx, chunk_content in enumerate(chunks):
            chunk = DocumentChunk(
                document_id=doc.id,
                instance_id=doc.instance_id,
                content=chunk_content,
                chunk_index=idx
            )
            db.session.add(chunk)

        doc.chunk_count = len(chunks)
        doc.status = 'ready'
        db.session.commit()

        logger.info(f"Document {document_id} processed: {len(chunks)} chunks")

    except Exception as exc:
        logger.error(f"Document processing failed for {document_id}: {exc}")
        try:
            doc = Document.query.get(document_id)
            if doc:
                doc.status = 'error'
                doc.error_message = str(exc)[:500]
                db.session.commit()
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=30)
