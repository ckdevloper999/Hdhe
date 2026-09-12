import time
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="AI Photo Prompts API")

# Enable CORS so any website can access your API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Cache results in memory to avoid reaching target site limits
CACHE = {}
CACHE_TIMEOUT = 3600  # Cache lasts 1 hour

def scrape_category(category: str):
    now = time.time()
    
    # Return cached data if available and fresh
    if category in CACHE and (now - CACHE[category]["timestamp"]) < CACHE_TIMEOUT:
        return CACHE[category]["data"]

    url = f"https://promptplum.com/library/{category}"
    response = requests.get(url, headers=HEADERS)

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch prompts from source website")

    soup = BeautifulSoup(response.text, "html.parser")
    prompts = []

    # Select all card elements from the page
    cards = soup.select("article") or soup.select(".grid > div")

    for idx, card in enumerate(cards):
        title_el = card.select_one("h2, h3, .title, a")
        prompt_el = card.select_one("p, .prompt-text, code")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else f"Prompt {idx+1}"
        prompt_text = prompt_el.get_text(strip=True) if prompt_el else ""
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else ""

        if prompt_text or title:
            prompts.append({
                "id": idx + 1,
                "title": title,
                "prompt": prompt_text,
                "image_url": image_url,
                "category": category
            })

    CACHE[category] = {"timestamp": now, "data": prompts}
    return prompts

@app.get("/")
def home():
    return {"status": "online", "message": "API is active"}

@app.get("/api/prompts/{category}")
def get_prompts(category: str):
    data = scrape_category(category.lower())
    return {
        "count": len(data),
        "category": category,
        "prompts": data
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
  
