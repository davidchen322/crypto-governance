"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { postChat, streamChat, type ChatResponse, type Citation } from "@/lib/api";

type Stage = { node: string; label: string };

const STAGE_LABELS: Record<string, string> = {
  router: "Routing...",
  sql: "Querying structured data...",
  vector: "Retrieving passages...",
  hybrid: "Retrieving and querying...",
  synthesis: "Answering...",
  error: "Something went wrong",
};

export default function ChatPage() {
  const [question, setQuestion] = useState("");
  const [stage, setStage] = useState<Stage | null>(null);
  const [response, setResponse] = useState<ChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;
    setResponse(null);
    setError(null);
    setStage({ node: "router", label: STAGE_LABELS.router });

    stopRef.current?.();
    stopRef.current = streamChat(
      question,
      (node, payload) => {
        if (node === "error") {
          setError(String(payload.error ?? "unknown error"));
          setStage(null);
          return;
        }
        setStage({ node, label: STAGE_LABELS[node] ?? `${node}...` });
        if (node === "synthesis") {
          setResponse(payload as unknown as ChatResponse);
        }
      },
      () => setStage(null),
    );
  }

  // Fallback used only if SSE never resolves — the plan keeps POST /api/v1/chat as an
  // explicit path precisely so a fragile SSE connection doesn't strand the user.
  async function askViaPost() {
    setError(null);
    setStage({ node: "router", label: "Asking..." });
    try {
      const r = await postChat(question);
      setResponse(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setStage(null);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4">Ask the governance agent</h1>

      <form onSubmit={ask} className="flex gap-2 mb-4">
        <input
          type="text"
          className="border rounded px-3 py-2 text-sm flex-1"
          placeholder="What did Aave decide about..."
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button
          type="submit"
          className="bg-blue-600 text-white rounded px-4 py-2 text-sm disabled:opacity-40"
          disabled={!!stage}
        >
          Ask
        </button>
      </form>

      {stage && (
        <p className="text-sm text-gray-500 mb-4 animate-pulse">{stage.label}</p>
      )}

      {error && (
        <div className="text-sm text-red-600 mb-4">
          {error}{" "}
          <button className="underline" onClick={askViaPost}>
            retry without streaming
          </button>
        </div>
      )}

      {response && <Answer response={response} />}
    </div>
  );
}

function Answer({ response }: { response: ChatResponse }) {
  if (response.data_gap) {
    return (
      <div className="border rounded bg-gray-50 p-4 text-sm text-gray-600">
        {response.answer ?? "The corpus does not contain material that answers this question."}
      </div>
    );
  }

  const citationByMarker = new Map(response.citations.map((c) => [c.marker, c]));
  const parts = (response.answer ?? "").split(/(\[c\d+\])/g);

  return (
    <div className="border rounded bg-white p-4">
      <p className="text-sm whitespace-pre-wrap leading-relaxed">
        {parts.map((part, i) => {
          const match = part.match(/^\[(c\d+)\]$/);
          if (!match) return <span key={i}>{part}</span>;
          const citation = citationByMarker.get(match[1]);
          if (!citation) return <span key={i}>{part}</span>;
          return <CitationChip key={i} citation={citation} />;
        })}
      </p>
    </div>
  );
}

function CitationChip({ citation }: { citation: Citation }) {
  const [open, setOpen] = useState(false);

  // Only proposal citations have a screen to link to in this phase — forum citations show
  // an inline expandable snippet instead of a broken or fabricated link (see phase-8-plan.md).
  if (citation.source === "proposal") {
    return (
      <Link
        href={`/proposals/${encodeURIComponent(citation.document_id)}`}
        className="inline-block mx-0.5 px-1.5 rounded bg-blue-100 text-blue-800 text-xs align-middle hover:bg-blue-200"
        title={citation.title ?? undefined}
      >
        {citation.marker}
      </Link>
    );
  }

  return (
    <span className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-block mx-0.5 px-1.5 rounded bg-purple-100 text-purple-800 text-xs align-middle hover:bg-purple-200"
      >
        {citation.marker}
      </button>
      {open && (
        <span className="absolute z-10 left-0 top-full mt-1 w-64 border rounded bg-white shadow-lg p-2 text-xs text-gray-700 not-italic">
          <strong>{citation.protocol_name}</strong> · forum · {citation.title}
          <br />
          distance {citation.distance}
        </span>
      )}
    </span>
  );
}
