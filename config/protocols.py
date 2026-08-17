"""Protocols under coverage.

Snapshot space ids are verified against the live API, not guessed — `aave.eth` looks
plausible and returns nothing; the real space is `aavedao.eth`. Proposal counts as of
14 Aug 2026 are noted so an id that silently goes empty is obvious.

Scope is deliberately five Ethereum-ecosystem protocols with both an active Snapshot space
and a busy forum: enough variety to prove the pipeline generalizes, few enough to debug.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Protocol:
    name: str
    snapshot_space: str
    discourse_host: str
    note: str = ""
    # How the protocol is written in prose. Chunk text carries this so the embedding can
    # see which protocol a chunk belongs to — `protocol_name` is a column, and a column is
    # invisible to a vector.
    display: str = ""

    @property
    def label(self) -> str:
        return self.display or self.name.title()


PROTOCOLS: tuple[Protocol, ...] = (
    Protocol("aave", "aavedao.eth", "governance.aave.com", "~970 proposals", "Aave"),
    Protocol("uniswap", "uniswapgovernance.eth", "gov.uniswap.org", "~197 proposals", "Uniswap"),
    Protocol(
        "arbitrum",
        "arbitrumfoundation.eth",
        "forum.arbitrum.foundation",
        "~415 proposals",
        "Arbitrum",
    ),
    Protocol("optimism", "opcollective.eth", "gov.optimism.io", "~93 proposals", "Optimism"),
    Protocol("ens", "ens.eth", "discuss.ens.domains", "~98 proposals", "ENS"),
)

BY_NAME = {p.name: p for p in PROTOCOLS}


def resolve(names: list[str] | None) -> list[Protocol]:
    if not names:
        return list(PROTOCOLS)
    unknown = sorted(set(names) - BY_NAME.keys())
    if unknown:
        raise KeyError(f"unknown protocol(s): {unknown}; known: {sorted(BY_NAME)}")
    return [BY_NAME[n] for n in names]
