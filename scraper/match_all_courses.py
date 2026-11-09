import json
import re
import time
from rapidfuzz import fuzz, process

nptel_path = r'c:\Users\JASMINE\Desktop\webscrapper\scraper\output\nptel_courses_catalog.json'
with open(nptel_path, 'r', encoding='utf-8') as f:
    nptel_data = json.load(f)

courses_path = r'c:\Users\JASMINE\Downloads\courses_catalog.json'
with open(courses_path, 'r', encoding='utf-8') as f:
    catalog_data = json.load(f)

def clean_title(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r'^(?:NOC|NPTEL)\s*:\s*', '', text, flags=re.IGNORECASE)
    t = re.sub(r'\b(video|web|online)\b', '', t, flags=re.IGNORECASE)
    t = t.replace('&', ' and ')
    t = re.sub(r'[^\w\s]', ' ', t)
    return ' '.join(t.lower().split())

nptel_list = []
for item in nptel_data['items']:
    raw_title = item.get('title', '')
    cleaned = clean_title(raw_title)
    if cleaned:
        nptel_list.append({
            'raw_title': raw_title,
            'clean_title': cleaned,
            'course_id': item.get('course_id'),
            'url': item.get('url'),
            'discipline': item.get('discipline'),
            'instructor': item.get('instructor'),
            'institute': item.get('institute')
        })

nptel_clean_titles = [x['clean_title'] for x in nptel_list]
catalog_courses = catalog_data['courses']

print(f"Matching {len(catalog_courses)} courses against {len(nptel_list)} NPTEL courses...")

start_time = time.time()
results = []
exact_count = 0
high_count = 0
mod_count = 0

for subno, cinfo in catalog_courses.items():
    cname = cinfo.get('name', '')
    clean_cname = clean_title(cname)
    if not clean_cname:
        results.append({
            'subno': subno,
            'name': cname,
            'nptel_match': None
        })
        continue

    # Extract best match
    best = process.extractOne(
        clean_cname,
        nptel_clean_titles,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=70.0
    )

    if best:
        match_title, score, idx = best
        matched_nptel = nptel_list[idx]
        if score == 100.0:
            confidence = "Exact"
            exact_count += 1
        elif score >= 90.0:
            confidence = "High"
            high_count += 1
        elif score >= 80.0:
            confidence = "Moderate"
            mod_count += 1
        else:
            confidence = "Low"

        results.append({
            'subno': subno,
            'name': cname,
            'nptel_match': {
                'course_id': matched_nptel['course_id'],
                'title': matched_nptel['raw_title'],
                'url': matched_nptel['url'],
                'discipline': matched_nptel['discipline'],
                'instructor': matched_nptel['instructor'],
                'institute': matched_nptel['institute'],
                'similarity_score': round(score, 2),
                'confidence': confidence
            }
        })
    else:
        results.append({
            'subno': subno,
            'name': cname,
            'nptel_match': None
        })

elapsed = time.time() - start_time
matched_total = sum(1 for r in results if r['nptel_match'] is not None)
print(f"Done in {elapsed:.2f}s!")
print(f"Total matched (score >= 70%): {matched_total} / {len(catalog_courses)}")
print(f" - Exact (100%): {exact_count}")
print(f" - High (>= 90%): {high_count}")
print(f" - Moderate (80-89%): {mod_count}")
print(f" - Low (70-79%): {matched_total - exact_count - high_count - mod_count}")
