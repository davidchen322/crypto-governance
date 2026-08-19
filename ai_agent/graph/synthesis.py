"""Turn retrieved material into an analyst answer with citations that resolve.

Two mechanisms here are load-bearing, and both exist because of things Phase 4 measured.

THE RELEVANCE JUDGE
Retrieval returns candidates; it does not decide whether they answer the question. Phase 4
found one case no distance threshold can reject — a chunk about Aave App REVENUE PROJECTIONS
retrieved for a question about FOUNDER COMPENSATION, at distance 0.451 while a genuinely
answerable question sat further away at 0.458. The two are in the wrong order, so no cutoff
can separate them. Judging them directly was decisive: top relevance 0 versus 3.

Note the measured asymmetry: the same model used to REORDER results made thematic recall
worse (0.78 -> 0.72), because it prefers one deeply detailed passage over the spread of
documents a cross-protocol question needs. So it judges, it does not rerank.

CITATIONS AS A CONTRACT
"Cite your sources" is an instruction, not enforcement. Instead every claim carries a marker,
markers resolve to chunk ids, and `validate_citations` checks each id against what was
actually retrieved for THIS question. A fabricated citation fails a lookup instead of
reaching a reader — which is a test, not a hope.
"""

from __future__ import annotations

from typing import Any

from ai_agent.graph.llm import ROUTER_MODEL, SYNTHESIS_MODEL, complete_json
from ai_agent.graph.state import AgentState

MAX_CONTEXT_CHUNKS = 24
RELEVANT_SCORE = 2  # >= this counts as usable evidence

JUDGE_PROMPT = """Score how well each passage helps answer the question.

3 = directly answers it, or is clearly one of the documents being asked for
2 = same topic, useful supporting evidence
1 = related subject area but does not address the question
0 = unrelated, or merely shares vocabulary

Shared vocabulary is not relevance. A passage about a protocol's REVENUE does not answer a
question about someone's SALARY, even though both concern money and the same protocol.

QUESTION: {question}

PASSAGES:
{passages}

Return ONLY JSON: {{"scores":[{{"i":0,"s":3}}, ...]}} with an entry for every passage."""

ANALYST_PROMPT = """You are a governance analyst. Answer using ONLY the evidence provided.

Evidence comes in two forms and they are cited differently:

  STRUCTURED RESULTS — rows from a database query. These are authoritative for counts,
  dates, states and orderings. Their provenance is the query itself, so claims drawn from
  them need NO marker. If structured results are present, they are sufficient evidence for
  a factual answer on their own.

  PASSAGES — governance prose. Every claim drawn from a passage MUST carry its marker,
  e.g. [c0].

Cover, where the evidence supports it, three dimensions:
  TECHNICAL  what actually changes — parameters, contracts, mechanisms
  ECONOMIC   what it costs or is worth, who pays, who benefits
  POLITICAL  who proposed it, who objected, how contested it was

Write prose a person can read. Never return a raw data structure as the answer — turn counts
and rows into sentences.

Do not make claims the evidence does not support. Set "data_gap": true ONLY when neither
structured results nor passages address the question — not merely because one of the two is
absent.

QUESTION: {question}

{structured}

EVIDENCE:
{passages}

Cite by using the marker shown in brackets above each passage, e.g. [c0]. Use ONLY markers
that appear above — never invent one, and never renumber them.

Return ONLY JSON:
{{"answer":"<prose citing passages as [c0], [c3] ...>", "data_gap": false}}"""


def _passage_block(chunks: list[Any]) -> tuple[str, dict[str, Any]]:
    """Render chunks for the prompt, keyed by the marker the model must cite.

    The model NEVER supplies a source identity — it only selects a marker from this list.
    That is what makes a fabricated citation structurally impossible rather than merely
    discouraged: an invented marker resolves to nothing and is reported.

    The first version of this showed `[0] ref=proposal:0xaa68…#3` and asked the model to
    echo the ref. It cited "0" — the bracket index — and every citation in a fluent,
    authoritative answer failed to resolve. Asking a model to reproduce an opaque string is
    an avoidable failure mode when it could just pick a label.
    """
    lines, index = [], {}
    for i, c in enumerate(chunks[:MAX_CONTEXT_CHUNKS]):
        marker = f"c{i}"
        index[marker] = c
        title = (c.title or "").strip()
        lines.append(f"[{marker}] {c.protocol_name} | {c.source} | {title}\n{c.text[:900]}")
    return "\n\n".join(lines), index


def judge(question: str, chunks: list[Any]) -> dict[int, int]:
    if not chunks:
        return {}
    passages, _ = _passage_block(chunks)
    out = complete_json(
        JUDGE_PROMPT.format(question=question, passages=passages), model=ROUTER_MODEL
    )
    return {int(s["i"]): int(s["s"]) for s in out.get("scores", []) if "i" in s and "s" in s}


def validate_citations(
    answer: str, index: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve every marker used in the prose against what was actually retrieved.

    Citations are DERIVED from the answer rather than declared alongside it. A separate
    declared list can disagree with the prose — markers cited but not declared, or declared
    but never used — and reconciling the two is work with no upside when the prose is the
    thing a reader actually sees.

    A marker that resolves to nothing is reported and dropped, so a fabricated source fails
    a dictionary lookup instead of reaching a reader.
    """
    import re

    kept, problems, seen = [], [], set()
    for marker in re.findall(r"\[(c\d+)\]", answer or ""):
        if marker in seen:
            continue
        seen.add(marker)
        chunk = index.get(marker)
        if chunk is None:
            problems.append(f"[{marker}] does not match any retrieved passage")
            continue
        kept.append(
            {
                "marker": marker,
                "chunk_ref": f"{chunk.source}:{chunk.document_id}#{chunk.chunk_index}",
                "source": chunk.source,
                "document_id": chunk.document_id,
                "protocol_name": chunk.protocol_name,
                "title": chunk.title,
                "distance": round(chunk.distance, 4),
            }
        )
    return kept, problems


def synthesis_node(state: AgentState) -> dict[str, Any]:
    question = state["question"]
    chunks = list(state.get("chunks") or [])
    rows = list(state.get("rows") or [])

    relevant = chunks
    if chunks:
        scores = judge(question, chunks)
        relevant = [
            c
            for i, c in enumerate(chunks[:MAX_CONTEXT_CHUNKS])
            if scores.get(i, 0) >= RELEVANT_SCORE
        ]

    # Nothing survived the judge and no structured rows either: say so rather than write
    # something confident out of passages the judge already called irrelevant.
    if not relevant and not rows:
        return {
            "answer": "DATA GAP IDENTIFIED — the corpus does not contain material that "
            "answers this question.",
            "citations": [],
            "data_gap": True,
        }

    passages, index = _passage_block(relevant)
    structured = "STRUCTURED RESULTS: none"
    if rows:
        preview = rows[:25]
        structured = f"STRUCTURED RESULTS ({len(rows)} row(s), authoritative):\n{preview}"

    out = complete_json(
        ANALYST_PROMPT.format(
            question=question, structured=structured, passages=passages or "(none)"
        ),
        model=SYNTHESIS_MODEL,
    )
    answer = str(out.get("answer", "")).strip()
    kept, problems = validate_citations(answer, index)

    # A structured answer legitimately has no markers — its provenance is the query, which
    # `retrieval_note` records. Treating "no citations" as a data gap made every pure-SQL
    # question refuse to answer while holding correct rows.
    return {
        "answer": answer,
        "citations": kept,
        "data_gap": bool(out.get("data_gap")) or not answer,
        "retrieval_note": (
            state.get("retrieval_note", "") + ("; " + "; ".join(problems) if problems else "")
        ),
    }
