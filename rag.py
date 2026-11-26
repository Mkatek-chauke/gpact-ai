import re
import logging
from collections import Counter

from integrations import groq_answer

logger = logging.getLogger(__name__)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*", re.I)
STOP_WORDS = {
    "a", "about", "after", "also", "an", "and", "are", "as", "at", "be",
    "before", "can", "could", "did", "do", "does", "for", "from", "had",
    "has", "have", "how", "in", "into", "is", "it", "its", "may", "might",
    "happen", "must", "not", "of", "on", "or", "our", "required", "should", "that", "the", "their",
    "them", "there", "these", "this", "those", "to", "under", "was", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "using", "you", "your",
}
PERSONAL_DATA_RE = re.compile(
    r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|"
    r"\b\d{13}\b|"
    r"(?<!\w)(?:\+?27|0)\s?\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\w)"
)


def tokenize(text):
    return [word.lower() for word in WORD_RE.findall(text) if word.lower() not in STOP_WORDS]


def make_chunks(document):
    chunks = []
    section = "Document"
    for paragraph in re.split(r"\n\s*\n", document["text"]):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        lines = paragraph.splitlines()
        if len(lines) == 1 and (len(lines[0]) < 100 and (
            re.match(r"^(\d+(\.\d+)*|[A-Z])[\.)]?\s+", lines[0])
            or lines[0].istitle()
        )):
            section = lines[0]
            continue
        text = " ".join(line.strip() for line in lines)
        chunks.append({
            "id": f'{document["id"]}-c{len(chunks) + 1}',
            "text": text,
            "section": section if section != "Document" else document.get("section", "Document"),
        })
    if not chunks and document["text"].strip():
        chunks.append({
            "id": f'{document["id"]}-c1',
            "text": document["text"].strip(),
            "section": document.get("section", "Document"),
        })
    return chunks


def rank_passages(question, documents, clearance):
    terms = set(tokenize(question))
    if not terms:
        return []
    passages = []
    for document in documents:
        if document.get("clearance_rank", 0) > clearance:
            continue
        for chunk in make_chunks(document):
            words = tokenize(f'{chunk["text"]} {chunk["section"]} {document["title"]}')
            counts = Counter(words)
            overlap = terms.intersection(counts)
            if len(overlap) < min(2, len(terms)):
                continue
            score = len(overlap) / len(terms)
            if score >= 0.5:
                score += min(sum(counts[word] > 1 for word in overlap), 2) * 0.01
                passages.append({
                    **chunk,
                    "document_id": document["id"],
                    "title": document["title"],
                    "version": document["version"],
                    "status": document["status"],
                    "classification": document["classification"],
                    "owner": document["owner"],
                    "date": document["date"],
                    "repository": document["repository"],
                    "document_type": document.get("document_type", "Document"),
                    "score": score,
                })
    passages.sort(key=lambda item: (
        item["score"],
        item["status"] == "Current",
        item["date"],
    ), reverse=True)
    if passages:
        minimum_score = passages[0]["score"] * 0.72
        passages = [item for item in passages if item["score"] >= minimum_score]
    selected = passages[:5]
    for index, item in enumerate(selected, start=1):
        item["ref"] = f"C{index}"
    return selected


def _fallback_answer(passages):
    if not passages:
        return (
            "I couldn't find supporting information for that question in the documents "
            "you are authorised to access. Please check with your departmental policy "
            "owner or records officer."
        )
    lines = []
    for item in passages[:3]:
        sentences = re.split(r"(?<=[.!?])\s+", item["text"])
        useful = next((sentence.strip() for sentence in sentences if sentence.strip()), "")
        if useful:
            lines.append(f'{useful} [{item["ref"]}]')
    return " ".join(lines) or (
        "I couldn't find supporting information in the documents you are authorised to access. "
        "Please check with your departmental policy owner or records officer."
    )


def _all_sentences_cited(answer, passages):
    allowed = {f'[{item["ref"]}]' for item in passages}
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", answer) if part.strip()]
    return bool(sentences) and all(any(ref in sentence for ref in allowed) for sentence in sentences)


def answer_question(question, history, documents, clearance):
    follow_up_context = " ".join(
        item.get("content", "")[:300]
        for item in history[-4:]
        if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str)
    )
    passages = rank_passages(f"{follow_up_context} {question}", documents, clearance)
    answer = None
    provider = "Local extractive retrieval"
    safe_for_external_provider = (
        passages
        and all(item["classification"] == "Public" for item in passages)
        and not PERSONAL_DATA_RE.search(question)
        and not any(PERSONAL_DATA_RE.search(item["text"]) for item in passages)
    )
    if safe_for_external_provider:
        try:
            candidate = groq_answer(question, [], passages)
            if candidate and _all_sentences_cited(candidate, passages):
                answer = candidate
                provider = "Groq"
            elif candidate:
                logger.warning("Groq answer omitted required source citations; using local answer.")
        except Exception:
            logger.warning("Groq provider unavailable; using local extractive retrieval.")
            answer = None
    if answer is None:
        answer = _fallback_answer(passages)
    return answer, passages, provider
