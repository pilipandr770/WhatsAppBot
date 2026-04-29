import io
import re
import os
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

CHUNK_SIZE = 400    # words per chunk
CHUNK_OVERLAP = 40  # overlap words


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
    import PyPDF2
    text_parts = []
    reader = PyPDF2.PdfReader(io.BytesIO(content))
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
    Uses keyword scoring (PostgreSQL full-text is v2 upgrade path).
    """
    from app.models import DocumentChunk
    from app import db

    # Only use chunks from ready documents
    chunks = (
        DocumentChunk.query
        .filter_by(instance_id=instance_id)
        .all()
    )

    if not chunks:
        return None

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
