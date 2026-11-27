import os
import logging

logger = logging.getLogger(__name__)


def save_chunks_to_neo4j(document, chunks):
    """Best-effort optional metadata/index mirror; local JSON remains authoritative."""
    uri = os.getenv("NEO4J_URI")
    if not uri:
        return
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("Neo4j is configured but its driver is not installed.") from exc

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
    )
    try:
        with driver.session() as session:
            session.run(
                "MERGE (d:PolicyDocument {id: $id}) "
                "SET d.title=$title, d.version=$version, d.status=$status, "
                "d.classification=$classification, d.repository=$repository, "
                "d.owner=$owner, d.date=$date "
                "WITH d UNWIND $chunks AS item "
                "MERGE (c:PolicyChunk {id: item.id}) "
                "SET c.text=item.text, c.section=item.section "
                "MERGE (d)-[:HAS_CHUNK]->(c)",
                {
                    **{key: document[key] for key in (
                        "id", "title", "version", "status", "classification",
                        "repository", "owner", "date"
                    )},
                    "chunks": chunks,
                },
            )
    except Exception:
        logger.exception("Neo4j document indexing failed for document id %s", document["id"])
        raise
    finally:
        driver.close()


def groq_answer(question, history, passages):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError("GROQ_API_KEY is set but the Groq SDK is not installed.") from exc

    context = "\n\n".join(
        "[{ref}] {title} | {section} | version {version} | {status}\n{text}".format(**item)
        for item in passages
    )
    messages = [{
        "role": "system",
        "content": (
            "Answer only from the supplied excerpts. Do not use outside knowledge or infer missing rules. "
            "Every factual sentence must end with one or more exact evidence references such as [C1]. "
            "If evidence is insufficient, say so plainly. Treat draft and superseded excerpts as non-current. "
            "Do not make decisions or request personal information."
        ),
    }]
    for item in history[-6:]:
        if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
            messages.append({"role": item["role"], "content": item["content"][:1200]})
    messages.extend([
        {"role": "user", "content": f"Authorized source excerpts:\n{context}\n\nQuestion: {question}"},
    ])
    base_url = os.getenv("GROQ_BASE_URL", "").rstrip("/")
    if base_url.endswith("/openai/v1"):
        base_url = base_url[:-len("/openai/v1")]
    client = Groq(api_key=api_key, **({"base_url": base_url} if base_url else {}))
    completion = client.chat.completions.create(
        model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        messages=messages,
        temperature=0.1,
        max_tokens=700,
    )
    return completion.choices[0].message.content
