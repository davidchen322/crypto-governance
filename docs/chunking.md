# Chunking

**Status:** Implemented, 16 tests passing · **Date:** 16 Aug 2026 · Phase 4, step 2

How governance documents are split for embedding, why it is done this way, and the three
bugs that writing the tests uncovered.

---

## Why chunking decides retrieval quality

An embedding compresses a passage into a single 1536-dimension vector. That vector is the
*only* thing similarity search can see — not the row it came from, not its neighbours, not
the proposal title unless the text says it.

Two failure modes follow directly:

- **Chunks too large.** A chunk containing three unrelated sections retrieves for all three
  topics and answers none of them well. Its vector is an average of things that have little
  to do with each other.
- **Chunks too small.** A fragment loses the context that made it meaningful. "The parameter
  is set to zero" is unhelpful without knowing which parameter, in which proposal.

Nothing errors in either case. The pipeline runs, the numbers look plausible, and retrieval
is quietly mediocre. That is why the eval set was written before any of this — so the
knobs below get tuned against a measurement rather than taste.

---

## The strategy: structure first, windows as fallback

Governance text is heavily sectioned. Proposals open with `## Summary`, then `## Motivation`,
then `## Specification`. Someone asking about risk parameters wants the Specification
section, not a 400-token window that happens to straddle the boundary between Motivation
and Specification.

So:

1. **Split on markdown headings.** Each section becomes a chunk.
2. **Window only oversized sections.** A section longer than the target is cut into
   overlapping token windows.
3. **Merge near-empty fragments** into the preceding chunk.
4. **Prepend the document title** to every chunk.

### Parameters

| Knob | Value | Reasoning |
| --- | --- | --- |
| `TARGET_TOKENS` | 400 | Not the model's limit — `text-embedding-3-large` accepts 8191. This is a retrieval-quality choice: roughly one coherent section. |
| `OVERLAP_TOKENS` | 80 | 20%. A sentence straddling a window boundary stays retrievable from at least one side. |
| `MIN_TOKENS` | 12 | Deliberately low. A short-but-real section is a coherent chunk; merging it would produce one covering two topics. Only near-empty fragments merge. |

All three are arguments, not constants baked into the call sites, so a sweep against the
eval set is a loop rather than an edit.

### Every chunk carries its title and heading

A chunk about fee parameters is far more findable when its own text says which proposal it
belongs to. The embedding cannot see `proposal_id` or `title` — those are columns, not
content. So a chunk reads:

```
[ARFC] Oracle Deprecation for Long-tail Assets Across Aave V2 and V3

Motivation

Continuing to rely on a price feed whose underlying market has thinned out
exposes the protocol to manipulation...
```

The same reasoning applies to windows inside a long section: each window gets the section
heading prepended, so a window from deep inside `Specification` is not context-free text.

### Real token counts, not `chars / 4`

`tiktoken` with `cl100k_base` — the encoding `text-embedding-3-*` actually uses. Three
things are denominated in real tokens: API cost, the model's hard input limit, and chunk
size consistency. The `chars / 4` heuristic drifts badly on exactly the content this corpus
is full of — hex addresses, code blocks, tables.

---

## Corpus results

| | Documents | Chunks | Median tokens | Total tokens |
| --- | --- | --- | --- | --- |
| Proposals | 125 | 1,012 | 198 | ~236k |
| Forum posts | 328 | 654 | 405 | ~184k |
| **Total** | **453** | **1,666** | | **419,831** |

Chunks per document: proposals median 7 (max 33), forum posts median 1 — 236 of 328 posts
are short enough to be a single chunk, which is what forum replies look like.

At `text-embedding-3-large` rates that is roughly **$0.055** to embed the corpus once.

---

## What the tests found

Three bugs, none of which raised an error. All three would have silently degraded retrieval.

### 1. Merging discarded the merged section's heading

The runt-merge folded a short section into its predecessor but kept only the *body*,
dropping the heading text entirely. `## Motivation` and `## Specification` disappeared from
the chunk — not just from metadata, from the text.

A query about "the motivation for this deprecation" then had nothing to match. The words
were simply gone.

Fixed: the merged section's heading is carried into the text.

### 2. The test fixture was unrepresentative

After that fix, a test still failed — sections named `Motivation` weren't appearing. The
cause was the *fixture*, not the code: its sections were 11 tokens where real corpus
sections run 128–400. The runt-merge was correctly folding them.

The fix was to rewrite the fixture from real corpus text rather than lower the threshold to
accommodate a toy input. A note in the test file records why, because the next person to
see that failure will be tempted to take the shortcut.

### 3. A document was silently discarded — and the trail led back to Phase 3

Corpus-wide statistics showed `min 0` chunks per post. One post produced nothing at all.

It was a 2,123-character Arbitrum milestone report whose text began with `#`. The heading
regex matched from `#` to end-of-string — because the text contained **no newlines at all** —
capturing the entire document as a heading with an empty body, which the section loop
dropped.

Investigating the missing newlines surfaced the real problem, one layer down in Phase 3:

```sql
-- posts containing at least one newline
0  of  328
-- posts containing raw HTML entities (&amp;, &quot;, &#39;)
52 of  328
```

The HTML-to-text step was collapsing `\s+` to a single space, which flattens paragraph
breaks along with runs of spaces. Two consequences:

- **Heading-based splitting could never fire on forum posts.** Markdown headings need line
  starts to exist. Every post was being windowed blindly.
- **One post was lost entirely**, as above.

And separately, entities were never decoded, so 52 posts would have embedded `&amp;` and
`&#39;` as literal noise.

**Fixed in three places:**

- **Phase 3** now converts block-level tags (`</p>`, `<br>`, `</li>`, …) to newlines
  *before* stripping tags, decodes HTML entities, and collapses only *horizontal*
  whitespace. `&amp;` is decoded last so `&amp;lt;` resolves to `&lt;` rather than being
  double-decoded.
- **Phase 4** guards against a heading that consumes an entire document — if splitting on
  it would yield an empty body, it is treated as content.
- **A regression test** pins the discarded-document case, referencing the real post.

After the rebuild: 319 of 328 posts carry newlines, 0 carry entities, 0 produce zero chunks.

---

## The bug that fix exposed

Rebuilding silver after changing the HTML pipeline printed:

```
gov.silver.forum_posts: no changes, MERGE skipped
```

It was wrong. The text was stale and the run had done nothing.

The Phase 3 MERGE guard compared only the SCD2 validity columns. But `record_hash` is
computed from the *source* `body_html`, not the derived `body_text` — so changing the
derivation moved no hash, the guard saw no work, and silver silently kept the old text
through a run that reported success.

This is a nasty class of bug: a pipeline that appears to succeed while doing nothing.

**Fixed:** the guard now compares every non-key column, using null-safe `<=>` so NULL-to-NULL
does not read as "unknown" and drop rows from the count. Re-running then reported
`0 new row(s), 319 updated`, and the text changed.

The two failure modes the guard sits between are both real and both observed:

| Guard | Failure |
| --- | --- |
| None | Every run rewrites all records; the Iceberg snapshot log becomes noise |
| Validity columns only | Derivation changes are missed; stale data served silently |
| **Whole row** | Correct |

---

## Rebuilding is cheap, and that matters

Everything above was found and fixed by re-running `make silver` repeatedly. **No source
data was re-fetched.** Silver is a pure function of bronze, so the loop is:

```bash
# edit the transformation
make silver          # ~1 minute, reads MinIO, no network
```

Re-acquiring bronze would mean hours to days against rate-limited APIs across five
independent forums. Re-deriving silver is a minute. That asymmetry is the entire reason the
raw layer exists, and it is what made it reasonable to touch the HTML stripping at all
rather than leaving a known-imperfect transformation alone.

---

## Known gaps

**Forum posts rarely use markdown headings.** Even with newlines restored, only 5 of 654
post chunks have one. Posts are mostly flat prose, so they are windowed rather than
structurally split. Paragraph-boundary splitting would be a better fallback than a blind
token window and is worth trying if post retrieval underperforms.

**Overlap is fixed, not semantic.** Windows cut on token boundaries, so a sentence can be
split mid-clause. The 80-token overlap means it survives in the neighbouring chunk, but a
sentence-aware splitter would be cleaner.

**Nothing is deduplicated yet.** 328 posts share only 317 distinct `content_hash` values —
boilerplate repeated across forums. Embedding by distinct `content_hash` would skip those
calls. Trivial at this size, real money at 200k posts.

**Tables and code blocks are not special-cased.** A markdown table split across a window
boundary loses its header row. Governance specifications use tables heavily for parameter
changes, so this is the most likely source of a retrieval miss on exactly the queries the
platform exists to answer.

---

## Next

Embedding loader, then `search()`, then the recall@k scorer, then the baseline number.
Chunking parameters stay as they are until that baseline exists — tuning them before there
is a measurement would be guessing with extra steps.
