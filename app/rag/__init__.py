"""Regulatory document preprocessing pipeline.

Stage scope, deliberately limited:

* PDF validation, manifest normalisation
* page-level text extraction and OCR-candidate detection
* section detection and section-aware chunking
* provenance attachment and local JSONL output
* validation and extraction reports

Not in scope at this stage, by design: embeddings, pgvector, vector search,
BM25, hybrid retrieval, reranking, Groq/LLM calls, FastAPI endpoints.
"""
