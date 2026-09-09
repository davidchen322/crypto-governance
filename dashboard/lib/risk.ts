// Three simple, explainable heuristics from columns silver already has — not a scoring
// model. See docs/phase-8-plan.md: no earlier phase computes risk, so this is defined here,
// concretely, rather than left vague. Every threshold below is a tunable constant, not a
// discovered property — same discipline DEFAULT_MAX_DISTANCE documents in retrieval.py.

import type { ProposalDetail } from "./api";

const CLOSE_VOTE_MARGIN_FRACTION = 0.05; // top two choices within 5% of total votes
const LOW_TURNOUT_FLOOR = 10; // vote_count below this is flagged, regardless of protocol

export type RiskFlag = {
  key: "quorum" | "close_vote" | "low_turnout";
  label: string;
  evidence: string;
};

export function computeRiskFlags(p: ProposalDetail): RiskFlag[] {
  const flags: RiskFlag[] = [];

  if (p.quorum != null && p.quorum > 0 && (p.scores_total ?? 0) < p.quorum) {
    flags.push({
      key: "quorum",
      label: "Quorum not met",
      evidence: `${(p.scores_total ?? 0).toLocaleString()} of ${p.quorum.toLocaleString()} required`,
    });
  }

  if (p.scores && p.scores.length >= 2 && p.scores_total) {
    const sorted = [...p.scores].sort((a, b) => b - a);
    const [top, second] = sorted;
    const margin = (top - second) / p.scores_total;
    if (margin < CLOSE_VOTE_MARGIN_FRACTION) {
      flags.push({
        key: "close_vote",
        label: "Close vote",
        evidence: `top two options within ${(margin * 100).toFixed(1)}% of total votes`,
      });
    }
  }

  if (p.vote_count != null && p.vote_count < LOW_TURNOUT_FLOOR) {
    flags.push({
      key: "low_turnout",
      label: "Low turnout",
      evidence: `only ${p.vote_count} vote(s) cast`,
    });
  }

  return flags;
}
