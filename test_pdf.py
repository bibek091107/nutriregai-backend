import asyncio
from app.api.pdf_report import generate_audit_report_pdf
from app.api import db

async def run():
    await db.connect()
    try:
        product = await db.fetch_one("SELECT id FROM products LIMIT 1")
        if product:
            analysis = await db.fetch_one("SELECT id FROM analyses WHERE product_id = $1 LIMIT 1", product["id"])
            if analysis:
                print("Generating...")
                key = await generate_audit_report_pdf(str(product["id"]), str(analysis["id"]), "Test Report")
                print("Generated:", key)
            else:
                print("No analysis found")
        else:
            print("No product found")
    finally:
        await db.disconnect()

asyncio.run(run())
