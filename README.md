# PoE-Valdo-Prices

Tools for building a local Path of Exile knowledge base from public game
resources.

This does **not** permanently train a model. Instead, it creates a
Retrieval-Augmented Generation (RAG) data layer that an AI assistant can search
before answering Path of Exile questions.

## What it ingests

- PoE Wiki page extracts for core mechanics, league mechanics, crafting,
  mapping, skills, defenses, and curated high-value items.
- PoE Wiki Valdo's Puzzle Box reward table.
- poe.ninja current-league economy snapshots for uniques, skill gems, cluster
  jewels, invitations, currency, and fragments.

## Build the knowledge base

```bash
python3 scripts/poe_knowledge_base.py build
```

The default output is written to:

```text
data/poe_knowledge/
```

Generated files:

- `wiki_pages.jsonl` - full extracted wiki pages
- `wiki_chunks.jsonl` - chunked wiki text ready for embeddings/vector search
- `economy_items.jsonl` - poe.ninja item and currency snapshot
- `valdo_rewards.csv` - Valdo reward counts matched to poe.ninja reward prices
- `metadata.json` - source/league/generation metadata
- `assistant_system_prompt.md` - starter system prompt for a PoE assistant

## Search locally

```bash
python3 scripts/poe_knowledge_base.py search "how does Mageblood work"
python3 scripts/poe_knowledge_base.py search "Valdo Puzzle Box profitable"
python3 scripts/poe_knowledge_base.py search "spell suppression"
```

This is a simple dependency-free lexical search. A production AI assistant
should load `wiki_chunks.jsonl` into a vector database and use
`economy_items.jsonl`/`valdo_rewards.csv` as structured tools.

## Notes

- poe.ninja prices are snapshots and can change quickly.
- Official trade pricing requires access to the Path of Exile trade API/session;
  this repo currently prepares the reward/economy side of Valdo profitability.
- Valdo profitability should account for both raw profit and completion risk:

```text
expected profit = completion chance * reward value - map purchase price
```
