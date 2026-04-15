"""
Módulo de base de datos para restaurantes.
Maneja la persistencia de restaurantes, sistemas de reserva y caché.
"""

import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_connection():
    """Obtiene conexión a PostgreSQL de Railway."""
    if not DATABASE_URL:
        log.error("DATABASE_URL no configurada")
        return None
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Crea la tabla de restaurantes si no existe."""
    try:
        conn = get_connection()
        if not conn:
            return False
        
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS restaurantes (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(255) NOT NULL,
                nombre_normalizado VARCHAR(255) NOT NULL,
                sistema_reservas VARCHAR(50),
                url_reservas TEXT,
                telefono VARCHAR(50),
                direccion TEXT,
                barrio VARCHAR(100),
                ciudad VARCHAR(100) DEFAULT 'Buenos Aires',
                tipo_cocina VARCHAR(100),
                precio_rango VARCHAR(20),
                notas TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
            
            CREATE INDEX IF NOT EXISTS idx_restaurantes_nombre 
            ON restaurantes(nombre_normalizado);
            
            CREATE INDEX IF NOT EXISTS idx_restaurantes_barrio 
            ON restaurantes(barrio);
        """)
        
        # Insertar restaurantes conocidos si la tabla está vacía
        cur.execute("SELECT COUNT(*) FROM restaurantes")
        count = cur.fetchone()[0]
        
        if count == 0:
            restaurantes_iniciales = [
                ('Don Julio', 'don julio', 'restorando', 'https://www.restorando.com.ar/don-julio', '11 4832-6058', 'Guatemala 4699', 'Palermo', 'Parrilla', '$$$$'),
                ('Osten', 'osten', 'meitre', 'https://osten.meitre.com', '11 2504-3476', 'Juana Manso 1890', 'Puerto Madero', 'Contemporánea', '$$$$'),
                ('La Carnicería', 'la carniceria', 'thefork', 'https://www.thefork.com.ar/restaurante/la-carniceria', '11 4776-9595', 'Thames 2317', 'Palermo', 'Parrilla', '$$$'),
                ('Proper', 'proper', 'meitre', 'https://proper.meitre.com', '11 4778-4273', 'Aráoz 1676', 'Palermo', 'Contemporánea', '$$$'),
                ('Anchoíta', 'anchoita', 'restorando', 'https://www.restorando.com.ar/anchoita', '11 4831-7673', 'Costa Rica 5300', 'Palermo', 'Mediterránea', '$$$'),
                ('Elena', 'elena', 'opentable', 'https://www.opentable.com/elena-four-seasons', '11 4321-1728', 'Posadas 1086', 'Recoleta', 'Internacional', '$$$$'),
                ('Aramburu', 'aramburu', 'telefono', None, '11 4305-0439', 'Salta 1050', 'Constitución', 'Autor', '$$$$'),
                ('Mishiguene', 'mishiguene', 'restorando', 'https://www.restorando.com.ar/mishiguene', '11 3971-5765', 'Lafinur 3368', 'Palermo', 'Judía', '$$$'),
                ('Tegui', 'tegui', 'telefono', None, '11 5291-3333', 'Costa Rica 5852', 'Palermo', 'Autor', '$$$$'),
                ('Chila', 'chila', 'meitre', 'https://chila.meitre.com', '11 4343-6067', 'Av. Alicia Moreau de Justo 1160', 'Puerto Madero', 'Autor', '$$$$'),
                ('La Mar', 'la mar', 'restorando', 'https://www.restorando.com.ar/la-mar-cebicheria', '11 4776-5543', 'Arévalo 2024', 'Palermo', 'Peruana', '$$$'),
                ('Osaka', 'osaka', 'restorando', 'https://www.restorando.com.ar/osaka', '11 4775-6964', 'Soler 5608', 'Palermo', 'Nikkei', '$$$$'),
                ('Florería Atlántico', 'floreria atlantico', 'telefono', None, '11 4313-6093', 'Arroyo 872', 'Retiro', 'Bar/Coctelería', '$$$'),
                ('Gran Dabbang', 'gran dabbang', 'restorando', 'https://www.restorando.com.ar/gran-dabbang', '11 4832-1186', 'Scalabrini Ortiz 1543', 'Palermo', 'India/Fusión', '$$$'),
                ('Crizia', 'crizia', 'meitre', 'https://crizia.meitre.com', '11 4776-7777', 'Gorriti 5143', 'Palermo', 'Italiana', '$$$'),
            ]
            
            cur.executemany("""
                INSERT INTO restaurantes 
                (nombre, nombre_normalizado, sistema_reservas, url_reservas, telefono, direccion, barrio, tipo_cocina, precio_rango)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, restaurantes_iniciales)
            
            log.info(f"Insertados {len(restaurantes_iniciales)} restaurantes iniciales")
        
        conn.commit()
        cur.close()
        conn.close()
        log.info("Tabla restaurantes inicializada correctamente")
        return True
        
    except Exception as e:
        log.error(f"Error inicializando DB restaurantes: {e}")
        return False

def normalizar_nombre(nombre: str) -> str:
    """Normaliza el nombre para búsqueda."""
    import unicodedata
    nombre = nombre.lower().strip()
    nombre = unicodedata.normalize('NFD', nombre)
    nombre = ''.join(c for c in nombre if unicodedata.category(c) != 'Mn')
    return nombre

def buscar_restaurante(nombre: str) -> dict | None:
    """Busca un restaurante por nombre (fuzzy match)."""
    try:
        conn = get_connection()
        if not conn:
            return None
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        nombre_norm = normalizar_nombre(nombre)
        
        # Búsqueda exacta primero
        cur.execute("""
            SELECT * FROM restaurantes 
            WHERE nombre_normalizado = %s
        """, (nombre_norm,))
        
        result = cur.fetchone()
        
        if not result:
            # Búsqueda parcial
            cur.execute("""
                SELECT * FROM restaurantes 
                WHERE nombre_normalizado LIKE %s
                ORDER BY LENGTH(nombre_normalizado)
                LIMIT 1
            """, (f"%{nombre_norm}%",))
            result = cur.fetchone()
        
        cur.close()
        conn.close()
        
        return dict(result) if result else None
        
    except Exception as e:
        log.error(f"Error buscando restaurante: {e}")
        return None

def agregar_restaurante(nombre: str, sistema: str = None, url: str = None, 
                        telefono: str = None, direccion: str = None,
                        barrio: str = None, tipo_cocina: str = None,
                        precio: str = None) -> bool:
    """Agrega o actualiza un restaurante."""
    try:
        conn = get_connection()
        if not conn:
            return False
        
        cur = conn.cursor()
        nombre_norm = normalizar_nombre(nombre)
        
        cur.execute("""
            INSERT INTO restaurantes 
            (nombre, nombre_normalizado, sistema_reservas, url_reservas, 
             telefono, direccion, barrio, tipo_cocina, precio_rango)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (nombre_normalizado) DO UPDATE SET
                sistema_reservas = COALESCE(EXCLUDED.sistema_reservas, restaurantes.sistema_reservas),
                url_reservas = COALESCE(EXCLUDED.url_reservas, restaurantes.url_reservas),
                telefono = COALESCE(EXCLUDED.telefono, restaurantes.telefono),
                direccion = COALESCE(EXCLUDED.direccion, restaurantes.direccion),
                barrio = COALESCE(EXCLUDED.barrio, restaurantes.barrio),
                tipo_cocina = COALESCE(EXCLUDED.tipo_cocina, restaurantes.tipo_cocina),
                precio_rango = COALESCE(EXCLUDED.precio_rango, restaurantes.precio_rango),
                updated_at = NOW()
        """, (nombre, nombre_norm, sistema, url, telefono, direccion, barrio, tipo_cocina, precio))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
        
    except Exception as e:
        log.error(f"Error agregando restaurante: {e}")
        return False

def listar_restaurantes(barrio: str = None, tipo: str = None) -> list:
    """Lista restaurantes, opcionalmente filtrados."""
    try:
        conn = get_connection()
        if not conn:
            return []
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        query = "SELECT * FROM restaurantes WHERE 1=1"
        params = []
        
        if barrio:
            query += " AND LOWER(barrio) LIKE %s"
            params.append(f"%{barrio.lower()}%")
        
        if tipo:
            query += " AND LOWER(tipo_cocina) LIKE %s"
            params.append(f"%{tipo.lower()}%")
        
        query += " ORDER BY nombre"
        
        cur.execute(query, params)
        results = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return [dict(r) for r in results]
        
    except Exception as e:
        log.error(f"Error listando restaurantes: {e}")
        return []


# Inicializar al importar el módulo
if __name__ != "__main__":
    init_db()
