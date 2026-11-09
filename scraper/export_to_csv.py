"""Export scraped JSON datasets to CSV format for spreadsheets and reporting."""

import csv
import json
from pathlib import Path

OUTPUT_DIR = Path(r"c:\Users\JASMINE\Desktop\webscrapper\scraper\output")

def export_matched_courses():
    in_path = OUTPUT_DIR / "courses_matched_nptel_detailed.json"
    out_path = OUTPUT_DIR / "courses_matched_nptel.csv"
    if not in_path.exists():
        print(f"Skipping {in_path}: file not found")
        return

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    matches = data.get("matches", [])
    if not matches:
        print("No matches to export")
        return

    fieldnames = [
        "subno",
        "catalog_name",
        "dept",
        "credits",
        "nptel_course_id",
        "nptel_title",
        "nptel_url",
        "nptel_discipline",
        "nptel_instructor",
        "nptel_institute",
        "match_score",
        "confidence",
        "duration",
        "enrollment_period",
        "exam_date",
        "syllabus_pdf_url",
        "intro_video_url",
        "total_units",
        "total_lectures"
    ]

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for m in matches:
            writer.writerow(m)

    print(f"Exported {len(matches)} matched courses to {out_path}")

def export_all_lectures():
    in_path = OUTPUT_DIR / "nptel_matched_courses_details.json"
    out_path = OUTPUT_DIR / "nptel_lectures_directory.csv"
    if not in_path.exists():
        print(f"Skipping {in_path}: file not found")
        return

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    courses = data.get("courses", [])
    rows = []

    for c in courses:
        cid = c.get("nptel_course_id")
        ctitle = c.get("title")
        instructor = c.get("instructor")
        institute = c.get("institute")
        syllabus = c.get("syllabus_pdf_url")

        for u in c.get("units", []):
            uname = u.get("unit_name")
            for l in u.get("lessons", []):
                rows.append({
                    "nptel_course_id": cid,
                    "course_title": ctitle,
                    "instructor": instructor,
                    "institute": institute,
                    "unit_name": uname,
                    "lesson_id": l.get("lesson_id"),
                    "lecture_title": l.get("title"),
                    "youtube_id": l.get("youtube_id"),
                    "youtube_url": l.get("youtube_url"),
                    "syllabus_pdf_url": syllabus
                })

    fieldnames = [
        "nptel_course_id",
        "course_title",
        "instructor",
        "institute",
        "unit_name",
        "lesson_id",
        "lecture_title",
        "youtube_id",
        "youtube_url",
        "syllabus_pdf_url"
    ]

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} lectures to {out_path}")

def export_full_nptel_catalog():
    in_path = OUTPUT_DIR / "nptel_courses_catalog.json"
    out_path = OUTPUT_DIR / "nptel_courses_catalog.csv"
    if not in_path.exists():
        print(f"Skipping {in_path}: file not found")
        return

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    fieldnames = ["course_id", "title", "discipline", "instructor", "institute", "url"]

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)

    print(f"Exported {len(items)} NPTEL catalog courses to {out_path}")

if __name__ == "__main__":
    export_matched_courses()
    export_all_lectures()
    export_full_nptel_catalog()
