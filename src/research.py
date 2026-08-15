"""Free web research via DuckDuckGo — no API key required."""

from ddgs import DDGS

from .llm_optimize import research_max_results

DEFAULT_KEYWORDS = 'privacy OR "data governance" OR anonymization'


def research_prospect(
    prospect: str,
    company: str | None = None,
    max_results: int | None = None,
    keywords: str | None = None,
) -> list[dict]:
    """
    Search the web for context about a prospect or company.
    Returns list of {title, snippet, url}.
    """
    if max_results is None:
        max_results = research_max_results()
    query = company or prospect
    search_q = f'"{query}" {keywords or DEFAULT_KEYWORDS}'

    results = []
    try:
        with DDGS() as ddgs:
            for hit in ddgs.text(search_q, max_results=max_results):
                results.append({
                    "title": hit.get("title", ""),
                    "snippet": hit.get("body", ""),
                    "url": hit.get("href", ""),
                })
    except Exception:
        return []

    return results


def format_research(results: list[dict]) -> str:
    if not results:
        return "No web research results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet'][:220]}")
        if r["url"]:
            lines.append(f"   {r['url']}")
    return "\n".join(lines)
