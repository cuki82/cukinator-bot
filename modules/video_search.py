"""
Módulo mejorado de búsqueda de videos en YouTube.
Verifica disponibilidad antes de enviar y tiene fallback a múltiples resultados.

Autor: Cukinator Bot
Fecha: 2026-04-15
"""

import logging
import re
from typing import Optional, Dict, List
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)


class VideoSearchError(Exception):
    """Error personalizado para búsqueda de videos"""
    pass


def verificar_disponibilidad(video_url: str) -> Dict:
    """
    Verifica que un video de YouTube esté disponible y sea público.
    
    Returns:
        Dict con info del video si está disponible
    
    Raises:
        VideoSearchError si el video no está disponible
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'socket_timeout': 10,
    }
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            if not info:
                raise VideoSearchError("No se pudo extraer información del video")
            
            # Verificar que no sea privado o eliminado
            if info.get('is_private'):
                raise VideoSearchError("El video es privado")
            
            if info.get('availability') not in [None, 'public', 'unlisted']:
                raise VideoSearchError(f"Video no disponible: {info.get('availability')}")
            
            # Verificar que tenga duración (videos eliminados no la tienen)
            if not info.get('duration'):
                raise VideoSearchError("El video no tiene duración válida (posiblemente eliminado)")
            
            return {
                'url': video_url,
                'title': info.get('title', 'Sin título'),
                'duration': info.get('duration', 0),
                'channel': info.get('channel', info.get('uploader', 'Desconocido')),
                'view_count': info.get('view_count', 0),
                'upload_date': info.get('upload_date', ''),
                'thumbnail': info.get('thumbnail', ''),
                'is_available': True
            }
            
    except VideoSearchError:
        raise
    except Exception as e:
        error_msg = str(e).lower()
        if 'private' in error_msg:
            raise VideoSearchError("El video es privado")
        elif 'unavailable' in error_msg or 'not available' in error_msg:
            raise VideoSearchError("El video no está disponible")
        elif 'removed' in error_msg or 'deleted' in error_msg:
            raise VideoSearchError("El video fue eliminado")
        elif 'copyright' in error_msg:
            raise VideoSearchError("El video fue removido por copyright")
        else:
            raise VideoSearchError(f"Error verificando video: {e}")


def buscar_videos_youtube(query: str, max_results: int = 5) -> List[Dict]:
    """
    Busca videos en YouTube y devuelve una lista de resultados.
    
    Args:
        query: Término de búsqueda
        max_results: Cantidad máxima de resultados a obtener
        
    Returns:
        Lista de dicts con info básica de cada video
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'default_search': 'ytsearch',
        'socket_timeout': 15,
    }
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            
            if not result or 'entries' not in result:
                return []
            
            videos = []
            for entry in result['entries']:
                if entry and entry.get('id'):
                    videos.append({
                        'url': f"https://www.youtube.com/watch?v={entry['id']}",
                        'id': entry['id'],
                        'title': entry.get('title', 'Sin título'),
                        'duration': entry.get('duration', 0),
                        'channel': entry.get('channel', entry.get('uploader', '')),
                    })
            
            return videos
            
    except Exception as e:
        logger.error(f"Error buscando videos: {e}")
        return []


def buscar_video_verificado(
    query: str, 
    max_duration: int = 600,
    max_intentos: int = 5
) -> Optional[Dict]:
    """
    Busca un video en YouTube y verifica que esté disponible antes de devolverlo.
    Si el primer resultado no está disponible, prueba con los siguientes.
    
    Args:
        query: Término de búsqueda
        max_duration: Duración máxima en segundos (default 10 min)
        max_intentos: Cantidad de videos a probar si los anteriores fallan
        
    Returns:
        Dict con info del video verificado, o None si no encuentra ninguno disponible
    """
    logger.info(f"Buscando video: '{query}' (max {max_duration}s)")
    
    # Obtener lista de candidatos
    candidatos = buscar_videos_youtube(query, max_results=max_intentos)
    
    if not candidatos:
        logger.warning(f"No se encontraron resultados para: {query}")
        return None
    
    # Probar cada candidato hasta encontrar uno disponible
    errores = []
    for i, video in enumerate(candidatos):
        try:
            logger.info(f"Verificando video {i+1}/{len(candidatos)}: {video['title'][:50]}...")
            
            info = verificar_disponibilidad(video['url'])
            
            # Verificar duración
            if info['duration'] > max_duration:
                logger.info(f"Video muy largo ({info['duration']}s > {max_duration}s), saltando...")
                errores.append(f"Video {i+1}: muy largo ({info['duration']}s)")
                continue
            
            logger.info(f"Video verificado OK: {info['title']}")
            return info
            
        except VideoSearchError as e:
            logger.warning(f"Video {i+1} no disponible: {e}")
            errores.append(f"Video {i+1}: {e}")
            continue
        except Exception as e:
            logger.error(f"Error inesperado verificando video {i+1}: {e}")
            errores.append(f"Video {i+1}: error inesperado")
            continue
    
    # Ningún video disponible
    logger.error(f"Ningún video disponible de {len(candidatos)} candidatos. Errores: {errores}")
    return None


def formatear_duracion(segundos: int) -> str:
    """Formatea duración en formato legible"""
    if not segundos:
        return "?"
    mins, secs = divmod(segundos, 60)
    if mins >= 60:
        hours, mins = divmod(mins, 60)
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


# Función principal para usar desde el bot
def buscar_video(query: str, max_duration: int = 600) -> Dict:
    """
    Función principal para buscar videos desde el bot.
    
    Args:
        query: Búsqueda
        max_duration: Duración máxima en segundos
        
    Returns:
        Dict con 'success', 'url', 'title', 'duration', 'message'
    """
    try:
        video = buscar_video_verificado(query, max_duration)
        
        if video:
            return {
                'success': True,
                'url': video['url'],
                'title': video['title'],
                'duration': formatear_duracion(video['duration']),
                'channel': video['channel'],
                'message': f"🎬 **{video['title']}**\n⏱ {formatear_duracion(video['duration'])} | 📺 {video['channel']}\n\n{video['url']}"
            }
        else:
            return {
                'success': False,
                'url': None,
                'title': None,
                'message': f"No encontré videos disponibles para '{query}'. Puede que estén privados o bloqueados en tu región."
            }
            
    except Exception as e:
        logger.error(f"Error en buscar_video: {e}")
        return {
            'success': False,
            'url': None,
            'title': None,
            'message': f"Error buscando video: {e}"
        }


# Para testing directo
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = "River Plate goles 2026"
    
    print(f"Buscando: {query}\n")
    resultado = buscar_video(query)
    
    if resultado['success']:
        print(resultado['message'])
    else:
        print(f"ERROR: {resultado['message']}")
