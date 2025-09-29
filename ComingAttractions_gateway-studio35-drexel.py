import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# A friendly UA helps avoid blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    )
}

# -------------------------------
# Helpers
# -------------------------------
def parse_time_12h_to_24h(tstr: str):
    """Parse '7:30 pm' or '7 pm' -> 'HH:MM' (24h) or None."""
    t = (tstr or "").strip().lower()
    for fmt in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(t, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None

def parse_gateway_runtime_minutes(movie_url: str) -> int | None:
    """Fetch Gateway movie page and parse 'Run Time: ### min.' -> minutes."""
    try:
        r = requests.get(movie_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        s = BeautifulSoup(r.text, "html.parser")
        specs = s.select_one("div.show-description p.show-specs")
        if not specs:
            return None
        text = specs.get_text(" ", strip=True)
        # Look for 'Run Time: 123 min.'
        m = re.search(r"Run Time:\s*(\d+)\s*min\.?", text, flags=re.I)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None

def parse_studio35_runtime_minutes_from_soup(movie_soup: BeautifulSoup) -> int | None:
    """
    Parse Studio 35 runtime from JSON-LD duration 'PT#H#M' on the movie page.
    Returns total minutes or None.
    """
    try:
        # There can be multiple LD+JSON blocks; find the Movie one with a duration
        for tag in movie_soup.find_all("script", {"type": "application/ld+json"}):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            data = json.loads(raw)
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") == "Movie":
                    duration = obj.get("duration")
                    if not duration:
                        continue
                    # ISO 8601 duration like PT2H42M or PT102M
                    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration)
                    if not m:
                        continue
                    hours = int(m.group(1) or 0)
                    minutes = int(m.group(2) or 0)
                    return hours * 60 + minutes
    except Exception:
        return None
    return None

def parse_drexel_runtime_minutes(descriptive_text: str) -> int | None:
    """Parse something like '...| 2 hr 35 min' or '...| 107 min'."""
    if not descriptive_text:
        return None
    text = descriptive_text.strip()
    # Prefer hours + minutes
    m = re.search(r"(\d+)\s*hr[s]?\s*(\d+)\s*min", text, flags=re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # Or only minutes
    m = re.search(r"(\d+)\s*min", text, flags=re.I)
    if m:
        return int(m.group(1))
    return None

# -------------------------------
# Gateway Film Center
#   - Upcoming page (+ labels like 4K)
#   - Homepage Now Playing
#   - Runtime via movie page
# -------------------------------
def fetch_gateway():
    upcoming_url = "https://gatewayfilmcenter.org/our-program/upcoming-films/"
    home_url = "https://gatewayfilmcenter.org/"

    # Cache for movie page requests (to get runtime once per unique URL)
    runtime_cache: dict[str, int | None] = {}

    def showtimes_from_block(block: BeautifulSoup):
        """Read date-list epochs + times; capture suffixes like '4K' if present near time link."""
        # Map epoch -> date
        date_map: dict[str, datetime] = {}
        for li in block.select("ul.datelist li.show-date[data-date]"):
            try:
                epoch = li.get("data-date", "").strip()
                if not epoch:
                    continue
                d = datetime.fromtimestamp(int(epoch))
                date_map[epoch] = d
            except Exception:
                continue

        shows: list[str] = []
        # Look across all li[data-date] in the times list
        for li in block.select("ol.showtimes li[data-date]"):
            epoch = li.get("data-date", "").strip()
            if epoch not in date_map:
                continue
            a = li.find("a", class_="showtime")
            if not a:
                continue
            time_text = (a.get_text(strip=True) or "").strip()
            t24 = parse_time_12h_to_24h(time_text)
            if not t24:
                continue

            # Pull possible label/suffix next to/within this li (e.g., series pill or text after time)
            label = None

            # Look for a pill near this item and reduce '4K Restoration' -> '4K'
            pill = li.find_next(lambda tag: tag.name == "a" and "pill" in (tag.get("class") or []))
            if pill:
                label_candidate = pill.get_text(strip=True)
                if re.search(r"\b4K\b", label_candidate, flags=re.I):
                    label = "4K"
                else:
                    short = label_candidate[:20].strip()
                    if short:
                        label = short

            if not label:
                trailing_text = (li.get_text(" ", strip=True) or "")
                if re.search(r"\b4K\b", trailing_text, flags=re.I):
                    label = "4K"

            d = date_map[epoch]
            shows.append(f"{d.strftime('%Y-%m-%d')} {t24}" + (f" ({label})" if label else ""))

        return sorted(set(shows))

    def collect_from_upcoming():
        out = {}
        try:
            r = requests.get(upcoming_url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            blocks = soup.select("div.showtimes-description")
            if not blocks:
                # fallback
                for h2 in soup.find_all("h2", class_="show-title"):
                    b = h2.find_parent("div", class_="showtimes-description")
                    if b:
                        blocks.append(b)

            for block in blocks:
                title_el = block.select_one("h2.show-title a.title, h2.show-title a")
                if title_el:
                    title = title_el.get_text(strip=True)
                    link = title_el.get("href")
                else:
                    h2 = block.select_one("h2.show-title")
                    if not h2:
                        continue
                    title = h2.get_text(strip=True)
                    link = None

                showtimes = showtimes_from_block(block)

                runtime = None
                if link:
                    if link not in runtime_cache:
                        runtime_cache[link] = parse_gateway_runtime_minutes(link)
                    runtime = runtime_cache[link]

                key = link or title
                if key not in out:
                    out[key] = {"title": title, "url": link, "showtimes": set(), "runtime": runtime}
                out[key]["showtimes"].update(showtimes)
                if runtime and not out[key]["runtime"]:
                    out[key]["runtime"] = runtime
        except Exception:
            pass
        return out

    def collect_from_homepage():
        out = {}
        try:
            r = requests.get(home_url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            s = BeautifulSoup(r.text, "html.parser")

            links = set()
            now_playing = s.find(id="now-playing")
            if now_playing:
                for a in now_playing.select("a[href*='/movies/']"):
                    href = a.get("href")
                    if href:
                        links.add(urljoin(home_url, href))
            if not links:
                for a in s.select(".show a[href*='/movies/']"):
                    href = a.get("href")
                    if href:
                        links.add(urljoin(home_url, href))

            for murl in sorted(links):
                try:
                    mr = requests.get(murl, headers=HEADERS, timeout=30)
                    mr.raise_for_status()
                    ms = BeautifulSoup(mr.text, "html.parser")

                    # Title
                    title_el = ms.select_one("h2.show-title a.title, h2.show-title a") or ms.select_one("h1, h2.show-title")
                    title = title_el.get_text(strip=True) if title_el else "Unknown"

                    # Showtimes via epoch map + time anchors
                    showtimes = []
                    epoch_map = {}
                    for li in ms.select("ul.datelist li.show-date[data-date]"):
                        try:
                            epoch = li.get("data-date", "").strip()
                            if epoch:
                                epoch_map[epoch] = datetime.fromtimestamp(int(epoch))
                        except Exception:
                            continue
                    for li in ms.select("ol.showtimes li[data-date]"):
                        epoch = li.get("data-date", "").strip()
                        if epoch not in epoch_map:
                            continue
                        a = li.find("a", class_="showtime")
                        if not a:
                            continue
                        t24 = parse_time_12h_to_24h(a.get_text(strip=True))
                        if not t24:
                            continue

                        label = None
                        pill = li.find_next(lambda tag: tag.name == "a" and "pill" in (tag.get("class") or []))
                        if pill:
                            pill_text = pill.get_text(strip=True)
                            if re.search(r"\b4K\b", pill_text, flags=re.I):
                                label = "4K"
                            else:
                                short = pill_text[:20].strip()
                                if short:
                                    label = short
                        if not label:
                            li_text = (li.get_text(" ", strip=True) or "")
                            if re.search(r"\b4K\b", li_text, flags=re.I):
                                label = "4K"

                        d = epoch_map[epoch]
                        showtimes.append(f"{d.strftime('%Y-%m-%d')} {t24}" + (f" ({label})" if label else ""))

                    # Runtime (cache by URL)
                    runtime = parse_gateway_runtime_minutes(murl)

                    out[murl] = {
                        "title": title,
                        "url": murl,
                        "showtimes": set(showtimes),
                        "runtime": runtime
                    }
                except Exception:
                    continue
        except Exception:
            pass
        return out

    # Merge both sources
    by_key = {}
    part_a = collect_from_upcoming()
    part_b = collect_from_homepage()

    for k, v in part_a.items():
        by_key.setdefault(k, {"title": v["title"], "url": v["url"], "showtimes": set(), "runtime": v.get("runtime")})
        by_key[k]["showtimes"].update(v["showtimes"])
        if v.get("runtime"):
            by_key[k]["runtime"] = v["runtime"]

    for k, v in part_b.items():
        by_key.setdefault(k, {"title": v["title"], "url": v["url"], "showtimes": set(), "runtime": v.get("runtime")})
        by_key[k]["showtimes"].update(v["showtimes"])
        if v.get("runtime"):
            by_key[k]["runtime"] = v["runtime"]

    results = []
    for obj in by_key.values():
        results.append({
            "title": obj["title"],
            "url": obj["url"],
            "runtime": obj.get("runtime"),
            "showtimes": sorted(obj["showtimes"])
        })

    print(f"Gateway: saved {len(results)} shows")
    return results

# -------------------------------
# Studio 35 (Playwright; showtimes + runtime from JSON-LD)
# -------------------------------
def fetch_studio35():
    base_url = "https://studio35.com/home"
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(base_url, timeout=60000)

        movie_links = page.query_selector_all("a[href*='/movie/']")
        links = sorted(set([ml.get_attribute("href") for ml in movie_links if ml.get_attribute("href")]))

        for link in links:
            full_link = "https://studio35.com" + link if link.startswith("/") else link
            page.goto(full_link, timeout=60000)

            # Pull full HTML for BeautifulSoup (for runtime JSON-LD)
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Title
            title_el = soup.select_one("h1[itemprop='name']") or soup.find("h1")
            title = title_el.get_text(strip=True) if title_el else "Unknown"

            # Showtimes (h2 a[href*='/checkout/showing/'] with text like "September 24, 8:45 pm")
            showtimes = []
            for st in soup.select("h2 a[href*='/checkout/showing/']"):
                text = st.get_text(strip=True)
                try:
                    dt = datetime.strptime(f"{text} {datetime.now().year}", "%B %d, %I:%M %p %Y")
                    showtimes.append(dt.strftime("%Y-%m-%d %H:%M"))
                except ValueError:
                    continue

            # Runtime from JSON-LD (minutes)
            runtime = parse_studio35_runtime_minutes_from_soup(soup)

            results.append({
                "title": title,
                "url": full_link,
                "runtime": runtime,
                "showtimes": sorted(set(showtimes))
            })

        browser.close()

    print(f"Studio 35: saved {len(results)} shows")
    return results

# -------------------------------
# Drexel Theatre (list page; showtimes + runtime from descriptive)
# -------------------------------
def fetch_drexel():
    url = "https://prod1.agileticketing.net/websales/pages/list.aspx?epguid=ab0b2f82-403c-4972-9998-5475e7dcfa0e&"
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    items = soup.find_all("div", class_="ItemInfo")
    for item in items:
        title_tag = item.find("h3", class_="Name")
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)

        # Descriptive line for runtime
        desc_div = title_tag.find("div", class_="Descriptive")
        runtime = parse_drexel_runtime_minutes(desc_div.get_text(" ", strip=True) if desc_div else "")

        link_tag = item.find_next("a", class_="ViewLink")
        link = "https://prod1.agileticketing.net/websales/pages/" + link_tag["href"] if link_tag else None

        showtimes = []
        for st_block in item.find_all("div", class_="ShowingTimes"):
            date_span = st_block.find("span", class_="Date")
            if not date_span:
                continue
            date_text = date_span.get_text(strip=True)  # e.g. "Mon, Sep 29"

            for time_a in st_block.select("span.Showing a"):
                time_text = time_a.get_text(strip=True)  # e.g. "7:00 PM"
                # Append current year to avoid ambiguity
                try:
                    dt = datetime.strptime(f"{date_text} {time_text} {datetime.now().year}", "%a, %b %d %I:%M %p %Y")
                    showtimes.append(dt.strftime("%Y-%m-%d %H:%M"))
                except ValueError:
                    continue

        results.append({
            "title": title,
            "url": link,
            "runtime": runtime,
            "showtimes": sorted(set(showtimes))
        })

    print(f"Drexel: saved {len(results)} shows")
    return results

# -------------------------------
# Combine All
# -------------------------------
def fetch_all_cinemas():
    data = {
        "gateway": fetch_gateway(),
        "studio35": fetch_studio35(),
        "drexel": fetch_drexel()
    }

    with open("cinemas.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print("Saved combined results to cinemas.json")

if __name__ == "__main__":
    fetch_all_cinemas()
