# Regulatory Document Extraction Report

Generated: 2026-09-28T22:02:23.158266+00:00

Authority: **FSSAI** (Food Safety and Standards Authority of India)  
Jurisdiction: **INDIA_FSSAI**

Scope is preprocessing only: PDF validation, page extraction, OCR detection, section-aware chunking and provenance. No embeddings, no pgvector, no retrieval, no LLM calls.

## Run summary

| Metric | Value |
|---|---|
| Documents discovered | 200 |
| Documents processed | 200 |
| Documents failed | 0 |
| Pages extracted | 6015 |
| Pages with usable text | 3531 |
| Pages flagged for OCR | 2484 |
| Sections detected | 1699 |
| Chunks produced | 4443 |
| Chunks per document (mean) | 22.21 |

## Chunk size distribution

| Statistic | Tokens |
|---|---|
| min | 2 |
| p25 | 401 |
| p50 | 1201 |
| p75 | 1210 |
| p90 | 1224 |
| p99 | 1258 |
| max | 1297 |
| mean | 897 |
| chunks above max | 0 |
| chunks below min | 588 |

Target band is 200-1500 tokens with a 150-token overlap. Chunks below the minimum are sections that are genuinely short in the source document; they are kept whole rather than padded with neighbouring sections, because padding would misattribute the text.

## Sections

- Chunks with a section number: **4198**
- Chunks with `section = null`: **245**

A null section means no reliable section marker was present in the source. No section number is ever inferred, completed or guessed.

## Content requiring attention

### Manifest collisions (one file, several manifest entries)

**2 files** are claimed by more than one manifest entry, suppressing 3 duplicate extractions. Each file was extracted once under a canonical document ID.

- `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Fortification_of_Food__Regulation__2018.pdf`
  - canonical: Food Safety and Standards (Fortification of Food) Regulation, 2018
  - claimants: 2
  - suppressed alias URL: https://www.fssai.gov.in/docs/standards/product-standard/Food_Fortification_Regulations.pdf
- `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Health_Supplements__Nutraceuticals__Food_for_Special_Dietary_Use__Food_for_Special_Medical_Pu.pdf`
  - canonical: Food Safety and Standards (Health Supplements, Nutraceuticals, Food for Special Dietary Use, Food for Special Medical Purpose, Functional Food and Novel Food) Regulations, 2016
  - claimants: 3
  - suppressed alias URL: https://www.fssai.gov.in/docs/food-law/regulations/Compendium_Nutra_29_09_2021.pdf
  - suppressed alias URL: https://www.fssai.gov.in/docs/food-law/regulations/Direction_New_compressed.pdf

### Duplicate source files (identical bytes, different filenames)

- sha256 `14f5b5f7c5445664...`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Contaminants__Toxins_and_Residues__Regulation__2011.pdf`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Contaminants__Toxins_and_Residues__Regulations__2011.pdf`
- sha256 `159827c4dc2301c7...`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Alcoholic_Beverages__Regulation__2018.pdf`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Alcoholic_Beverages__Regulations__2018.pdf`
- sha256 `4103de0420068e1d...`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Organic_Food__Regulation__2017.pdf`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Organic_Foods__Regulations__2017.pdf`
- sha256 `bd31da71e3a3ea43...`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Food_Products_Standards_and_Food_Additives__Regulation__2011.pdf`
  - `regulatory_knowledge/india_fssai/regulations/Food_Safety_and_Standards__Food_Products_Standards_and_Food_Additives__Regulations__2011.pdf`

### Documents that produced no chunks

**25 documents** (732 pages) yielded no extractable text on any page. They are image scans, so the pipeline correctly produced zero chunks for them rather than inventing text. They are a coverage gap: until they are OCR'd, they cannot be retrieved from.

| Pages | Title | Source type |
|---|---|---|
| 286 | Food Safety and Standards (Food Products Standards and Food Additives) | REGULATION |
| 172 | Food Safety and Standards (Ayurveda Aahara) Regulations, 2022 (Order ( | REGULATION |
| 75 | Food Safety and Standards (Food Products Standards and Food Additives) | REGULATION |
| 64 | Food Safety and Standards (Licensing and Registration of Food Business | REGULATION |
| 24 | Food Safety and Standards (Contaminants, Toxins and Residues) Regulati | REGULATION |
| 18 | Transaction of Business at its Meetings Regulations, 2010 | REGULATION |
| 16 | Procedure for Transaction of Business of the Central Advisory Committe | REGULATION |
| 12 | Salary, Allowances and Other Conditions of Service of Officers and Emp | REGULATION |
| 11 | Food Safety and Standards (Laboratory and Sampling Analysis) Regulatio | REGULATION |
| 10 | Food Safety and Standards (Vegan Foods) Regulations, 2022 (Revised gui | REGULATION |
| 9 | Food Products Standards and Food Additives Gazette Amendment Notificat | NOTIFICATION |
| 9 | Food Safety and Standards (Prohibition and Restriction of Sales) Regul | REGULATION |
| 3 | Contaminants, Toxins and Residues Gazette Amendment Notification (3rd  | NOTIFICATION |
| 3 | Food Products Standards and Food Additives Gazette Amendment Notificat | NOTIFICATION |
| 3 | Food Products Standards and Food Additives Gazette Amendment Notificat | NOTIFICATION |
| 3 | Labelling and Display Gazette Amendment Notification (27th January 202 | NOTIFICATION |
| 3 | Labelling and Display Gazette Amendment Notification (30th August 2022 | NOTIFICATION |
| 2 | Food Products Standards and Food Additives Gazette Amendment Notificat | NOTIFICATION |
| 2 | Food Products Standards and Food Additives Gazette Amendment Notificat | NOTIFICATION |
| 2 | Labelling and Display Gazette Amendment Notification (28th March 2025) | NOTIFICATION |
| 1 | Food Safety and Standards (Contaminants, Toxins and Residues) Regulati | REGULATION |
| 1 | Food Safety and Standards (Laboratory and Sampling Analysis) Regulatio | REGULATION |
| 1 | Food Safety and Standards (Licensing and Registration of Food Business | REGULATION |
| 1 | Food Safety and Standards (Prohibition and Restriction of Sales) Regul | REGULATION |
| 1 | Food Safety and Standards (Vegan Foods) Regulations, 2022 (Application | REGULATION |


### Pages flagged for OCR within otherwise readable documents

**158 documents** contain pages that should be OCR'd before their text is trusted (41.3% of all pages). Detection only — no OCR was performed.

Reason breakdown across all documents:

| Signal | Pages | Meaning |
|---|---|---|
| broken_devanagari | 1724 | Devanagari whose glyph ordering was destroyed by the source font: dependent vowel signs detached from their consonant, or glyphs substituted from an unrelated script. Reads as Hindi but is meaningless to search or embeddings. |
| image_dominated | 748 | Page is mostly image area with a thin text layer. |
| low_text | 735 | Very little extractable text, typically a near-blank or table-only page. |
| mojibake | 568 | Western code points standing in for a non-Latin script, characteristic of pre-Unicode legacy font encodings. |


These counts are a detection signal, not a defect in this pipeline. OCRing a `broken_devanagari` page with a modern Hindi model is unlikely to help, because the damage is in the text layer the PDF stores rather than in the pixels; those pages need a font-recovery pass or re-sourcing. `image_dominated` and `low_text` pages are the ones OCR is expected to resolve.

### Failures

_none_

## Coverage by category

| Category | Documents | Pages | Chunks | OCR pages |
|---|---|---|---|---|
| Advertising and Claims | 5 | 75 | 79 | 41 |
| Contaminants, Toxins and Residues | 21 | 279 | 149 | 171 |
| Food Products Standards and Food Additives | 107 | 3762 | 2841 | 1439 |
| General Regulatory Framework | 52 | 1324 | 765 | 644 |
| Health Supplements / Nutraceuticals / Functional Foods / Novel Food | 4 | 427 | 478 | 120 |
| Labelling and Display | 11 | 148 | 131 | 69 |


## Coverage by source type

| Source type | Documents |
|---|---|
| NOTIFICATION | 113 |
| REGULATION | 87 |


## Outputs

```
processed/documents/<document_id>.json   per-document record
processed/pages/<document_id>.jsonl      one line per page
processed/chunks/<document_id>.jsonl     one line per chunk
processed/reports/sample_chunks.json     readable samples
```
