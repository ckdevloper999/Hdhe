import os
import sys
import re
import time
import sqlite3
import logging
from typing import Optional
from urllib.parse import unquote
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Setup & Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("prompts_api")

DB_FILE = "prompts.db"
BASE_URL = "https://promptplum.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                category TEXT DEFAULT 'Men',
                model TEXT DEFAULT 'Gemini',
                image_url TEXT,
                tags TEXT,
                source_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slug ON prompts(slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON prompts(category)")
    conn.close()

def save_prompt(item: dict) -> bool:
    conn = get_db()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO prompts (slug, title, prompt, category, model, image_url, tags, source_url)
                VALUES (:slug, :title, :prompt, :category, :model, :image_url, :tags, :source_url)
                ON CONFLICT(slug) DO UPDATE SET
                    title=excluded.title,
                    prompt=excluded.prompt,
                    category=excluded.category,
                    model=excluded.model,
                    image_url=excluded.image_url,
                    tags=excluded.tags,
                    source_url=excluded.source_url
                """,
                item,
            )
        return True
    except Exception as e:
        logger.error(f"Failed to save {item.get('slug')}: {e}")
        return False
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# PromptPlum Scraper
# ---------------------------------------------------------------------------
scraper_state = {"status": "idle", "total_scraped": 0, "last_error": None}

def parse_prompt_page(url: str, default_category: str = "Men") -> Optional[dict]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        slug = url.rstrip("/").split("/")[-1]

        # 1. Title
        title_tag = soup.find("h1")
        og_title = soup.find("meta", property="og:title")
        title = title_tag.get_text(strip=True) if title_tag else (
            og_title["content"] if og_title else slug.replace("-", " ").title()
        )
        title = re.sub(r"\s*\|\s*PromptPlum.*", "", title, flags=re.IGNORECASE).strip()

        # 2. Image URL
        og_img = soup.find("meta", property="og:image")
        image_url = og_img["content"] if og_img else None
        if not image_url:
            img = soup.find("img", src=re.compile(r"promptplum|s3|cloudfront"))
            image_url = img["src"] if img else None

        # 3. Model
        model = "Gemini"
        model_sec = soup.find(string=re.compile(r"MODEL OR TOOL|Optimized for", re.IGNORECASE))
        if model_sec and model_sec.parent:
            text = model_sec.parent.get_text()
            for candidate in ["Gemini", "ChatGPT", "Midjourney", "Flux"]:
                if candidate.lower() in text.lower():
                    model = candidate
                    break

        # 4. Tags
        tags = []
        tag_container = soup.find(string=re.compile(r"TAGS", re.IGNORECASE))
        if tag_container and tag_container.find_parent():
            parent_text = tag_container.find_parent().get_text()
            extracted = re.findall(r"#([\w\s]+)", parent_text)
            tags = [t.strip() for t in extracted if t.strip()]

        # 5. Extract Full Prompt
        prompt_text = ""
        for header in soup.find_all(["h2", "h3", "div", "span"], string=re.compile(r"^PROMPT$", re.I)):
            candidate_container = header.find_parent()
            if candidate_container:
                paragraphs = candidate_container.find_all("p")
                for p in paragraphs:
                    text = p.get_text(strip=True)
                    if len(text) > 40 and not text.startswith("Optimized for"):
                        prompt_text = text
                        break
            if prompt_text:
                break

        if not prompt_text:
            meta_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
            if meta_desc and len(meta_desc.get("content", "")) > 30:
                prompt_text = meta_desc["content"].strip()
            else:
                paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 50]
                if paragraphs:
                    prompt_text = paragraphs[0]

        if not prompt_text:
            return None

        return {
            "slug": slug,
            "title": title,
            "prompt": prompt_text,
            "category": default_category.title(),
            "model": model,
            "image_url": image_url,
            "tags": ", ".join(tags),
            "source_url": url,
        }
    except Exception as e:
        logger.warning(f"Error scraping {url}: {e}")
        return None

def run_scraper(category: str = "men", max_items: int = 150):
    global scraper_state
    scraper_state["status"] = f"scraping {category}"
    scraper_state["total_scraped"] = 0
    scraper_state["last_error"] = None

    url = f"{BASE_URL}/library/{category.lower().strip()}/"
    logger.info(f"Starting scraper for: {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            scraper_state["status"] = "failed"
            scraper_state["last_error"] = f"HTTP {resp.status_code} fetching category"
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        links = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/prompt/" in href and not href.endswith("/prompt/"):
                if href.startswith("/"):
                    href = BASE_URL + href
                links.add(href)

        logger.info(f"Found {len(links)} prompt links in category '{category}'")

        count = 0
        for prompt_url in list(links)[:max_items]:
            data = parse_prompt_page(prompt_url, default_category=category)
            if data and save_prompt(data):
                count += 1
                scraper_state["total_scraped"] = count
                logger.info(f"[{count}] Saved: {data['title']}")
            time.sleep(0.4)

        scraper_state["status"] = "completed"
        logger.info(f"Scraper finished! Saved {count} prompts.")

    except Exception as e:
        logger.error(f"Scraper encountered an error: {e}")
        scraper_state["status"] = "error"
        scraper_state["last_error"] = str(e)

# ---------------------------------------------------------------------------
# FastAPI Application & Global CORS Setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Photo AI Prompts API + CORS Proxy",
    description="REST API serving curated AI photo prompts scraped from PromptPlum with built-in CORS Proxy.",
    version="1.1.0",
)

# Global CORS middleware for all endpoints
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()

# ---------------------------------------------------------------------------
# Built-in Streaming CORS Proxy
# ---------------------------------------------------------------------------
@app.api_route("/api/proxy", methods=["GET", "HEAD"])
@app.api_route("/proxy", methods=["GET", "HEAD"])
def cors_proxy(url: str = Query(..., description="Target URL (image/API) to fetch via CORS proxy")):
    """
    Acts as a CORS proxy. Forwards images, JSON, or external APIs to any frontend,
    stripping referrer and hotlink checks while appending permissive CORS headers.
    """
    target_url = unquote(url).strip()
    if not target_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid target URL. Must start with http:// or https://")

    try:
        req_headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "*/*",
            "Referer": BASE_URL,
        }

        remote_resp = requests.get(target_url, headers=req_headers, stream=True, timeout=20)

        # Hop-by-hop headers to drop
        excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        response_headers = {
            k: v for k, v in remote_resp.headers.items() if k.lower() not in excluded_headers
        }

        # Inject universal CORS headers
        response_headers["Access-Control-Allow-Origin"] = "*"
        response_headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response_headers["Access-Control-Allow-Headers"] = "*"
        response_headers["Cache-Control"] = "public, max-age=86400"

        return StreamingResponse(
            remote_resp.iter_content(chunk_size=8192),
            status_code=remote_resp.status_code,
            headers=response_headers,
            media_type=remote_resp.headers.get("content-type", "application/octet-stream"),
        )
    except Exception as e:
        logger.error(f"Proxy failed for {target_url}: {e}")
        raise HTTPException(status_code=502, detail=f"Proxy Error: {str(e)}")

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "status": "online",
        "documentation": "/docs",
        "endpoints": {
            "get_prompts": "/api/prompts",
            "get_random": "/api/prompts/random",
            "get_categories": "/api/categories",
            "cors_proxy_example": "/api/proxy?url=https://example.com/image.jpg",
            "scrape_now": "POST /api/scrape?category=men",
            "scraper_status": "/api/scrape/status",
        },
    }

@app.get("/api/prompts")
def list_prompts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: Optional[str] = None,
    q: Optional[str] = None,
):
    offset = (page - 1) * limit
    conn = get_db()
    cursor = conn.cursor()

    conditions = []
    params = []

    if category:
        conditions.append("LOWER(category) = LOWER(?)")
        params.append(category)

    if q:
        conditions.append("(title LIKE ? OR prompt LIKE ? OR tags LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    cursor.execute(f"SELECT COUNT(*) FROM prompts {where_clause}", params)
    total = cursor.fetchone()[0]

    query = f"""
        SELECT id, slug, title, prompt, category, model, image_url, tags, source_url, created_at
        FROM prompts
        {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    cursor.execute(query, params + [limit, offset])
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit if total > 0 else 1,
        "data": rows,
    }

@app.get("/api/prompts/random")
def get_random_prompt(category: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    if category:
        cursor.execute("SELECT * FROM prompts WHERE LOWER(category) = LOWER(?) ORDER BY RANDOM() LIMIT 1", (category,))
    else:
        cursor.execute("SELECT * FROM prompts ORDER BY RANDOM() LIMIT 1")
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="No prompts found")
    return dict(row)

@app.get("/api/prompts/{slug_or_id}")
def get_prompt_by_id(slug_or_id: str):
    conn = get_db()
    cursor = conn.cursor()
    if slug_or_id.isdigit():
        cursor.execute("SELECT * FROM prompts WHERE id = ?", (int(slug_or_id),))
    else:
        cursor.execute("SELECT * FROM prompts WHERE slug = ?", (slug_or_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return dict(row)

@app.get("/api/categories")
def get_categories():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT category, COUNT(*) as count FROM prompts GROUP BY category ORDER BY count DESC")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/api/scrape")
def trigger_scraper(background_tasks: BackgroundTasks, category: str = "men", max_items: int = 100):
    if scraper_state["status"].startswith("scraping"):
        return {"message": "Scraper is already running", "status": scraper_state}

    background_tasks.add_task(run_scraper, category=category, max_items=max_items)
    return {"message": f"Scraper started for category '{category}' in background", "check_status_at": "/api/scrape/status"}

@app.get("/api/scrape/status")
def get_scraper_status():
    return scraper_state

# ---------------------------------------------------------------------------
# CLI Command Support
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "--scrape":
        target_category = sys.argv[2] if len(sys.argv) > 2 else "men"
        print(f"Starting terminal scraper for category: {target_category}...")
        run_scraper(category=target_category, max_items=150)
    else:
        import uvicorn
        port = int(os.environ.get("PORT", 8000))
        uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
    
