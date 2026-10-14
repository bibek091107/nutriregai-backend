"""PDF Report generation for Regulatory Audit."""
import io
import os
import uuid
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from app.api import db
from app.api.config import get_settings

async def generate_audit_report_pdf(product_id: str, analysis_id: str, report_title: str) -> str:
    """Generate a PDF audit report and return its storage_key."""
    # 1. Fetch data
    product = await db.fetch_one("SELECT * FROM products WHERE id = $1", product_id)
    ingredients = await db.fetch_all(
        """
        SELECT pi.*, i.name, i.cas_number
        FROM product_ingredients pi
        JOIN ingredients i ON pi.ingredient_id = i.id
        WHERE pi.product_id = $1 AND pi.is_active
        """,
        product_id
    )
    claims = await db.fetch_all("SELECT * FROM claims WHERE product_id = $1", product_id)
    documents = await db.fetch_all("SELECT * FROM documents WHERE product_id = $1", product_id)
    findings = await db.fetch_all("SELECT * FROM findings WHERE analysis_id = $1", analysis_id)
    checklist = await db.fetch_all("SELECT * FROM compliance_items WHERE analysis_id = $1", analysis_id)
    
    # 2. Build PDF
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40
    )
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    normal = styles["Normal"]
    
    elements = []
    
    # Title
    elements.append(Paragraph(report_title, title_style))
    elements.append(Spacer(1, 20))
    
    # Exec Summary
    elements.append(Paragraph("Executive Summary", h1))
    elements.append(Paragraph(f"<b>Product Name:</b> {product['name']}", normal))
    elements.append(Paragraph(f"<b>Category:</b> {product['category']}", normal))
    elements.append(Paragraph(f"<b>Target Market:</b> {product['target_market']}", normal))
    elements.append(Paragraph(f"<b>Status:</b> {product['status']}", normal))
    elements.append(Spacer(1, 10))
    
    elements.append(Paragraph(f"Ingredients Reviewed: {len(ingredients)}", normal))
    elements.append(Paragraph(f"Claims Reviewed: {len(claims)}", normal))
    elements.append(Paragraph(f"Documents Reviewed: {len(documents)}", normal))
    elements.append(Paragraph(f"Findings Detected: {len(findings)}", normal))
    elements.append(Spacer(1, 20))
    
    # Findings
    elements.append(Paragraph("Findings", h1))
    if findings:
        data = [["Title", "Severity", "Category", "Status"]]
        for f in findings:
            data.append([
                Paragraph(f['title'], normal),
                f['severity'],
                f['category'],
                f['status']
            ])
        t = Table(data, colWidths=[240, 60, 120, 60])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No findings detected.", normal))
    elements.append(Spacer(1, 20))

    # Checklist
    elements.append(Paragraph("Compliance Checklist", h1))
    if checklist:
        data = [["Category", "Label", "Passed"]]
        for c in checklist:
            passed_str = "Yes" if c['passed'] is True else ("No" if c['passed'] is False else "TBD")
            data.append([
                c['category'],
                Paragraph(c['label'], normal),
                passed_str
            ])
        t = Table(data, colWidths=[120, 300, 60])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No checklist items generated.", normal))
    
    doc.build(elements)
    
    # 3. Save PDF
    pdf_bytes = buffer.getvalue()
    storage_key = f"{uuid.uuid4().hex}.pdf"
    settings = get_settings()
    os.makedirs(settings.upload_dir, exist_ok=True)
    destination = os.path.join(settings.upload_dir, storage_key)
    with open(destination, "wb") as f:
        f.write(pdf_bytes)
        
    return storage_key
