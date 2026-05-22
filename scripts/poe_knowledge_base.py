#!/usr/bin/env python3
"""Build and search a local Path of Exile knowledge base.

The generated files are intentionally plain JSONL/CSV so they can be used by
either a simple local search tool or a future embedding/vector database.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import textwrap
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


WIKI_API = "https://www.poewiki.net/w/api.php"
WIKI_PAGE_URL = "https://www.poewiki.net/wiki/"
POE_NINJA_BASE = "https://poe.ninja/poe1/api"
USER_AGENT = "PoEKnowledgeBase/0.1 (+https://github.com/poe-valdo-prices)"

DEFAULT_OUTPUT_DIR = Path("data/poe_knowledge")

CURATED_WIKI_TITLES = [
    "Path of Exile",
    "Game mechanics",
    "Character class",
    "Ascendancy class",
    "Passive skill",
    "Keystone passive skill",
    "Atlas of Worlds",
    "Atlas passive skill",
    "Map",
    "Map device",
    "Valdo's Puzzle Box",
    "List of Valdo's Puzzle Box foil maps",
    "Voidborn Reliquary Key",
    "League",
    "League mechanic",
    "Currency",
    "Crafting",
    "Vendor recipe system",
    "Modifier",
    "Item",
    "Unique item",
    "Skill gem",
    "Support gem",
    "Vaal skill gem",
    "Awakened support gem",
    "Quality",
    "Corrupted",
    "Influenced item",
    "Eldritch implicit modifier",
    "Fractured item",
    "Synthesised item",
    "Essence",
    "Fossil",
    "Resonator",
    "Harvest crafting",
    "Betrayal",
    "Bestiary",
    "Incursion",
    "Delve",
    "Heist",
    "Expedition",
    "Blight",
    "Delirium",
    "Ritual",
    "Ultimatum",
    "Breach",
    "Legion",
    "Sanctum",
    "Affliction league",
    "Necropolis league",
    "Settlers league",
    "Damage",
    "Hit",
    "Ailment",
    "Resistance",
    "Armour",
    "Evasion",
    "Energy shield",
    "Block",
    "Suppression",
    "Leech",
    "Reservation",
    "Flask",
    "Minion",
    "Totem",
    "Mine",
    "Trap",
    "Projectile",
    "Area of effect",
    "Critical strike",
    "Mageblood",
    "Headhunter",
    "Nimis",
    "Progenesis",
    "Kalandra's Touch",
    "Ashes of the Stars",
    "Original Sin",
    "The Squire",
    "Watcher's Eye",
    "Voices",
]

CURATED_WIKI_CATEGORIES = [
    "Unique items",
    "Currency items",
    "Skill gems",
    "Support gems",
    "Maps",
    "League mechanics",
    "Atlas passive skills",
    "Crafting",
]

STASH_OVERVIEW_TYPES = [
    "UniqueWeapon",
    "UniqueArmour",
    "UniqueAccessory",
    "UniqueFlask",
    "UniqueJewel",
    "UniqueMap",
    "SkillGem",
    "ClusterJewel",
    "Invitation",
    "Memory",
]

EXCHANGE_OVERVIEW_TYPES = [
    "Currency",
    "Fragment",
]

VALDO_ALIASES = {
    "Cloak of Flames": "Cloak of Flame",
    "Fulcrum": "The Fulcrum",
}


def request_json(url: str, *, retries: int = 3, delay: float = 0.25) -> dict[str, Any]:
    """Fetch JSON with light retry/backoff and a clear user agent."""

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - preserve the original exception in the final error
            last_error = exc
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"Failed to fetch JSON from {url}: {last_error}") from last_error


def wiki_api(params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode({**params, "format": "json"})
    return request_json(f"{WIKI_API}?{query}")


def clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonical_reward_name(value: str) -> str:
    name = clean_title(value)
    return VALDO_ALIASES.get(name, name)


def wiki_url(title: str) -> str:
    return WIKI_PAGE_URL + quote(title.replace(" ", "_"), safe="/'()")


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def chunk_text(text: str, *, max_chars: int = 1_600, overlap: int = 180) -> list[str]:
    """Split text into mostly paragraph-respecting chunks."""

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + max_chars].strip())
            start += max_chars - overlap
        current = ""
    if current:
        chunks.append(current)
    return chunks


def batched(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def fetch_category_members(category: str, limit: int) -> list[str]:
    titles: list[str] = []
    cmcontinue: str | None = None
    while len(titles) < limit:
        params: dict[str, Any] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmnamespace": 0,
            "cmlimit": min(50, limit - len(titles)),
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        data = wiki_api(params)
        titles.extend(member["title"] for member in data.get("query", {}).get("categorymembers", []))
        cmcontinue = data.get("continue", {}).get("cmcontinue")
        if not cmcontinue:
            break
    return [clean_title(title) for title in titles]


def fetch_wiki_pages(titles: list[str]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for batch in batched(titles, 20):
        data = wiki_api(
            {
                "action": "query",
                "prop": "extracts|categories",
                "explaintext": 1,
                "cllimit": 25,
                "redirects": 1,
                "titles": "|".join(batch),
            }
        )
        for page in data.get("query", {}).get("pages", {}).values():
            if "missing" in page:
                continue
            extract = clean_extract(page.get("extract", ""))
            if not extract:
                continue
            title = clean_title(page["title"])
            pages.append(
                {
                    "source": "poewiki",
                    "title": title,
                    "url": wiki_url(title),
                    "categories": [
                        category["title"].replace("Category:", "")
                        for category in page.get("categories", [])
                    ],
                    "extract": extract,
                }
            )
        time.sleep(0.15)
    pages.sort(key=lambda page: page["title"].lower())
    return pages


def clean_extract(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def fetch_current_league() -> str:
    state = request_json(f"{POE_NINJA_BASE}/data/index-state")
    leagues = state.get("economyLeagues", [])
    if not leagues:
        raise RuntimeError("poe.ninja did not return economy leagues")
    return leagues[0]["name"]


def fetch_poe_ninja_items(league: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item_type in STASH_OVERVIEW_TYPES:
        url = (
            f"{POE_NINJA_BASE}/economy/stash/current/item/overview?"
            f"{urlencode({'league': league, 'type': item_type})}"
        )
        try:
            data = request_json(url)
        except RuntimeError:
            continue
        for line in data.get("lines", []):
            items.append(normalize_poe_ninja_line(line, league=league, price_type=item_type, mode="stash"))
        time.sleep(0.1)

    for item_type in EXCHANGE_OVERVIEW_TYPES:
        url = (
            f"{POE_NINJA_BASE}/economy/exchange/current/overview?"
            f"{urlencode({'league': league, 'type': item_type})}"
        )
        try:
            data = request_json(url)
        except RuntimeError:
            continue
        core_items = {item["id"]: item for item in data.get("core", {}).get("items", [])}
        for line in data.get("lines", []):
            item_id = line.get("item")
            item = core_items.get(item_id, {})
            items.append(
                {
                    "source": "poe.ninja",
                    "league": league,
                    "mode": "exchange",
                    "type": item_type,
                    "name": item.get("name") or item_id,
                    "details_id": item.get("detailsId"),
                    "chaos_value": line.get("chaosEquivalent"),
                    "divine_value": line.get("divineEquivalent"),
                    "listing_count": line.get("listingCount") or line.get("count"),
                    "raw": line,
                }
            )
        time.sleep(0.1)
    return items


def normalize_poe_ninja_line(
    line: dict[str, Any], *, league: str, price_type: str, mode: str
) -> dict[str, Any]:
    return {
        "source": "poe.ninja",
        "league": league,
        "mode": mode,
        "type": price_type,
        "name": line.get("name"),
        "base_type": line.get("baseType"),
        "variant": line.get("variant"),
        "details_id": line.get("detailsId"),
        "item_type": line.get("itemType"),
        "chaos_value": line.get("chaosValue"),
        "divine_value": line.get("divineValue"),
        "listing_count": line.get("listingCount") or line.get("count"),
        "raw": line,
    }


def fetch_valdo_rewards() -> Counter[str]:
    data = wiki_api(
        {
            "action": "parse",
            "page": "List of Valdo's Puzzle Box foil maps",
            "prop": "wikitext",
        }
    )
    wikitext = data["parse"]["wikitext"]["*"]
    rewards: Counter[str] = Counter()
    for line in wikitext.splitlines():
        if not line.startswith("| <span") or "|| [[" not in line:
            continue
        columns = line.split("||")
        if len(columns) < 4:
            continue
        match = re.search(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", columns[3])
        if match:
            rewards[canonical_reward_name(match.group(1))] += 1
    return rewards


def chaos_per_divine(economy_items: list[dict[str, Any]]) -> float:
    for item in economy_items:
        if item["mode"] == "exchange" and item["name"] == "Divine Orb":
            chaos_value = item.get("chaos_value")
            if isinstance(chaos_value, (int, float)) and chaos_value > 0:
                return float(chaos_value)
    # poe.ninja item values already include chaos values; this is only for display.
    return 1.0


def format_price(chaos_value: float | None, c_per_div: float) -> str:
    if chaos_value is None:
        return "N/A"
    if c_per_div > 0 and chaos_value >= c_per_div:
        divine = chaos_value / c_per_div
        if divine >= 100:
            return f"{divine:.0f} div"
        if divine >= 10:
            return f"{divine:.1f}".rstrip("0").rstrip(".") + " div"
        return f"{divine:.2f}".rstrip("0").rstrip(".") + " div"
    if chaos_value >= 10:
        return f"{chaos_value:.0f}c"
    if chaos_value >= 1:
        return f"{chaos_value:.1f}".rstrip("0").rstrip(".") + "c"
    return f"{chaos_value:.2f}".rstrip("0").rstrip(".") + "c"


def item_chaos_value(item: dict[str, Any], c_per_div: float) -> float | None:
    chaos_value = item.get("chaos_value")
    if isinstance(chaos_value, (int, float)):
        return float(chaos_value)
    divine_value = item.get("divine_value")
    if isinstance(divine_value, (int, float)):
        return float(divine_value) * c_per_div
    return None


def build_valdo_price_rows(
    rewards: Counter[str], economy_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    c_per_div = chaos_per_divine(economy_items)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in economy_items:
        if item["mode"] == "stash" and item.get("name"):
            by_name[clean_title(item["name"])].append(item)

    rows: list[dict[str, Any]] = []
    for reward, wiki_entries in rewards.items():
        candidates = by_name.get(reward, [])
        priced = [(candidate, item_chaos_value(candidate, c_per_div)) for candidate in candidates]
        priced = [(candidate, value) for candidate, value in priced if value is not None]
        listing_count = sum(candidate.get("listing_count") or 0 for candidate in candidates)

        if not candidates:
            rows.append(
                {
                    "reward": reward,
                    "wiki_entries": wiki_entries,
                    "price_min_chaos": "",
                    "price_max_chaos": "",
                    "price_display": "N/A",
                    "listing_count": "",
                    "note": "not on poe.ninja current league",
                    "sort_price": -1,
                }
            )
            continue

        if not priced:
            rows.append(
                {
                    "reward": reward,
                    "wiki_entries": wiki_entries,
                    "price_min_chaos": "",
                    "price_max_chaos": "",
                    "price_display": "N/A",
                    "listing_count": listing_count,
                    "note": "listed but no price",
                    "sort_price": -1,
                }
            )
            continue

        # Prefer unvarianted entries for the normal item; otherwise show the full variant range.
        unvarianted = [(candidate, value) for candidate, value in priced if not candidate.get("variant")]
        selected = unvarianted or priced
        values = [float(value) for _, value in selected]
        price_min = min(values)
        price_max = max(values)
        if math.isclose(price_min, price_max):
            display = format_price(price_max, c_per_div)
        else:
            display = f"{format_price(price_min, c_per_div)}-{format_price(price_max, c_per_div)}"

        note = f"{len(candidates)} variants" if len(candidates) > 1 else ""
        rows.append(
            {
                "reward": reward,
                "wiki_entries": wiki_entries,
                "price_min_chaos": round(price_min, 4),
                "price_max_chaos": round(price_max, 4),
                "price_display": display,
                "listing_count": listing_count,
                "note": note,
                "sort_price": price_max,
            }
        )

    rows.sort(key=lambda row: (row["sort_price"], row["wiki_entries"], row["reward"]), reverse=True)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def build_knowledge_base(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    category_titles: list[str] = []
    for category in CURATED_WIKI_CATEGORIES:
        category_titles.extend(fetch_category_members(category, args.category_limit))

    curated_titles = dedupe_titles(CURATED_WIKI_TITLES)
    expanded_titles = [title for title in dedupe_titles(category_titles) if title not in set(curated_titles)]
    if args.max_wiki_pages:
        remaining_slots = max(args.max_wiki_pages - len(curated_titles), 0)
        all_titles = curated_titles + expanded_titles[:remaining_slots]
    else:
        all_titles = curated_titles + expanded_titles

    wiki_pages = fetch_wiki_pages(all_titles)
    chunks: list[dict[str, Any]] = []
    for page in wiki_pages:
        for index, chunk in enumerate(chunk_text(page["extract"])):
            chunks.append(
                {
                    "id": f"poewiki:{page['title']}:{index}",
                    "source": "poewiki",
                    "title": page["title"],
                    "url": page["url"],
                    "text": chunk,
                }
            )

    league = args.league or fetch_current_league()
    economy_items = fetch_poe_ninja_items(league)
    valdo_rewards = fetch_valdo_rewards()
    valdo_rows = build_valdo_price_rows(valdo_rewards, economy_items)

    wiki_page_count = write_jsonl(output_dir / "wiki_pages.jsonl", wiki_pages)
    wiki_chunk_count = write_jsonl(output_dir / "wiki_chunks.jsonl", chunks)
    economy_count = write_jsonl(output_dir / "economy_items.jsonl", economy_items)

    with (output_dir / "valdo_rewards.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "reward",
            "wiki_entries",
            "price_min_chaos",
            "price_max_chaos",
            "price_display",
            "listing_count",
            "note",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in valdo_rows:
            writer.writerow({field: row[field] for field in fieldnames})

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league": league,
        "wiki_pages": wiki_page_count,
        "wiki_chunks": wiki_chunk_count,
        "economy_items": economy_count,
        "valdo_rewards": len(valdo_rows),
        "wiki_category_limit": args.category_limit,
        "max_wiki_pages": args.max_wiki_pages,
        "sources": {
            "poewiki": "https://www.poewiki.net/",
            "poe_ninja": "https://poe.ninja/",
        },
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    (output_dir / "assistant_system_prompt.md").write_text(
        build_system_prompt(metadata), encoding="utf-8"
    )

    print(json.dumps(metadata, indent=2))


def build_system_prompt(metadata: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""\
        # Path of Exile Assistant System Prompt

        You are a Path of Exile assistant. Use the local knowledge base first:

        - `wiki_chunks.jsonl` for PoE Wiki mechanics, items, systems, and league explanations.
        - `economy_items.jsonl` for poe.ninja economy data from league `{metadata['league']}`.
        - `valdo_rewards.csv` for Valdo's Puzzle Box reward counts and reward prices.

        Rules:

        1. Prefer retrieved facts over memory.
        2. Include source titles/URLs when giving factual game-mechanics answers.
        3. Treat poe.ninja prices as snapshots, not guarantees.
        4. For profitability, subtract acquisition cost from reward value and adjust for completion risk.
        5. If official trade data is unavailable, say so instead of inventing prices.
        6. Mention build-breaking map mods when discussing Valdo map safety.
        """
    )


def dedupe_titles(titles: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for title in titles:
        cleaned = clean_title(title)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


@dataclass
class SearchDocument:
    title: str
    source: str
    url: str
    text: str


def load_search_documents(data_dir: Path) -> list[SearchDocument]:
    documents: list[SearchDocument] = []
    chunks_path = data_dir / "wiki_chunks.jsonl"
    if chunks_path.exists():
        for row in read_jsonl(chunks_path):
            documents.append(
                SearchDocument(
                    title=row["title"],
                    source=row["source"],
                    url=row.get("url", ""),
                    text=row["text"],
                )
            )

    economy_path = data_dir / "economy_items.jsonl"
    if economy_path.exists():
        for row in read_jsonl(economy_path):
            parts = [
                row.get("name") or "",
                row.get("type") or "",
                row.get("variant") or "",
                row.get("base_type") or "",
                f"chaos value {row.get('chaos_value')}",
                f"divine value {row.get('divine_value')}",
            ]
            documents.append(
                SearchDocument(
                    title=row.get("name") or "poe.ninja item",
                    source=f"poe.ninja:{row.get('type')}",
                    url="https://poe.ninja/",
                    text=" | ".join(str(part) for part in parts if part),
                )
            )
    return documents


def search_knowledge_base(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    documents = load_search_documents(data_dir)
    if not documents:
        raise SystemExit(f"No knowledge base found in {data_dir}. Run `build` first.")

    query_tokens = tokenize(args.query)
    if not query_tokens:
        raise SystemExit("Search query is empty.")

    document_frequencies: Counter[str] = Counter()
    tokenized_documents: list[list[str]] = []
    for document in documents:
        tokens = tokenize(f"{document.title} {document.text}")
        tokenized_documents.append(tokens)
        document_frequencies.update(set(tokens))

    scores: list[tuple[float, SearchDocument]] = []
    document_count = len(documents)
    query_phrase = args.query.lower()
    for document, tokens in zip(documents, tokenized_documents):
        token_counts = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            tf = token_counts[token]
            if tf == 0:
                continue
            idf = math.log((document_count + 1) / (document_frequencies[token] + 1)) + 1
            score += (1 + math.log(tf)) * idf
        haystack = f"{document.title}\n{document.text}".lower()
        title = document.title.lower()
        if query_phrase in title:
            score += 15
        if all(token in tokenize(document.title) for token in query_tokens):
            score += 8
        if query_phrase in haystack:
            score += 8
        if score > 0:
            scores.append((score, document))

    scores.sort(key=lambda item: item[0], reverse=True)
    for rank, (score, document) in enumerate(scores[: args.limit], start=1):
        snippet = document.text.replace("\n", " ")
        if len(snippet) > 320:
            snippet = snippet[:317].rstrip() + "..."
        print(f"{rank}. [{document.source}] {document.title} (score {score:.2f})")
        if document.url:
            print(f"   {document.url}")
        print(f"   {snippet}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Fetch PoE Wiki and poe.ninja data")
    build_parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    build_parser.add_argument("--league", default=None, help="poe.ninja league name; defaults to current")
    build_parser.add_argument(
        "--category-limit",
        type=int,
        default=30,
        help="Number of pages to pull from each curated wiki category",
    )
    build_parser.add_argument(
        "--max-wiki-pages",
        type=int,
        default=220,
        help="Maximum number of wiki page extracts to fetch",
    )
    build_parser.set_defaults(func=build_knowledge_base)

    search_parser = subparsers.add_parser("search", help="Search the generated knowledge base")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--data-dir", default=str(DEFAULT_OUTPUT_DIR), help="Knowledge data directory")
    search_parser.add_argument("--limit", type=int, default=8, help="Number of results")
    search_parser.set_defaults(func=search_knowledge_base)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
