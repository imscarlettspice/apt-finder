#!/usr/bin/env python3
“””
Columbia City Apartment Finder — Railway Edition (ScraperAPI-powered)
Routes all rental site requests through ScraperAPI to bypass bot detection.
“””

import sqlite3
import json
import re
import csv
import io
import hashlib
import time
import threading
import logging
import os
from datetime import datetime
from pathlib import Path

import requests as http_requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template, Response
from flask_cors import CORS

# ─── APP SETUP ─────────────────────────────────────────────────────────────────

app = Flask(**name**, template_folder=“templates”, static_folder=“static”)
CORS(app)

DB_PATH = Path(os.environ.get(“DB_PATH”, “/tmp/apartments.db”))
SCRAPER_API_KEY = os.environ.get(“SCRAPER_API_KEY”, “”)

CRITERIA = {
“neighborhood”: “Columbia City”,
“city”: “Seattle”,
“state”: “WA”,
“min_sqft”: 401,
“min_price”: 1600,
“max_price”: 1799,
“min_beds”: 0,
“max_beds”: 1,
“move_in_month”: 5,
“move_in_year”: 2026,
}

CC_BOUNDS = {
“lat_min”: 47.5486, “lat_max”: 47.5666,
“lng_min”: -122.2986, “lng_max”: -122.2736,
}

logging.basicConfig(level=logging.INFO, format=”%(asctime)s [%(levelname)s] %(message)s”)
log = logging.getLogger(“apt-finder”)

scrape_status = {“running”: False, “last_run”: None, “message”: “Ready”}

# ─── SCRAPERAPI HELPER ─────────────────────────────────────────────────────────

def scraper_get(url, render_js=False, timeout=60):
“””
Fetch a URL through ScraperAPI.
render_js=True uses a headless browser (costs 10 credits instead of 1,
but needed for JS-heavy sites like Zillow and Apartments.com).
“””
if not SCRAPER_API_KEY:
raise ValueError(“SCRAPER_API_KEY not set — add it in Railway Variables”)

```
params = {
    "api_key": SCRAPER_API_KEY,
    "url": url,
    "country_code": "us",
}
if render_js:
    params["render"] = "true"

resp = http_requests.get(
    "https://api.scraperapi.com",
    params=params,
    timeout=timeout,
)
resp.raise_for_status()
return resp
```

# ─── DATABASE ──────────────────────────────────────────────────────────────────

def get_db():
conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
return conn

def init_db():
conn = sqlite3.connect(str(DB_PATH))
conn.execute(“PRAGMA journal_mode=WAL”)
conn.execute(”””
CREATE TABLE IF NOT EXISTS listings (
id TEXT PRIMARY KEY,
source TEXT NOT NULL,
name TEXT,
address TEXT,
price INTEGER,
sqft INTEGER,
bedrooms TEXT,
bathrooms TEXT,
url TEXT,
image_url TEXT,
latitude REAL,
longitude REAL,
available_date TEXT,
description TEXT,
amenities TEXT,
pet_policy TEXT,
first_seen TEXT NOT NULL,
last_seen TEXT NOT NULL,
status TEXT DEFAULT ‘active’,
notes TEXT DEFAULT ‘’,
favorite INTEGER DEFAULT 0
)
“””)
conn.execute(”””
CREATE TABLE IF NOT EXISTS scrape_log (
id INTEGER PRIMARY KEY AUTOINCREMENT,
source TEXT NOT NULL,
timestamp TEXT NOT NULL,
listings_found INTEGER DEFAULT 0,
listings_matched INTEGER DEFAULT 0,
error TEXT
)
“””)
conn.execute(“CREATE INDEX IF NOT EXISTS idx_status ON listings(status)”)
conn.execute(“CREATE INDEX IF NOT EXISTS idx_source ON listings(source)”)
conn.commit()
conn.close()

def upsert_listing(conn, listing: dict):
raw_id = f”{listing[‘source’]}:{listing.get(‘address’, ‘’)}:{listing.get(‘name’, ‘’)}”
lid = hashlib.md5(raw_id.encode()).hexdigest()[:12]
now = datetime.now().isoformat()
existing = conn.execute(“SELECT id FROM listings WHERE id = ?”, (lid,)).fetchone()
if existing:
conn.execute(”””
UPDATE listings SET price=?, sqft=?, url=?, image_url=?,
available_date=?, last_seen=?, status=‘active’,
description=?, amenities=?, pet_policy=?
WHERE id=?
“””, (listing.get(“price”), listing.get(“sqft”), listing.get(“url”),
listing.get(“image_url”), listing.get(“available_date”), now,
listing.get(“description”), listing.get(“amenities”),
listing.get(“pet_policy”), lid))
else:
conn.execute(”””
INSERT INTO listings (id,source,name,address,price,sqft,bedrooms,bathrooms,
url,image_url,latitude,longitude,available_date,description,amenities,
pet_policy,first_seen,last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
“””, (lid, listing[“source”], listing.get(“name”), listing.get(“address”),
listing.get(“price”), listing.get(“sqft”), listing.get(“bedrooms”),
listing.get(“bathrooms”), listing.get(“url”), listing.get(“image_url”),
listing.get(“latitude”), listing.get(“longitude”),
listing.get(“available_date”), listing.get(“description”),
listing.get(“amenities”), listing.get(“pet_policy”), now, now))
return lid

def log_scrape(conn, source, found, matched, error=None):
conn.execute(
“INSERT INTO scrape_log (source,timestamp,listings_found,listings_matched,error) VALUES (?,?,?,?,?)”,
(source, datetime.now().isoformat(), found, matched, error))
conn.commit()

# ─── FILTERING ─────────────────────────────────────────────────────────────────

def passes_filter(listing):
price = listing.get(“price”)
if price and (price < CRITERIA[“min_price”] or price > CRITERIA[“max_price”]):
return False
sqft = listing.get(“sqft”)
if sqft and sqft < CRITERIA[“min_sqft”]:
return False
bedrooms = str(listing.get(“bedrooms”, “”)).lower()
if bedrooms:
m = re.search(r”(\d+)”, bedrooms)
if m and int(m.group(1)) > CRITERIA[“max_beds”]:
return False
return True

# ─── SCRAPERS (via ScraperAPI) ─────────────────────────────────────────────────

def scrape_craigslist():
log.info(“Scraping Craigslist via ScraperAPI…”)
listings = []
url = (f”https://seattle.craigslist.org/search/see/apa”
f”?query=columbia+city&min_price={CRITERIA[‘min_price’]}”
f”&max_price={CRITERIA[‘max_price’]}&min_bedrooms={CRITERIA[‘min_beds’]}”
f”&max_bedrooms={CRITERIA[‘max_beds’]}&minSqft={CRITERIA[‘min_sqft’]}”)
try:
resp = scraper_get(url, render_js=False)
soup = BeautifulSoup(resp.text, “lxml”)

```
    for item in soup.select("li.cl-static-search-result, .cl-search-result, li.result-row"):
        try:
            te = item.select_one("a.titlestring, a.result-title, .title, a.posting-title")
            if not te:
                continue
            name = te.get_text(strip=True)
            link = te.get("href", "")
            if link and not link.startswith("http"):
                link = "https://seattle.craigslist.org" + link
            pe = item.select_one(".priceinfo, .result-price, .price")
            price = None
            if pe:
                pm = re.search(r"\$?([\d,]+)", pe.get_text())
                if pm:
                    price = int(pm.group(1).replace(",", ""))
            meta = item.get_text()
            sm = re.search(r"(\d{3,4})\s*(?:ft|sq)", meta)
            bm = re.search(r"(\d+)\s*(?:br|bed|BR)", meta)
            listings.append({
                "source": "Craigslist", "name": name, "address": "",
                "price": price, "sqft": int(sm.group(1)) if sm else None,
                "bedrooms": f"{bm.group(1)} BR" if bm else "Studio",
                "bathrooms": None, "url": link, "image_url": None,
                "latitude": None, "longitude": None, "available_date": None,
                "description": name, "amenities": None, "pet_policy": None,
            })
        except Exception:
            continue
except Exception as e:
    log.error(f"Craigslist failed: {e}")
return listings
```

def scrape_apartments_com():
log.info(“Scraping Apartments.com via ScraperAPI…”)
listings = []
url = (f”https://www.apartments.com/columbia-city-seattle-wa/”
f”min-1-bedrooms-1-bedrooms-{CRITERIA[‘min_price’]}-to-{CRITERIA[‘max_price’]}/”)
try:
resp = scraper_get(url, render_js=True)
soup = BeautifulSoup(resp.text, “lxml”)

```
    for card in soup.select("li.mortar-wrapper article, section.placard, article[data-listingid], li[data-listingid]"):
        try:
            te = card.select_one(".property-title, .js-placardTitle, span.title")
            name = te.get_text(strip=True) if te else "Unknown"
            ae = card.select_one(".property-address, div.property-address")
            address = ae.get_text(strip=True) if ae else ""
            le = card.select_one("a.property-link, a[href*='apartments.com']")
            link = ""
            if le and le.get("href"):
                link = le["href"]
                if not link.startswith("http"):
                    link = "https://www.apartments.com" + link
            pe = card.select_one(".property-pricing, .price-range, p.property-pricing")
            price = None
            if pe:
                prices = re.findall(r"\$?([\d,]+)", pe.get_text())
                if prices:
                    price = int(prices[0].replace(",", ""))
            be = card.select_one(".property-beds, .bed-range")
            se = card.select_one(".property-sqft, .sqft-range")
            sqft = None
            if se:
                sm = re.search(r"([\d,]+)", se.get_text())
                if sm:
                    sqft = int(sm.group(1).replace(",", ""))
            ie = card.select_one("img[src], img[data-src]")
            img = ie.get("data-src") or ie.get("src") if ie else None
            listings.append({
                "source": "Apartments.com", "name": name, "address": address,
                "price": price, "sqft": sqft,
                "bedrooms": be.get_text(strip=True) if be else None,
                "bathrooms": None, "url": link, "image_url": img,
                "latitude": None, "longitude": None, "available_date": None,
                "description": None, "amenities": None, "pet_policy": None,
            })
        except Exception:
            continue
except Exception as e:
    log.error(f"Apartments.com failed: {e}")
return listings
```

def scrape_zillow():
log.info(“Scraping Zillow via ScraperAPI…”)
listings = []
search_state = json.dumps({
“pagination”: {},
“mapBounds”: {“north”: CC_BOUNDS[“lat_max”], “south”: CC_BOUNDS[“lat_min”],
“east”: CC_BOUNDS[“lng_max”], “west”: CC_BOUNDS[“lng_min”]},
“filterState”: {
“price”: {“min”: CRITERIA[“min_price”], “max”: CRITERIA[“max_price”]},
“beds”: {“min”: CRITERIA[“min_beds”], “max”: CRITERIA[“max_beds”]},
“sqft”: {“min”: CRITERIA[“min_sqft”]},
“fr”: {“value”: True}, “fsba”: {“value”: False}, “fsbo”: {“value”: False},
“nc”: {“value”: False}, “cmsn”: {“value”: False},
“auc”: {“value”: False}, “fore”: {“value”: False},
},
})
url = f”https://www.zillow.com/columbia-city-seattle-wa/rentals/?searchQueryState={search_state}”
try:
resp = scraper_get(url, render_js=True)
soup = BeautifulSoup(resp.text, “lxml”)

```
    for script in soup.find_all("script", {"type": "application/json"}):
        try:
            data = json.loads(script.string or "")
            results = None
            if isinstance(data, dict):
                for path in [
                    ["cat1", "searchResults", "listResults"],
                    ["props", "pageProps", "searchPageState", "cat1", "searchResults", "listResults"],
                ]:
                    obj = data
                    for key in path:
                        obj = obj.get(key) if isinstance(obj, dict) else None
                        if obj is None:
                            break
                    if isinstance(obj, list):
                        results = obj
                        break
            if not results:
                continue
            for item in results:
                price = item.get("price")
                if isinstance(price, str):
                    pm = re.search(r"[\d,]+", price.replace("$", ""))
                    price = int(pm.group().replace(",", "")) if pm else None
                elif isinstance(price, (int, float)):
                    price = int(price)
                detail_url = item.get("detailUrl", "")
                if detail_url and not detail_url.startswith("http"):
                    detail_url = "https://www.zillow.com" + detail_url
                listings.append({
                    "source": "Zillow",
                    "name": item.get("statusText", item.get("address", "Zillow Listing")),
                    "address": item.get("address", ""),
                    "price": price, "sqft": item.get("area"),
                    "bedrooms": f"{item.get('beds', '?')} BR",
                    "bathrooms": str(item.get("baths", "")),
                    "url": detail_url, "image_url": item.get("imgSrc"),
                    "latitude": item.get("latLong", {}).get("latitude"),
                    "longitude": item.get("latLong", {}).get("longitude"),
                    "available_date": None, "description": None,
                    "amenities": None, "pet_policy": None,
                })
        except (json.JSONDecodeError, TypeError):
            continue

    if not listings:
        for card in soup.select("article[data-test='property-card'], div[class*='property-card']"):
            try:
                ae = card.select_one("address, [data-test='property-card-addr']")
                pe = card.select_one("[data-test='property-card-price'], span[class*='Price']")
                le = card.select_one("a[href*='/homedetails/'], a[href*='/b/']")
                price = None
                if pe:
                    pm = re.search(r"\$?([\d,]+)", pe.get_text())
                    if pm:
                        price = int(pm.group(1).replace(",", ""))
                link = le["href"] if le else ""
                if link and not link.startswith("http"):
                    link = "https://www.zillow.com" + link
                meta_text = card.get_text()
                bm = re.search(r"(\d+)\s*(?:bd|bed|br|BR)", meta_text)
                sm = re.search(r"([\d,]+)\s*(?:sqft|sq ft|SF)", meta_text)
                listings.append({
                    "source": "Zillow",
                    "name": ae.get_text(strip=True) if ae else "Zillow Listing",
                    "address": ae.get_text(strip=True) if ae else "",
                    "price": price,
                    "sqft": int(sm.group(1).replace(",", "")) if sm else None,
                    "bedrooms": f"{bm.group(1)} BR" if bm else None,
                    "bathrooms": None, "url": link, "image_url": None,
                    "latitude": None, "longitude": None, "available_date": None,
                    "description": None, "amenities": None, "pet_policy": None,
                })
            except Exception:
                continue
except Exception as e:
    log.error(f"Zillow failed: {e}")
return listings
```

def scrape_redfin():
log.info(“Scraping Redfin via ScraperAPI…”)
listings = []
url = (“https://www.redfin.com/neighborhood/530871/WA/Seattle/Columbia-City/”
“apartments-for-rent/filter/min-price=1.6k,max-price=1.8k,min-beds=0,max-beds=1,min-sqft=401-sqft”)
try:
resp = scraper_get(url, render_js=True)
soup = BeautifulSoup(resp.text, “lxml”)

```
    for card in soup.select(".HomeCard, .RentalHomeCard, div[data-rf-test-id='MapHomeCard'], .HomeCardContainer"):
        try:
            ae = card.select_one(".homeAddressV2, .link-and-anchor, .HomecardV2__Address")
            address = ae.get_text(strip=True) if ae else ""
            pe = card.select_one(".homecardV2Price, .HomeCardContainer__price, span[class*='price']")
            price = None
            if pe:
                pm = re.search(r"\$?([\d,]+)", pe.get_text())
                if pm:
                    price = int(pm.group(1).replace(",", ""))
            le = card.select_one("a[href]")
            link = le["href"] if le else ""
            if link and not link.startswith("http"):
                link = "https://www.redfin.com" + link
            stats = card.get_text()
            bm = re.search(r"(\d+)\s*(?:Bed|BR|bd|bed)", stats, re.IGNORECASE)
            sm = re.search(r"([\d,]+)\s*(?:Sq|sq|SF|sf|sqft)", stats)
            listings.append({
                "source": "Redfin", "name": address or "Redfin Listing",
                "address": address, "price": price,
                "sqft": int(sm.group(1).replace(",", "")) if sm else None,
                "bedrooms": f"{bm.group(1)} BR" if bm else None,
                "bathrooms": None, "url": link, "image_url": None,
                "latitude": None, "longitude": None, "available_date": None,
                "description": None, "amenities": None, "pet_policy": None,
            })
        except Exception:
            continue
except Exception as e:
    log.error(f"Redfin failed: {e}")
return listings
```

def scrape_hotpads():
log.info(“Scraping HotPads via ScraperAPI…”)
listings = []
url = (f”https://hotpads.com/columbia-city-seattle-wa/apartments-for-rent”
f”?beds=0-1&price={CRITERIA[‘min_price’]}-{CRITERIA[‘max_price’]}&sqft={CRITERIA[‘min_sqft’]}”)
try:
resp = scraper_get(url, render_js=True)
soup = BeautifulSoup(resp.text, “lxml”)

```
    for card in soup.select("[data-test='listing-card'], .ListingCard, .listing-card, div[class*='ListingCard']"):
        try:
            te = card.select_one(".ListingCard-title, .listing-title, a[data-test], div[class*='title']")
            name = te.get_text(strip=True) if te else "HotPads Listing"
            ae = card.select_one(".ListingCard-address, .listing-address, div[class*='address']")
            address = ae.get_text(strip=True) if ae else ""
            pe = card.select_one(".ListingCard-price, .listing-price, div[class*='price']")
            price = None
            if pe:
                pm = re.search(r"\$?([\d,]+)", pe.get_text())
                if pm:
                    price = int(pm.group(1).replace(",", ""))
            le = card.select_one("a[href]")
            link = le["href"] if le else ""
            if link and not link.startswith("http"):
                link = "https://hotpads.com" + link
            meta = card.get_text()
            bm = re.search(r"(\d+)\s*(?:bed|br|BR|Bed)", meta, re.IGNORECASE)
            sm = re.search(r"([\d,]+)\s*(?:sq|SF|sqft)", meta, re.IGNORECASE)
            listings.append({
                "source": "HotPads", "name": name, "address": address,
                "price": price, "sqft": int(sm.group(1).replace(",", "")) if sm else None,
                "bedrooms": f"{bm.group(1)} BR" if bm else None,
                "bathrooms": None, "url": link, "image_url": None,
                "latitude": None, "longitude": None, "available_date": None,
                "description": None, "amenities": None, "pet_policy": None,
            })
        except Exception:
            continue
except Exception as e:
    log.error(f"HotPads failed: {e}")
return listings
```

SCRAPERS = {
“Craigslist”: scrape_craigslist,
“Apartments.com”: scrape_apartments_com,
“Zillow”: scrape_zillow,
“Redfin”: scrape_redfin,
“HotPads”: scrape_hotpads,
}

def run_scrape():
conn = get_db()
total_found = total_matched = 0
for name, fn in SCRAPERS.items():
try:
scrape_status[“message”] = f”Scraping {name}…”
raw = fn()
found = len(raw)
total_found += found
matched = [l for l in raw if passes_filter(l)]
for l in matched:
upsert_listing(conn, l)
total_matched += len(matched)
log_scrape(conn, name, found, len(matched))
log.info(f”  {name}: {found} found, {len(matched)} matched”)
time.sleep(1)
except Exception as e:
log.error(f”  {name}: {e}”)
log_scrape(conn, name, 0, 0, str(e))
conn.commit()
conn.close()
return total_found, total_matched

# ─── API ROUTES ────────────────────────────────────────────────────────────────

@app.route(”/”)
def index():
return render_template(“index.html”)

@app.route(”/api/listings”)
def api_listings():
conn = get_db()
status = request.args.get(“status”, “active”)
source = request.args.get(“source”, “”)
sort = request.args.get(“sort”, “price_asc”)
favs = request.args.get(“favorites”, “false”) == “true”
q = “SELECT * FROM listings WHERE 1=1”
p = []
if status and status != “all”:
q += “ AND status=?”; p.append(status)
if source:
q += “ AND source=?”; p.append(source)
if favs:
q += “ AND favorite=1”
sort_map = {“price_asc”: “price ASC”, “price_desc”: “price DESC”,
“sqft_desc”: “sqft DESC”, “newest”: “first_seen DESC”,
“source”: “source ASC, price ASC”}
q += f” ORDER BY {sort_map.get(sort, ‘price ASC’)}”
rows = conn.execute(q, p).fetchall()
conn.close()
return jsonify({“listings”: [dict(r) for r in rows], “count”: len(rows), “criteria”: CRITERIA})

@app.route(”/api/listings/<lid>”, methods=[“PATCH”])
def api_update(lid):
conn = get_db()
data = request.json
updates, params = [], []
for f in [“status”, “notes”, “favorite”]:
if f in data:
updates.append(f”{f}=?”); params.append(data[f])
if not updates:
return jsonify({“error”: “No fields”}), 400
params.append(lid)
conn.execute(f”UPDATE listings SET {’,’.join(updates)} WHERE id=?”, params)
conn.commit(); conn.close()
return jsonify({“success”: True})

@app.route(”/api/listings/<lid>”, methods=[“DELETE”])
def api_delete(lid):
conn = get_db()
conn.execute(“DELETE FROM listings WHERE id=?”, (lid,))
conn.commit(); conn.close()
return jsonify({“success”: True})

@app.route(”/api/refresh”, methods=[“POST”])
def api_refresh():
if scrape_status[“running”]:
return jsonify({“status”: “already_running”}), 409
def do_scrape():
scrape_status[“running”] = True
scrape_status[“message”] = “Starting scrape…”
try:
found, matched = run_scrape()
scrape_status[“message”] = f”Done — {found} found, {matched} matched criteria”
scrape_status[“last_run”] = datetime.now().isoformat()
except Exception as e:
scrape_status[“message”] = f”Failed: {e}”
finally:
scrape_status[“running”] = False
threading.Thread(target=do_scrape, daemon=True).start()
return jsonify({“status”: “started”})

@app.route(”/api/refresh/status”)
def api_refresh_status():
return jsonify(scrape_status)

@app.route(”/api/stats”)
def api_stats():
conn = get_db()
active = conn.execute(“SELECT COUNT(*) FROM listings WHERE status=‘active’”).fetchone()[0]
stale = conn.execute(“SELECT COUNT(*) FROM listings WHERE status=‘stale’”).fetchone()[0]
by_source = {}
for r in conn.execute(“SELECT source, COUNT(*) c FROM listings WHERE status=‘active’ GROUP BY source”):
by_source[r[“source”]] = r[“c”]
pr = conn.execute(“SELECT MIN(price) mn, MAX(price) mx, AVG(price) av FROM listings WHERE status=‘active’ AND price IS NOT NULL”).fetchone()
favs = conn.execute(“SELECT COUNT(*) FROM listings WHERE favorite=1”).fetchone()[0]
last = conn.execute(“SELECT timestamp FROM scrape_log ORDER BY id DESC LIMIT 1”).fetchone()
conn.close()
return jsonify({
“total_active”: active, “total_stale”: stale, “by_source”: by_source,
“price”: {“min”: pr[“mn”], “max”: pr[“mx”], “avg”: round(pr[“av”]) if pr[“av”] else None},
“favorites”: favs, “last_scrape”: last[“timestamp”] if last else None,
})

@app.route(”/api/export”)
def api_export():
conn = get_db()
rows = conn.execute(“SELECT source,name,address,price,sqft,bedrooms,url,status,favorite,notes FROM listings WHERE status=‘active’ ORDER BY price”).fetchall()
conn.close()
output = io.StringIO()
writer = csv.writer(output)
writer.writerow([“Source”,“Name”,“Address”,“Price”,“SqFt”,“Bedrooms”,“URL”,“Status”,“Favorite”,“Notes”])
for r in rows:
writer.writerow(list(r))
return Response(output.getvalue(), mimetype=“text/csv”,
headers={“Content-Disposition”: “attachment;filename=apartments.csv”})

@app.route(”/api/scrape-log”)
def api_scrape_log():
conn = get_db()
rows = conn.execute(“SELECT * FROM scrape_log ORDER BY id DESC LIMIT 30”).fetchall()
conn.close()
return jsonify([dict(r) for r in rows])

# ─── STARTUP ───────────────────────────────────────────────────────────────────

init_db()

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, 5151))
app.run(host=“0.0.0.0”, port=port, debug=True)
