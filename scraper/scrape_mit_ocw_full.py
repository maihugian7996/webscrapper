"""Full recursive scraper for MIT OCW 6.0001:
Crawls and extracts content from every single page, subpage, video, and resource file
under https://ocw.mit.edu/courses/6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ocw.mit.edu/courses/6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/"
OUTPUT_PATH = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output\mit_ocw_6_0001_complete.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
})

def classify_page(url: str) -> str:
    u = url.lower()
    if u.rstrip('/') == BASE_URL.rstrip('/'):
        return "course_home"
    if "/pages/syllabus" in u:
        return "syllabus"
    if "/pages/readings" in u:
        return "readings"
    if "/pages/instructor-insights" in u:
        return "instructor_insights"
    if "/video_galleries/lecture-videos" in u:
        return "lecture_video_gallery"
    if "/pages/lecture-slides-code" in u:
        return "lecture_slides_and_code"
    if "/pages/in-class-questions-and-video-solutions" in u:
        if "video-solutions" in u:
            return "in_class_video_solutions"
        return "in_class_questions"
    if "/pages/assignments" in u:
        return "assignments"
    if "/resources/lecture-" in u:
        return "lecture_video_resource"
    if "/resources/" in u:
        return "resource_page"
    if "/download" in u:
        return "download_page"
    return "course_page"

def extract_page_data(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. Page Title
    title = ""
    for sel in ["h1", "h2.video-title", "h5.video-title", "title"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(" ", strip=True)
            break
    if not title and soup.title:
        title = soup.title.string.strip()

    # 2. Main content text
    main_el = soup.find("main", id="course-content-section") or soup.find("article", class_="content") or soup.find("main") or soup.body
    content_text = ""
    if main_el:
        # Remove nav, script, style
        for unwanted in main_el.find_all(["nav", "script", "style", "footer", "header"]):
            unwanted.decompose()
        content_text = " ".join(main_el.get_text(" ", strip=True).split())

    # 3. Downloadable resource links on this page (PDF, PY, ZIP, etc.)
    downloads = []
    seen_downloads = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_href = urljoin(url, href).split("#")[0]
        ext = Path(urlsplit(full_href).path).suffix.lower()
        if ext in [".pdf", ".py", ".zip", ".tar", ".gz", ".m"]:
            if full_href not in seen_downloads:
                seen_downloads.add(full_href)
                downloads.append({
                    "filename": Path(urlsplit(full_href).path).name,
                    "file_type": ext.lstrip("."),
                    "link_text": a.get_text(" ", strip=True),
                    "url": full_href
                })

    # 4. Embedded / linked YouTube videos
    youtube_videos = []
    seen_yt = set()
    
    # Iframes
    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"]
        m = re.search(r"youtube\.com/embed/([a-zA-Z0-9_-]+)", src)
        if m:
            yid = m.group(1)
            if yid not in seen_yt:
                seen_yt.add(yid)
                youtube_videos.append({
                    "youtube_id": yid,
                    "watch_url": f"https://www.youtube.com/watch?v={yid}",
                    "embed_url": src
                })

    # YouTube links or thumbnails
    for img in soup.find_all("img", src=True):
        m = re.search(r"img\.youtube\.com/vi/([a-zA-Z0-9_-]+)", img["src"])
        if m:
            yid = m.group(1)
            if yid not in seen_yt:
                seen_yt.add(yid)
                youtube_videos.append({
                    "youtube_id": yid,
                    "watch_url": f"https://www.youtube.com/watch?v={yid}",
                    "thumbnail_url": img["src"]
                })

    # Sub-links under this course
    internal_links = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        full_a = urljoin(url, a["href"]).split("#")[0].rstrip("/")
        if full_a.startswith(BASE_URL.rstrip("/")) and full_a not in seen_links and full_a != url.rstrip("/"):
            seen_links.add(full_a)
            internal_links.append(full_a)

    return {
        "url": url,
        "page_type": classify_page(url),
        "title": title,
        "content_length": len(content_text),
        "content_text": content_text,
        "downloads_count": len(downloads),
        "downloads": downloads,
        "youtube_videos_count": len(youtube_videos),
        "youtube_videos": youtube_videos,
        "internal_links_count": len(internal_links),
        "internal_links": internal_links
    }

def fetch_page(url: str) -> tuple[str, str | None]:
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code == 200:
                return url, r.text
            elif r.status_code in (429, 500, 502, 503):
                time.sleep(1.0 * (2 ** attempt))
        except Exception:
            if attempt == 2:
                return url, None
            time.sleep(1.0)
    return url, None

def main():
    print(f"Starting deep crawl of MIT OCW 6.0001 from {BASE_URL}...")
    start_time = time.time()

    visited_urls = set()
    to_visit = [BASE_URL]
    file_urls = set()
    scraped_pages = []

    # 1. Discover all URLs recursively
    print("Discovering all sub-pages...")
    while to_visit:
        batch = to_visit[:10]
        to_visit = to_visit[10:]
        
        # Filter already visited
        batch = [u for u in batch if u not in visited_urls]
        for u in batch:
            visited_urls.add(u)

        if not batch:
            continue

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(fetch_page, u): u for u in batch}
            for future in as_completed(future_to_url):
                url, html = future.result()
                if not html:
                    continue

                page_data = extract_page_data(url, html)
                scraped_pages.append(page_data)

                # Collect downloads
                for d in page_data["downloads"]:
                    file_urls.add((d["filename"], d["file_type"], d["url"]))

                # Add new internal links
                for link in page_data["internal_links"]:
                    # Don't queue static files
                    if any(link.lower().endswith(ext) for ext in [".pdf", ".py", ".zip", ".tar", ".gz"]):
                        file_urls.add((Path(urlsplit(link).path).name, Path(urlsplit(link).path).suffix.lstrip("."), link))
                    elif link not in visited_urls and link not in to_visit:
                        to_visit.append(link)

        print(f"  Visited: {len(visited_urls)} pages | Remaining in queue: {len(to_visit)} | Files: {len(file_urls)}")

    elapsed = time.time() - start_time
    print(f"\nCrawling complete in {elapsed:.1f}s!")
    print(f"Total HTML pages scraped: {len(scraped_pages)}")
    print(f"Total downloadable resource files discovered: {len(file_urls)}")

    # Aggregate all YouTube videos
    all_videos = {}
    for p in scraped_pages:
        for v in p["youtube_videos"]:
            yid = v["youtube_id"]
            if yid not in all_videos:
                all_videos[yid] = {
                    "youtube_id": yid,
                    "watch_url": v.get("watch_url"),
                    "found_on_page": p["url"],
                    "page_title": p["title"]
                }

    # Aggregate files list
    all_files_list = [
        {"filename": fn, "file_type": ft, "url": u}
        for fn, ft, u in sorted(file_urls)
    ]

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    output_data = {
        "metadata": {
            "course": "MIT OpenCourseWare 6.0001",
            "title": "Introduction to Computer Science and Programming in Python",
            "term": "Fall 2016",
            "instructors": ["Dr. Ana Bell", "Prof. Eric Grimson", "Prof. John Guttag"],
            "base_url": BASE_URL,
            "scraped_at": timestamp,
            "total_pages_scraped": len(scraped_pages),
            "total_downloadable_files": len(all_files_list),
            "total_youtube_videos": len(all_videos),
            "crawl_duration_seconds": round(elapsed, 1)
        },
        "all_downloadable_files": all_files_list,
        "all_youtube_videos": list(all_videos.values()),
        "pages": scraped_pages
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print(f"Saved complete scrape to {OUTPUT_PATH} (size: {OUTPUT_PATH.stat().st_size / 1024:.1f} KB)")

if __name__ == "__main__":
    main()
