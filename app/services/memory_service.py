"""Case memory: embed the transcript into knowledge_chunks (hybrid RAG) and build
the per-case knowledge graph (kg_nodes/kg_edges). Runs under the firm system context.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import IntakeContext
from app.agent.embeddings import embed_text, embed_texts
from app.agent.extraction import Extraction
from app.database import session_scope
from app.security.context import system_context
from app.services import case_vocab


def chunk_transcript(transcript: str, max_chars: int = 800) -> list[str]:
    """Group transcript lines into ~max_chars chunks (turn-aware)."""
    chunks: list[str] = []
    buf = ""
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(buf) + len(line) + 1 > max_chars and buf:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def vec(values: list[float]) -> str:
    """Serialize a float list to a pgvector/halfvec literal '[...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


_vec = vec  # backward-compatible alias (one release)


async def _store_chunks(
    db: AsyncSession, ctx: IntakeContext, source_id: uuid.UUID,
    chunks: list[str], embeddings: list[list[float]],
) -> int:
    for idx, (content, emb) in enumerate(zip(chunks, embeddings)):
        await db.execute(
            text(
                "INSERT INTO knowledge_chunks (organization_id, lead_id, source_type, source_id, "
                "chunk_index, content, embedding, token_count) "
                "VALUES (:o, :l, 'transcript', :src, :idx, :content, CAST(:emb AS halfvec), :tok)"
            ),
            {"o": ctx.organization_id, "l": ctx.lead_id, "src": source_id, "idx": idx,
             "content": content, "emb": _vec(emb), "tok": len(content.split())},
        )
    return len(chunks)


async def build_case_graph(db: AsyncSession, ctx: IntakeContext, ex: Extraction) -> dict:
    org, lead = ctx.organization_id, ctx.lead_id
    counts = {"nodes": 0, "edges": 0}

    async def node(node_type: str, label: str | None, props: dict | None = None) -> uuid.UUID:
        nid = uuid.uuid4()
        await db.execute(
            text("INSERT INTO kg_nodes (id, organization_id, lead_id, node_type, label, props) "
                 "VALUES (:id,:o,:l,:t,:label, CAST(:props AS jsonb))"),
            {"id": nid, "o": org, "l": lead, "t": node_type,
             "label": (label or node_type)[:255], "props": json.dumps(props or {})},
        )
        counts["nodes"] += 1
        return nid

    async def edge(subj: uuid.UUID, relation: str, obj: uuid.UUID, props: dict | None = None) -> None:
        await db.execute(
            text("INSERT INTO kg_edges (organization_id, lead_id, subject_node_id, relation, "
                 "object_node_id, props) VALUES (:o,:l,:s,:r,:ob, CAST(:props AS jsonb))"),
            {"o": org, "l": lead, "s": subj, "r": relation[:64], "ob": obj,
             "props": json.dumps(props or {})},
        )
        counts["edges"] += 1

    client = await node("person", ex.lead.full_name or "Client", {"role": case_vocab.CLIENT_ROLE})

    incident_node = None
    for inc in ex.incidents:
        incident_node = await node("incident", inc.description or "incident", {"date": inc.incident_date})
        await edge(client, "injured_in", incident_node)

    for inj in ex.injuries:
        n = await node("injury", inj.body_part or inj.description, {"severity": inj.severity})
        await edge(client, "suffered", n)

    for t in ex.treatments:
        if t.provider_name:
            n = await node("provider", t.provider_name, {"type": t.provider_type})
            await edge(client, "treated_by", n)

    at_fault_node = None
    for pa in ex.parties:
        n = await node("person", pa.full_name or pa.role, {"role": pa.role})
        await edge(n, pa.role or case_vocab.PARTY_FALLBACK_RELATION, incident_node or client)
        if pa.role == "at_fault":
            at_fault_node = n

    for p in ex.insurance_policies:
        if p.carrier_name:
            ins = await node("insurer", p.carrier_name, {"kind": p.policy_kind, "limit": p.coverage_limit})
            subject = client if p.party_role == case_vocab.CLAIMANT_ROLE else (at_fault_node or client)
            await edge(subject, "insured_by", ins, {"limit": p.coverage_limit})

    return counts


def _rrf(rankings: list[list], k: int = 60) -> list:
    scores: dict = {}
    for ranking in rankings:
        for pos, _id in enumerate(ranking):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores, key=lambda x: scores[x], reverse=True)


async def hybrid_search_lead(
    db: AsyncSession, lead_id: uuid.UUID, query_embedding: list[float], query_text: str, k: int = 5
) -> list[dict]:
    """Vector + keyword search fused with RRF, scoped to a lead (RLS). Channel-agnostic
    (no IntakeContext) so it's reusable from the voice agent and dashboard chat alike."""
    vec_rows = (await db.execute(
        text("SELECT id, content FROM knowledge_chunks WHERE lead_id = :l "
             "ORDER BY embedding <=> CAST(:q AS halfvec) LIMIT :k"),
        {"l": lead_id, "q": _vec(query_embedding), "k": k},
    )).all()
    kw_rows = (await db.execute(
        text("SELECT id, content FROM knowledge_chunks WHERE lead_id = :l "
             "AND content_tsv @@ plainto_tsquery('english', :qt) "
             "ORDER BY ts_rank(content_tsv, plainto_tsquery('english', :qt)) DESC LIMIT :k"),
        {"l": lead_id, "qt": query_text, "k": k},
    )).all()

    content_by_id = {r.id: r.content for r in [*vec_rows, *kw_rows]}
    fused = _rrf([[r.id for r in vec_rows], [r.id for r in kw_rows]])
    return [{"id": str(i), "content": content_by_id[i]} for i in fused[:k]]


async def hybrid_search(
    db: AsyncSession, ctx: IntakeContext, query_embedding: list[float], query_text: str, k: int = 5
) -> list[dict]:
    """Backward-compatible wrapper (voice IntakeContext)."""
    return await hybrid_search_lead(db, ctx.lead_id, query_embedding, query_text, k)


async def persist_memory(
    organization_id: uuid.UUID, lead_id: uuid.UUID, transcript_id: uuid.UUID,
    transcript_text: str, extraction: Extraction,
) -> dict:
    """Embed the transcript + build the KG. Embedding network call happens outside
    the DB transaction; writes happen inside one tx under the system context."""
    chunks = chunk_transcript(transcript_text)
    embeddings = await embed_texts(chunks)  # network, no DB tx held
    ctx = IntakeContext(organization_id=organization_id, caller_phone="", lead_id=lead_id)
    async with session_scope(system_context(organization_id)) as db:
        n_chunks = await _store_chunks(db, ctx, transcript_id, chunks, embeddings)
        graph = await build_case_graph(db, ctx, extraction)
    return {"chunks": n_chunks, **graph}
