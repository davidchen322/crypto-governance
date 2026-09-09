"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, getProposal, type ProposalDetail } from "@/lib/api";
import { computeRiskFlags, type RiskFlag } from "@/lib/risk";

export default function ProposalDetailPage({
  params,
}: PageProps<"/proposals/[id]">) {
  const { id } = use(params);
  const [proposal, setProposal] = useState<ProposalDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getProposal(id)
      .then((p) => !cancelled && setProposal(p))
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) setNotFound(true);
        else setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (notFound) return <p className="text-sm text-gray-500">No current proposal with this id.</p>;
  if (error) return <p className="text-sm text-red-600">Failed to load: {error}</p>;
  if (!proposal) return <p className="text-sm text-gray-500">Loading...</p>;

  const flags: RiskFlag[] = computeRiskFlags(proposal);

  return (
    <div>
      <Link href="/proposals" className="text-sm text-blue-600 hover:underline">
        ← back to proposals
      </Link>

      <h1 className="text-2xl font-semibold mt-2">{proposal.title}</h1>
      <p className="text-sm text-gray-500 mb-4">
        {proposal.protocol_name} · {proposal.proposal_state} · by{" "}
        {proposal.author ?? "unknown"}
      </p>

      <div className="flex gap-3 mb-4">
        <Link
          href={`/proposals/${encodeURIComponent(proposal.proposal_id)}/history`}
          className="text-sm border rounded px-3 py-1.5 hover:bg-gray-50"
        >
          View version history
        </Link>
        {proposal.discussion_url && (
          <a
            href={proposal.discussion_url}
            target="_blank"
            rel="noreferrer"
            className="text-sm border rounded px-3 py-1.5 hover:bg-gray-50"
          >
            View forum discussion
          </a>
        )}
      </div>

      <RiskPanel flags={flags} />

      <div className="grid grid-cols-2 gap-4 my-4 text-sm">
        <Stat label="Votes cast" value={String(proposal.vote_count ?? 0)} />
        <Stat
          label="Total voting power"
          value={proposal.scores_total?.toLocaleString() ?? "0"}
        />
      </div>

      {proposal.choices && proposal.scores && (
        <table className="w-full text-sm border rounded bg-white mb-6">
          <tbody>
            {proposal.choices.map((choice, i) => (
              <tr key={choice} className="border-b last:border-0">
                <td className="p-2">{choice}</td>
                <td className="p-2 text-right">
                  {(proposal.scores?.[i] ?? 0).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <article className="prose prose-sm max-w-none whitespace-pre-wrap border rounded bg-white p-4">
        {proposal.body}
      </article>
    </div>
  );
}

function RiskPanel({ flags }: { flags: RiskFlag[] }) {
  if (flags.length === 0) {
    return (
      <div className="border rounded bg-green-50 border-green-200 text-green-800 text-sm p-3 mb-4">
        No risk flags raised for this proposal.
      </div>
    );
  }
  return (
    <div className="border rounded bg-amber-50 border-amber-200 p-3 mb-4 space-y-1">
      {flags.map((f) => (
        <div key={f.key} className="text-sm text-amber-900">
          <span className="font-medium">{f.label}:</span> {f.evidence}
        </div>
      ))}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="border rounded bg-white p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="font-medium">{value}</div>
    </div>
  );
}
