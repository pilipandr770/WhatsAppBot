import io
import re
import os
import json
import math
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

CHUNK_SIZE = 400    # words per chunk
CHUNK_OVERLAP = 40  # overlap words

EMBEDDING_MODEL = os.environ.get('RAG_EMBEDDING_MODEL', 'text-embedding-3-small')
EMBEDDING_BATCH = 100   # max inputs per OpenAI embeddings request


def embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """
    Embed texts via the OpenAI embeddings API (plain HTTP, no SDK dependency).
    Returns a vector per text, or None if no API key / request failed
    (caller falls back to keyword search).
    """
    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key or not texts:
        return None

    import requests

    vectors: List[List[float]] = []
    try:
        for i in range(0, len(texts), EMBEDDING_BATCH):
            batch = texts[i:i + EMBEDDING_BATCH]
            resp = requests.post(
                'https://api.openai.com/v1/embeddings',
                headers={'Authorization': f'Bearer {api_key}'},
                json={'model': EMBEDDING_MODEL, 'input': batch},
                timeout=30,
            )
            resp.raise_for_status()
            data = sorted(resp.json()['data'], key=lambda d: d['index'])
            vectors.extend(d['embedding'] for d in data)
        return vectors
    except Exception as e:
        logger.warning(f"embed_texts failed ({len(texts)} texts): {e}")
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def extract_text(filepath: str, file_type: str) -> str:
    """Extract plain text from PDF, DOCX, or TXT."""
    with open(filepath, 'rb') as f:
        content = f.read()

    if file_type == 'pdf':
        return _extract_pdf(content)
    elif file_type == 'docx':
        return _extract_docx(content)
    else:
        return content.decode('utf-8', errors='replace')


def _extract_pdf(content: bytes) -> str:
    import pypdf
    text_parts = []
    reader = pypdf.PdfReader(io.BytesIO(content))
    for page in reader.pages:
        text = page.extract_text()
        if text:
            text_parts.append(text)
    return '\n'.join(text_parts)


def _extract_docx(content: bytes) -> str:
    import docx
    doc = docx.Document(io.BytesIO(content))
    return '\n'.join(para.text for para in doc.paragraphs if para.text.strip())


def chunk_text(text: str) -> List[str]:
    """Split text into overlapping word-based chunks."""
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []

    words = text.split()
    if len(words) <= CHUNK_SIZE:
        return [text]

    chunks = []
    i = 0
    while i < len(words):
        end = min(i + CHUNK_SIZE, len(words))
        chunk = ' '.join(words[i:end])
        chunks.append(chunk)
        if end == len(words):
            break
        i += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


def search_relevant_chunks(instance_id: int, query: str, limit: int = 3) -> Optional[str]:
    """
    Find most relevant document chunks for a query.
    Semantic search via embeddings when available, keyword scoring as fallback.
    """
    from app.models import DocumentChunk
    from app import db

    # Cap the number of chunks loaded into memory — prevents OOM on large corpora.
    # 500 chunks × ~400 words × ~6 bytes ≈ 1.2 MB max; enough for good recall.
    MAX_CHUNKS = int(os.environ.get('RAG_MAX_CHUNKS', '500'))
    chunks = (
        DocumentChunk.query
        .filter_by(instance_id=instance_id)
        .limit(MAX_CHUNKS)
        .all()
    )

    if not chunks:
        return None

    # ── Semantic search (embeddings) ──────────────────────────────────────────
    embedded = [c for c in chunks if c.embedding]
    if embedded:
        query_vecs = embed_texts([query])
        if query_vecs:
            qv = query_vecs[0]
            scored_sem = []
            for chunk in embedded:
                try:
                    cv = json.loads(chunk.embedding)
                except Exception:
                    continue
                scored_sem.append((_cosine(qv, cv), chunk.content))
            if scored_sem:
                scored_sem.sort(key=lambda x: x[0], reverse=True)
                return '\n\n'.join(content for _, content in scored_sem[:limit])

    # ── Keyword fallback ──────────────────────────────────────────────────────
    query_words = set(re.findall(r'\w+', query.lower()))
    # Remove very common German/English stop words
    stop_words = {'der', 'die', 'das', 'und', 'ist', 'ich', 'sie', 'the', 'is', 'and', 'a', 'in', 'zu', 'von'}
    query_words -= stop_words

    if not query_words:
        # No meaningful words, return first chunks
        return '\n\n'.join(c.content for c in chunks[:limit])

    scored = []
    for chunk in chunks:
        content_words = set(re.findall(r'\w+', chunk.content.lower()))
        score = len(query_words & content_words)
        if score > 0:
            scored.append((score, chunk.content))

    if not scored:
        # Fallback: return first N chunks
        return '\n\n'.join(c.content for c in chunks[:limit])

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [content for _, content in scored[:limit]]
    return '\n\n'.join(top)
