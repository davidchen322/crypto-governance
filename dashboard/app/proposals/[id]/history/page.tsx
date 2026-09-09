"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, getProposalHistory, type ProposalVersion } from "@/lib/api";

export default function ProposalHistoryPage({
  params,
}: PageProps<"/proposals/[id]/history">) {
  const { id } = use(params);
  const [versions, setVersions] = useState<ProposalVersion[] | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getProposalHistory(id)
      .then((v) => !cancelled && setVersions(v))
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) setNotFound(true);
        else setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (notFound) return <p className="text-sm text-gray-500">No proposal with this id.</p>;
  if (error) return <p className="text-sm text-red-600">Failed to load: {error}</p>;
  if (!versions) return <p className="text-sm text-gray-500">Loading...</p>;

  return (
    <div>
      <Link
        href={`/proposals/${encodeURIComponent(id)}`}
        className="text-sm text-blue-600 hover:underline"
      >
        ← back to proposal
      </Link>
      <h1 className="text-2xl font-semibold mt-2 mb-1">{versions[0]?.title}</h1>
      <p className="text-sm text-gray-500 mb-6">
        {versions.length === 1
          ? "This is the only known version of this proposal."
          : `${versions.length} known versions — the lakehouse's SCD2 history for this proposal.`}
      </p>

      <ol className="relative border-l-2 border-gray-200 ml-2">
        {versions.map((v, i) => {
          const prev = versions[i - 1];
          const changes = prev ? diff(prev, v) : null;
          return (
            <li key={v.valid_from} className="mb-6 ml-6">
              <span
                className={`absolute -left-[9px] w-4 h-4 rounded-full border-2 border-white ${
                  v.is_current ? "bg-green-500" : "bg-gray-300"
                }`}
              />
              <div className="border rounded bg-white p-3">
                <div className="text-xs text-gray-500 mb-1">
                  {v.valid_from} → {v.valid_to ?? "now"}
                  {v.is_current && (
                    <span className="ml-2 text-green-700 font-medium">current</span>
                  )}
                </div>
                <div className="text-sm">
                  state: <span className="font-medium">{v.proposal_state}</span> · votes:{" "}
                  <span className="font-medium">{v.vote_count ?? 0}</span> · total:{" "}
                  <span className="font-medium">
                    {v.scores_total?.toLocaleString() ?? 0}
                  </span>
                </div>
                {changes && changes.length > 0 && (
                  <div className="text-xs text-blue-700 mt-1">
                    changed: {changes.join(", ")}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function diff(prev: ProposalVersion, next: ProposalVersion): string[] {
  const changed: string[] = [];
  if (prev.proposal_state !== next.proposal_state) changed.push("state");
  if (prev.vote_count !== next.vote_count) changed.push("vote_count");
  if (prev.scores_total !== next.scores_total) changed.push("scores_total");
  return changed;
}
