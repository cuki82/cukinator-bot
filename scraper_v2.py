from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
import asyncio
from datetime import datetime
import uvicorn
import re

app = FastAPI(title="Scraper Reservas v2")

class ReservaQuery(BaseModel):
    restaurante: str
    fecha: str  # DD/MM/YYYY
    personas: int = 2

class ReservaResult(BaseModel):
    disponible: bool
    horarios: list[str] = []
    mensaje: str = ""
    source: str = ""

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.post("/meitre", response_model=ReservaResult)
async def buscar_meitre(query: ReservaQuery):
    try:
        parts = query.fecha.split('/')
        fecha_iso = f"{parts[2]}-{parts[1]}-{parts[0]}"

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
            page.set_default_timeout(20000)

            # Ir directo al restaurante en Meitre
            slug = query.restaurante.lower().strip()
            slug = re.sub(r"[áàä]", "a", slug)
            slug = re.sub(r"[éèë]", "e", slug)
            slug = re.sub(r"[íìï]", "i", slug)
            slug = re.sub(r"[óòö]", "o", slug)
            slug = re.sub(r"[úùü]", "u", slug)
            slug = re.sub(r"[^a-z0-9\s]", "", slug).strip()
            slug = slug.replace(" ", "-")

            url = f"https://reservas.meitre.com/restaurante/{slug}"
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Setear fecha
            try:
                await page.fill('input[type="date"]', fecha_iso)
                await page.wait_for_timeout(1000)
            except:
                pass

            # Setear personas
            try:
                select = page.locator('select').first
                await select.select_option(str(query.personas))
                await page.wait_for_timeout(1000)
            except:
                pass

            # Buscar botón de búsqueda y clickear
            try:
                await page.click('button[type="submit"], button:has-text("Buscar"), button:has-text("Ver")')
                await page.wait_for_timeout(3000)
            except:
                pass

            # Capturar horarios disponibles
            horarios = []
            selectors = [
                'button.time-slot:not([disabled])',
                'button[class*="slot"]:not([disabled])',
                'div[class*="time"]:not([class*="disabled"])',
                'button[class*="time"]:not([disabled])',
                'span[class*="hour"]',
            ]
            for sel in selectors:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    text = await el.text_content()
                    if text and re.match(r'\d{1,2}:\d{2}', text.strip()):
                        horarios.append(text.strip())
                if horarios:
                    break

            await browser.close()

            if horarios:
                return ReservaResult(
                    disponible=True,
                    horarios=list(dict.fromkeys(horarios))[:10],
                    mensaje=f"{len(horarios)} horarios en {query.restaurante.title()}",
                    source="meitre"
                )
            else:
                return ReservaResult(
                    disponible=False,
                    mensaje=f"Sin disponibilidad para {query.restaurante.title()} el {query.fecha} x{query.personas}",
                    source="meitre"
                )

    except Exception as e:
        return ReservaResult(
            disponible=False,
            mensaje=f"Error: {str(e)[:200]}",
            source="meitre"
        )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3334)
