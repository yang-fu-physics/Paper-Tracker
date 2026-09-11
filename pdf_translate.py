"""
arXiv source (LaTeX) -> translate (via Gemini) -> recompile PDF
Fallback: PDF -> LaTeX (via doc2x) -> translate -> recompile
"""
import os, re, time, tarfile, subprocess, shutil, tempfile, glob, requests, logging, zipfile, hashlib
from pathlib import Path
from stat import S_ISLNK

import config
from api_concurrency import api_call_slot

logger = logging.getLogger("pdf_translate")


def _file_sha256(pdf_path: str) -> str:
    digest = hashlib.sha256()
    with open(pdf_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(zip_path: str, target_dir: str):
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts:
                raise RuntimeError("doc2x archive contains an unsafe path")
            mode = info.external_attr >> 16
            if S_ISLNK(mode):
                raise RuntimeError("doc2x archive contains a symbolic link")
            destination = (target / name).resolve()
            try:
                destination.relative_to(target)
            except ValueError as exc:
                raise RuntimeError("doc2x archive escapes the workspace") from exc
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


# ── doc2x ──────────────────────────────────────────────────────────────────

def _doc2x_headers():
    return {"Authorization": f"Bearer {config.DOC2X_API_KEY}"}


def pdf_to_latex_dir(pdf_path: str, work_dir: str, source_sha256: str | None = None) -> str:
    """Convert PDF to LaTeX, reusing a cache only for the same source PDF."""
    tex_dir = os.path.join(work_dir, "tex_src")
    marker_path = os.path.join(work_dir, ".source_sha256")
    if os.path.isdir(tex_dir) and glob.glob(os.path.join(tex_dir, "**/*.tex"), recursive=True):
        if source_sha256 is None:
            logger.info(f"[doc2x] using cached tex_src at {tex_dir}")
            return tex_dir
        try:
            cached_sha256 = Path(marker_path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            cached_sha256 = ""
        if cached_sha256 == source_sha256:
            logger.info(f"[doc2x] using source-matched tex_src at {tex_dir}")
            return tex_dir
        logger.info("[doc2x] source changed; invalidating stale tex_src")
        shutil.rmtree(tex_dir, ignore_errors=True)
    logger.info("[doc2x] pre-upload request")
    r = requests.post(f"{config.DOC2X_BASE_URL}/parse/preupload", headers=_doc2x_headers(), timeout=15)
    r.raise_for_status()
    d = r.json()["data"]
    upload_url, uid = d["url"], d["uid"]
    logger.info(f"[doc2x] uid={uid}, uploading {pdf_path}")

    with open(pdf_path, "rb") as f:
        requests.put(upload_url, data=f, timeout=60).raise_for_status()
    logger.info("[doc2x] upload done, polling parse status")

    for i in range(60):
        r = requests.get(f"{config.DOC2X_BASE_URL}/parse/status", headers=_doc2x_headers(),
                         params={"uid": uid}, timeout=15)
        st = r.json()["data"]["status"]
        logger.info(f"[doc2x] parse status={st} (attempt {i+1})")
        if st == "success":
            break
        if st != "processing":
            raise RuntimeError(f"doc2x parse error: {r.json()}")
        time.sleep(5)
    else:
        raise RuntimeError("doc2x parse timeout")

    logger.info("[doc2x] submitting tex conversion")
    requests.post(f"{config.DOC2X_BASE_URL}/convert/parse", headers=_doc2x_headers(),
                  json={"uid": uid, "to": "tex", "formula_mode": "dollar", "filename": "output"},
                  timeout=15).raise_for_status()

    for i in range(36):
        r = requests.get(f"{config.DOC2X_BASE_URL}/convert/parse/result", headers=_doc2x_headers(),
                         params={"uid": uid}, timeout=15)
        d = r.json()["data"]
        logger.info(f"[doc2x] convert status={d['status']} (attempt {i+1})")
        if d["status"] == "success":
            break
        if d["status"] != "processing":
            raise RuntimeError(f"doc2x convert error: {r.json()}")
        time.sleep(3)
    else:
        raise RuntimeError("doc2x convert timeout")

    zip_path = os.path.join(work_dir, "doc2x_output.zip")
    logger.info(f"[doc2x] downloading result zip to {zip_path}")
    res = requests.get(d["url"], timeout=60)
    with open(zip_path, "wb") as f:
        f.write(res.content)

    _safe_extract_zip(zip_path, tex_dir)
    if source_sha256:
        with open(marker_path, "w", encoding="ascii") as marker:
            marker.write(source_sha256)
    logger.info(f"[doc2x] extracted to {tex_dir}")
    return tex_dir


# ── translation ────────────────────────────────────────────────────────────

def _split_tex(content: str, chunk_size: int = None):
    if chunk_size is None:
        chunk_size = config.TRANSLATE_CHUNK_SIZE
    paragraphs = content.split("\n\n")
    chunks, cur = [], ""
    for p in paragraphs:
        if len(cur) + len(p) > chunk_size and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = cur + "\n\n" + p if cur else p
    if cur:
        chunks.append(cur)
    return chunks

def _extract_and_mask_tex(content: str):
    preamble = ""
    match = re.search(r'^(.*?\\begin\{document\})', content, flags=re.DOTALL)
    if match:
        preamble = match.group(1)
        content = content[len(preamble):]
    
    blocks = []
    def repl(m):
        idx = len(blocks)
        blocks.append(m.group(0))
        return f"\n\n___TEX_BLOCK_{idx}___\n\n"
        
    content = re.sub(r'\\begin\{(equation|equation\*|align|align\*|figure|figure\*|table|table\*|tikzpicture|lstlisting|algorithm|multline|multline\*|wrapfigure|wrapfigure\*|minipage|minipage\*)\}.*?\\end\{\1\}', repl, content, flags=re.DOTALL)
    content = re.sub(r'\$\$[^\$]+\$\$', repl, content, flags=re.DOTALL)
    content = re.sub(r'\\\[.*?\\\]', repl, content, flags=re.DOTALL)
    
    return preamble, content, blocks

def _restore_masks(content: str, blocks: list):
    for i, block in enumerate(blocks):
        content = content.replace(f"___TEX_BLOCK_{i}___", block)
    return content


def _translate_chunk(chunk: str, idx: int, total: int, max_retries: int = 3) -> str:
    logger.info(f"[translate] chunk {idx+1}/{total} ({len(chunk)} chars)")
    payload = {
        "model": config.TRANSLATE_MODEL,
        "messages": [
            {"role": "system", "content": "You are a professional academic translator. Translate the following LaTeX content from English to Chinese. Rules: 1) Do NOT modify any LaTeX commands, environments, or math formulas. 2) Answer ONLY with the translated text, do NOT wrap it in markdown code fences. 3) Maintain the original paragraph structure."},
            {"role": "user", "content": chunk}
        ],
        "temperature": 0.2,
        "stream": True,
    }
    for attempt in range(1, max_retries + 1):
        try:
            with api_call_slot():
                r = requests.post(
                    f"{config.TRANSLATE_BASE_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {config.TRANSLATE_API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=(10, 120),  # (connect_timeout, read_timeout per chunk)
                    stream=True,
                )
                r.raise_for_status()
                # collect streamed SSE chunks
                import json as _json
                parts = []
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        obj = _json.loads(data_str)
                        delta = obj["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            parts.append(content)
                    except (_json.JSONDecodeError, KeyError, IndexError):
                        continue
            result = "".join(parts)
            logger.info(f"[translate] chunk {idx+1}/{total} done ({len(result)} chars)")
            return result
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"[translate] chunk {idx+1}/{total} attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                raise
            wait = 10 * attempt
            logger.info(f"[translate] retrying in {wait}s...")
            time.sleep(wait)


def _postprocess_translated(content: str) -> str:
    """Clean up common LLM translation artifacts."""
    # Strip markdown code fences
    content = re.sub(r'^```(?:latex|tex)?\s*\n?', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n?```\s*$', '', content, flags=re.MULTILINE)
    # Remove all \end{document} except the very last one
    parts = content.split(r'\end{document}')
    if len(parts) > 2:
        logger.warning(f"[postprocess] removed {len(parts)-2} spurious \\end{{document}}")
        content = ''.join(parts[:-1]) + r'\end{document}'
    # Inject CJK support if translated to Chinese but missing ctex/xeCJK
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', content))
    has_cjk_pkg = bool(re.search(r'\\usepackage.*\{(ctex|xeCJK)\}', content))
    if has_chinese and not has_cjk_pkg:
        # Safely insert before \begin{document} to avoid breaking multi-line \documentclass
        if r'\begin{document}' in content:
            content = content.replace(r'\begin{document}', '\\usepackage{fontspec}\n\\usepackage{ctex}\n\\begin{document}', 1)
        else:
            content = re.sub(
                r'(\\documentclass.*?\{[^}]+\})',
                r'\1\n\\usepackage{fontspec}\n\\usepackage{ctex}\n',
                content, count=1, flags=re.DOTALL
            )
        logger.info("[postprocess] injected fontspec+ctex for CJK support")
    return content


def translate_tex_file(tex_path: str, out_path: str):
    logger.info(f"[translate] reading {tex_path}")
    with open(tex_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    preamble, masked_content, blocks = _extract_and_mask_tex(content)
    
    chunks = _split_tex(masked_content)
    logger.info(f"[translate] {len(chunks)} chunks to translate")
    translated_chunks = [_translate_chunk(c, i, len(chunks)) for i, c in enumerate(chunks)]
    merged = "\n\n".join(translated_chunks)
    
    merged = _restore_masks(merged, blocks)
    
    if preamble:
        merged = preamble + "\n" + merged

    merged = _postprocess_translated(merged)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(merged)
    logger.info(f"[translate] written to {out_path}")


# ── compile ────────────────────────────────────────────────────────────────

def compile_latex(tex_dir: str, main_tex: str) -> str | None:
    """Run xelatex twice, return path to PDF or None on failure."""
    logger.info(f"[compile] running xelatex on {main_tex} in {tex_dir}")
    for i in range(2):
        r = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", main_tex],
            cwd=tex_dir, capture_output=True, timeout=120
        )
        logger.info(f"[compile] pass {i+1} returncode={r.returncode}")
        if r.returncode != 0:
            logger.warning(f"[compile] stderr: {r.stderr.decode(errors='replace')[-500:]}")
            logger.warning(f"[compile] stdout(last 1000): {r.stdout.decode(errors='replace')[-1000:]}")
    pdf = os.path.join(tex_dir, main_tex.replace(".tex", ".pdf"))
    if os.path.exists(pdf):
        logger.info(f"[compile] success: {pdf}")
        return pdf
    logger.error("[compile] PDF not found after compilation")
    return None


# ── arXiv source download ──────────────────────────────────────────────────

def arxiv_source_to_latex_dir(arxiv_id: str, work_dir: str) -> str | None:
    """Try to download arXiv LaTeX source. Return tex dir or None."""
    src_url = f"https://arxiv.org/e-print/{arxiv_id}"
    src_path = os.path.join(work_dir, "arxiv_source")
    tex_dir = os.path.join(work_dir, "tex_src")

    if os.path.isdir(tex_dir) and glob.glob(os.path.join(tex_dir, "**/*.tex"), recursive=True):
        logger.info(f"[source] using cached tex_src at {tex_dir}")
        return tex_dir

    logger.info(f"[source] downloading arXiv source from {src_url}")
    try:
        r = requests.get(src_url, timeout=60)
        if r.status_code != 200:
            logger.warning(f"[source] arXiv source not available (HTTP {r.status_code})")
            return None
        with open(src_path, "wb") as f:
            f.write(r.content)
    except Exception as e:
        logger.warning(f"[source] failed to download source: {e}")
        return None

    os.makedirs(tex_dir, exist_ok=True)

    # Detect format and extract
    try:
        if tarfile.is_tarfile(src_path):
            logger.info("[source] extracting tar archive")
            with tarfile.open(src_path) as t:
                t.extractall(tex_dir)
        elif zipfile.is_zipfile(src_path):
            logger.info("[source] extracting zip archive")
            with zipfile.ZipFile(src_path) as z:
                z.extractall(tex_dir)
        else:
            # Might be a single .tex file (gzipped or plain)
            import gzip
            try:
                with gzip.open(src_path, 'rb') as gz:
                    data = gz.read()
            except gzip.BadGzipFile:
                with open(src_path, 'rb') as f:
                    data = f.read()
            # Check if it looks like LaTeX
            if b'\\documentclass' in data or b'\\begin{document}' in data:
                tex_file = os.path.join(tex_dir, "main.tex")
                with open(tex_file, 'wb') as f:
                    f.write(data)
                logger.info(f"[source] single tex file saved to {tex_file}")
            else:
                logger.warning("[source] downloaded file is not recognizable LaTeX source")
                shutil.rmtree(tex_dir, ignore_errors=True)
                return None
    except Exception as e:
        logger.warning(f"[source] extraction failed: {e}")
        shutil.rmtree(tex_dir, ignore_errors=True)
        return None

    tex_files = glob.glob(os.path.join(tex_dir, "**/*.tex"), recursive=True)
    if tex_files:
        logger.info(f"[source] found {len(tex_files)} .tex files in source")
        return tex_dir
    else:
        logger.warning("[source] no .tex files found in source archive")
        shutil.rmtree(tex_dir, ignore_errors=True)
        return None


# ── main entry ─────────────────────────────────────────────────────────────

def _translate_and_compile(work_dir: str, tex_dir: str) -> dict:
    """Shared logic: find main tex, translate, compile."""
    # Find main .tex file
    tex_files = glob.glob(os.path.join(tex_dir, "**/*.tex"), recursive=True)
    if not tex_files:
        return {"ok": False, "error": "No .tex files found"}
    main_tex_path = max(tex_files, key=os.path.getsize)
    main_tex_name = os.path.basename(main_tex_path)
    main_tex_dir = os.path.dirname(main_tex_path)
    logger.info(f"[pipeline] main tex: {main_tex_path}")

    # Translate
    translated_name = "translated_" + main_tex_name
    translated_path = os.path.join(main_tex_dir, translated_name)
    logger.info("[pipeline] starting translation")
    translate_tex_file(main_tex_path, translated_path)

    # Compile
    logger.info("[pipeline] starting compilation")
    pdf_out = compile_latex(main_tex_dir, translated_name)
    if pdf_out:
        logger.info(f"[pipeline] done: {pdf_out}")
        return {"ok": True, "pdf": pdf_out}
    else:
        return {"ok": False, "error": "xelatex compilation failed", "tex": translated_path}


def translate_arxiv_pdf(arxiv_abs_url: str, output_dir: str) -> dict:
    arxiv_id = arxiv_abs_url.rstrip("/").split("/")[-1]
    if "v" in arxiv_id:
        arxiv_id = arxiv_id.split("v")[0]

    work_dir = os.path.join(output_dir, arxiv_id)
    os.makedirs(work_dir, exist_ok=True)
    logger.info(f"[pipeline] start arxiv_id={arxiv_id} work_dir={work_dir}")

    try:
        # Step 1: Try arXiv LaTeX source first
        tex_dir = arxiv_source_to_latex_dir(arxiv_id, work_dir)

        # Step 2: Fallback to doc2x if no source
        if tex_dir is None:
            logger.info("[pipeline] no arXiv source, falling back to doc2x")
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            pdf_path = os.path.join(work_dir, f"{arxiv_id}.pdf")
            if not os.path.exists(pdf_path):
                logger.info(f"[pipeline] downloading PDF from {pdf_url}")
                r = requests.get(pdf_url, timeout=60)
                r.raise_for_status()
                with open(pdf_path, "wb") as f:
                    f.write(r.content)
                logger.info(f"[pipeline] PDF saved to {pdf_path}")
            else:
                logger.info(f"[pipeline] using cached PDF {pdf_path}")

            logger.info("[pipeline] starting doc2x conversion")
            tex_dir = pdf_to_latex_dir(pdf_path, work_dir)

        return _translate_and_compile(work_dir, tex_dir)

    except Exception as e:
        logger.exception(f"[pipeline] failed: {e}")
        return {"ok": False, "error": str(e)}


def translate_uploaded_pdf(pdf_path: str, output_dir: str, job_id: str) -> dict:
    """Translate a user-uploaded PDF via doc2x -> translate -> compile."""
    work_dir = os.path.join(output_dir, job_id)
    os.makedirs(work_dir, exist_ok=True)
    logger.info(f"[upload-pipeline] start job_id={job_id} pdf={pdf_path} work_dir={work_dir}")

    try:
        # Step 1: doc2x conversion (PDF -> LaTeX)
        logger.info("[upload-pipeline] starting doc2x conversion")
        tex_dir = pdf_to_latex_dir(
            pdf_path, work_dir, source_sha256=_file_sha256(pdf_path)
        )

        # Step 2-3: Translate & compile
        return _translate_and_compile(work_dir, tex_dir)

    except Exception as e:
        logger.exception(f"[upload-pipeline] failed: {e}")
        return {"ok": False, "error": str(e)}
