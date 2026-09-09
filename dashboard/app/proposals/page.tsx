"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { listProposals, PROTOCOLS, STATES, type ProposalSummary } from "@/lib/api";

const PAGE_SIZE = 20;

export default function ProposalsPage() {
  const [protocol, setProtocol] = useState<string>("");
  const [state, setState] = useState<string>("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [proposals, setProposals] = useState<ProposalSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // `loading` is set to true in the event handlers below, not synchronously in the effect —
  // every trigger for a new fetch here is a user action (a select changing, a button click),
  // so the handler is the right place for it, and the effect only ever sets state inside its
  // async callbacks.
  function updateProtocol(value: string) {
    setProtocol(value);
    setOffset(0);
    setLoading(true);
  }

  function updateState(value: string) {
    setState(value);
    setOffset(0);
    setLoading(true);
  }

  function loadMore() {
    setOffset((o) => o + PAGE_SIZE);
    setLoading(true);
  }

  useEffect(() => {
    let cancelled = false;
    listProposals({ protocol: protocol || undefined, state: state || undefined, offset })
      .then((rows) => {
        if (cancelled) return;
        setProposals((prev) => (offset === 0 ? rows : [...prev, ...rows]));
        setError(null);
      })
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [protocol, state, offset]);

  const filtered = search
    ? proposals.filter((p) => p.title?.toLowerCase().includes(search.toLowerCase()))
    : proposals;

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4">Proposals</h1>

      <div className="flex flex-wrap gap-3 mb-4">
        <select
          className="border rounded px-3 py-1.5 text-sm"
          value={protocol}
          onChange={(e) => updateProtocol(e.target.value)}
        >
          <option value="">All protocols</option>
          {PROTOCOLS.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>

        <select
          className="border rounded px-3 py-1.5 text-sm"
          value={state}
          onChange={(e) => updateState(e.target.value)}
        >
          <option value="">All states</option>
          {STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <input
          type="text"
          placeholder="Filter loaded results by title..."
          className="border rounded px-3 py-1.5 text-sm flex-1 min-w-48"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && (
        <p className="text-red-600 text-sm mb-4">Failed to load proposals: {error}</p>
      )}

      <ul className="divide-y border rounded bg-white">
        {filtered.length === 0 && !loading && (
          <li className="p-4 text-sm text-gray-500">No proposals match these filters.</li>
        )}
        {filtered.map((p) => (
          <li key={p.proposal_id}>
            <Link
              href={`/proposals/${encodeURIComponent(p.proposal_id)}`}
              className="flex items-center justify-between p-3 hover:bg-gray-50"
            >
              <div>
                <div className="font-medium text-sm">{p.title ?? "(untitled)"}</div>
                <div className="text-xs text-gray-500">
                  {p.protocol_name} · {p.proposal_state ?? "unknown"} ·{" "}
                  {p.vote_count ?? 0} votes
                </div>
              </div>
              <span className="text-xs text-gray-400">
                {p.proposal_created?.slice(0, 10)}
              </span>
            </Link>
          </li>
        ))}
      </ul>

      <div className="mt-4 flex items-center gap-3">
        <button
          className="border rounded px-4 py-1.5 text-sm disabled:opacity-40"
          disabled={loading}
          onClick={loadMore}
        >
          {loading ? "Loading..." : "Load more"}
        </button>
        <span className="text-xs text-gray-500">{proposals.length} loaded</span>
      </div>
    </div>
  );
}
