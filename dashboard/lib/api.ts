// Typed client for backend_api (Phase 6). Direct browser calls, no server-side proxy — the
// API has no auth to broker (see backend_api/main.py's own docstring on that), so a Next.js
// API route here would only be a translation layer with nothing to translate.

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }
  return res.json();
}

export type ProposalSummary = {
  proposal_id: string;
  protocol_name: string;
  title: string | null;
  proposal_state: string | null;
  proposal_created: string | null;
  vote_count: number | null;
};

export type ProposalDetail = ProposalSummary & {
  body: string | null;
  author: string | null;
  voting_start: string | null;
  voting_end: string | null;
  scores_total: number | null;
  quorum: number | null;
  choices: string[] | null;
  scores: number[] | null;
  discussion_url: string | null;
};

export type ProposalVersion = {
  proposal_id: string;
  protocol_name: string;
  title: string | null;
  proposal_state: string | null;
  vote_count: number | null;
  scores_total: number | null;
  content_hash: string;
  valid_from: string;
  valid_to: string | null;
  is_current: boolean;
};

export type Citation = {
  marker: string;
  chunk_ref: string;
  source: string;
  document_id: string;
  protocol_name: string;
  title: string | null;
  distance: number;
};

export type ChatResponse = {
  question: string;
  route: string | null;
  scope: string | null;
  filters: Record<string, unknown> | null;
  answer: string | null;
  data_gap: boolean | null;
  citations: Citation[];
  retrieval_note: string | null;
};

export const PROTOCOLS = ["aave", "uniswap", "arbitrum", "optimism", "ens"] as const;
export const STATES = ["active", "closed", "pending"] as const;

export function listProposals(params: {
  protocol?: string;
  state?: string;
  limit?: number;
  offset?: number;
}): Promise<ProposalSummary[]> {
  const q = new URLSearchParams();
  if (params.protocol) q.set("protocol", params.protocol);
  if (params.state) q.set("state", params.state);
  q.set("limit", String(params.limit ?? 20));
  q.set("offset", String(params.offset ?? 0));
  return getJSON(`/proposals?${q}`);
}

export function getProposal(id: string): Promise<ProposalDetail> {
  return getJSON(`/proposals/${encodeURIComponent(id)}`);
}

export function getProposalHistory(id: string): Promise<ProposalVersion[]> {
  return getJSON(`/proposals/${encodeURIComponent(id)}/history`);
}

export async function postChat(question: string): Promise<ChatResponse> {
  const res = await fetch(`${API_URL}/api/v1/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }
  return res.json();
}

// One callback per graph-node SSE event, in the shape /api/v1/chat/stream actually sends —
// per-node progress, not token-by-token prose. See backend_api/routes/chat.py's docstring.
export function streamChat(
  question: string,
  onEvent: (node: string, payload: Record<string, unknown>) => void,
  onDone: () => void,
): () => void {
  const url = `${API_URL}/api/v1/chat/stream?question=${encodeURIComponent(question)}`;
  const source = new EventSource(url);
  const nodeNames = ["router", "sql", "vector", "hybrid", "synthesis", "error"];
  for (const node of nodeNames) {
    source.addEventListener(node, (e) => {
      onEvent(node, JSON.parse((e as MessageEvent).data));
      if (node === "synthesis" || node === "error") {
        source.close();
        onDone();
      }
    });
  }
  source.onerror = () => {
    source.close();
    onDone();
  };
  return () => source.close();
}
