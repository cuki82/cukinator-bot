"""
Módulo de búsqueda de disponibilidad en restaurantes.
Usa Playwright para navegación dinámica y scraping de sistemas de reservas.
Soporta: TheFork, Resy, OpenTable, y sitios propios.
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Intentar importar playwright
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright no disponible - instalar con: pip install playwright && playwright install chromium")


async def buscar_restaurante(query: str) -> dict:
    """
    Busca un restaurante y obtiene su URL de reservas.
    """
    from modules.tools import search_web_tool  # Import interno para evitar circular
    
    # Buscar el restaurante
    search_query = f"{query} restaurante reservas"
    results = await search_web_tool(search_query)
    
    return {
        "query": query,
        "search_results": results,
        "status": "search_complete"
    }


async def detectar_sistema_reservas(url: str) -> str:
    """
    Detecta qué sistema de reservas usa el restaurante.
    """
    url_lower = url.lower()
    
    if "thefork" in url_lower or "lafourchette" in url_lower:
        return "thefork"
    elif "resy.com" in url_lower:
        return "resy"
    elif "opentable" in url_lower:
        return "opentable"
    elif "covermanager" in url_lower:
        return "covermanager"
    elif "restorando" in url_lower:
        return "restorando"
    else:
        return "unknown"


async def scrape_thefork(url: str, fecha: str, personas: int = 2) -> dict:
    """
    Scrape de disponibilidad en TheFork.
    fecha: formato YYYY-MM-DD
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright no instalado", "install": "pip install playwright && playwright install chromium"}
    
    horarios_disponibles = []
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            # Construir URL con parámetros
            # TheFork usa formato: ?date=YYYY-MM-DD&partySize=N
            if "?" in url:
                full_url = f"{url}&date={fecha}&partySize={personas}"
            else:
                full_url = f"{url}?date={fecha}&partySize={personas}"
            
            await page.goto(full_url, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            
            # Esperar a que carguen los slots de horarios
            await asyncio.sleep(2)
            
            # Buscar slots de horarios disponibles (selectores comunes de TheFork)
            selectors = [
                "[data-testid='time-slot']",
                ".timeSlot",
                ".availability-slot",
                "button[class*='slot']",
                ".booking-slot",
                "[class*='TimeSlot']"
            ]
            
            for selector in selectors:
                try:
                    slots = await page.query_selector_all(selector)
                    if slots:
                        for slot in slots:
                            text = await slot.text_content()
                            if text and re.search(r'\d{1,2}[:\.]?\d{0,2}', text):
                                horarios_disponibles.append(text.strip())
                        if horarios_disponibles:
                            break
                except:
                    continue
            
            # Si no encontró con selectores específicos, buscar patrón de horarios
            if not horarios_disponibles:
                content = await page.content()
                # Buscar patrones de horarios (12:00, 13:30, etc)
                horarios = re.findall(r'\b([012]?\d)[:\.]([0-5]\d)\b', content)
                horarios_filtrados = []
                for h, m in horarios:
                    hora_int = int(h)
                    if 11 <= hora_int <= 23:  # Horarios típicos de restaurante
                        horarios_filtrados.append(f"{h}:{m}")
                horarios_disponibles = list(set(horarios_filtrados))[:10]
            
            # Obtener nombre del restaurante
            nombre = ""
            try:
                title = await page.title()
                nombre = title.split("-")[0].strip() if title else ""
            except:
                pass
            
            await browser.close()
            
            return {
                "restaurante": nombre,
                "sistema": "thefork",
                "fecha": fecha,
                "personas": personas,
                "horarios_disponibles": sorted(horarios_disponibles),
                "url": full_url,
                "status": "success" if horarios_disponibles else "no_availability"
            }
            
    except PlaywrightTimeout:
        return {"error": "Timeout al cargar la página", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


async def scrape_opentable(url: str, fecha: str, personas: int = 2) -> dict:
    """
    Scrape de disponibilidad en OpenTable.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright no instalado"}
    
    horarios_disponibles = []
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            
            # OpenTable usa parámetros diferentes
            if "?" in url:
                full_url = f"{url}&covers={personas}&dateTime={fecha}T19:00"
            else:
                full_url = f"{url}?covers={personas}&dateTime={fecha}T19:00"
            
            await page.goto(full_url, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(2)
            
            # Selectores de OpenTable
            selectors = [
                "[data-test='time-button']",
                ".timeslot",
                "button[class*='TimeButton']",
                ".availability-time"
            ]
            
            for selector in selectors:
                try:
                    slots = await page.query_selector_all(selector)
                    if slots:
                        for slot in slots:
                            text = await slot.text_content()
                            if text:
                                horarios_disponibles.append(text.strip())
                        if horarios_disponibles:
                            break
                except:
                    continue
            
            nombre = ""
            try:
                title = await page.title()
                nombre = title.split("|")[0].strip() if title else ""
            except:
                pass
            
            await browser.close()
            
            return {
                "restaurante": nombre,
                "sistema": "opentable",
                "fecha": fecha,
                "personas": personas,
                "horarios_disponibles": horarios_disponibles,
                "url": full_url,
                "status": "success" if horarios_disponibles else "no_availability"
            }
            
    except Exception as e:
        return {"error": str(e), "url": url}


async def scrape_generic(url: str, fecha: str, personas: int = 2) -> dict:
    """
    Intento genérico de scraping para sistemas desconocidos.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright no instalado"}
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            
            # Buscar links o botones de reserva
            reserva_links = []
            selectors_reserva = [
                "a[href*='reserv']",
                "a[href*='booking']",
                "button:has-text('Reservar')",
                "button:has-text('Book')",
                "a:has-text('Reservar')",
                "[class*='reserv']",
                "[class*='booking']"
            ]
            
            for selector in selectors_reserva:
                try:
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        href = await el.get_attribute("href")
                        text = await el.text_content()
                        if href or text:
                            reserva_links.append({"href": href, "text": text.strip() if text else ""})
                except:
                    continue
            
            # Buscar horarios en el contenido
            content = await page.content()
            horarios = re.findall(r'\b([012]?\d)[:\.]([0-5]\d)\s*(hs|hrs|h)?\b', content, re.IGNORECASE)
            horarios_encontrados = list(set([f"{h}:{m}" for h, m, _ in horarios if 11 <= int(h) <= 23]))
            
            nombre = ""
            try:
                title = await page.title()
                nombre = title.split("|")[0].split("-")[0].strip() if title else ""
            except:
                pass
            
            await browser.close()
            
            return {
                "restaurante": nombre,
                "sistema": "unknown",
                "links_reserva": reserva_links[:5],
                "horarios_encontrados": horarios_encontrados[:10],
                "url": url,
                "nota": "Sistema no reconocido - se encontraron estos elementos de reserva"
            }
            
    except Exception as e:
        return {"error": str(e), "url": url}


async def consultar_disponibilidad(
    restaurante: str,
    fecha: Optional[str] = None,
    personas: int = 2,
    url: Optional[str] = None
) -> dict:
    """
    Función principal para consultar disponibilidad.
    
    Args:
        restaurante: Nombre del restaurante a buscar
        fecha: Fecha en formato YYYY-MM-DD (default: mañana)
        personas: Cantidad de personas (default: 2)
        url: URL directa del restaurante (opcional, si no se busca)
    
    Returns:
        dict con horarios disponibles o error
    """
    
    # Si no hay fecha, usar mañana
    if not fecha:
        fecha = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Si no hay URL, buscar el restaurante
    if not url:
        # Buscar en TheFork primero (más común en Argentina/LATAM)
        search_results = await buscar_restaurante(f"{restaurante} thefork")
        
        # Extraer URL de los resultados (simplificado)
        # En implementación real, parsear los resultados de búsqueda
        return {
            "status": "search_required",
            "restaurante": restaurante,
            "fecha": fecha,
            "personas": personas,
            "message": "Necesito la URL del restaurante. Buscá en TheFork/OpenTable y pasame el link.",
            "search_suggestion": f"https://www.thefork.com.ar/search?q={restaurante.replace(' ', '+')}"
        }
    
    # Detectar sistema de reservas
    sistema = await detectar_sistema_reservas(url)
    
    # Llamar al scraper correspondiente
    if sistema == "thefork":
        return await scrape_thefork(url, fecha, personas)
    elif sistema == "opentable":
        return await scrape_opentable(url, fecha, personas)
    else:
        return await scrape_generic(url, fecha, personas)


# Tool definition para el agente
TOOL_DEFINITION = {
    "name": "restaurant_availability",
    "description": "Busca disponibilidad de horarios en restaurantes. Soporta TheFork, OpenTable, Resy y otros sistemas. Necesita el nombre del restaurante o URL directa, fecha y cantidad de personas.",
    "parameters": {
        "type": "object",
        "properties": {
            "restaurante": {
                "type": "string",
                "description": "Nombre del restaurante"
            },
            "fecha": {
                "type": "string",
                "description": "Fecha para la reserva en formato YYYY-MM-DD. Default: mañana"
            },
            "personas": {
                "type": "integer",
                "description": "Cantidad de personas. Default: 2"
            },
            "url": {
                "type": "string",
                "description": "URL directa del restaurante en TheFork/OpenTable (opcional)"
            }
        },
        "required": ["restaurante"]
    }
}
