import re
import logging
from collections import Counter
from difflib import SequenceMatcher

from integrations import groq_answer, groq_web_search

logger = logging.getLogger(__name__)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*", re.I)
NO_EVIDENCE_ANSWER = (
    "I couldn't find supporting information for that question in the official government sources currently available."
)
STOP_WORDS = {
    "a", "about", "after", "also", "an", "and", "are", "as", "at", "be",
    "before", "can", "could", "did", "do", "does", "for", "from", "get", "had",
    "has", "have", "how", "in", "into", "is", "it", "its", "may", "might",
    "happen", "happed", "i", "must", "not", "of", "on", "or", "our", "required", "should", "that", "the", "their",
    "them", "there", "these", "this", "those", "to", "under", "was", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "using", "you", "your", "s",
}
QUERY_SYNONYMS = {
    "age": {"older", "elderly", "senior"},
    "arrest": {"arrested", "detained", "detention"},
    "apply": {"application", "applying"},
    "id": {"identity", "identification", "document", "smart"},
    "nsfas": {"national", "student", "financial", "aid", "bursary", "funding"},
    "old": {"older", "elderly", "senior"},
}
PERSONAL_DATA_RE = re.compile(
    r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|"
    r"\b\d{13}\b|"
    r"(?<!\w)(?:\+?27|0)\s?\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\w)"
)
PERSONAL_DATA_REQUEST_RE = re.compile(
    r"\b(?:show|list|give|reveal|export|find|provide)\b.{0,80}\b"
    r"(?:employee\s+ids?|id\s+numbers?|personal\s+addresses|home\s+addresses|"
    r"phone\s+numbers|personal\s+details|staff\s+records)\b",
    re.I,
)
DECISION_REQUEST_RE = re.compile(
    r"\b(?:should\s+i|should\s+we|do\s+i|do\s+we)\s+"
    r"(?:approve|reject|award|select|appoint|dismiss)\b",
    re.I,
)
HISTORICAL_VERSION_RE = re.compile(
    r"\b(?:superseded|previous|older\s+(?:version|policy|document|edition)|historical|version\s*\d|v\d)\b",
    re.I,
)
FOLLOW_UP_CONTEXT_RE = re.compile(
    r"\b(?:it|they|them|their|this|that|those|these|there|then|same|former|latter|above)\b|"
    r"\bwhat about (?:it|that|those|these|the same|then)\b|"
    r"\b(?:tell me|say|explain) more\b",
    re.I,
)
DOCUMENT_SUMMARY_RE = re.compile(
    r"\b(?:summar(?:y|ies|ize|ise|izing|ising)|overview|key\s+points|main\s+points)\b",
    re.I,
)


def tokenize(text):
    words = []
    for raw_word in WORD_RE.findall(text):
        word = raw_word.lower()
        if word == "happed":
            word = "happen"
        elif word.endswith("ies") and len(word) > 4:
            word = f"{word[:-3]}y"
        elif word.endswith("ied") and len(word) > 4:
            word = f"{word[:-3]}y"
        elif word.endswith("ed") and len(word) > 5:
            word = word[:-2]
        elif word.endswith("s") and len(word) > 4:
            word = word[:-1]
        if word not in STOP_WORDS:
            words.append(word)
    return words


def _query_term_matches(term, document_words):
    candidates = QUERY_SYNONYMS.get(term, set()) | {term}
    if candidates.intersection(document_words):
        return True
    if len(term) < 4:
        return False
    return any(
        len(word) >= 4
        and word[0] == term[0]
        and abs(len(word) - len(term)) <= 2
        and SequenceMatcher(None, term, word).ratio() >= 0.78
        for word in document_words
    )


def make_chunks(document):
    chunks = []
    section = "Document"
    paragraph = []

    def flush_paragraph():
        if not paragraph:
            return
        text = " ".join(paragraph)
        chunks.append({
            "id": f'{document["id"]}-c{len(chunks) + 1}',
            "text": text,
            "section": section if section != "Document" else document.get("section", "Document"),
        })
        paragraph.clear()

    for raw_line in document["text"].splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if len(line) < 100 and (
            re.match(r"^(\d+(\.\d+)*|[A-Z])[\.)]?\s+", line)
            or line.istitle()
        ):
            flush_paragraph()
            section = line
            continue
        paragraph.append(line)
    flush_paragraph()
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
    current_families = {
        document.get("version_family", document.get("title", "").strip().casefold())
        for document in documents
        if document.get("status") == "Current"
    }
    include_history = bool(HISTORICAL_VERSION_RE.search(question))
    passages = []
    for document in documents:
        rank = document.get("clearance_rank")
        if not isinstance(rank, int) or rank > clearance:
            continue
        family = document.get("version_family", document.get("title", "").strip().casefold())
        if document.get("status") == "Superseded" and family in current_families and not include_history:
            continue
        for chunk in make_chunks(document):
            words = tokenize(f'{chunk["text"]} {chunk["section"]} {document["title"]}')
            counts = Counter(words)
            overlap = {term for term in terms if _query_term_matches(term, counts)}
            if len(overlap) < min(2, len(terms)):
                continue
            score = len(overlap) / len(terms)
            if score >= 0.5:
                score += min(sum(counts[word] > 1 for word in overlap), 2) * 0.01
                passages.append({
                    **chunk,
                    "document_id": document["id"],
                    "title": document["title"],
                    "version_family": family,
                    "version": document["version"],
                    "status": document["status"],
                    "classification": document["classification"],
                    "owner": document["owner"],
                    "date": document["date"],
                    "repository": document["repository"],
                    "document_type": document.get("document_type", "Document"),
                    "source_url": document.get("source_url"),
                    "page_url": document.get("page_url"),
                    "source_type": document.get("source_type", "project_document"),
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
        return NO_EVIDENCE_ANSWER
    lines = []
    for item in passages[:3]:
        sentences = re.split(r"(?<=[.!?])\s+", item["text"])
        useful = next((sentence.strip() for sentence in sentences if sentence.strip()), "")
        if useful:
            lines.append(f'{useful} [{item["ref"]}]')
    return " ".join(lines) or NO_EVIDENCE_ANSWER


def _document_passages(documents, clearance):
    passages = []
    for document in documents:
        rank = document.get("clearance_rank")
        if not isinstance(rank, int) or rank > clearance:
            continue
        family = document.get("version_family", document.get("title", "").strip().casefold())
        for chunk in make_chunks(document):
            passages.append({
                **chunk,
                "document_id": document["id"],
                "title": document["title"],
                "version_family": family,
                "version": document["version"],
                "status": document["status"],
                "classification": document["classification"],
                "owner": document["owner"],
                "date": document["date"],
                "repository": document["repository"],
                "document_type": document.get("document_type", "Document"),
                "source_url": document.get("source_url"),
                "page_url": document.get("page_url"),
                "source_type": document.get("source_type", "project_document"),
                "score": 1.0,
            })
    selected = passages[:5]
    for index, item in enumerate(selected, start=1):
        item["ref"] = f"C{index}"
    return selected


def _combine_passages(local_passages, web_passages):
    if local_passages and web_passages:
        selected = local_passages[:3] + web_passages[:2]
    else:
        selected = (local_passages or web_passages)[:5]
    selected = [dict(item) for item in selected]
    for index, item in enumerate(selected, start=1):
        item["ref"] = f"C{index}"
    return selected


def _all_sentences_cited(answer, passages):
    allowed = {f'[{item["ref"]}]' for item in passages}
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", answer) if part.strip()]
    return bool(sentences) and all(any(ref in sentence for ref in allowed) for sentence in sentences)


def answer_question(
    question,
    history,
    documents,
    clearance,
    document_context=False,
    web_search_enabled=False,
):
    if PERSONAL_DATA_RE.search(question) or PERSONAL_DATA_REQUEST_RE.search(question):
        return (
            "I can't help retrieve or disclose personal information. Use an authorised, "
            "privacy-approved process for any permitted records request.",
            [],
            "Local privacy safeguard",
        )
    follow_up_context = " ".join(
        item.get("content", "")[:300]
        for item in history[-4:]
        if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str)
    )
    retrieval_question = question
    if follow_up_context and FOLLOW_UP_CONTEXT_RE.search(question):
        retrieval_question = f"{follow_up_context} {question}"
    passages = rank_passages(retrieval_question, documents, clearance)
    is_document_summary = document_context and DOCUMENT_SUMMARY_RE.search(question)
    if is_document_summary:
        passages = _document_passages(documents, clearance)
    if DECISION_REQUEST_RE.search(question):
        if not passages:
            return NO_EVIDENCE_ANSWER, [], "Local extractive retrieval"
        prefix = (
            "I can't approve, reject, or make a procurement decision. "
        )
        if passages:
            answer = (
                prefix
                + "I can provide the policy information for an authorised official to assess: "
                + _fallback_answer(passages)
            )
        return answer, passages, "Local decision safeguard"

    web_search_attempted = False
    web_search_failed = False
    web_search_skipped = False
    web_passages = []
    if web_search_enabled and not document_context:
        if any(item["classification"] != "Public" for item in passages):
            web_search_skipped = True
        else:
            web_search_attempted = True
            try:
                web_documents = groq_web_search(retrieval_question)
                web_passages = rank_passages(retrieval_question, web_documents, 0)
            except Exception as exc:
                logger.warning(
                    "Groq official web search unavailable (%s).",
                    type(exc).__name__,
                )
                web_search_failed = True
    passages = _combine_passages(passages, web_passages)
    if not passages:
        if web_search_failed:
            return NO_EVIDENCE_ANSWER, [], "Groq web search unavailable"
        if web_search_attempted:
            return NO_EVIDENCE_ANSWER, [], "Groq web search (no relevant official results)"
        return NO_EVIDENCE_ANSWER, [], "Local extractive retrieval"

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
            provider_question = (
                "Summarize the key points of this document using only the supplied excerpts."
                if is_document_summary else question
            )
            candidate = groq_answer(provider_question, [], passages)
            if candidate and candidate.strip() == NO_EVIDENCE_ANSWER:
                return NO_EVIDENCE_ANSWER, [], "Groq"
            if candidate and _all_sentences_cited(candidate, passages):
                answer = candidate
                provider = "Groq + official web search" if web_passages else "Groq"
            elif candidate:
                logger.warning("Groq answer omitted required source citations; using local answer.")
                provider = "Local extractive retrieval (AI answer rejected)"
        except Exception as exc:
            logger.warning(
                "Groq provider unavailable (%s); using local extractive retrieval.",
                type(exc).__name__,
            )
            provider = "Local extractive retrieval (AI provider unavailable)"
            answer = None
    if answer is None:
        answer = _fallback_answer(passages)
    if web_search_failed:
        provider += " (official web search unavailable)"
    elif web_search_skipped:
        provider += " (official web search skipped for non-public context)"
    elif web_search_attempted and not web_passages:
        provider += " (no relevant official web results)"
    elif web_passages and not provider.startswith("Groq + official web search"):
        provider += " with official web search sources"
    return answer, passages, provider
