"""
Travian Legends Map Scraper
Scans the entire 401x401 map and exports all tile data to JSON/CSV.
Uses map/position endpoint with human-like delays to stay safe.
"""

import requests
import json
import csv
import time
import random
import re
import os
from datetime import datetime

BASE = "https://ts1.x1.europe.travian.com"
JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJzdWIiOiI2WFMyTDJncVg3bFdUWTFvbGllZGc0dm5xcG5hQjFZViIsImF1ZCI6IjdkOTQ1ODAwLTExOTEtMTFmMS02NTAxLTAxMDAwMDAwNWU0YyIsImV4cCI6MTc3Mzk0MDAzNCwicHJvcGVydGllcyI6eyJoYXNoIjoiYzRlNWM0ZTVjNGU1YzRlNTJrb2VBQldyd25iV0pySmIiLCJtb2JpbGVPcHRpbWl6YXRpb25zIjp0cnVlLCJsb2JieSI6dHJ1ZSwiZGlkIjo2OTEzMCwibGFuZ3VhZ2UiOiJlbi1VUyIsInZpbGxhZ2VQZXJzcGVjdGl2ZSI6InBlcnNwZWN0aXZlQnVpbGRpbmdzIn19.FF2M-ILhaDObJoC2N_UW8-9UozI-9DVjeoOTzNQiyxFKGRKeddDQo1sNODyyTawUPd-Op-viDM3bJgPfVFFvRFmqlzsbhh7kSqICFP2koGZkvMO2Ulvoiz4AtN2fgWqMgW3my3Yvj8dCg5OiqoBG7YB-Ok6tUYGjZZZHBwTXph9nEPiNOQf7ypsFsHa4F0b3JuSw9O9tE0qSZnBtEr013kjDrMdOalYjaK6VHG0twuqzkzkWgd0fSUmjf0YdcPNt_9keK0__1UreGEsEG9KX-1LGEPxFiN9LUKqFpmMXDcg5BLEoyk7VEifSwo25sVoOc9iRP8ulpoMKCSnX8Dftog"

COOKIES = {"JWT": JWT}
HEADERS = {"Content-Type": "application/json", "X-Version": "389"}
SESSION = requests.Session()
SESSION.cookies.update(COOKIES)
SESSION.headers.update(HEADERS)

# Template variable mappings
TRIBE_MAP = {
    "{a.v1}": "Romans", "{a.v2}": "Teutons", "{a.v3}": "Gauls",
    "{a.v4}": "Nature", "{a.v5}": "Natars", "{a.v6}": "Egyptians",
    "{a.v7}": "Huns", "{a.v8}": "Spartans", "{a.v9}": "Vikings"
}

FIELD_MAP = {
    "{k.f1}": "1-1-1-15", "{k.f2}": "1-1-1-15", "{k.f3}": "3-3-3-9",
    "{k.f4}": "4-4-4-6", "{k.f5}": "4-5-3-6", "{k.f6}": "5-3-4-6",
    "{k.f7}": "3-4-5-6", "{k.f8}": "4-4-3-7", "{k.f9}": "3-4-4-7",
    "{k.f10}": "4-3-4-7", "{k.f11}": "3-3-3-9", "{k.f12}": "4-4-4-6",
}


def resolve_templates(text):
    """Resolve Travian template variables in text."""
    if not text:
        return text
    for k, v in TRIBE_MAP.items():
        text = text.replace(k, v)
    for k, v in FIELD_MAP.items():
        text = text.replace(k, v)
    text = text.replace("{k.dt}", "Village")
    text = text.replace("{k.vt}", "Unoccupied")
    text = text.replace("{k.spieler}", "Player")
    text = text.replace("{k.einwohner}", "Population")
    text = text.replace("{k.allianz}", "Alliance")
    text = text.replace("{k.volk}", "Tribe")
    # Strip HTML
    text = re.sub(r'<[^>]+>', ' ', text)
    # Strip unicode control chars
    text = re.sub(r'[\u200d\u200e\u200f\u202a-\u202e]', '', text)
    text = re.sub(r'&[a-z]+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_tile(tile):
    """Parse a tile response into a clean dict."""
    pos = tile["position"]
    title = resolve_templates(tile.get("title", ""))
    text = resolve_templates(tile.get("text", ""))
    
    result = {
        "x": pos["x"],
        "y": pos["y"],
        "title": title,
        "uid": tile.get("uid"),        # player ID
        "aid": tile.get("aid"),        # alliance ID
        "did": tile.get("did"),        # village ID
    }
    
    # Extract player name, population, alliance, tribe from text
    if "Player" in text:
        m = re.search(r'Player\s+(.+?)(?:\s+Population|\s*$)', text)
        if m:
            result["player"] = m.group(1).strip()
    if "Population" in text:
        m = re.search(r'Population\s+(\d+)', text)
        if m:
            result["population"] = int(m.group(1))
    if "Alliance" in text:
        m = re.search(r'Alliance\s+(.+?)\s+Tribe\s+', text)
        if m:
            result["alliance"] = m.group(1).strip()
        else:
            # Alliance without Tribe following
            m = re.search(r'Alliance\s+(.+?)(?:\s*$)', text)
            if m:
                result["alliance"] = m.group(1).strip()
    if "Tribe" in text:
        m = re.search(r'Tribe\s+(Romans|Teutons|Gauls|Egyptians|Huns|Natars|Spartans|Vikings|Nature)', text)
        if m:
            result["tribe"] = m.group(1).strip()
    
    # Determine tile type
    if result["uid"]:
        result["type"] = "village"
    elif "Forest" in title or "Lake" in title or "Clay" in title or "Iron" in title or "Cropland" in title:
        result["type"] = "oasis"
    elif "Unoccupied" in title:
        result["type"] = "unoccupied"
    else:
        result["type"] = "other"
    
    return result


def fetch_area(x, y):
    """Fetch tile data around a position. Returns list of parsed tiles."""
    resp = SESSION.post(f"{BASE}/api/v1/map/position", json={
        "data": {"x": x, "y": y, "zoomLevel": 1, "ignorePositions": []}
    })
    
    if resp.status_code != 200:
        print(f"  [!] HTTP {resp.status_code} at ({x}, {y})")
        return []
    
    try:
        data = resp.json()
    except:
        print(f"  [!] Invalid JSON at ({x}, {y})")
        return []
    
    if "error" in data:
        print(f"  [!] API error at ({x}, {y}): {data.get('message', data['error'])}")
        return []
    
    return [parse_tile(t) for t in data.get("tiles", [])]


def save_checkpoint(tiles, outdir):
    """Save incremental checkpoint to avoid data loss."""
    path = os.path.join(outdir, "map_checkpoint.json")
    data = sorted(tiles.values(), key=lambda t: (t["x"], t["y"]))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    villages = sum(1 for t in tiles.values() if t.get("type") == "village")
    print(f"  [checkpoint] saved {len(data)} tiles, {villages} villages")


def scan_map(existing=None, outdir="."):
    """Scan the entire map in a grid pattern."""
    all_tiles = dict(existing) if existing else {}
    
    step = 10
    coords = []
    for x in range(-200, 201, step):
        for y in range(-200, 201, step):
            coords.append((x, y))
    
    # Skip positions we already have good coverage for
    if all_tiles:
        covered = set()
        for (tx, ty) in all_tiles:
            # Round to nearest grid point
            gx = round(tx / step) * step
            gy = round(ty / step) * step
            covered.add((gx, gy))
        before = len(coords)
        coords = [(x, y) for (x, y) in coords if (x, y) not in covered]
        print(f"Skipping {before - len(coords)} already-covered positions, {len(coords)} remaining")
    
    total = len(coords)
    if total == 0:
        print("All positions already scanned!")
        return all_tiles
    
    print(f"Scanning {total} positions...")
    print(f"Estimated time: {total * 2 / 60:.0f}-{total * 4 / 60:.0f} minutes")
    print()
    
    start_time = time.time()
    errors = 0
    
    for i, (x, y) in enumerate(coords):
        tiles = fetch_area(x, y)
        
        if not tiles:
            errors += 1
            if errors > 10:
                print("[!] Too many errors, saving and stopping.")
                save_checkpoint(all_tiles, outdir)
                break
        else:
            errors = 0
        
        for t in tiles:
            key = (t["x"], t["y"])
            all_tiles[key] = t
        
        # Progress + incremental save every 100 positions
        if (i + 1) % 20 == 0 or i == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / rate / 60 if rate > 0 else 0
            villages = sum(1 for t in all_tiles.values() if t["type"] == "village")
            print(f"  [{i+1}/{total}] {len(all_tiles)} tiles, {villages} villages, ~{remaining:.1f}min left")
        
        if (i + 1) % 100 == 0:
            save_checkpoint(all_tiles, outdir)
        
        delay = random.uniform(1.5, 3.0)
        time.sleep(delay)
    
    # Final checkpoint
    save_checkpoint(all_tiles, outdir)
    
    elapsed = time.time() - start_time
    print(f"\nDone! {len(all_tiles)} tiles scanned in {elapsed/60:.1f} minutes")
    return all_tiles


def export_json(tiles, filename):
    """Export tiles to JSON."""
    data = sorted(tiles.values(), key=lambda t: (t["x"], t["y"]))
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(data)} tiles to {filename}")


def export_csv(tiles, filename):
    """Export tiles to CSV."""
    data = sorted(tiles.values(), key=lambda t: (t["x"], t["y"]))
    fields = ["x", "y", "type", "title", "uid", "aid", "did", "player", "population", "alliance", "tribe"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)
    print(f"Exported {len(data)} tiles to {filename}")


def export_villages_csv(tiles, filename):
    """Export only villages to a separate CSV."""
    villages = [t for t in tiles.values() if t["type"] == "village"]
    villages.sort(key=lambda t: -(t.get("population", 0)))
    fields = ["x", "y", "title", "player", "population", "alliance", "tribe", "uid", "aid", "did"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(villages)
    print(f"Exported {len(villages)} villages to {filename}")


def print_stats(tiles):
    """Print summary statistics."""
    villages = [t for t in tiles.values() if t["type"] == "village"]
    oases = [t for t in tiles.values() if t["type"] == "oasis"]
    unoccupied = [t for t in tiles.values() if t["type"] == "unoccupied"]
    
    players = set(t.get("uid") for t in villages if t.get("uid"))
    alliances = set(t.get("aid") for t in villages if t.get("aid"))
    
    print(f"\n{'='*50}")
    print(f"MAP STATISTICS")
    print(f"{'='*50}")
    print(f"Total tiles scanned:  {len(tiles)}")
    print(f"Villages:             {len(villages)}")
    print(f"Oases:                {len(oases)}")
    print(f"Unoccupied land:      {len(unoccupied)}")
    print(f"Unique players:       {len(players)}")
    print(f"Unique alliances:     {len(alliances)}")
    
    if villages:
        pops = [t.get("population", 0) for t in villages if t.get("population")]
        if pops:
            print(f"Avg village pop:      {sum(pops)/len(pops):.0f}")
            print(f"Max village pop:      {max(pops)}")
    
    # Top alliances
    alliance_counts = {}
    for v in villages:
        a = v.get("alliance", "None")
        alliance_counts[a] = alliance_counts.get(a, 0) + 1
    
    top = sorted(alliance_counts.items(), key=lambda x: -x[1])[:10]
    print(f"\nTop 10 Alliances:")
    for name, count in top:
        print(f"  {name}: {count} villages")


def load_existing(outdir):
    """Load tiles from any existing map JSON files to resume."""
    tiles = {}
    import glob
    for f in glob.glob(os.path.join(outdir, "map_*.json")):
        print(f"Loading existing data from {os.path.basename(f)}...")
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            for t in data:
                key = (t["x"], t["y"])
                tiles[key] = t
    if tiles:
        print(f"Loaded {len(tiles)} existing tiles")
    return tiles


if __name__ == "__main__":
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    outdir = os.path.dirname(os.path.abspath(__file__))
    
    print(f"Travian Map Scraper — ts1.x1.europe")
    print(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Quick auth test
    print("Testing auth...")
    resp = SESSION.post(f"{BASE}/api/v1/graphql", json={"query": "{ownPlayer{name}}"})
    try:
        name = resp.json()["data"]["ownPlayer"]["name"]
        print(f"Logged in as: {name}\n")
    except:
        print("[!] Auth failed! JWT may be expired.")
        print(f"Response: {resp.text[:200]}")
        exit(1)
    
    # Load existing data for resume
    existing = load_existing(outdir)
    tiles = scan_map(existing, outdir)
    
    export_json(tiles, os.path.join(outdir, f"map_{timestamp}.json"))
    export_csv(tiles, os.path.join(outdir, f"map_{timestamp}.csv"))
    export_villages_csv(tiles, os.path.join(outdir, f"villages_{timestamp}.csv"))
    print_stats(tiles)
