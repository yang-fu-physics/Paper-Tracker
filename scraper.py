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

_SYSTEM_PROMPT = """你是一个凝聚态物理专家。任务：
1. 判定列表中的论文是否与"输运(transport)"或"电子输运"研究相关。相关返回 is_transport=true，否则 false。
2. 若相关，必须把英文标题和英文摘要精准且完整地翻译为流畅的学术中文。
3. 提供一个 150 字以内的中文总结 summary_zh。

要求仅返回严格格式的 JSON 数据，格式如下：
{
  "results": [
    {
      "index": <整数，原列表序号>,
      "is_transport": <布尔值>,
      "title_zh": "<如果相关则提供中文标题>",
      "summary_zh": "<中文核心总结，150字以内>",
      "abstract_zh": "<完整客观的全文翻译>"
    }
  ]
}
不要返回 json 之外的任何 Markdown。"""

async def _process_batch(
    papers_batch: List[Dict], start_idx: int, base_url: str, api_key: str, model: str
) -> Tuple[List[Dict], List[str]]:
    texts = []
    for i, p in enumerate(papers_batch):
        texts.append(f"Index: {start_idx + i}\nTitle: {p['title']}\nAbstract: {p['abstract']}\n")
    user_content = "\n---\n".join(texts)
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
    }
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    evaluated_papers = []
    errors = []
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
        content = data["choices"][0]["message"]["content"].strip()
        m = re.search(r"\{[\s\S]*\}", content)
        if m: content = m.group(0)
        parsed = json.loads(content)
        
        for item in parsed.get("results", []):
            idx = item.get("index", -1) - start_idx
            if 0 <= idx < len(papers_batch):
                paper = papers_batch[idx].copy()
                is_trans = item.get("is_transport")
                paper["is_transport"] = 1 if is_trans else 0
                if is_trans:
                    paper["title_zh"] = item.get("title_zh", "")
                    paper["summary_zh"] = item.get("summary_zh", "")
                    paper["abstract_zh"] = item.get("abstract_zh", "")
                paper["abstract_en"] = paper.get("abstract", "") # backup original
                evaluated_papers.append(paper)
    except Exception as e:
        logger.warning(f"Batch AI API failed: {e}")
        errors.append(str(e))
        
    return evaluated_papers, errors

async def filter_and_translate(
    papers: List[Dict], base_url: str, api_key: str, model: str, batch_size: int = 15
) -> Tuple[List[Dict], List[str]]:
    tasks = []
    for i in range(0, len(papers), batch_size):
        batch = papers[i : i + batch_size]
        tasks.append(_process_batch(batch, i, base_url, api_key, model))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)
    final_evaluated, all_errors = [], []
    for r in results:
        if isinstance(r, tuple):
            final_evaluated.extend(r[0])
            all_errors.extend(r[1])
        else:
            all_errors.append(str(r))
            
    return final_evaluated, all_errors
