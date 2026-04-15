"""
Módulo de reservas de restaurantes.
Conecta con el scraper corriendo en el VPS para buscar disponibilidad.
"""

import httpx
import asyncio
from datetime import datetime, timedelta
import re

SCRAPER_URL = "http://31.97.151.119:3334"

# Restaurantes conocidos y sus plataformas
RESTAURANTES_CONOCIDOS = {
    # Meitre
    "don julio": {"plataforma": "meitre", "slug": "don-julio"},
    "la carniceria": {"plataforma": "meitre", "slug": "la-carniceria"},
    "elena": {"plataforma": "meitre", "slug": "elena"},
    "aramburu": {"plataforma": "meitre", "slug": "aramburu"},
    "mishiguene": {"plataforma": "meitre", "slug": "mishiguene"},
    "anchoita": {"plataforma": "meitre", "slug": "anchoita"},
    "proper": {"plataforma": "meitre", "slug": "proper"},
    "la mar": {"plataforma": "meitre", "slug": "la-mar"},
    "alo's": {"plataforma": "meitre", "slug": "alos"},
    "osaka": {"plataforma": "meitre", "slug": "osaka"},
    "gran dabbang": {"plataforma": "meitre", "slug": "gran-dabbang"},
    "narda comedor": {"plataforma": "meitre", "slug": "narda-comedor"},
    "chila": {"plataforma": "meitre", "slug": "chila"},
    "tegui": {"plataforma": "meitre", "slug": "tegui"},
    "victoria brown": {"plataforma": "meitre", "slug": "victoria-brown"},
    "la alacena": {"plataforma": "meitre", "slug": "la-alacena"},
    "floreria atlantico": {"plataforma": "meitre", "slug": "floreria-atlantico"},
    
    # TheFork - agregar según necesidad
    "la parolaccia": {"plataforma": "thefork", "id": "la-parolaccia-ba"},
    "i latina": {"plataforma": "thefork", "id": "i-latina"},
}


def normalizar_restaurante(nombre: str) -> dict:
    """Busca el restaurante en la base de conocidos."""
    nombre_lower = nombre.lower().strip()
    
    for key, data in RESTAURANTES_CONOCIDOS.items():
        if key in nombre_lower or nombre_lower in key:
            return {"nombre": key.title(), **data}
    
    # Si no está en la lista, intentar con slug genérico para Meitre
    slug = nombre_lower.replace(" ", "-").replace("'", "")
    return {"nombre": nombre, "plataforma": "meitre", "slug": slug}


def parsear_fecha(texto_fecha: str) -> str:
    """Convierte texto de fecha a formato YYYY-MM-DD."""
    hoy = datetime.now()
    texto = texto_fecha.lower().strip()
    
    # Días de la semana
    dias_semana = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
    }
    
    if texto in ["hoy", "today"]:
        return hoy.strftime("%Y-%m-%d")
    elif texto in ["mañana", "manana", "tomorrow"]:
        return (hoy + timedelta(days=1)).strftime("%Y-%m-%d")
    elif texto in ["pasado mañana", "pasado manana"]:
        return (hoy + timedelta(days=2)).strftime("%Y-%m-%d")
    
    # Buscar día de la semana
    for dia, num in dias_semana.items():
        if dia in texto:
            dias_adelante = (num - hoy.weekday()) % 7
            if dias_adelante == 0:
                dias_adelante = 7  # próxima semana
            if "próximo" in texto or "proximo" in texto or "que viene" in texto:
                dias_adelante += 7
            fecha = hoy + timedelta(days=dias_adelante)
            return fecha.strftime("%Y-%m-%d")
    
    # Intentar parsear fecha explícita
    formatos = ["%d/%m/%Y", "%d-%m-%Y", "%d/%m", "%d-%m", "%Y-%m-%d"]
    for fmt in formatos:
        try:
            fecha = datetime.strptime(texto, fmt)
            if fecha.year == 1900:  # Sin año
                fecha = fecha.replace(year=hoy.year)
            return fecha.strftime("%Y-%m-%d")
        except ValueError:
            continue
    
    # Default: mañana
    return (hoy + timedelta(days=1)).strftime("%Y-%m-%d")


async def buscar_disponibilidad(
    restaurante: str,
    fecha: str,
    personas: int = 2,
    hora_preferida: str = "21:00"
) -> dict:
    """
    Busca disponibilidad en un restaurante.
    
    Args:
        restaurante: Nombre del restaurante
        fecha: Fecha en texto natural o YYYY-MM-DD
        personas: Cantidad de personas
        hora_preferida: Hora preferida HH:MM
        
    Returns:
        dict con slots disponibles o error
    """
    # Normalizar restaurante
    info = normalizar_restaurante(restaurante)
    
    # Parsear fecha
    fecha_parsed = parsear_fecha(fecha)
    
    # Preparar request
    plataforma = info["plataforma"]
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if plataforma == "meitre":
                response = await client.post(
                    f"{SCRAPER_URL}/meitre",
                    json={
                        "restaurant_slug": info.get("slug"),
                        "date": fecha_parsed,
                        "party_size": personas,
                        "preferred_time": hora_preferida
                    }
                )
            elif plataforma == "thefork":
                response = await client.post(
                    f"{SCRAPER_URL}/thefork",
                    json={
                        "restaurant_id": info.get("id"),
                        "date": fecha_parsed,
                        "party_size": personas,
                        "preferred_time": hora_preferida
                    }
                )
            else:
                return {"error": f"Plataforma {plataforma} no soportada"}
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "restaurante": info["nombre"],
                    "fecha": fecha_parsed,
                    "personas": personas,
                    "plataforma": plataforma,
                    **data
                }
            else:
                return {
                    "error": f"Error del scraper: {response.status_code}",
                    "detalle": response.text
                }
                
    except httpx.TimeoutException:
        return {"error": "Timeout - el restaurante tardó mucho en responder"}
    except httpx.ConnectError:
        return {"error": "No se pudo conectar al scraper de reservas"}
    except Exception as e:
        return {"error": f"Error inesperado: {str(e)}"}


def formatear_resultado(resultado: dict) -> str:
    """Formatea el resultado para mostrar en Telegram."""
    if "error" in resultado:
        return f"❌ {resultado['error']}"
    
    slots = resultado.get("slots", [])
    restaurante = resultado.get("restaurante", "Restaurante")
    fecha = resultado.get("fecha", "")
    personas = resultado.get("personas", 2)
    
    if not slots:
        return f"No hay disponibilidad en {restaurante} para el {fecha} ({personas} personas)"
    
    # Formatear slots
    lineas = [f"**{restaurante}** - {fecha} ({personas} personas)\n"]
    lineas.append("Horarios disponibles:")
    
    for slot in slots[:10]:  # Máximo 10 slots
        hora = slot.get("time", slot.get("hora", "??:??"))
        lineas.append(f"  • {hora}")
    
    if len(slots) > 10:
        lineas.append(f"  ... y {len(slots) - 10} horarios más")
    
    # Link de reserva si existe
    if resultado.get("booking_url"):
        lineas.append(f"\n🔗 Reservar: {resultado['booking_url']}")
    
    return "\n".join(lineas)
