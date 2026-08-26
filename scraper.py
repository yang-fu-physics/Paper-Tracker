import asyncio
import calendar
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import feedparser
import httpx

import config

logger = logging.getLogger("scraper")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)

# ── 来源配置 ────────────────────────────────────────────────────────
ARXIV_CATEGORIES = [
    "cond-mat.mtrl-sci",
    "cond-mat.mes-hall",
    "cond-mat.quant-gas",
    "cond-mat.str-el",
    "cond-mat.supr-con",
]
ARXIV_RSS = "https://arxiv.org/rss/{category}"

NATURE_JOURNALS: Dict[str, str] = {
    "nature": "Nature",
    "ncomms": "Nature Communications",
    "nphys": "Nature Physics",
    "nmat": "Nature Materials",
    "nnano": "Nature Nanotechnology",
    "natelectron": "Nature Electronics",
}
NATURE_RSS = "https://www.nature.com/{slug}.rss"

APS_RSS: Dict[str, str] = {
    "https://feeds.aps.org/rss/recent/prl.xml": "Physical Review Letters",
    "https://feeds.aps.org/rss/recent/prx.xml": "Physical Review X",
    "https://feeds.aps.org/rss/recent/prb.xml": "Physical Review B",
    "https://feeds.aps.org/rss/recent/prmaterials.xml": "Physical Review Materials",
    "https://feeds.aps.org/rss/recent/prapplied.xml": "Physical Review Applied",
}

SCIENCE_RSS: Dict[str, str] = {
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science": "Science",
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv": "Science Advances",
}

WILEY_RSS: Dict[str, str] = {
    "https://onlinelibrary.wiley.com/feed/15214095/most-recent": "Advanced Materials",
    "https://onlinelibrary.wiley.com/feed/16163028/most-recent": "Advanced Functional Materials",
    "https://onlinelibrary.wiley.com/feed/2199160X/most-recent": "Advanced Electronic Materials",
}

ACS_RSS: Dict[str, str] = {
    "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=nalefd": "Nano Letters",
    "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=ancac3": "ACS Nano",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}



# ── 通用工具 ───────────────────────────────────────────────────────────

async def _fetch_raw(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None

def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()

def _parse_date(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            except Exception:
                pass
    return None

def _is_recent(dt: Optional[datetime], days: int = None) -> bool:
    if days is None:
        days = config.RECENT_DAYS
    if dt is None: return True
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return dt >= cutoff

def _entry_to_paper(entry, journal: str) -> Dict:
    title = _clean_html(entry.get("title", ""))
    abstract = _clean_html(entry.get("summary", entry.get("description", "")))
    link = entry.get("link", "")
    published = _parse_date(entry)
    authors_raw = entry.get("authors", [])
    authors = ", ".join(a.get("name", "") for a in authors_raw) if authors_raw else entry.get("author", "")
    return {
        "title": title, "abstract": abstract, "url": link,
        "journal": journal, "published": published, "authors": authors,
        "title_zh": "", "abstract_zh": "", "summary_zh": "", "abstract_en": ""
    }

async def _fetch_and_parse(url: str, journal: str, filter_date: bool = True) -> List[Dict]:
    raw = await _fetch_raw(url)
    if not raw: return []
    loop = asyncio.get_event_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, raw)
    papers = []
    for entry in feed.entries:
        paper = _entry_to_paper(entry, journal)
        if not paper["title"]: continue
        if filter_date and not _is_recent(paper["published"]): continue
        papers.append(paper)
    return papers


# ── 各来源抓取 ─────────────────────────────────────────────────────────

async def fetch_arxiv_papers() -> List[Dict]:
    tasks = [_fetch_and_parse(ARXIV_RSS.format(category=c), f"arXiv:{c}", False) for c in ARXIV_CATEGORIES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    seen, papers = set(), []
    for res in results:
        if isinstance(res, Exception): continue
        for p in res:
            if p["url"] not in seen:
                seen.add(p["url"])
                papers.append(p)
    logger.info(f"arXiv found {len(papers)} new papers")
    return papers

async def fetch_nature_papers() -> List[Dict]:
    tasks = [_fetch_and_parse(NATURE_RSS.format(slug=s), n) for s, n in NATURE_JOURNALS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    papers = []
    for res in results:
        if not isinstance(res, Exception): papers.extend(res)
    logger.info(f"Nature found {len(papers)} papers")
    return papers

async def fetch_aps_papers() -> List[Dict]:
    tasks = [_fetch_and_parse(u, n) for u, n in APS_RSS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    papers = []
    for res in results:
        if not isinstance(res, Exception): papers.extend(res)
    logger.info(f"APS found {len(papers)} papers")
    return papers

async def fetch_science_papers() -> List[Dict]:
    tasks = [_fetch_and_parse(u, n) for u, n in SCIENCE_RSS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    papers = []
    for res in results:
        if not isinstance(res, Exception): papers.extend(res)
    logger.info(f"Science found {len(papers)} papers")
    return papers

async def fetch_wiley_papers() -> List[Dict]:
    tasks = [_fetch_and_parse(u, n) for u, n in WILEY_RSS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    papers = []
    for res in results:
        if not isinstance(res, Exception): papers.extend(res)
    logger.info(f"Wiley found {len(papers)} papers")
    return papers

async def fetch_acs_papers() -> List[Dict]:
    tasks = [_fetch_and_parse(u, n) for u, n in ACS_RSS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    papers = []
    for res in results:
        if not isinstance(res, Exception): papers.extend(res)
    logger.info(f"ACS found {len(papers)} papers")
    return papers

async def fetch_all_papers() -> List[Dict]:
    tasks = [
        fetch_arxiv_papers(),
        fetch_nature_papers(),
        fetch_aps_papers(),
        fetch_science_papers(),
        fetch_wiley_papers(),
        fetch_acs_papers(),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_papers = []
    for src in results:
        if isinstance(src, list): all_papers.extend(src)
    return all_papers


# ── AI 判定逻辑 ────────────────────────────────────────────────────────


class ModelResponseError(ValueError):
    """The model returned syntactically valid but incomplete batch data."""


def _escape_invalid_json_backslashes(text: str) -> str:
    """Repair bare backslashes inside JSON strings using math context.

    Valid JSON escapes are always preserved in ordinary text. Ambiguous escapes
    such as ``\\n`` or ``\\t`` are treated as LaTeX only inside ``$...$``,
    ``\\(...\\)``, ``\\[...\\]``, or when a multi-letter command is followed by
    a TeX argument/operator marker. Invalid JSON escapes are repaired anywhere.
    """
    out = []
    i = 0
    in_string = False
    dollar_math = False
    latex_math_depth = 0
    hexdigits = set("0123456789abcdefABCDEF")
    control_escapes = {"b", "f", "n", "r", "t"}
    ascii_letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    while i < len(text):
        char = text[i]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
                dollar_math = False
                latex_math_depth = 0
            i += 1
            continue

        if char == '"':
            out.append(char)
            in_string = False
            dollar_math = False
            latex_math_depth = 0
            i += 1
            continue
        if char == "$":
            out.append(char)
            dollar_math = not dollar_math
            i += 1
            continue
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(text):
            out.append("\\\\")
            i += 1
            continue

        nxt = text[i + 1]
        if nxt in {'"', "\\", "/"}:
            out.extend(("\\", nxt))
            i += 2
            continue
        if nxt == "u" and i + 5 < len(text) and all(
            ch in hexdigits for ch in text[i + 2 : i + 6]
        ):
            out.append(text[i : i + 6])
            i += 6
            continue
        if nxt in "([":
            latex_math_depth += 1
            out.append("\\\\")
            i += 1
            continue
        if nxt in ")]":
            latex_math_depth = max(0, latex_math_depth - 1)
            out.append("\\\\")
            i += 1
            continue
        if nxt in control_escapes:
            end = i + 2
            while end < len(text) and text[end] in ascii_letters:
                end += 1
            token = text[i + 1 : end]
            following = text[end] if end < len(text) else ""
            in_math = dollar_math or latex_math_depth > 0
            command_has_tex_marker = len(token) > 1 and following in "{[_^"
            if not in_math and not command_has_tex_marker:
                out.extend(("\\", nxt))
                i += 2
                continue

        # Invalid JSON escape, or an ambiguous command in explicit TeX context.
        out.append("\\\\")
        i += 1

    return "".join(out)


def _parse_model_json(content: str) -> Dict:
    content = content.strip()
    match = re.search(r"\{[\s\S]*\}", content)
    if match:
        content = match.group(0)
    repaired = _escape_invalid_json_backslashes(content)
    return json.loads(repaired)


async def _process_batch(
    papers_batch: List[Dict], start_idx: int, base_url: str, api_key: str, model: str,
    *, max_attempts: int = None, base_delay: float = None,
    client_factory=httpx.AsyncClient, sleep_func=asyncio.sleep,
) -> Tuple[List[Dict], List[str], int]:
    texts = []
    for i, p in enumerate(papers_batch):
        texts.append(f"Index: {start_idx + i}\nTitle: {p['title']}\nAbstract: {p['abstract']}\n")
    user_content = "\n---\n".join(texts)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": config.FILTER_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if max_attempts is None:
        max_attempts = getattr(config, "AI_MAX_ATTEMPTS", 5)
    if base_delay is None:
        base_delay = getattr(config, "AI_RETRY_BASE_DELAY", 10.0)
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            async with client_factory(timeout=120) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            if not isinstance(data, dict):
                raise ModelResponseError("API response is not an object")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ModelResponseError("API response has no choices")
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ModelResponseError("API response choice has no message object")
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelResponseError("API response message content is empty or not a string")

            parsed = _parse_model_json(content)
            if not isinstance(parsed, dict):
                raise ModelResponseError("model JSON root is not an object")
            results = parsed.get("results")
            if not isinstance(results, list):
                raise ModelResponseError("response field 'results' is not a list")

            expected_indexes = set(range(start_idx, start_idx + len(papers_batch)))
            seen_indexes = set()
            evaluated_papers = []
            for item in results:
                if not isinstance(item, dict):
                    raise ModelResponseError("each result must be an object")
                absolute_idx = item.get("index")
                if type(absolute_idx) is not int:
                    raise ModelResponseError(
                        f"result index must be an integer, got {type(absolute_idx).__name__}"
                    )
                if absolute_idx not in expected_indexes or absolute_idx in seen_indexes:
                    raise ModelResponseError(
                        f"unexpected or duplicate result index: {absolute_idx!r}"
                    )
                is_trans = item.get("is_transport")
                if type(is_trans) is not bool:
                    raise ModelResponseError(
                        f"is_transport for index {absolute_idx} must be a boolean"
                    )
                if is_trans:
                    for field in ("title_zh", "summary_zh", "abstract_zh"):
                        value = item.get(field)
                        if not isinstance(value, str) or not value.strip():
                            raise ModelResponseError(
                                f"related result index {absolute_idx} has invalid {field}"
                            )

                seen_indexes.add(absolute_idx)
                idx = absolute_idx - start_idx
                paper = papers_batch[idx].copy()
                paper["is_transport"] = 1 if is_trans else 0
                if is_trans:
                    paper["title_zh"] = item["title_zh"]
                    paper["summary_zh"] = item["summary_zh"]
                    paper["abstract_zh"] = item["abstract_zh"]
                paper["abstract_en"] = paper.get("abstract", "")
                evaluated_papers.append(paper)
            if seen_indexes != expected_indexes:
                missing = sorted(expected_indexes - seen_indexes)
                raise ModelResponseError(f"missing result indexes: {missing}")
            return evaluated_papers, [], attempt - 1
        except Exception as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            retryable = (
                status == 429
                or (status is not None and 500 <= status <= 599)
                or isinstance(
                    exc, (httpx.RequestError, json.JSONDecodeError, ModelResponseError)
                )
            )
            if retryable and attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Batch %s-%s attempt %s/%s failed (%s); retrying in %.1fs",
                    start_idx,
                    start_idx + len(papers_batch) - 1,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await sleep_func(delay)
                continue
            message = (
                f"batch {start_idx}-{start_idx + len(papers_batch) - 1} "
                f"failed after {attempt} attempt(s): {exc}"
            )
            logger.warning(message)
            return [], [message], attempt - 1

    raise AssertionError("unreachable")

async def filter_and_translate(
    papers: List[Dict], base_url: str, api_key: str, model: str,
    batch_size: int = 15, max_concurrency: int = None,
) -> Tuple[List[Dict], List[str], Dict]:
    if max_concurrency is None:
        max_concurrency = getattr(config, "AI_MAX_CONCURRENCY", 2)
    if type(max_concurrency) is not int or not 2 <= max_concurrency <= 4:
        raise ValueError("max_concurrency must be an integer between 2 and 4")

    semaphore = asyncio.Semaphore(max_concurrency)
    batches = []

    async def run_batch(batch: List[Dict], start_idx: int):
        async with semaphore:
            return await _process_batch(batch, start_idx, base_url, api_key, model)

    tasks = []
    for i in range(0, len(papers), batch_size):
        batch = papers[i : i + batch_size]
        batches.append(batch)
        tasks.append(run_batch(batch, i))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    final_evaluated, all_errors = [], []
    stats = {
        "total_batches": len(batches),
        "successful_batches": 0,
        "failed_batches": 0,
        "evaluated_papers": 0,
        "retry_pending_papers": 0,
        "retry_attempts": 0,
    }
    for batch, result in zip(batches, results):
        if isinstance(result, tuple):
            evaluated, errors, retry_attempts = result
            final_evaluated.extend(evaluated)
            all_errors.extend(errors)
            stats["retry_attempts"] += retry_attempts
            if errors:
                stats["failed_batches"] += 1
                stats["retry_pending_papers"] += len(batch)
            else:
                stats["successful_batches"] += 1
        else:
            all_errors.append(str(result))
            stats["failed_batches"] += 1
            stats["retry_pending_papers"] += len(batch)
    stats["evaluated_papers"] = len(final_evaluated)

    return final_evaluated, all_errors, stats
