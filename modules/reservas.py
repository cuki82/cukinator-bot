"""
Módulo de búsqueda de reservas en restaurantes
Conecta con el scraper corriendo en el VPS
"""
import httpx
import os
from datetime import datetime, timedelta
import re

SCRAPER_URL = os.getenv("SCRAPER_RESERVAS_URL", "http://31.97.151.119:3334")

async def buscar_disponibilidad(restaurante: str, fecha: str = None, personas: int = 2, plataforma: str = "auto") -> dict:
    """
    Busca disponibilidad en restaurantes.
    
    Args:
        restaurante: Nombre del restaurante
        fecha: Fecha en formato DD/MM/YYYY o texto como "mañana", "sábado", etc.
        personas: Cantidad de personas (default 2)
        plataforma: "meitre", "thefork", o "auto" (prueba ambas)
    
    Returns:
        dict con disponibilidad y horarios
    """
    # Parsear fecha si viene en texto
    fecha_parsed = parse_fecha(fecha) if fecha else get_tomorrow()
    
    query = {
        "restaurante": restaurante,
        "fecha": fecha_parsed,
        "personas": personas
    }
    
    results = []
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        if plataforma in ("auto", "meitre"):
            try:
                resp = await client.post(f"{SCRAPER_URL}/meitre", json=query)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("disponible") or data.get("horarios"):
                        results.append(data)
            except Exception as e:
                results.append({
                    "disponible": False,
                    "mensaje": f"Error Meitre: {str(e)}",
                    "source": "meitre"
                })
        
        if plataforma in ("auto", "thefork"):
            try:
                resp = await client.post(f"{SCRAPER_URL}/thefork", json=query)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("disponible") or data.get("horarios"):
                        results.append(data)
            except Exception as e:
                results.append({
                    "disponible": False,
                    "mensaje": f"Error TheFork: {str(e)}",
                    "source": "thefork"
                })
    
    # Consolidar resultados
    return consolidar_resultados(results, restaurante, fecha_parsed, personas)


def parse_fecha(texto: str) -> str:
    """Convierte texto de fecha a DD/MM/YYYY"""
    if not texto:
        return get_tomorrow()
    
    # Si ya está en formato DD/MM/YYYY
    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', texto):
        return texto
    
    texto = texto.lower().strip()
    hoy = datetime.now()
    
    if texto in ("hoy", "today"):
        return hoy.strftime("%d/%m/%Y")
    
    if texto in ("mañana", "manana", "tomorrow"):
        return (hoy + timedelta(days=1)).strftime("%d/%m/%Y")
    
    if texto in ("pasado mañana", "pasado manana"):
        return (hoy + timedelta(days=2)).strftime("%d/%m/%Y")
    
    # Días de la semana
    dias = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
    }
    
    for dia, num in dias.items():
        if dia in texto:
            days_ahead = num - hoy.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = hoy + timedelta(days=days_ahead)
            return target.strftime("%d/%m/%Y")
    
    # Si hay números, intentar parsear
    match = re.search(r'(\d{1,2})[/\-](\d{1,2})', texto)
    if match:
        dia, mes = match.groups()
        año = hoy.year
        if int(mes) < hoy.month:
            año += 1
        return f"{int(dia):02d}/{int(mes):02d}/{año}"
    
    # Default: mañana
    return get_tomorrow()


def get_tomorrow() -> str:
    """Devuelve la fecha de mañana en DD/MM/YYYY"""
    return (datetime.now() + timedelta(days=1)).strftime("%d/%m/%Y")


def consolidar_resultados(results: list, restaurante: str, fecha: str, personas: int) -> dict:
    """Consolida resultados de múltiples fuentes"""
    if not results:
        return {
            "encontrado": False,
            "mensaje": f"No pude conectar con el scraper de reservas",
            "restaurante": restaurante,
            "fecha": fecha,
            "personas": personas
        }
    
    # Buscar resultados con disponibilidad
    disponibles = [r for r in results if r.get("disponible")]
    
    if disponibles:
        # Tomar el que tenga más horarios
        mejor = max(disponibles, key=lambda x: len(x.get("horarios", [])))
        return {
            "encontrado": True,
            "disponible": True,
            "restaurante": restaurante,
            "fecha": fecha,
            "personas": personas,
            "horarios": mejor.get("horarios", []),
            "fuente": mejor.get("source", "desconocida"),
            "mensaje": f"Hay disponibilidad en {restaurante} para el {fecha}"
        }
    
    # Si ninguno tiene disponibilidad
    mensajes = [r.get("mensaje", "") for r in results if r.get("mensaje")]
    return {
        "encontrado": True,
        "disponible": False,
        "restaurante": restaurante,
        "fecha": fecha,
        "personas": personas,
        "horarios": [],
        "mensaje": mensajes[0] if mensajes else f"No hay disponibilidad en {restaurante} para el {fecha}",
        "fuentes_consultadas": [r.get("source") for r in results]
    }


# Para testing
if __name__ == "__main__":
    import asyncio
    
    async def test():
        result = await buscar_disponibilidad("Don Julio", "sábado", 4)
        print(result)
    
    asyncio.run(test())
