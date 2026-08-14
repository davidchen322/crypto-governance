# External Sources and API Keys

**Last verified:** 14 Aug 2026 · Snapshot and Discourse tested empirically this session; the
rest is from provider documentation and not yet exercised.

Every external source the platform touches, whether it needs a key, and when.

---

## At a glance

| Source | Key needed? | First required | Cost |
| --- | --- | --- | --- |
| Snapshot GraphQL | **No** | — | Free |
| Discourse forums (×5) | **No** — and not obtainable | — | Free |
| **OpenAI** | **Yes — get this** | **Phase 4** | Pay per token |
| Anthropic | Optional | Phase 5 | Pay per token |
| Voyage AI | Optional (OpenAI alternative) | Phase 4 | Pay per token |
| Etherscan | Free tier | Phase 9 | Free at this volume |
| Sourcify | No | Phase 9 | Free |
| Discord / Slack | Webhook URL, not a key | Phase 10 | Free |
| Cloud provider | Account, not an API key | Phase 11 | Metered |

**Only one is on the critical path: OpenAI.** Nothing built so far, and nothing in Phase 3,
needs any credential at all.

---

## The three model calls

Model choice is not one decision. The agent makes three distinct kinds of call, and they are
chosen independently:

| Call | Job | What it needs |
| --- | --- | --- |
| **Router** | Classify a query — SQL, vector, or hybrid? One short structured output. | A small, cheap model |
| **Analyst** (synthesis) | Read retrieved chunks, write the risk assessment with citations. | Strong reasoning |
| **Embeddings** | Convert text into a 1536-dimension vector. Not a chat model. | An embedding model |

This is what "only if you want Claude for the analyst node" meant: Claude is an option for the
**middle** row alone. **Anthropic publishes no embeddings endpoint**, so the bottom row comes
from OpenAI or Voyage no matter which provider writes the analysis. An Anthropic key therefore
*adds* a provider rather than replacing one.

---

## Sources that need nothing

### Snapshot — `hub.snapshot.org/graphql`

Verified working anonymously: 100 proposals fetched across five spaces with zero 429s at a
self-imposed 60 requests/minute.

No key exists to obtain for the public GraphQL endpoint. If a future full backfill (Aave alone
has ~970 proposals) starts drawing throttling, check whether Snapshot has introduced a keyed
tier before assuming the client is at fault — but nothing in the current scope needs one.

### Discourse forums — five independent instances

`governance.aave.com`, `gov.uniswap.org`, `forum.arbitrum.foundation`, `gov.optimism.io`,
`discuss.ens.domains`. All five verified returning HTTP 200 with full topic and post bodies
anonymously.

**Discourse API keys are per-instance and issued by each forum's own administrators** — that is,
by Aave's community, Uniswap's community, and so on, individually. They are not realistically
obtainable by a third party and are not needed. The correct posture is politeness, not
authentication: the harvester runs a separate 30 requests/minute token bucket per host, honours
`Retry-After`, and backs off exponentially.

Read each forum's terms before scaling up. They are separate organizations with separate rules.

### Sourcify

Open contract-source repository, no authentication. Phase 9 fallback when Etherscan lacks
verified source.

---

## Sources that need a key

### OpenAI — the one to get now

**Required at Phase 4.** Without it there are no embeddings, no vector search, and no retrieval —
Phase 4 cannot start.

Two uses, only the first mandatory:

1. **Embeddings** — `text-embedding-3-large` at `dimensions=1536`, the model settled on
   14 Aug 2026. See [`implementation-plan.md`](implementation-plan.md) for why 1536 rather than
   3072 (pgvector refuses to index above 2000 dimensions).
2. **Generation** — optionally the router and analyst nodes too, if you don't use Claude.

**Set a hard spend cap before Phase 4.** It is the first phase containing a loop that can run
away, and the backfill embeds the entire corpus in one pass. Verify current per-token rates at
the provider rather than trusting any figure written in a planning document.

Cost shape worth knowing: embedding is billed **per token, not per dimension**. Requesting 1536
dimensions costs exactly what 3072 costs. Narrow vectors are free on the API side and cheaper on
the database side — the opposite of most people's intuition.

```bash
OPENAI_API_KEY=sk-...
```

### Anthropic — optional, generation only

Useful only if you want Claude writing the analysis. Adds a provider; removes none.

Current models and list pricing per million tokens:

| Model | ID | Input | Output |
| --- | --- | --- | --- |
| Claude Opus 5 | `claude-opus-5` | $5.00 | $25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | $3.00 (intro $2.00 through 31 Aug 2026) | $15.00 (intro $10.00) |
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1.00 | $5.00 |

A sensible split if you go this route: **Haiku 4.5 for the router** (classification is cheap
work) and **Opus 5 for the analyst** (governance risk assessment is not). That mirrors the
plan's existing instinct of using a small model for routing and a strong one for synthesis.

Two API details worth knowing before Phase 5, because they differ from older Claude code you
may find in examples:

- Thinking is on by default on Opus 5 — omitting the parameter runs adaptive thinking.
  `max_tokens` caps thinking **plus** response text together, so size it with headroom.
- `temperature`, `top_p`, and `top_k` are rejected on current models. Steer with prompting.

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

### Voyage AI — optional embeddings alternative

Relevant only as a substitute for OpenAI embeddings. `voyage-3` is 1024 dimensions, comfortably
under pgvector's 2000-dimension index ceiling and compatible with a `VECTOR(1024)` column.

Not recommended for the initial build purely because the schema, the plan, and the settled
decision are all built around 1536. Worth revisiting if Phase 4's eval set says otherwise —
the `embedding_model` column exists precisely so two models can coexist during that comparison.

### Etherscan — free tier, Phase 9

Fetches verified contract source for the addresses referenced in proposals. The free tier is
adequate at this volume. Two minutes to register; no rush until Phase 9.

```bash
ETHERSCAN_API_KEY=...
```

### Discord or Slack — Phase 10

A **webhook URL**, not an API key. Created from the channel's own integration settings. Treat it
as a secret regardless — anyone holding it can post to the channel.

```bash
ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### Cloud provider — Phase 11

An account and deployment credentials rather than an API key. Out of scope until the cloud
target is chosen. Set billing alerts before the first deploy.

---

## Where keys go

**Into `.env`. Never `.env.example`.**

`.env` is gitignored, and `tests/test_phase0_repo.py` fails the build if it ever becomes tracked
— specifically guarding the case where a `.gitignore` entry is added *after* the file was already
staged, which does not untrack it.

`.env.example` is committed and holds only local sandbox placeholders. A separate test asserts
that every variable read by application code appears there, so a new key cannot silently become
tribal knowledge.

```bash
cp .env.example .env
# add real keys to .env only
```

Two habits worth keeping:

- **Cap spending at the provider**, not in application code. A runaway loop cannot exceed a
  limit it has no power to change.
- **Scope keys narrowly.** An embeddings-only key cannot be used to run generation if it leaks.

---

## Summary

Get an **OpenAI key with a spend cap** — that's the only credential blocking forward progress,
and it's needed two phases out. Optionally grab the free **Etherscan** key while you're at it.

Everything else is either free and unauthenticated, not obtainable, or not needed until the
cloud phase.
