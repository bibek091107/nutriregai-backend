import os, requests, re, json, urllib.parse

BASE_URL = "https://www.fssai.gov.in"
REG_PORTAL = "https://www.fssai.gov.in/cms/food-safety-and-standards-regulations.php"
GUIDE_PORTAL = "https://www.fssai.gov.in/cms/guidance-notes.php"

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

def parse_food_safety_regs():
    url = f"{BASE_URL}/assets/FoodSafetyRegulations-Dt7RYACl.js"
    r = requests.get(url, headers=headers)
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
                "title": doc_title,
                "category": cat,
                "source_type": stype,
                "document_url": doc_url,
                "source_url": REG_PORTAL,
                "effective_date": "2011"
            })
    return items

def parse_product_standards():
    url = f"{BASE_URL}/assets/ProductStandards-DPlQpibm.js"
    r = requests.get(url, headers=headers)
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
            "title": title,
            "category": cat,
            "source_type": "REGULATION",
            "document_url": doc_url,
            "source_url": REG_PORTAL,
            "effective_date": "2011"
        })
    return items

def parse_food_products_compendium():
    url = f"{BASE_URL}/assets/FoodProductsCompendium-DxnlDZvz.js"
    r = requests.get(url, headers=headers)
    text = r.text
    base_match = re.search(r'const\s+([a-zA-Z0-9_]+)\s*=\s*\"(/[^\"]+)\"', text)
    base_path = base_match.group(2) if base_match else '/docs/food-law/regulations/compendium/food-products-standards'
    
    items = []
    comp_matches = re.findall(r'label:\"([^\"]+)\"[^}]*?href:`\$\{([a-zA-Z0-9_]+)\}(/[^`]+)`', text)
    for label, v_name, rel_p in comp_matches:
        doc_url = BASE_URL + base_path + rel_p
        title = f"Compendium of Food Products Standards - {label}"
        items.append({
            "title": title,
            "category": "Food Products Standards and Food Additives",
            "source_type": "REGULATION",
            "document_url": doc_url,
            "source_url": REG_PORTAL,
            "effective_date": "Current Compendium"
        })
    return items

def parse_amendments(chunk_name, category, default_stype="NOTIFICATION"):
    url = f"{BASE_URL}/{chunk_name}"
    r = requests.get(url, headers=headers)
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
            "title": title,
            "category": category,
            "source_type": default_stype,
            "document_url": doc_url,
            "source_url": REG_PORTAL,
            "effective_date": date_str
        })
    return items

def parse_guidance():
    url = f"{BASE_URL}/assets/GuidanceDocument-qapZdCO2.js"
    r = requests.get(url, headers=headers)
    text = r.text
    
    items = []
    pdf_matches = re.findall(r'\"([^\"]*Guidance[^\"]*\.pdf)\"', text, re.IGNORECASE)
    for pdf in set(pdf_matches):
        filename = os.path.basename(pdf)
        title = filename.replace('_', ' ').replace('.pdf', '')
        if not pdf.startswith("/"):
            doc_url = f"{BASE_URL}/docs/{pdf}"
        else:
            doc_url = f"{BASE_URL}{pdf}"
        items.append({
            "title": title,
            "category": "Health Supplements / Nutraceuticals / Functional Foods / Novel Food",
            "source_type": "GUIDANCE",
            "document_url": doc_url,
            "source_url": GUIDE_PORTAL,
            "effective_date": "Current Guidance"
        })
    return items

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

print(f"Total Discovered Items across focus areas: {len(all_items)}")

categories = {}
for item in all_items:
    cat = item["category"]
    categories[cat] = categories.get(cat, 0) + 1

for cat, count in sorted(categories.items()):
    print(f"  {cat}: {count} items")

