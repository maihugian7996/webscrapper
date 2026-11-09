"""Match course names from courses_catalog.json against nptel_courses_catalog.json using fuzzy distance."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from rapidfuzz import fuzz, process

CATALOG_PATH = Path(r"c:\Users\JASMINE\Downloads\courses_catalog.json")
NPTEL_PATH = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output\nptel_courses_catalog.json")
OUTPUT_DIR = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output")

def clean_title(text: str) -> str:
    """Normalize course titles for fuzzy comparison."""
    if not text:
        return ""
    # Strip leading prefixes
    t = re.sub(r'^(?:NOC|NPTEL)\s*:\s*', '', text, flags=re.IGNORECASE)
    # Strip media indicators
    t = re.sub(r'\b(video|web|online)\b', '', t, flags=re.IGNORECASE)
    # Normalize ampersands & punctuation
    t = t.replace('&', ' and ')
    t = re.sub(r'[^\w\s]', ' ', t)
    return ' '.join(t.lower().split())

def main():
    print(f"Loading NPTEL catalog from {NPTEL_PATH}...")
    with open(NPTEL_PATH, "r", encoding="utf-8") as f:
        nptel_data = json.load(f)

    print(f"Loading curriculum courses from {CATALOG_PATH}...")
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        catalog_data = json.load(f)

    nptel_items = nptel_data["items"]
    nptel_lookup = []
    for item in nptel_items:
        raw_title = item.get("title", "")
        cleaned = clean_title(raw_title)
        if cleaned:
            nptel_lookup.append({
                "course_id": item.get("course_id"),
                "raw_title": raw_title,
                "clean_title": cleaned,
                "url": item.get("url"),
                "discipline": item.get("discipline"),
                "instructor": item.get("instructor"),
                "institute": item.get("institute"),
            })

    nptel_clean_titles = [x["clean_title"] for x in nptel_lookup]
    catalog_courses = catalog_data["courses"]

    print(f"Matching {len(catalog_courses)} courses against {len(nptel_lookup)} NPTEL courses...")

    matched_only = []
    enriched_catalog = {}

    exact_matches = 0
    high_matches = 0
    mod_matches = 0

    for subno, cinfo in catalog_courses.items():
        cname = cinfo.get("name", "")
        clean_cname = clean_title(cname)

        match_info = None

        if clean_cname:
            # First check for exact normalized match
            best = process.extractOne(
                clean_cname,
                nptel_clean_titles,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=85.0
            )

            if best:
                match_str, score, idx = best
                nptel_course = nptel_lookup[idx]

                if score == 100.0:
                    conf = "Exact"
                    exact_matches += 1
                elif score >= 92.0:
                    conf = "High"
                    high_matches += 1
                else:
                    conf = "Moderate"
                    mod_matches += 1

                match_info = {
                    "subno": subno,
                    "catalog_name": cname,
                    "dept": cinfo.get("dept"),
                    "credits": cinfo.get("credits"),
                    "nptel_course_id": nptel_course["course_id"],
                    "nptel_title": nptel_course["raw_title"],
                    "nptel_url": nptel_course["url"],
                    "nptel_discipline": nptel_course["discipline"],
                    "nptel_instructor": nptel_course["instructor"],
                    "nptel_institute": nptel_course["institute"],
                    "match_score": round(score, 2),
                    "confidence": conf
                }
                matched_only.append(match_info)

        # Enriched catalog copy
        c_copy = dict(cinfo)
        if match_info:
            c_copy["nptel_course_id"] = match_info["nptel_course_id"]
            c_copy["nptel_title"] = match_info["nptel_title"]
            c_copy["nptel_url"] = match_info["nptel_url"]
            c_copy["nptel_match_score"] = match_info["match_score"]
            c_copy["nptel_confidence"] = match_info["confidence"]
        else:
            c_copy["nptel_course_id"] = None
            c_copy["nptel_title"] = None
            c_copy["nptel_url"] = None
            c_copy["nptel_match_score"] = None
            c_copy["nptel_confidence"] = None

        enriched_catalog[subno] = c_copy

    matched_only.sort(key=lambda x: (x["match_score"], x["subno"]), reverse=True)

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # 1. Output file with matches only
    matched_output = {
        "metadata": {
            "catalog_source": str(CATALOG_PATH),
            "nptel_source": str(NPTEL_PATH),
            "matched_at": timestamp,
            "total_catalog_courses": len(catalog_courses),
            "total_matched_courses": len(matched_only),
            "exact_matches": exact_matches,
            "high_confidence_matches": high_matches,
            "moderate_confidence_matches": mod_matches,
            "cutoff_threshold": 85.0
        },
        "matches": matched_only
    }

    out_matched_path = OUTPUT_DIR / "courses_matched_nptel.json"
    with open(out_matched_path, "w", encoding="utf-8") as f:
        json.dump(matched_output, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(matched_only)} matched courses to {out_matched_path}")

    # 2. Output enriched full catalog
    full_enriched_data = {
        "metadata": {
            **catalog_data.get("metadata", {}),
            "nptel_match_info": {
                "matched_at": timestamp,
                "nptel_catalog": str(NPTEL_PATH),
                "total_matched": len(matched_only),
                "exact_matches": exact_matches
            }
        },
        "specializations": catalog_data.get("specializations", {}),
        "courses": enriched_catalog
    }

    out_full_path = OUTPUT_DIR / "courses_catalog_with_nptel_ids.json"
    with open(out_full_path, "w", encoding="utf-8") as f:
        json.dump(full_enriched_data, f, indent=2, ensure_ascii=False)
    print(f"Wrote full catalog with NPTEL IDs to {out_full_path}")

    print("\nSummary:")
    print(f"  Total catalog courses: {len(catalog_courses)}")
    print(f"  Courses matched with NPTEL ID: {len(matched_only)}")
    print(f"    - Exact (100%): {exact_matches}")
    print(f"    - High (>= 92%): {high_matches}")
    print(f"    - Moderate (85-91%): {mod_matches}")

if __name__ == "__main__":
    main()
