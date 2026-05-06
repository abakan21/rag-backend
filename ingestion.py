import os
import re
import base64
import hashlib
from urllib.parse import urlparse, urljoin, urlunparse
from urllib.robotparser import RobotFileParser
from datetime import datetime, timezone
from collections import deque

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from models import init_db, Source, IngestJob, StrategyEnum, StatusEnum, Evidence

MIN_TEXT_LENGTH = 100


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_url(url: str) -> str:
    """Strip fragment, normalize trailing slash."""
    p = urlparse(url)
    # Keep query params but strip fragment
    normalized = urlunparse((p.scheme, p.netloc, p.path.rstrip('/') or '/', p.params, p.query, ''))
    return normalized


def _check_robots_txt(url: str) -> bool:
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        allowed = rp.can_fetch("*", url)
        if not allowed:
            print(f"[Robots.txt] Crawling BLOCKED for {url}")
        return allowed
    except Exception as e:
        print(f"[Robots.txt] Error: {e} — allowing crawl")
        return True


def _scrape_html(url: str) -> tuple[str, str | None]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header",
                          "aside", "form", "iframe", "noscript"]):
            tag.decompose()

        body = soup.find("body") or soup
        markdown = md(str(body), heading_style="ATX", strip=["a"])
        markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
        return markdown, html
    except Exception as e:
        print(f"[HTTP Scraper] Failed for {url}: {e}")
        raise


def _extract_links(html: str, base_url: str, max_links: int = 50) -> list[str]:
    """Extract internal links, skip query-param variants to avoid explosion."""
    soup = BeautifulSoup(html, "html.parser")
    base_parsed = urlparse(base_url)
    seen_paths = set()
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Only same domain
        if parsed.netloc != base_parsed.netloc:
            continue

        # Skip if only difference is query params — avoid explosion
        path_key = parsed.path.rstrip('/')
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        # Clean URL — strip query and fragment for crawling
        clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
        links.append(clean)

        if len(links) >= max_links:
            break

    return links


def _save_markdown(text: str, job_id: int, idx: int, url: str, data_dir: str, db) -> str:
    os.makedirs(data_dir, exist_ok=True)
    safe_url = re.sub(r'[^\w]', '_', url.replace("https://", "").replace("http://", ""))[:60]
    filename = f"{data_dir}/job_{job_id}_{idx}_{safe_url}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(text)
    md_hash = _compute_sha256(text.encode("utf-8"))
    db.add(Evidence(
        job_id=job_id, evidence_type="markdown",
        storage_uri=filename, file_hash=md_hash,
        created_ts=datetime.now(timezone.utc),
    ))
    db.commit()
    return filename


def ingest_url(url: str, source_id: int, deep_crawl: bool = False,
               max_depth: int = 2, preferred_strategy: str = None):
    db = init_db()
    print(f"[Ingest] Starting for URL: {url}, deep_crawl={deep_crawl}, max_depth={max_depth}")

    source = db.query(Source).filter(Source.id == source_id).first()
    # Fix: check permission_type for robots.txt bypass
    override_robots = source and source.permission_type in ("consent", "contract")

    if override_robots:
        print(f"[Robots.txt] Skipping — permission_type={source.permission_type}")
    elif not _check_robots_txt(url):
        print(f"[BLOCKED] robots.txt disallows {url}")
        job = db.query(IngestJob).filter(
            IngestJob.source_id == source_id,
            IngestJob.status == StatusEnum.RUNNING
        ).order_by(IngestJob.id.desc()).first()
        if job:
            job.status = StatusEnum.FAILED
            job.error_code = "ROBOTS_TXT_BLOCKED"
            job.completed_ts = datetime.now(timezone.utc)
            db.commit()
        return

    job = db.query(IngestJob).filter(
        IngestJob.source_id == source_id,
        IngestJob.status == StatusEnum.RUNNING
    ).order_by(IngestJob.id.desc()).first()

    if not job:
        print(f"[Ingest] No running job found for source {source_id}")
        return

    data_dir = os.getenv("DATA_DIR", "data")

    try:
        if not deep_crawl:
            # Single page
            try:
                text_content, _ = _scrape_html(url)
                print(f"[Ingest] Got {len(text_content)} chars")
            except Exception as e:
                job.status = StatusEnum.FAILED
                job.error_code = str(e)[:200]
                job.completed_ts = datetime.now(timezone.utc)
                db.commit()
                return

            lower = text_content.lower()
            if any(kw in lower for kw in ["captcha", "verify you are human", "cloudflare"]):
                job.status = StatusEnum.CAPTCHA_DETECTED
                job.error_code = "CAPTCHA"
                db.commit()
                return

            if not text_content or len(text_content.strip()) < MIN_TEXT_LENGTH:
                job.status = StatusEnum.FAILED
                job.error_code = "NO_CONTENT"
                job.completed_ts = datetime.now(timezone.utc)
                db.commit()
                return

            filename = _save_markdown(text_content, job.id, 0, url, data_dir, db)
            from rag import index_markdown_file
            index_markdown_file(filename, url, job_id=job.id)
            job.status = StatusEnum.COMPLETED

        else:
            # BFS crawl — limit pages to avoid explosion
            MAX_PAGES = 30
            visited = set()
            queue = deque([(_normalize_url(url), 0)])
            idx = 0
            success_count = 0

            from rag import index_markdown_file

            while queue and idx < MAX_PAGES:
                current_url, depth = queue.popleft()
                if current_url in visited:
                    continue
                visited.add(current_url)

                print(f"[Crawl] Depth {depth} ({idx+1}/{MAX_PAGES}): {current_url}")
                try:
                    text_content, raw_html = _scrape_html(current_url)
                except Exception as e:
                    print(f"[Crawl] Skip {current_url}: {e}")
                    continue

                if text_content and len(text_content.strip()) >= MIN_TEXT_LENGTH:
                    filename = _save_markdown(text_content, job.id, idx, current_url, data_dir, db)
                    index_markdown_file(filename, current_url, job_id=job.id)
                    success_count += 1
                    idx += 1

                # Add child links only if not at max depth
                if depth < max_depth and raw_html and idx < MAX_PAGES:
                    links = _extract_links(raw_html, url, max_links=30)
                    for link in links:
                        norm = _normalize_url(link)
                        if norm not in visited:
                            queue.append((norm, depth + 1))

            print(f"[Crawl] Done. {success_count} pages indexed, {len(visited)} visited.")
            job.status = StatusEnum.COMPLETED if success_count > 0 else StatusEnum.FAILED
            if success_count == 0:
                job.error_code = "NO_CONTENT"

        job.completed_ts = datetime.now(timezone.utc)
        db.commit()
        print(f"[Ingest] Job {job.id} → {job.status}")

    except Exception as e:
        print(f"[ERROR] {e}")
        job.status = StatusEnum.FAILED
        job.error_code = str(e)[:500]
        job.completed_ts = datetime.now(timezone.utc)
        db.commit()
