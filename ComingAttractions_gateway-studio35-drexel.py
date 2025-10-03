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
        m = re.search(r"Run Time:\s*(\d+)\s*min\.?", text, flags=re.I)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None

def parse_studio35_runtime_minutes(movie_soup: BeautifulSoup) -> int | None:
    """
    Prefer JSON-LD Movie.duration (PT#H#M). Fallback to microdata span[itemprop="duration"].
    Returns minutes or None.
    """
    # 1) JSON-LD blocks: there may be multiple; one is MovieTheater, one is Movie
    try:
        for tag in movie_soup.select("script[type='application/ld+json']"):
            text = tag.string or tag.get_text(strip=True) or ""
            if not text:
                continue
            # Some pages embed multiple JSON-LD objects; handle both object and array
            data = json.loads(text)
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") == "Movie":
                    dur = obj.get("duration")
                    if dur:
                        m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?$", dur)
                        if m:
                            hours = int(m.group(1) or 0)
                            minutes = int(m.group(2) or 0)
                            return hours * 60 + minutes
        # 2) Microdata fallback: <span itemprop="duration">PT2H13M</span>
        dur_span = movie_soup.select_one("[itemprop='duration']")
        if dur_span and dur_span.get_text(strip=True):
            dur = dur_span.get_text(strip=True)
            m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?$", dur)
            if m:
                hours = int(m.group(1) or 0)
                minutes = int(m.group(2) or 0)
                return hours * 60 + minutes
    except Exception:
        pass
    return None


def parse_drexel_runtime_minutes(descriptive_text: str) -> int | None:
    """Parse something like '...| 2 hr 35 min' or '...| 107 min'."""
    if not descriptive_text:
        return None
    text = descriptive_text.strip()
    m = re.search(r"(\d+)\s*hr[s]?\s*(\d+)\s*min", text, flags=re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+)\s*min", text, flags=re.I)
    if m:
        return int(m.group(1))
    return None

# -------------------------------
# Gateway Film Center
# -------------------------------
def fetch_gateway():
    upcoming_url = "https://gatewayfilmcenter.org/our-program/upcoming-films/"
    home_url = "https://gatewayfilmcenter.org/"

    runtime_cache: dict[str, int | None] = {}

    def parse_showtimes_from_block(block: BeautifulSoup):
        date_map: dict[str, datetime] = {}
        for li in block.select("ul.datelist li.show-date[data-date]"):
            try:
                epoch = li.get("data-date", "").strip()
                if epoch:
                    d = datetime.fromtimestamp(int(epoch))
                    date_map[epoch] = d
            except Exception:
                continue

        shows: list[str] = []
        for li in block.select("ol.showtimes li[data-date]"):
            epoch = li.get("data-date", "").strip()
            if epoch not in date_map:
                continue
            a = li.find("a", class_="showtime")
            if not a:
                continue

            # Grab full raw text of this li to capture anything after the time
            raw_text = li.get_text(" ", strip=True)
            time_match = re.match(r"(\d{1,2}(:\d{2})?\s*(?:am|pm))", raw_text, flags=re.I)
            if not time_match:
                continue
            time_str = time_match.group(1)
            t24 = parse_time_12h_to_24h(time_str)
            if not t24:
                continue

            # Grab whatever follows the time and include it verbatim
            extra_text = raw_text[len(time_match.group(0)):].strip()

            d = date_map[epoch]
            if extra_text:
                shows.append(f"{d.strftime('%Y-%m-%d')} {t24} ({extra_text})")
            else:
                shows.append(f"{d.strftime('%Y-%m-%d')} {t24}")

        return sorted(set(shows))

    def collect_from_upcoming():
        out = {}
        try:
            r = requests.get(upcoming_url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            blocks = soup.select("div.showtimes-description")
            if not blocks:
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

                showtimes = parse_showtimes_from_block(block)

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

                    title_el = ms.select_one("h2.show-title a.title, h2.show-title a") or ms.select_one("h1, h2.show-title")
                    title = title_el.get_text(strip=True) if title_el else "Unknown"

                    # Reuse same showtime parsing logic
                    showtimes = parse_showtimes_from_block(ms)

                    if murl not in runtime_cache:
                        runtime_cache[murl] = parse_gateway_runtime_minutes(murl)
                    runtime = runtime_cache[murl]

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
# Studio 35
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

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            title_el = soup.select_one("h1[itemprop='name']") or soup.find("h1")
            title = title_el.get_text(strip=True) if title_el else "Unknown"

            showtimes = []
            for st in soup.select("h2 a[href*='/checkout/showing/']"):
                text = st.get_text(strip=True)
                try:
                    dt = datetime.strptime(f"{text} {datetime.now().year}", "%B %d, %I:%M %p %Y")
                    showtimes.append(dt.strftime("%Y-%m-%d %H:%M"))
                except ValueError:
                    continue

            runtime = parse_studio35_runtime_minutes(soup)

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
# Drexel Theatre
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

        desc_div = title_tag.find("div", class_="Descriptive")
        runtime = parse_drexel_runtime_minutes(desc_div.get_text(" ", strip=True) if desc_div else "")

        link_tag = item.find_next("a", class_="ViewLink")
        link = "https://prod1.agileticketing.net/websales/pages/" + link_tag["href"] if link_tag else None

        showtimes = []
        for st_block in item.find_all("div", class_="ShowingTimes"):
            date_span = st_block.find("span", class_="Date")
            if not date_span:
                continue
            date_text = date_span.get_text(strip=True)

            for time_a in st_block.select("span.Showing a"):
                time_text = time_a.get_text(strip=True)
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
