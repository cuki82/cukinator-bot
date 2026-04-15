"""
Módulo de reservas de restaurantes.
Conecta con el scraper corriendo en el VPS para buscar disponibilidad.
"""

import httpx
import os
from datetime import datetime, timedelta
import re

SCRAPER_URL = os.getenv("SCRAPER_RESERVAS_URL", "http://147.93.82.228:3334")

# Restaurantes conocidos y sus plataformas
RESTAURANTES = {
    # Meitre
    "don julio": {"plataforma": "meitre", "slug": "don-julio"},
    "elena": {"plataforma": "meitre", "slug": "elena"},
    "aramburu": {"plataforma": "meitre", "slug": "aramburu"},
    "anchoita": {"plataforma": "meitre", "slug": "anchoita"},
    "anafe": {"plataforma": "meitre", "slug": "anafe"},
    "mishiguene": {"plataforma": "meitre", "slug": "mishiguene"},
    "la mar": {"plataforma": "meitre", "slug": "la-mar"},
    "osaka": {"plataforma": "meitre", "slug": "osaka"},
    "proper": {"plataforma": "meitre", "slug": "proper"},
    "la carniceria": {"plataforma": "meitre", "slug": "la-carniceria"},
    "chori": {"plataforma": "meitre", "slug": "chori"},
    "fogon asado": {"plataforma": "meitre", "slug": "fogon-asado"},
    "victoria brown": {"plataforma": "meitre", "slug": "victoria-brown"},
    
    # TheFork
    "la parolaccia": {"plataforma": "thefork", "id": "la-parolaccia"},
    "sottovoce": {"plataforma": "thefork", "id": "sottovoce"},
    "il matterello": {"plataforma": "thefork", "id": "il-matterello"},
}

def parse_fecha(texto: str) -> str:
    """Convierte texto como 'viernes', 'mañana', '25/4' a YYYY-MM-DD"""
    hoy = datetime.now()
    texto = texto.lower().strip()
    
    # Día específico
    if re.match(r'\d{1,2}/\d{1,2}', texto):
        partes = texto.split('/')
        dia = int(partes[0])
        mes = int(partes[1])
        año = hoy.year
        if mes < hoy.month or (mes == hoy.month and dia < hoy.day):
            año += 1
        return f"{año}-{mes:02d}-{dia:02d}"
    
    # Relativos
    if texto in ['hoy']:
        return hoy.strftime("%Y-%m-%d")
    if texto in ['mañana', 'manana']:
        return (hoy + timedelta(days=1)).strftime("%Y-%m-%d")
    if texto in ['pasado mañana', 'pasado manana']:
        return (hoy + timedelta(days=2)).strftime("%Y-%m-%d")
    
    # Días de la semana
    dias_semana = {
        'lunes': 0, 'martes': 1, 'miércoles': 2, 'miercoles': 2,
        'jueves': 3, 'viernes': 4, 'sábado': 5, 'sabado': 5, 'domingo': 6
    }
    
    for nombre, num in dias_semana.items():
        if nombre in texto:
            dias_adelante = (num - hoy.weekday()) % 7
            if dias_adelante == 0:
                dias_adelante = 7  # Próxima semana
            return (hoy + timedelta(days=dias_adelante)).strftime("%Y-%m-%d")
    
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
        fecha: Fecha en texto natural ('viernes', 'mañana', '25/4')
        personas: Cantidad de comensales
        hora_preferida: Hora preferida HH:MM
        
    Returns:
        dict con disponibilidad o error
    """
    resto_lower = restaurante.lower().strip()
    
    # Buscar restaurante conocido
    resto_info = None
    for nombre, info in RESTAURANTES.items():
        if nombre in resto_lower or resto_lower in nombre:
            resto_info = info
            resto_info['nombre'] = nombre
            break
    
    if not resto_info:
        return {
            "ok": False,
            "error": f"No conozco el restaurante '{restaurante}'. Los que tengo son: {', '.join(RESTAURANTES.keys())}"
        }
    
    # Parsear fecha
    fecha_iso = parse_fecha(fecha)
    
    # Llamar al scraper
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if resto_info['plataforma'] == 'meitre':
                response = await client.post(
                    f"{SCRAPER_URL}/meitre",
                    json={
                        "restaurant_slug": resto_info['slug'],
                        "date": fecha_iso,
                        "party_size": personas,
                        "preferred_time": hora_preferida
                    }
                )
            else:  # thefork
                response = await client.post(
                    f"{SCRAPER_URL}/thefork",
                    json={
                        "restaurant_id": resto_info['id'],
                        "date": fecha_iso,
                        "party_size": personas
                    }
                )
            
            if response.status_code == 200:
                data = response.json()
                data['restaurante'] = resto_info['nombre'].title()
                data['fecha_consultada'] = fecha_iso
                data['personas'] = personas
                return data
            else:
                return {
                    "ok": False,
                    "error": f"Error del scraper: {response.status_code}"
                }
                
    except httpx.TimeoutException:
        return {
            "ok": False,
            "error": "Timeout consultando disponibilidad. El scraper tardó demasiado."
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"Error conectando al scraper: {str(e)}"
        }


def formatear_resultado(resultado: dict) -> str:
    """Formatea el resultado para mostrar al usuario"""
    if not resultado.get('ok', False):
        return f"❌ {resultado.get('error', 'Error desconocido')}"
    
    resto = resultado.get('restaurante', 'Restaurante')
    fecha = resultado.get('fecha_consultada', '')
    personas = resultado.get('personas', 2)
    slots = resultado.get('available_slots', [])
    
    if not slots:
        return f"No hay disponibilidad en {resto} para el {fecha} ({personas} personas)"
    
    lineas = [f"**{resto}** — {fecha} ({personas} personas)", ""]
    lineas.append("Horarios disponibles:")
    
    for slot in slots[:10]:  # Max 10 horarios
        lineas.append(f"  • {slot}")
    
    if len(slots) > 10:
        lineas.append(f"  ... y {len(slots) - 10} más")
    
    if resultado.get('booking_url'):
        lineas.append(f"\n🔗 Reservar: {resultado['booking_url']}")
    
    return "\n".join(lineas)
