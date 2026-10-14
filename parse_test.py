import requests, re, json, urllib.parse

BASE_URL = "https://www.fssai.gov.in"

def parse_regulations_chunk():
    url = f"{BASE_URL}/assets/FoodSafetyRegulations-Dt7RYACl.js"
    r = requests.get(url)
    text = r.text
    base_match = re.search(r'const\s+([a-zA-Z0-9_]+)\s*=\s*\"(/[^\"]+)\"', text)
    var_name, base_path = base_match.groups() if base_match else ('n', '/docs/food-law/regulations')
    
    # Extract reg blocks: id:"...",title:"...", ... docs:[...]
    items = []
    # Find all {id:"...",title:"...",...docs:[...]}
    reg_blocks = re.findall(r'\{id:\"[^\"]+\",title:\"([^\"]+)\"[^\]]*?docs:\[(.*?)\]\}', text)
    for title, docs_str in reg_blocks:
        doc_items = re.findall(r'\{label:\"([^\"]+)\"[^}]*?href:`\$\{([a-zA-Z0-9_]+)\}(/[^`]+)`\}', docs_str)
        for label, v_name, rel_p in doc_items:
            full_rel = base_path + rel_p
            doc_url = BASE_URL + full_rel
            doc_title = f"{title} ({label})" if label.lower() not in title.lower() else title
            items.append({
                "title": doc_title,
                "label": label,
                "parent_title": title,
                "document_url": doc_url,
                "source_type": "REGULATION" if label in ["Regulation", "Compendium"] else ("NOTIFICATION" if label == "Direction" else "REGULATION")
            })
    return items

items = parse_regulations_chunk()
print(f"Parsed {len(items)} items from FoodSafetyRegulations chunk:")
for it in items[:10]:
    print(" -", it['source_type'], "|", it['title'], "->", it['document_url'])

