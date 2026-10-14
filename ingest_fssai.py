import os
import re
import json
import time
import urllib.parse
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://www.fssai.gov.in"
REG_PORTAL = "https://www.fssai.gov.in/cms/food-safety-and-standards-regulations.php"
GUIDE_PORTAL = "https://www.fssai.gov.in/cms/guidance-notes.php"

OUTPUT_DIR = "/Users/bibekkhadka/Desktop/NutriReg AI/backend/regulatory_knowledge/india_fssai"
REGULATIONS_DIR = os.path.join(OUTPUT_DIR, "regulations")
NOTIFICATIONS_DIR = os.path.join(OUTPUT_DIR, "notifications")
GUIDANCE_DIR = os.path.join(OUTPUT_DIR, "guidance")
METADATA_DIR = os.path.join(OUTPUT_DIR, "metadata")

for d in [REGULATIONS_DIR, NOTIFICATIONS_DIR, GUIDANCE_DIR, METADATA_DIR]:
    os.makedirs(d, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def sanitize_filename(title):
    s = re.sub(r'[^\w\s\.-]', '_', title)
    s = re.sub(r'\s+', '_', s).strip('_')
    return s[:120]

def parse_food_safety_regs():
    url = f"{BASE_URL}/assets/FoodSafetyRegulations-Dt7RYACl.js"
    r = requests.get(url, headers=HEADERS)
    text = r.text
    base_match = re.search(r'const\s+([a-zA-Z0-9_]+)\s*=\s*\"(/[^\"]+)\"', text)
    base_path = base_match.group(2) if base_match else '/docs/food-law/regulations'
    
    items = []
    reg_blocks = re.findall(r'\{id:\"[^\"]+\",title:\"([^\"]+)\"[^\]]*?docs:\[(.*?)\]\}', text)
    for title, docs_str in reg_blocks:
        cat = "General Regulatory Framework"
        t_lower = title.lower()
        if "food products standards" in t_lower or "additive" in t_lower:
            cat = "Food Products Standards and Food Additives"
        elif "labelling" in t_lower or "display" in t_lower:
            cat = "Labelling and Display"
        elif "health supplements" in t_lower or "nutraceutical" in t_lower or "novel" in t_lower or "special dietary" in t_lower:
            cat = "Health Supplements / Nutraceuticals / Functional Foods / Novel Food"
        elif "advertising" in t_lower or "claim" in t_lower:
            cat = "Advertising and Claims"
        elif "contaminant" in t_lower or "toxin" in t_lower or "residue" in t_lower:
            cat = "Contaminants, Toxins and Residues"

        doc_items = re.findall(r'\{label:\"([^\"]+)\"[^}]*?href:`\$\{([a-zA-Z0-9_]+)\}(/[^`]+)`\}', docs_str)
        for label, v_name, rel_p in doc_items:
            doc_url = BASE_URL + base_path + rel_p
            doc_title = f"{title} ({label})" if label.lower() not in title.lower() else title
            stype = "REGULATION" if label in ["Regulation", "Compendium", "Corrigenda"] else ("NOTIFICATION" if label == "Direction" else "REGULATION")
            items.append({
                "title": doc_title.strip(),
                "category": cat,
                "source_type": stype,
                "document_url": doc_url,
                "source_url": REG_PORTAL,
                "effective_date": "2011"
            })
    return items

def parse_product_standards():
    url = f"{BASE_URL}/assets/ProductStandards-DPlQpibm.js"
    r = requests.get(url, headers=HEADERS)
    text = r.text
    base_match = re.search(r'const\s+([a-zA-Z0-9_]+)\s*=\s*\"(/[^\"]+)\"', text)
    base_path = base_match.group(2) if base_match else '/docs/standards/product-standard'
    
    items = []
    file_matches = re.findall(r'title:\"([^\"]+)\",file:\"([^\"]+)\"', text)
    for title, filename in file_matches:
        t_lower = title.lower()
        cat = "General Regulatory Framework"
        if "food products standards" in t_lower or "additive" in t_lower:
            cat = "Food Products Standards and Food Additives"
        elif "labelling" in t_lower or "packaging" in t_lower:
            cat = "Labelling and Display"
        elif "health supplements" in t_lower or "nutraceutical" in t_lower or "novel" in t_lower:
            cat = "Health Supplements / Nutraceuticals / Functional Foods / Novel Food"
        elif "advertising" in t_lower or "claim" in t_lower:
            cat = "Advertising and Claims"
        elif "contaminant" in t_lower or "toxin" in t_lower:
            cat = "Contaminants, Toxins and Residues"

        doc_url = f"{BASE_URL}{base_path}/{filename}"
        items.append({
            "title": title.strip(),
            "category": cat,
            "source_type": "REGULATION",
            "document_url": doc_url,
            "source_url": REG_PORTAL,
            "effective_date": "2011"
        })
    return items

def parse_food_products_compendium():
    url = f"{BASE_URL}/assets/FoodProductsCompendium-DxnlDZvz.js"
    r = requests.get(url, headers=HEADERS)
    text = r.text
    base_match = re.search(r'const\s+([a-zA-Z0-9_]+)\s*=\s*\"(/[^\"]+)\"', text)
    base_path = base_match.group(2) if base_match else '/docs/food-law/regulations/compendium/food-products-standards'
    
    items = []
    comp_matches = re.findall(r'label:\"([^\"]+)\"[^}]*?href:`\$\{([a-zA-Z0-9_]+)\}(/[^`]+)`', text)
    for label, v_name, rel_p in comp_matches:
        doc_url = BASE_URL + base_path + rel_p
        title = f"Compendium of Food Products Standards and Food Additives - {label}"
        items.append({
            "title": title.strip(),
            "category": "Food Products Standards and Food Additives",
            "source_type": "REGULATION",
            "document_url": doc_url,
            "source_url": REG_PORTAL,
            "effective_date": "Current Compendium"
        })
    return items

def parse_amendments(chunk_name, category, default_stype="NOTIFICATION"):
    url = f"{BASE_URL}/{chunk_name}"
    r = requests.get(url, headers=HEADERS)
    text = r.text
    if r.status_code != 200:
        return []
        
    base_match = re.search(r'const\s+([a-zA-Z0-9_]+)\s*=\s*\"(/[^\"]+)\"', text)
    base_path = base_match.group(2) if base_match else '/docs'
    
    items = []
    amend_matches = re.findall(r'\{no:\s*\d+,\s*date:\s*\"([^\"]+)\"[^}]*?href:`\$\{([a-zA-Z0-9_]+)\}(/[^`]+)`', text)
    for date_str, v_name, rel_p in amend_matches:
        doc_url = BASE_URL + base_path + rel_p
        filename = os.path.basename(rel_p)
        title = f"{category} Gazette Amendment Notification ({date_str}) - {filename}"
        items.append({
            "title": title.strip(),
            "category": category,
            "source_type": default_stype,
            "document_url": doc_url,
            "source_url": REG_PORTAL,
            "effective_date": date_str
        })
    return items

def parse_guidance():
    url = f"{BASE_URL}/assets/GuidanceDocument-qapZdCO2.js"
    r = requests.get(url, headers=HEADERS)
    text = r.text
    
    items = []
    pdf_matches = re.findall(r'\"([^\"]*Guidance[^\"]*\.pdf)\"', text, re.IGNORECASE)
    for pdf in set(pdf_matches):
        filename = os.path.basename(pdf)
        title = f"FSMS Guidance Document - {filename.replace('_', ' ').replace('.pdf', '')}"
        if not pdf.startswith("/"):
            doc_url = f"{BASE_URL}/docs/{pdf}"
        else:
            doc_url = f"{BASE_URL}{pdf}"
        items.append({
            "title": title.strip(),
            "category": "Health Supplements / Nutraceuticals / Functional Foods / Novel Food",
            "source_type": "GUIDANCE",
            "document_url": doc_url,
            "source_url": GUIDE_PORTAL,
            "effective_date": "Current Guidance"
        })
    return items

def process_item(item, retrieved_at):
    doc_url = item["document_url"]
    title = item["title"]
    source_type = item["source_type"]
    category = item["category"]
    effective_date = item.get("effective_date", "Current")

    if source_type == "REGULATION":
        target_dir = REGULATIONS_DIR
    elif source_type == "NOTIFICATION":
        target_dir = NOTIFICATIONS_DIR
    elif source_type == "GUIDANCE":
        target_dir = GUIDANCE_DIR
    else:
        target_dir = REGULATIONS_DIR

    ext = ".pdf" if doc_url.lower().endswith(".pdf") or ".pdf" in doc_url.lower() else ".html"
    clean_name = sanitize_filename(title) + ext
    local_path = os.path.join(target_dir, clean_name)

    try:
        r = requests.get(doc_url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.content) > 100:
            with open(local_path, "wb") as f:
                f.write(r.content)
            rel_local_path = os.path.relpath(local_path, OUTPUT_DIR)
            manifest_entry = {
                "authority": "FSSAI",
                "jurisdiction": "INDIA_FSSAI",
                "title": title,
                "source_type": source_type,
                "category": category,
                "source_url": item["source_url"],
                "document_url": doc_url,
                "local_path": f"regulatory_knowledge/india_fssai/{rel_local_path}",
                "version": "Current",
                "effective_date": effective_date,
                "retrieved_at": retrieved_at
            }
            return True, manifest_entry, None
        else:
            pending_entry = {
                "authority": "FSSAI",
                "jurisdiction": "INDIA_FSSAI",
                "title": title,
                "source_type": source_type,
                "category": category,
                "source_url": item["source_url"],
                "document_url": doc_url,
                "reason": f"HTTP status code {r.status_code}",
                "discovered_at": retrieved_at
            }
            return False, None, pending_entry
    except Exception as e:
        pending_entry = {
            "authority": "FSSAI",
            "jurisdiction": "INDIA_FSSAI",
            "title": title,
            "source_type": source_type,
            "category": category,
            "source_url": item["source_url"],
            "document_url": doc_url,
            "reason": str(e),
            "discovered_at": retrieved_at
        }
        return False, None, pending_entry

def main():
    print("=== NutriReg AI Regulatory Knowledge Ingestion System ===")
    print("Authority: Food Safety and Standards Authority of India (FSSAI)")
    print("Jurisdiction: INDIA_FSSAI\n")

    all_items = []
    all_items.extend(parse_food_safety_regs())
    all_items.extend(parse_product_standards())
    all_items.extend(parse_food_products_compendium())
    all_items.extend(parse_amendments("assets/FoodProductsAmendments-BPRMtFb0.js", "Food Products Standards and Food Additives"))
    all_items.extend(parse_amendments("assets/LabellingDisplayAmendments-CQ9ANEaf.js", "Labelling and Display"))
    all_items.extend(parse_amendments("assets/NutraceuticalsAmendments-awyAdhPF.js", "Health Supplements / Nutraceuticals / Functional Foods / Novel Food"))
    all_items.extend(parse_amendments("assets/NonSpecificFoodAmendments-CRMaRCsw.js", "Health Supplements / Nutraceuticals / Functional Foods / Novel Food"))
    all_items.extend(parse_amendments("assets/AdvertisingClaimsAmendments-0K02d1Xx.js", "Advertising and Claims"))
    all_items.extend(parse_amendments("assets/ContaminantsToxinsAmendments-Z9_noutg.js", "Contaminants, Toxins and Residues"))
    all_items.extend(parse_amendments("assets/PackagingAmendments-DHl0oRwA.js", "Labelling and Display"))
    all_items.extend(parse_guidance())

    # Deduplicate items by document_url
    unique_items = {}
    for item in all_items:
        url = item["document_url"]
        if url not in unique_items:
            unique_items[url] = item

    print(f"Total Unique Discovered Official Sources: {len(unique_items)}")

    retrieved_at = datetime.now(timezone.utc).isoformat()
    manifest = []
    pending_sources = []

    print("Starting parallel verification and downloading...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_item = {executor.submit(process_item, item, retrieved_at): item for item in unique_items.values()}
        for future in as_completed(future_to_item):
            success, manifest_entry, pending_entry = future.result()
            if success:
                manifest.append(manifest_entry)
            else:
                pending_sources.append(pending_entry)

    print(f"\nIngestion Complete!")
    print(f"  Successfully Downloaded & Verified Documents: {len(manifest)}")
    print(f"  Pending / Manual Download Sources: {len(pending_sources)}")

    # Sort manifest entries by category and title for clean presentation
    manifest.sort(key=lambda x: (x["category"], x["title"]))
    pending_sources.sort(key=lambda x: (x["category"], x["title"]))

    manifest_file = os.path.join(METADATA_DIR, "manifest.json")
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to: {manifest_file}")

    pending_file = os.path.join(METADATA_DIR, "pending_sources.json")
    with open(pending_file, "w", encoding="utf-8") as f:
        json.dump(pending_sources, f, indent=2)
    print(f"Pending sources written to: {pending_file}")

if __name__ == "__main__":
    main()
