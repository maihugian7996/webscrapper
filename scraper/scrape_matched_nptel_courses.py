"""Scrape detailed information for all matched NPTEL courses from courses_matched_nptel.json.

Fetches subject details (abstract, duration, exam date, syllabus PDF URL)
and course outline (units, weeks, lectures with YouTube links) for every unique NPTEL URL.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests

INPUT_PATH = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output\courses_matched_nptel.json")
OUTPUT_DETAILS_PATH = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output\nptel_matched_courses_details.json")
OUTPUT_ENRICHED_PATH = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output\courses_matched_nptel_detailed.json")

SESSION = requests.Session()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*"
}

def scrape_single_course(course_id: int, url: str) -> dict:
    """Scrape details and outline for a single NPTEL course."""
    api_url = f"https://nptel.ac.in/api/subject-details/{course_id}"
    details = {}
    
    # 1. Subject details API
    for attempt in range(3):
        try:
            r = SESSION.get(api_url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                details = r.json().get("data", {})
                break
            elif r.status_code in (429, 500, 502, 503):
                time.sleep(1.0 * (2 ** attempt))
        except Exception:
            if attempt == 2:
                break
            time.sleep(1.0)

    # 2. HTML fetch for units / lecture outline
    units_data = []
    total_lectures = 0
    for attempt in range(3):
        try:
            r_html = SESSION.get(url, headers=HEADERS, timeout=15)
            if r_html.status_code == 200:
                html = r_html.text
                idx = html.find("courseOutline:")
                if idx != -1:
                    u_idx = html.find("units:[", idx)
                    if u_idx != -1:
                        # Find units and lesson definitions
                        unit_blocks = re.findall(
                            r'\{id:\s*(\d+),\s*name:\s*"([^"]+)",\s*lessons:\s*\[(.*?)\]\}',
                            html[u_idx:u_idx + 800000]
                        )
                        for u_id, u_name, lessons_str in unit_blocks:
                            lessons = []
                            for lm in re.finditer(
                                r'\{id:\s*(\d+),\s*name:\s*"([^"]+)"(?:,\s*youtube_id:\s*"([^"]*)")?',
                                lessons_str
                            ):
                                lid, lname, yid = lm.group(1), lm.group(2), lm.group(3)
                                lname = lname.replace('\\t', ' ').replace('\\"', '"').strip()
                                lessons.append({
                                    "lesson_id": int(lid),
                                    "title": lname,
                                    "youtube_id": yid if yid else None,
                                    "youtube_url": f"https://www.youtube.com/watch?v={yid}" if yid else None
                                })
                            total_lectures += len(lessons)
                            units_data.append({
                                "unit_id": int(u_id),
                                "unit_name": u_name.strip(),
                                "lecture_count": len(lessons),
                                "lessons": lessons
                            })
                break
            elif r_html.status_code in (429, 500, 502, 503):
                time.sleep(1.0 * (2 ** attempt))
        except Exception:
            if attempt == 2:
                break
            time.sleep(1.0)

    intro_yt = details.get("intro_video")
    intro_url = f"https://www.youtube.com/watch?v={intro_yt}" if intro_yt else None

    # Determine syllabus url
    syllabus_pdf = details.get("syllabus_url")
    if not syllabus_pdf or "undefined" in syllabus_pdf:
        syllabus_pdf = f"https://archive.nptel.ac.in/content/syllabus_pdf/{course_id}.pdf"

    return {
        "nptel_course_id": course_id,
        "nptel_url": url,
        "noc_code": details.get("courseid"),
        "title": details.get("title"),
        "instructor": details.get("professor"),
        "institute": details.get("institutename"),
        "institute_logo": details.get("institute_logo"),
        "duration": details.get("duration"),
        "enrollment_period": details.get("enrollment"),
        "exam_registration_period": details.get("registration"),
        "exam_date": details.get("exam"),
        "syllabus_pdf_url": syllabus_pdf,
        "intro_video_url": intro_url,
        "course_abstract": details.get("course_abstract"),
        "total_units": len(units_data),
        "total_lectures": total_lectures,
        "units": units_data
    }

def main():
    print(f"Loading matches from {INPUT_PATH}...")
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        matched_data = json.load(f)

    matches = matched_data.get("matches", [])
    print(f"Total matched course records: {len(matches)}")

    # Extract unique (course_id, url)
    unique_courses = {}
    for m in matches:
        cid = m.get("nptel_course_id")
        url = m.get("nptel_url")
        if cid and url and cid not in unique_courses:
            unique_courses[cid] = url

    total_unique = len(unique_courses)
    print(f"Total unique NPTEL courses to scrape: {total_unique}")

    start_time = time.time()
    results = {}
    completed_count = 0

    # Use ThreadPoolExecutor with 6 workers (balanced speed and politeness)
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_cid = {
            executor.submit(scrape_single_course, cid, url): cid
            for cid, url in unique_courses.items()
        }

        for future in as_completed(future_to_cid):
            cid = future_to_cid[future]
            try:
                data = future.result()
                results[cid] = data
            except Exception as exc:
                print(f"Failed to scrape course {cid}: {exc}")
                results[cid] = {
                    "nptel_course_id": cid,
                    "nptel_url": unique_courses[cid],
                    "error": str(exc)
                }

            completed_count += 1
            if completed_count % 50 == 0 or completed_count == total_unique:
                elapsed = time.time() - start_time
                rate = completed_count / elapsed if elapsed > 0 else 0
                print(f"  Progress: {completed_count}/{total_unique} scraped ({completed_count/total_unique*100:.1f}%) [{rate:.1f} courses/s]")

    total_elapsed = time.time() - start_time
    print(f"\nAll {total_unique} courses scraped in {total_elapsed:.1f} seconds!")

    # 1. Save all scraped unique NPTEL course details
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    
    details_output = {
        "metadata": {
            "source": "https://nptel.ac.in",
            "scraped_at": timestamp,
            "total_courses_scraped": len(results),
            "courses_with_outline": sum(1 for c in results.values() if c.get("total_units", 0) > 0),
            "total_lectures_extracted": sum(c.get("total_lectures", 0) for c in results.values()),
            "elapsed_seconds": round(total_elapsed, 1)
        },
        "courses": list(results.values())
    }

    with open(OUTPUT_DETAILS_PATH, "w", encoding="utf-8") as f:
        json.dump(details_output, f, indent=2, ensure_ascii=False)
    print(f"Saved unique course details to {OUTPUT_DETAILS_PATH}")

    # 2. Enrich matched courses with details
    enriched_matches = []
    for m in matches:
        cid = m.get("nptel_course_id")
        m_copy = dict(m)
        if cid in results:
            c_details = results[cid]
            m_copy["duration"] = c_details.get("duration")
            m_copy["enrollment_period"] = c_details.get("enrollment_period")
            m_copy["exam_date"] = c_details.get("exam_date")
            m_copy["syllabus_pdf_url"] = c_details.get("syllabus_pdf_url")
            m_copy["intro_video_url"] = c_details.get("intro_video_url")
            m_copy["course_abstract"] = c_details.get("course_abstract")
            m_copy["total_units"] = c_details.get("total_units")
            m_copy["total_lectures"] = c_details.get("total_lectures")
            m_copy["units"] = c_details.get("units")
        enriched_matches.append(m_copy)

    enriched_output = {
        "metadata": {
            **matched_data.get("metadata", {}),
            "details_scraped_at": timestamp,
            "total_matched_courses": len(enriched_matches),
            "unique_nptel_courses": len(results)
        },
        "matches": enriched_matches
    }

    with open(OUTPUT_ENRICHED_PATH, "w", encoding="utf-8") as f:
        json.dump(enriched_output, f, indent=2, ensure_ascii=False)
    print(f"Saved enriched matches to {OUTPUT_ENRICHED_PATH}")

    print("\nSummary:")
    print(f"  Total matched courses enriched: {len(enriched_matches)}")
    print(f"  Unique NPTEL course pages scraped: {len(results)}")
    print(f"  Total lectures extracted across all courses: {details_output['metadata']['total_lectures_extracted']}")

if __name__ == "__main__":
    main()
