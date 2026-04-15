"""
Módulo de búsqueda de disponibilidad en restaurantes.
Conecta con el scraper en VPS para Meitre y TheFork.
"""

import httpx
from datetime import datetime, timedelta
import re

SCRAPER_URL = "http://31.97.151.119:3334"

async def buscar_disponibilidad(restaurante: str, fecha: str = None, personas: int = 2, plataforma: str = "auto") -> dict:
    """
    Busca disponibilidad en restaurantes.
    
    Args:
        restaurante: Nombre del restaurante
        fecha: Fecha en formato DD/MM/YYYY o texto natural (mañana, viernes, etc)
        personas: Cantidad de personas
        plataforma: 'meitre', 'thefork', o 'auto' (prueba ambas)
    
    Returns:
        dict con disponibilidad y horarios
    """
    
    # Normalizar fecha
    fecha_norm = normalizar_fecha(fecha)
    
    query = {
        "restaurante": restaurante,
        "fecha": fecha_norm,
        "personas": personas
    }
    
    resultados = []
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        if plataforma in ("auto", "meitre"):
            try:
                resp = await client.post(f"{SCRAPER_URL}/meitre", json=query)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("disponible") or data.get("horarios"):
                        resultados.append(data)
            except Exception as e:
                resultados.append({
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
                        resultados.append(data)
            except Exception as e:
                resultados.append({
                    "disponible": False,
                    "mensaje": f"Error TheFork: {str(e)}",
                    "source": "thefork"
                })
    
    # Consolidar resultados
    if not resultados:
        return {
            "encontrado": False,
            "mensaje": f"No pude consultar disponibilidad para {restaurante}",
            "restaurante": restaurante,
            "fecha": fecha_norm,
            "personas": personas
        }
    
    # Tomar el mejor resultado
    for r in resultados:
        if r.get("disponible") and r.get("horarios"):
            return {
                "encontrado": True,
                "disponible": True,
                "restaurante": restaurante,
                "fecha": fecha_norm,
                "personas": personas,
                "horarios": r["horarios"],
                "source": r["source"],
                "mensaje": r.get("mensaje", "")
            }
    
    # Si ninguno tiene disponibilidad
    return {
        "encontrado": True,
        "disponible": False,
        "restaurante": restaurante,
        "fecha": fecha_norm,
        "personas": personas,
        "mensaje": resultados[0].get("mensaje", "Sin disponibilidad"),
        "source": resultados[0].get("source", "")
    }


def normalizar_fecha(fecha_input: str = None) -> str:
    """Convierte fecha natural a DD/MM/YYYY"""
    
    if not fecha_input:
        # Default: mañana
        tomorrow = datetime.now() + timedelta(days=1)
        return tomorrow.strftime("%d/%m/%Y")
    
    fecha_lower = fecha_input.lower().strip()
    today = datetime.now()
    
    # Fecha ya en formato DD/MM/YYYY
    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', fecha_input):
        return fecha_input
    
    # Fecha en formato DD/MM
    match = re.match(r'(\d{1,2})/(\d{1,2})', fecha_input)
    if match:
        day, month = match.groups()
        return f"{int(day):02d}/{int(month):02d}/{today.year}"
    
    # Palabras clave
    if fecha_lower in ("hoy", "today"):
        return today.strftime("%d/%m/%Y")
    
    if fecha_lower in ("mañana", "manana", "tomorrow"):
        return (today + timedelta(days=1)).strftime("%d/%m/%Y")
    
    if fecha_lower in ("pasado mañana", "pasado manana"):
        return (today + timedelta(days=2)).strftime("%d/%m/%Y")
    
    # Días de la semana
    dias = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
    }
    
    for dia_nombre, dia_num in dias.items():
        if dia_nombre in fecha_lower:
            days_ahead = dia_num - today.weekday()
            if days_ahead <= 0:  # Si ya pasó esta semana
                days_ahead += 7
            target = today + timedelta(days=days_ahead)
            return target.strftime("%d/%m/%Y")
    
    # Si no matchea nada, devolver mañana
    return (today + timedelta(days=1)).strftime("%d/%m/%Y")


# Tool function para el bot
async def tool_buscar_reserva(restaurante: str, fecha: str = None, personas: int = 2) -> str:
    """
    Tool function que llama el bot.
    Devuelve string formateado para mostrar al usuario.
    """
    result = await buscar_disponibilidad(restaurante, fecha, personas)
    
    if not result.get("encontrado"):
        return result["mensaje"]
    
    if result.get("disponible") and result.get("horarios"):
        horarios_str = ", ".join(result["horarios"][:8])
        return (
            f"**{result['restaurante']}** — {result['fecha']} ({result['personas']} personas)\n"
            f"✅ Disponible en {result['source'].title()}\n"
            f"Horarios: {horarios_str}"
        )
    else:
        return (
            f"**{result['restaurante']}** — {result['fecha']} ({result['personas']} personas)\n"
            f"❌ {result['mensaje']}"
        )
