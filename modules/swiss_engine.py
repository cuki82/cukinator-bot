"""
swiss_engine.py — Motor astrológico autoritativo basado en pyswisseph.

Principios:
- Todo el cálculo se hace en UT (Universal Time)
- Coordenadas: Este=+, Oeste=-, Norte=+, Sur=-
- Sistema de casas Placidus por defecto ('P')
- Tropical por defecto (sin modo sidéreo salvo que se indique)
- houses_ex() como función principal de casas
- Asignación de casa por cúspides reales, nunca por división de 30°
"""

import swisseph as swe
import datetime
import pytz
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# ── Constantes ─────────────────────────────────────────────────────────────────
MOSH = swe.FLG_MOSEPH | swe.FLG_SPEED  # efemérides Moshier + velocidades reales

PLANETAS = [
    (swe.SUN,       "Sol"),
    (swe.MOON,      "Luna"),
    (swe.MERCURY,   "Mercurio"),
    (swe.VENUS,     "Venus"),
    (swe.MARS,      "Marte"),
    (swe.JUPITER,   "Jupiter"),
    (swe.SATURN,    "Saturno"),
    (swe.URANUS,    "Urano"),
    (swe.NEPTUNE,   "Neptuno"),
    (swe.PLUTO,     "Pluton"),
    (swe.MEAN_NODE, "Nodo N."),
]

SIGNOS = [
    "Aries", "Tauro", "Geminis", "Cancer", "Leo", "Virgo",
    "Libra", "Escorpio", "Sagitario", "Capricornio", "Acuario", "Piscis"
]

HOUSE_SYSTEMS = {
    'P': 'Placidus',
    'K': 'Koch',
    'W': 'Whole Sign',
    'E': 'Equal',
    'C': 'Campanus',
    'R': 'Regiomontanus',
}

# ── Helpers ─────────────────────────────────────────────────────────────────────
def lon_to_sign(lon: float) -> str:
    lon = lon % 360
    signo = SIGNOS[int(lon // 30)]
    g = lon % 30
    return f"{signo} {int(g):02d}\u00b0{int((g % 1) * 60):02d}'"


def normalize_lon(lon: float) -> float:
    return lon % 360


# ── 1. Conversión a Julian Day UT ───────────────────────────────────────────────
def to_julian_ut(fecha: str, hora: str, lugar: str) -> dict:
    """
    Convierte fecha/hora local a JD UT.

    Args:
        fecha: 'DD/MM/AAAA'
        hora:  'HH:MM'
        lugar: nombre de ciudad/país

    Returns dict con:
        jd_ut, dt_local, dt_ut, timezone, offset_hours, lat, lon, lugar_nombre
    """
    # Geocodificación
    geo = Nominatim(user_agent="swiss_engine_v1")
    loc = geo.geocode(lugar, language="es", timeout=10)
    if not loc:
        raise ValueError(f"No se pudo geocodificar: {lugar}")

    lat = loc.latitude
    lon = loc.longitude  # Este=+, Oeste=- (correcto por defecto en geopy)
    lugar_nombre = loc.address

    # Timezone desde coordenadas
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon) or "UTC"
    tz = pytz.timezone(tz_name)

    # Parsear y localizar
    dt_local = datetime.datetime.strptime(f"{fecha} {hora}", "%d/%m/%Y %H:%M")
    dt_local = tz.localize(dt_local)
    dt_ut = dt_local.astimezone(pytz.utc)

    offset_hours = dt_local.utcoffset().total_seconds() / 3600

    # Julian Day sobre UT
    jd_ut = swe.julday(
        dt_ut.year, dt_ut.month, dt_ut.day,
        dt_ut.hour + dt_ut.minute / 60.0 + dt_ut.second / 3600.0
    )

    return {
        "jd_ut":        jd_ut,
        "dt_local_str": dt_local.strftime("%Y-%m-%d %H:%M %Z"),
        "dt_ut_str":    dt_ut.strftime("%Y-%m-%d %H:%M UTC"),
        "timezone":     tz_name,
        "offset_hours": offset_hours,
        "lat":          lat,
        "lon":          lon,
        "lugar_nombre": lugar_nombre,
    }


# ── 2. Cálculo de planetas ──────────────────────────────────────────────────────
def calc_planets(jd_ut: float, flags: int = MOSH) -> dict:
    """
    Calcula posiciones planetarias.

    Returns dict: nombre -> {lon, lat, dist, speed, retrogrado, signo}
    """
    resultado = {}
    for pid, nombre in PLANETAS:
        try:
            pos, ret = swe.calc_ut(jd_ut, pid, flags)
            lon = normalize_lon(pos[0])
            resultado[nombre] = {
                "lon":        lon,
                "lat":        pos[1],
                "dist":       pos[2],
                "speed":      pos[3],
                "retrogrado": pos[3] < 0,
                "signo":      lon_to_sign(lon),
            }
        except Exception as e:
            resultado[nombre] = {"error": str(e)}
    return resultado


# ── 3. Cálculo de casas ─────────────────────────────────────────────────────────
def calc_houses(
    jd_ut: float,
    lat: float,
    lon: float,
    house_system: str = 'P',
    sidereal: bool = False,
    sid_mode: int = swe.SIDM_LAHIRI
) -> dict:
    """
    Calcula cúspides de casas usando houses_ex().

    Args:
        jd_ut:        Julian Day en UT
        lat:          latitud (Norte=+, Sur=-)
        lon:          longitud (Este=+, Oeste=-)
        house_system: 'P'=Placidus, 'K'=Koch, 'W'=Whole Sign, etc.
        sidereal:     True para zodíaco sidéreo
        sid_mode:     modo sidéreo (swe.SIDM_LAHIRI por defecto)

    Returns dict con cusps (1-12), asc, mc, armc, vertex, equasc
    """
    flags = 0
    if sidereal:
        swe.set_sid_mode(sid_mode)
        flags |= swe.FLG_SIDEREAL

    hsys = house_system.encode('ascii')

    cusps, ascmc = swe.houses_ex(jd_ut, lat, lon, hsys, flags)

    # cusps[0] es la cúspide de casa 1 (ASC), cusps[1] casa 2, etc.
    # ascmc[0]=ASC, ascmc[1]=MC, ascmc[2]=ARMC, ascmc[3]=Vertex, ascmc[4]=EquaASC

    resultado = {
        "cusps": [normalize_lon(c) for c in cusps],   # índices 0-11 = casas 1-12
        "asc":   normalize_lon(ascmc[0]),
        "mc":    normalize_lon(ascmc[1]),
        "ic":    normalize_lon((ascmc[1] + 180) % 360),
        "dc":    normalize_lon((ascmc[0] + 180) % 360),
        "armc":  ascmc[2],
        "vertex": normalize_lon(ascmc[3]),
        "house_system": HOUSE_SYSTEMS.get(house_system, house_system),
        "sidereal": sidereal,
        "cusps_str": [lon_to_sign(c) for c in cusps],
    }
    return resultado


# ── 4. Asignación planeta → casa ─────────────────────────────────────────────────
def assign_planet_house(planet_lon: float, cusps: list) -> int:
    """
    Determina en qué casa cae planet_lon usando las cúspides reales.

    Args:
        planet_lon: longitud eclíptica del planeta (0-360)
        cusps:      lista de 12 longitudes de cúspides (casas 1-12), normalizadas 0-360

    Returns: número de casa (1-12)
    """
    lon = normalize_lon(planet_lon)

    for i in range(12):
        inicio = cusps[i]
        fin = cusps[(i + 1) % 12]

        if inicio <= fin:
            # Segmento normal (no cruza 0°)
            if inicio <= lon < fin:
                return i + 1
        else:
            # Segmento que cruza 0° (wraparound)
            if lon >= inicio or lon < fin:
                return i + 1

    # Fallback: buscar la casa cuya cúspide está más cerca por debajo
    min_diff = 360
    casa = 1
    for i in range(12):
        diff = (lon - cusps[i]) % 360
        if diff < min_diff:
            min_diff = diff
            casa = i + 1
    return casa


# ── 5. Carta completa ───────────────────────────────────────────────────────────
def calc_carta_completa(
    fecha: str,
    hora: str,
    lugar: str,
    house_system: str = 'P',
    sidereal: bool = False,
    sid_mode: int = swe.SIDM_LAHIRI,
    flags: int = MOSH
) -> dict:
    """
    Calcula carta natal completa de forma consistente.
    Usa el mismo JD UT para planetas y casas.
    """
    # 1. Conversión temporal y geográfica
    base = to_julian_ut(fecha, hora, lugar)
    jd_ut = base["jd_ut"]
    lat   = base["lat"]
    lon   = base["lon"]

    # 2. Planetas
    planetas = calc_planets(jd_ut, flags)

    # 3. Casas (mismo jd_ut, mismas coords)
    casas = calc_houses(jd_ut, lat, lon, house_system, sidereal, sid_mode)

    # 4. Asignar casa a cada planeta
    cusps = casas["cusps"]
    for nombre, data in planetas.items():
        if "error" not in data:
            data["casa"] = assign_planet_house(data["lon"], cusps)

    # 5. Aspectos
    aspectos = calc_aspectos(planetas)

    return {
        # Metadata de auditoría
        "debug": {
            "fecha_original":   f"{fecha} {hora}",
            "lugar_original":   lugar,
            "lugar_geocodificado": base["lugar_nombre"],
            "timezone":         base["timezone"],
            "offset_horas":     base["offset_hours"],
            "hora_local":       base["dt_local_str"],
            "hora_ut":          base["dt_ut_str"],
            "jd_ut":            round(jd_ut, 6),
            "lat":              round(lat, 6),
            "lon":              round(lon, 6),
            "sistema_casas":    casas["house_system"],
            "zodiaco":          "Sidereo" if sidereal else "Tropical",
            "flags_swe":        flags,
        },
        "planetas": planetas,
        "casas": {
            "cuspides": [
                {"numero": i + 1, "lon": casas["cusps"][i], "signo": casas["cusps_str"][i]}
                for i in range(12)
            ],
            "asc":    {"lon": casas["asc"],    "signo": lon_to_sign(casas["asc"])},
            "mc":     {"lon": casas["mc"],     "signo": lon_to_sign(casas["mc"])},
            "ic":     {"lon": casas["ic"],     "signo": lon_to_sign(casas["ic"])},
            "dc":     {"lon": casas["dc"],     "signo": lon_to_sign(casas["dc"])},
            "vertex": {"lon": casas["vertex"], "signo": lon_to_sign(casas["vertex"])},
        },
        "aspectos": aspectos,
    }


# ── 6. Aspectos ─────────────────────────────────────────────────────────────────
ASPECTOS_DEF = [
    (0,   "Conjuncion",  "☌", 8.0),
    (60,  "Sextil",      "⚹", 5.0),
    (90,  "Cuadratura",  "□", 7.0),
    (120, "Trigono",     "△", 8.0),
    (180, "Oposicion",   "☍", 8.0),
]

def calc_aspectos(planetas: dict, orb_default: float = 5.0) -> list:
    nombres = [n for n, d in planetas.items() if "error" not in d]
    resultado = []
    for i in range(len(nombres)):
        for j in range(i + 1, len(nombres)):
            n1, n2 = nombres[i], nombres[j]
            lon1 = planetas[n1]["lon"]
            lon2 = planetas[n2]["lon"]
            diff = abs(lon1 - lon2) % 360
            diff = min(diff, 360 - diff)
            for angulo, nombre_asp, simbolo, orb in ASPECTOS_DEF:
                orb_real = abs(diff - angulo)
                if orb_real <= orb:
                    resultado.append({
                        "planeta1": n1,
                        "planeta2": n2,
                        "aspecto":  nombre_asp,
                        "simbolo":  simbolo,
                        "angulo":   angulo,
                        "orb":      round(orb_real, 2),
                        "aplicante": planetas[n1]["speed"] > 0,
                    })
    return resultado


# ── 7. Formateo de ficha técnica ─────────────────────────────────────────────────
def formatear_ficha(carta: dict, incluir_debug: bool = False) -> str:
    lines = []
    d = carta["debug"]

    lines.append("╔══════════════════════════════════════════════╗")
    lines.append("║        CARTA NATAL — FICHA TECNICA           ║")
    lines.append("╚══════════════════════════════════════════════╝")
    lines.append(f"  Fecha    : {d['fecha_original']}")
    lines.append(f"  Lugar    : {d['lugar_geocodificado'][:60]}")
    lines.append(f"  TZ       : {d['timezone']}  (UTC{d['offset_horas']:+.1f})")
    lines.append(f"  Hora UT  : {d['hora_ut']}")
    lines.append(f"  JD UT    : {d['jd_ut']}")
    lines.append(f"  Coords   : {d['lat']:.4f}N  {d['lon']:.4f}E")
    lines.append(f"  Casas    : {d['sistema_casas']}  |  Zodiaco: {d['zodiaco']}")
    lines.append("")

    # Planetas
    lines.append("── PLANETAS ──────────────────────────────────────")
    lines.append(f"  {'Planeta':<12} {'Posicion':<22} {'Casa':>5}  R")
    lines.append(f"  {'─'*12} {'─'*22} {'─'*5}  ─")
    for nombre, data in carta["planetas"].items():
        if "error" in data:
            lines.append(f"  {nombre:<12} [error: {data['error'][:30]}]")
            continue
        r = "R" if data["retrogrado"] else " "
        casa = f"C{data.get('casa', '?')}"
        lines.append(f"  {nombre:<12} {data['signo']:<22} {casa:>5}  {r}")
    lines.append("")

    # Ángulos
    c = carta["casas"]
    lines.append("── ANGULOS ───────────────────────────────────────")
    lines.append(f"  ASC    : {c['asc']['signo']}")
    lines.append(f"  MC     : {c['mc']['signo']}")
    lines.append(f"  DSC    : {c['dc']['signo']}")
    lines.append(f"  IC     : {c['ic']['signo']}")
    lines.append(f"  Vertex : {c['vertex']['signo']}")
    lines.append("")

    # Cúspides
    lines.append("── CUSPIDES (Placidus) ───────────────────────────")
    for cusp in c["cuspides"]:
        lines.append(f"  Casa {cusp['numero']:2d} : {cusp['signo']}")
    lines.append("")

    # Aspectos
    lines.append("── ASPECTOS (orbe variable) ──────────────────────")
    if carta["aspectos"]:
        lines.append(f"  {'Planeta 1':<12} {'Asp':^12} {'Planeta 2':<12} {'Orbe'}")
        lines.append(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*5}")
        for a in carta["aspectos"]:
            asp = f"{a['simbolo']} {a['aspecto']}"
            lines.append(f"  {a['planeta1']:<12} {asp:<12} {a['planeta2']:<12} {a['orb']}°")
    else:
        lines.append("  (Sin aspectos en el orbe configurado)")

    # Debug opcional
    if incluir_debug:
        lines.append("")
        lines.append("── DEBUG COMPLETO ────────────────────────────────")
        import json
        lines.append(json.dumps(d, indent=2, ensure_ascii=False))

    return "\n".join(lines)


# ── 8. Verificación cruzada ──────────────────────────────────────────────────────
def verificar_carta(fecha: str, hora: str, lugar: str) -> str:
    """
    Calcula la carta y muestra un reporte de auditoría completo
    para verificación cruzada.
    """
    carta = calc_carta_completa(fecha, hora, lugar)
    d = carta["debug"]

    lines = ["=== REPORTE DE AUDITORIA ==="]
    lines.append(f"Input original  : {fecha} {hora} en {lugar}")
    lines.append(f"Geocodificado   : {d['lugar_geocodificado']}")
    lines.append(f"Coords          : lat={d['lat']} lon={d['lon']}")
    lines.append(f"Timezone        : {d['timezone']} (UTC{d['offset_horas']:+.1f})")
    lines.append(f"Hora local      : {d['hora_local']}")
    lines.append(f"Hora UT         : {d['hora_ut']}")
    lines.append(f"JD UT           : {d['jd_ut']}")
    lines.append(f"Sistema casas   : {d['sistema_casas']}")
    lines.append(f"Zodiaco         : {d['zodiaco']}")
    lines.append(f"Flags SWE       : {d['flags_swe']}")
    lines.append("")
    lines.append("CUSPS REALES USADAS PARA ASIGNACION DE CASAS:")
    for cusp in carta["casas"]["cuspides"]:
        lines.append(f"  Casa {cusp['numero']:2d}: {cusp['signo']} ({cusp['lon']:.4f}°)")
    lines.append(f"  ASC   : {carta['casas']['asc']['signo']} ({carta['casas']['asc']['lon']:.4f}°)")
    lines.append(f"  MC    : {carta['casas']['mc']['signo']} ({carta['casas']['mc']['lon']:.4f}°)")
    lines.append("")
    lines.append("PLANETAS Y CASAS ASIGNADAS:")
    for nombre, data in carta["planetas"].items():
        if "error" in data:
            lines.append(f"  {nombre:<12}: ERROR")
            continue
        r = "(R)" if data["retrogrado"] else "   "
        lines.append(f"  {nombre:<12}: {data['signo']:<22} {r} -> Casa {data.get('casa','?')}")

    return "\n".join(lines)


# ── Test rápido ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(verificar_carta("11/07/1982", "23:30", "Capital Federal, Argentina"))


# ── Dignidades ──────────────────────────────────────────────────────────────────
DIGNIDADES = {
    "Sol":      {"domicilio": ["Leo"], "exaltacion": ["Aries"], "exilio": ["Acuario"], "caida": ["Libra"]},
    "Luna":     {"domicilio": ["Cancer"], "exaltacion": ["Tauro"], "exilio": ["Capricornio"], "caida": ["Escorpio"]},
    "Mercurio": {"domicilio": ["Geminis","Virgo"], "exaltacion": ["Virgo"], "exilio": ["Sagitario","Piscis"], "caida": ["Piscis"]},
    "Venus":    {"domicilio": ["Tauro","Libra"], "exaltacion": ["Piscis"], "exilio": ["Aries","Escorpio"], "caida": ["Virgo"]},
    "Marte":    {"domicilio": ["Aries","Escorpio"], "exaltacion": ["Capricornio"], "exilio": ["Libra","Tauro"], "caida": ["Cancer"]},
    "Jupiter":  {"domicilio": ["Sagitario","Piscis"], "exaltacion": ["Cancer"], "exilio": ["Geminis","Virgo"], "caida": ["Capricornio"]},
    "Saturno":  {"domicilio": ["Capricornio","Acuario"], "exaltacion": ["Libra"], "exilio": ["Cancer","Leo"], "caida": ["Aries"]},
    "Urano":    {"domicilio": ["Acuario"], "exaltacion": ["Escorpio"], "exilio": ["Leo"], "caida": ["Tauro"]},
    "Neptuno":  {"domicilio": ["Piscis"], "exaltacion": ["Cancer"], "exilio": ["Virgo"], "caida": ["Capricornio"]},
    "Pluton":   {"domicilio": ["Escorpio"], "exaltacion": ["Leo"], "exilio": ["Tauro"], "caida": ["Acuario"]},
    "Nodo N.":  {"domicilio": [], "exaltacion": [], "exilio": [], "caida": []},
}

REGENTES_SIGNO = {
    "Aries": "Marte", "Tauro": "Venus", "Geminis": "Mercurio",
    "Cancer": "Luna", "Leo": "Sol", "Virgo": "Mercurio",
    "Libra": "Venus", "Escorpio": "Pluton", "Sagitario": "Jupiter",
    "Capricornio": "Saturno", "Acuario": "Urano", "Piscis": "Neptuno",
}

def get_signo_base(signo_completo: str) -> str:
    """Extrae solo el nombre del signo de un string como 'Cancer 19°25'"""
    for s in SIGNOS:
        if signo_completo.startswith(s):
            return s
    return signo_completo.split()[0]

def calc_dignidad(nombre: str, signo_completo: str) -> str:
    signo = get_signo_base(signo_completo)
    if nombre not in DIGNIDADES:
        return "Peregrino"
    d = DIGNIDADES[nombre]
    if signo in d["domicilio"]:    return "Domicilio"
    if signo in d["exaltacion"]:   return "Exaltacion"
    if signo in d["exilio"]:       return "Exilio"
    if signo in d["caida"]:        return "Caida"
    return "Peregrino"

def calc_estado_dinamico(speed: float, nombre: str) -> str:
    umbral_lento = 0.3
    umbral_estacionario = 0.03
    if nombre in ["Sol", "Luna"]:
        umbral_lento = 0.8
        umbral_estacionario = 0.1
    abs_speed = abs(speed)
    if abs_speed <= umbral_estacionario: return "Estacionario"
    if abs_speed <= umbral_lento:        return "Lento"
    return "Rapido"

def calc_regentes(planetas: dict, casas: dict) -> dict:
    resultado = {}
    for nombre, data in planetas.items():
        if "error" in data: continue
        signo = get_signo_base(data["signo"])
        regente = REGENTES_SIGNO.get(signo, "?")
        datos_regente = planetas.get(regente, {})
        resultado[nombre] = {
            "regente": regente,
            "regente_signo": get_signo_base(datos_regente.get("signo", "?")) if "error" not in datos_regente else "?",
            "regente_casa": datos_regente.get("casa", "?") if "error" not in datos_regente else "?",
        }
    return resultado

def calc_intercepciones(cusps: list) -> dict:
    """
    Detecta casas y signos interceptados.
    - Signo interceptado: no aparece en ninguna cúspide
    - Casa interceptada: una casa cuya cúspide y la siguiente están en el mismo signo
    """
    signos_en_cuspides = set()
    for lon in cusps:
        signo = SIGNOS[int(lon // 30)]
        signos_en_cuspides.add(signo)

    signos_interceptados = [s for s in SIGNOS if s not in signos_en_cuspides]

    casas_interceptadas = []
    for i in range(12):
        s1 = SIGNOS[int(cusps[i] // 30)]
        s2 = SIGNOS[int(cusps[(i+1) % 12] // 30)]
        if s1 == s2:
            casas_interceptadas.append({
                "casa": i + 1,
                "signo_contenedor": s1,
                "eje": f"Casa {i+1} - Casa {((i+6) % 12) + 1}"
            })

    return {
        "signos_interceptados": signos_interceptados,
        "casas_interceptadas": casas_interceptadas,
    }

def calc_jerarquias(planetas: dict, aspectos: list) -> dict:
    conteo_aspectos = {n: 0 for n in planetas if "error" not in planetas[n]}
    for a in aspectos:
        conteo_aspectos[a["planeta1"]] = conteo_aspectos.get(a["planeta1"], 0) + 1
        conteo_aspectos[a["planeta2"]] = conteo_aspectos.get(a["planeta2"], 0) + 1

    mas_aspectados = sorted(conteo_aspectos.items(), key=lambda x: -x[1])

    elementos = {"Fuego": 0, "Tierra": 0, "Aire": 0, "Agua": 0}
    elem_map = {
        "Aries":"Fuego","Leo":"Fuego","Sagitario":"Fuego",
        "Tauro":"Tierra","Virgo":"Tierra","Capricornio":"Tierra",
        "Geminis":"Aire","Libra":"Aire","Acuario":"Aire",
        "Cancer":"Agua","Escorpio":"Agua","Piscis":"Agua",
    }
    polaridad = {"Yang": 0, "Yin": 0}
    yang_signos = ["Aries","Geminis","Leo","Libra","Sagitario","Acuario"]

    casas_count = {}
    stellium = []

    for nombre, data in planetas.items():
        if "error" in data: continue
        signo = get_signo_base(data["signo"])
        elem = elem_map.get(signo, "?")
        if elem in elementos: elementos[elem] += 1
        if signo in yang_signos: polaridad["Yang"] += 1
        else: polaridad["Yin"] += 1
        casa = data.get("casa", 0)
        casas_count[casa] = casas_count.get(casa, 0) + 1

    for casa, count in casas_count.items():
        if count >= 3:
            stellium.append(f"Casa {casa} ({count} planetas)")

    casas_dominantes = sorted(casas_count.items(), key=lambda x: -x[1])[:3]

    return {
        "mas_aspectados": mas_aspectados[:5],
        "elementos": elementos,
        "polaridad": polaridad,
        "stellium": stellium,
        "casas_dominantes": casas_dominantes,
        "conteo_aspectos": conteo_aspectos,
    }


def calc_carta_completa_v2(fecha, hora, lugar, house_system='P', sidereal=False, sid_mode=None):
    """Versión extendida con dignidades, intercepciones, jerarquías."""
    import swisseph as _swe
    carta = calc_carta_completa(fecha, hora, lugar, house_system, sidereal, sid_mode or _swe.SIDM_LAHIRI)

    planetas = carta["planetas"]
    casas = carta["casas"]
    aspectos = carta["aspectos"]
    cusps = [c["lon"] for c in casas["cuspides"]]

    # Agregar dignidades y estado dinámico
    for nombre, data in planetas.items():
        if "error" in data: continue
        data["dignidad"] = calc_dignidad(nombre, data["signo"])
        data["estado_dinamico"] = calc_estado_dinamico(data["speed"], nombre)

    carta["regentes"] = calc_regentes(planetas, casas)
    carta["intercepciones"] = calc_intercepciones(cusps)
    carta["jerarquias"] = calc_jerarquias(planetas, aspectos)

    return carta


# ── Formato Ficha Técnica Completa ───────────────────────────────────────────────
def formatear_ficha_tecnica(carta: dict) -> str:
    """Formato técnico completo según especificación."""
    d       = carta["debug"]
    pl      = carta["planetas"]
    casas   = carta["casas"]
    asp     = carta["aspectos"]
    cusps   = [c["lon"] for c in casas["cuspides"]]
    inter   = calc_intercepciones(cusps)
    jerarq  = calc_jerarquias(pl, asp)
    regentes = calc_regentes(pl, casas)

    lines = []

    # ── 0) BASE LIMPIA ──────────────────────────────────────────────────────────
    lines.append("## 0) BASE LIMPIA DE LA CARTA")
    lines.append("")
    lines.append("### Cuspides")
    lines.append(f"* ASC         : {casas['asc']['signo']}")
    for c in casas["cuspides"][1:]:
        label = f"MC (Casa 10)" if c["numero"] == 10 else f"Casa {c['numero']}"
        lines.append(f"* {label:<13}: {c['signo']}")
    lines.append("")

    lines.append("### Planetas")
    for nombre, data in pl.items():
        if "error" in data:
            lines.append(f"* {nombre}: ERROR - {data['error'][:50]}")
            continue
        signo_full = data["signo"]
        casa = data.get("casa", "?")
        r = "Retrogrado" if data["retrogrado"] else "Directo"
        speed = data["speed"]
        estado = data.get("estado_dinamico") or calc_estado_dinamico(speed, nombre)
        lines.append(f"* {nombre}: {signo_full} – Casa {casa} – {r} – Velocidad: {speed:.4f}°/dia – Estado: {estado}")
    lines.append("")

    # ── 1) ASPECTOS ─────────────────────────────────────────────────────────────
    lines.append("## 1) ASPECTOS MAYORES")
    lines.append("")
    aspectos_std = [a for a in asp if a["aspecto"] in ("Conjuncion","Oposicion","Cuadratura","Trigono","Sextil")]
    if aspectos_std:
        for a in sorted(aspectos_std, key=lambda x: x["orb"]):
            orb_g = int(a["orb"])
            orb_m = int((a["orb"] % 1) * 60)
            aplic = "aplicativo" if a.get("aplicante") else "separativo"
            lines.append(f"* {a['planeta1']} {a['simbolo']} {a['aspecto']} {a['planeta2']} (orbe {orb_g}°{orb_m:02d}', {aplic})")
    else:
        lines.append("* (ninguno dentro del orbe configurado)")
    lines.append("")
    lines.append("### Aspectos plenivalentes")
    elem_map = {
        "Aries":"Fuego","Leo":"Fuego","Sagitario":"Fuego",
        "Tauro":"Tierra","Virgo":"Tierra","Capricornio":"Tierra",
        "Geminis":"Aire","Libra":"Aire","Acuario":"Aire",
        "Cancer":"Agua","Escorpio":"Agua","Piscis":"Agua",
    }
    plenivalentes = []
    nombres_pl = [n for n, d in pl.items() if "error" not in d]
    for i in range(len(nombres_pl)):
        for j in range(i+1, len(nombres_pl)):
            n1, n2 = nombres_pl[i], nombres_pl[j]
            s1 = get_signo_base(pl[n1]["signo"])
            s2 = get_signo_base(pl[n2]["signo"])
            if elem_map.get(s1) == elem_map.get(s2) and s1 != s2:
                lon1, lon2 = pl[n1]["lon"], pl[n2]["lon"]
                diff = abs(lon1 - lon2) % 360
                diff = min(diff, 360 - diff)
                for angulo, nombre_asp, simbolo, orb in ASPECTOS_DEF:
                    orb_real = abs(diff - angulo)
                    if orb_real <= orb + 3:
                        plenivalentes.append(f"* {n1} {simbolo} {nombre_asp} {n2} (mismo elemento: {elem_map[s1]}, orbe {orb_real:.2f}°)")
    lines.extend(plenivalentes if plenivalentes else ["* (ninguno detectado)"])
    lines.append("")

    # ── 2) REGENTES Y CADENA DISPOSITORA ────────────────────────────────────────
    lines.append("## 2) REGENTES Y CADENA DISPOSITORA")
    lines.append("")
    for nombre, data in regentes.items():
        lines.append(f"* {nombre} en {get_signo_base(pl[nombre]['signo'])} -> regente: {data['regente']} en {data['regente_signo']} (Casa {data['regente_casa']})")
    lines.append("")

    # ── 3) DIGNIDADES Y DEBILIDADES ──────────────────────────────────────────────
    lines.append("## 3) DIGNIDADES Y DEBILIDADES")
    lines.append("")
    for nombre, data in pl.items():
        if "error" in data: continue
        signo = get_signo_base(data["signo"])
        dig = data.get("dignidad") or calc_dignidad(nombre, data["signo"])
        lines.append(f"* {nombre:<12}: {signo:<14} -> {dig}")
    lines.append("")

    # ── 4) JERARQUIAS Y CENTROS GRAVITACIONALES ──────────────────────────────────
    lines.append("## 4) JERARQUIAS Y CENTROS GRAVITACIONALES")
    lines.append("")
    lines.append("Planetas mas aspectados:")
    for nombre, count in jerarq["mas_aspectados"]:
        lines.append(f"  {nombre}: {count} aspectos")
    lines.append(f"Elementos: {jerarq['elementos']}")
    lines.append(f"Polaridad: Yin={jerarq['polaridad']['Yin']} Yang={jerarq['polaridad']['Yang']}")
    if jerarq["stellium"]:
        lines.append(f"Stellium: {', '.join(jerarq['stellium'])}")
    lines.append(f"Casas dominantes: {jerarq['casas_dominantes']}")
    lines.append("")

    # ── 5) REDES DE ASPECTOS ─────────────────────────────────────────────────────
    lines.append("## 5) REDES DE ASPECTOS")
    lines.append("")
    redes = {}
    for a in asp:
        for p in [a["planeta1"], a["planeta2"]]:
            if p not in redes: redes[p] = []
        otro1 = a["planeta2"] if a["planeta1"] else a["planeta1"]
        otro2 = a["planeta1"] if a["planeta2"] else a["planeta2"]
        redes[a["planeta1"]].append(f"{a['simbolo']}{a['aspecto']} {a['planeta2']} ({a['orb']}°)")
        redes[a["planeta2"]].append(f"{a['simbolo']}{a['aspecto']} {a['planeta1']} ({a['orb']}°)")
    for nombre in pl:
        if "error" in pl[nombre]: continue
        conexiones = redes.get(nombre, [])
        lines.append(f"* {nombre}: {', '.join(conexiones) if conexiones else 'sin aspectos'}")
    lines.append("")

    # ── 6) VECTORES ENERGETICOS ──────────────────────────────────────────────────
    lines.append("## 6) VECTORES ENERGETICOS")
    lines.append("")
    emisores = [n for n, c in jerarq["conteo_aspectos"].items() if c >= 3]
    receptores = [n for n, c in jerarq["conteo_aspectos"].items() if c <= 1]
    lines.append(f"Emisores (>=3 aspectos): {', '.join(emisores) if emisores else 'ninguno'}")
    lines.append(f"Receptores (<=1 aspecto): {', '.join(receptores) if receptores else 'ninguno'}")
    lines.append("")

    # ── 7) INTERCEPCIONES ────────────────────────────────────────────────────────
    lines.append("## 7) INTERCEPCIONES")
    lines.append("")
    lines.append("### A) Casas interceptadas")
    if inter["casas_interceptadas"]:
        for ci in inter["casas_interceptadas"]:
            lines.append(f"* Casa {ci['casa']} interceptada en {ci['signo_contenedor']} (eje: {ci['eje']})")
    else:
        lines.append("* Ninguna")
    lines.append("")
    lines.append("### B) Signos interceptados")
    if inter["signos_interceptados"]:
        for si in inter["signos_interceptados"]:
            lines.append(f"* {si} (no aparece en ninguna cuspide)")
    else:
        lines.append("* Ninguno")
    lines.append("")

    # ── 8) VALIDACION TECNICA ────────────────────────────────────────────────────
    lines.append("## 8) VALIDACION TECNICA")
    lines.append("")
    lines.append(f"* Fecha/hora original : {d['fecha_original']}")
    lines.append(f"* Timezone            : {d['timezone']} (UTC{d['offset_horas']:+.1f})")
    lines.append(f"* Hora UT             : {d['hora_ut']}")
    lines.append(f"* Julian Day UT       : {d['jd_ut']}")
    lines.append(f"* Latitud             : {d['lat']}")
    lines.append(f"* Longitud            : {d['lon']}")
    lines.append(f"* Sistema de casas    : {d['sistema_casas']}")
    lines.append(f"* Zodiaco             : {d['zodiaco']}")
    lines.append(f"* Flags SWE           : {d['flags_swe']} (SEFLG_MOSEPH)")

    return "\n".join(lines)


# ── 10. Tránsitos ────────────────────────────────────────────────────────────────
# Tránsitos = aspectos entre planetas en movimiento HOY (o fecha dada) contra los
# planetas natales fijos de la carta. Orbes mucho más chicos que natal (1-2°) —
# un tránsito "activo" es exacto; uno con orb 5° es débil o ya se fue.

TRANSIT_ASPECTS = [
    (0,   "Conjuncion",  "☌", 2.0),
    (60,  "Sextil",      "⚹", 1.5),
    (90,  "Cuadratura",  "□", 2.0),
    (120, "Trigono",     "△", 2.0),
    (180, "Oposicion",   "☍", 2.0),
]

# Planetas lentos tienen tránsitos más significativos (duran meses/años).
TRANSIT_WEIGHT = {
    "Sol": 1, "Luna": 1, "Mercurio": 1, "Venus": 1, "Marte": 2,
    "Jupiter": 3, "Saturno": 4, "Urano": 5, "Neptuno": 5, "Pluton": 5,
    "Nodo Norte": 3, "Quiron": 2,
}


def _julian_ahora_ut() -> float:
    """JD UT del momento actual."""
    now = datetime.datetime.utcnow()
    return swe.julday(now.year, now.month, now.day,
                      now.hour + now.minute / 60.0 + now.second / 3600.0)


def _jd_from_fecha(fecha: str = None, hora: str = None) -> float:
    """JD UT desde 'DD/MM/AAAA HH:MM' (asume UTC) o ahora si falta."""
    if not fecha:
        return _julian_ahora_ut()
    hora = hora or "12:00"
    dt = datetime.datetime.strptime(f"{fecha} {hora}", "%d/%m/%Y %H:%M")
    return swe.julday(dt.year, dt.month, dt.day,
                      dt.hour + dt.minute / 60.0)


def calc_transitos(carta_natal: dict, fecha: str = None, hora: str = None,
                   orb_multiplier: float = 1.0, solo_mayores: bool = True) -> dict:
    """
    Calcula los tránsitos (planetas actuales formando aspectos con natal).

    Args:
        carta_natal: output de calc_carta_completa() — necesita carta['planetas']
        fecha: 'DD/MM/AAAA' — si None, usa AHORA (UTC)
        hora:  'HH:MM' — UTC; default '12:00' o hora actual
        orb_multiplier: 1.0 default. Subir a 2.0 para ver aspectos "próximos" o
                        bajar a 0.5 para solo tránsitos exactos.
        solo_mayores: si True (default), solo aspectos mayores (cnj, sxt, cuad, trig, opp).

    Returns:
        {
            "jd_transito": float,
            "fecha_transito": str,
            "planetas_transito": {nombre: {lon, signo, speed, retrogrado}, ...},
            "aspectos": [
                {planeta_transito, planeta_natal, aspecto, simbolo, angulo,
                 orb, aplicante, peso, significancia},
                ...
            ] ordenados por significancia (peso del planeta × exactitud del orb)
        }

    Uso típico:
        natal = calc_carta_completa("23/09/1985", "14:30", "Buenos Aires")
        trans = calc_transitos(natal)  # tránsitos de ahora
        for a in trans["aspectos"][:5]:
            print(f"{a['planeta_transito']} {a['simbolo']} {a['planeta_natal']} natal (orb {a['orb']}°)")
    """
    jd_trans = _jd_from_fecha(fecha, hora)
    planetas_trans = calc_planets(jd_trans)

    natal_planetas = carta_natal.get("planetas", {})
    aspectos_def = TRANSIT_ASPECTS if solo_mayores else ASPECTOS_DEF

    aspectos = []
    for nt, dt in planetas_trans.items():
        if "error" in dt:
            continue
        for nn, dn in natal_planetas.items():
            if "error" in dn:
                continue
            diff = abs(dt["lon"] - dn["lon"]) % 360
            diff = min(diff, 360 - diff)
            for angulo, nombre_asp, simbolo, orb_base in aspectos_def:
                orb_max = orb_base * orb_multiplier
                orb_real = abs(diff - angulo)
                if orb_real <= orb_max:
                    peso = TRANSIT_WEIGHT.get(nt, 1)
                    # Significancia: planetas lentos + aspectos exactos primero
                    significancia = peso * (1 - orb_real / max(orb_max, 0.01))
                    aspectos.append({
                        "planeta_transito": nt,
                        "planeta_natal":    nn,
                        "aspecto":          nombre_asp,
                        "simbolo":          simbolo,
                        "angulo":           angulo,
                        "orb":              round(orb_real, 2),
                        "aplicante":        dt["speed"] > 0,
                        "peso":             peso,
                        "significancia":    round(significancia, 2),
                    })

    aspectos.sort(key=lambda a: -a["significancia"])

    return {
        "jd_transito":        jd_trans,
        "fecha_transito":     fecha or datetime.datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC"),
        "planetas_transito":  planetas_trans,
        "aspectos":           aspectos,
    }


def formatear_transitos(transitos: dict, carta_natal: dict = None, top_n: int = 15,
                        etiqueta_natal: str = "natal") -> str:
    """Render de tránsitos. etiqueta_natal distingue si se aplicaron sobre natal, solar, lunar."""
    lines = [f"🪐 *Tránsitos al {transitos['fecha_transito']}* (sobre {etiqueta_natal})\n"]
    aspectos = transitos.get("aspectos", [])
    if not aspectos:
        lines.append("_(sin aspectos dentro del orb actual)_")
        return "\n".join(lines)

    for a in aspectos[:top_n]:
        apl = "→ aplicante" if a["aplicante"] else "← separándose"
        lines.append(
            f"• *{a['planeta_transito']}* {a['simbolo']} {a['planeta_natal']} {etiqueta_natal} · "
            f"orb {a['orb']}° · {apl}"
        )

    if len(aspectos) > top_n:
        lines.append(f"\n_… y {len(aspectos) - top_n} aspectos más de orb mayor._")
    return "\n".join(lines)


# ── 11. Retornos Solares y Lunares ───────────────────────────────────────────────
# Retorno Solar (SR): carta calculada al momento exacto en que el Sol regresa a su
# posición natal. Pasa una vez al año (cerca del cumpleaños). Establece el "tema"
# del año — casas, ascendente del SR, aspectos a natal.
# Retorno Lunar (LR): igual pero con la Luna, cada ~27.3 días. Tema del mes.
# Ambos dependen del LUGAR donde estuvo la persona ese día (para las casas).

def _geocode(lugar: str) -> tuple:
    """(lat, lon, nombre) — geocodificación. Reusa el setup de to_julian_ut."""
    geo = Nominatim(user_agent="swiss_engine_returns")
    loc = geo.geocode(lugar, language="es", timeout=10)
    if not loc:
        raise ValueError(f"No se pudo geocodificar: {lugar}")
    return (loc.latitude, loc.longitude, loc.address)


def calc_retorno_solar(carta_natal: dict, anio: int = None,
                       lugar_retorno: str = None) -> dict:
    """
    Encuentra el JD exacto del retorno solar del año dado.

    Args:
        carta_natal: output de calc_carta_completa(). Necesita planetas[Sol].lon.
        anio: año del retorno (default año actual UTC).
        lugar_retorno: ciudad/país donde la persona estuvo el día del retorno.
                       Si None, usa el lugar natal (clásico, pero menos predictivo).

    Returns: dict con la carta del retorno solar (mismo shape que calc_carta_completa).
    """
    sol_natal_lon = carta_natal["planetas"]["Sol"]["lon"]
    anio = anio or datetime.datetime.utcnow().year

    # Búsqueda: desde inicio del año buscamos el próximo cruce del Sol por la lon natal
    jd_start = swe.julday(anio, 1, 1, 0.0)
    try:
        _r = swe.solcross_ut(sol_natal_lon, jd_start)
        # La versión actual de pyswisseph retorna solo el JD (float);
        # versiones anteriores retornaban tuple (flag, jd). Manejamos ambos.
        tret = _r if isinstance(_r, (int, float)) else _r[1]
    except AttributeError:
        # fallback: iterar manualmente si la versión de pyswisseph es vieja
        tret = _find_return_iterative(sol_natal_lon, jd_start, swe.SUN)

    # Lugar de retorno
    if lugar_retorno:
        lat, lon, lugar_nombre = _geocode(lugar_retorno)
    else:
        lat = carta_natal["debug"]["lat"]
        lon = carta_natal["debug"]["lon"]
        lugar_nombre = f"(lugar natal: {carta_natal['debug'].get('lugar_nombre', '?')})"

    planetas = calc_planets(tret)
    casas = calc_houses(tret, lat, lon)
    for nombre_p, pdata in planetas.items():
        if "error" not in pdata:
            pdata["casa"] = assign_planet_house(pdata["lon"], casas["cusps"])
    aspectos = calc_aspectos(planetas)

    fecha_str = swe.revjul(tret, swe.GREG_CAL)  # (year, month, day, hour)
    return {
        "debug": {
            "tipo":         "retorno_solar",
            "anio":         anio,
            "jd_ut":        tret,
            "fecha_ut":     f"{int(fecha_str[0]):04d}-{int(fecha_str[1]):02d}-{int(fecha_str[2]):02d} {fecha_str[3]:.2f}h UTC",
            "lat":          lat,
            "lon":          lon,
            "lugar_nombre": lugar_nombre,
        },
        "planetas": planetas,
        "casas": {
            "cuspides": [{"numero": i + 1, "lon": casas["cusps"][i], "signo": casas["cusps_str"][i]} for i in range(12)],
            "asc":    {"lon": casas["asc"],    "signo": lon_to_sign(casas["asc"])},
            "mc":     {"lon": casas["mc"],     "signo": lon_to_sign(casas["mc"])},
            "ic":     {"lon": casas["ic"],     "signo": lon_to_sign(casas["ic"])},
            "dc":     {"lon": casas["dc"],     "signo": lon_to_sign(casas["dc"])},
        },
        "aspectos": aspectos,
    }


def calc_retorno_lunar(carta_natal: dict, fecha_ref: str = None,
                       lugar_retorno: str = None) -> dict:
    """
    Encuentra el próximo retorno lunar desde fecha_ref (default ahora).

    Args:
        carta_natal: output de calc_carta_completa().
        fecha_ref: 'DD/MM/AAAA' — desde qué fecha buscamos el próximo retorno lunar.
                   Si None, ahora. Como pasa cada ~27.3 días siempre hay uno cerca.
        lugar_retorno: donde estuvo la persona ese día.
    """
    luna_natal_lon = carta_natal["planetas"]["Luna"]["lon"]

    if fecha_ref:
        jd_start = _jd_from_fecha(fecha_ref)
    else:
        jd_start = _julian_ahora_ut()

    try:
        _r = swe.mooncross_ut(luna_natal_lon, jd_start)
        tret = _r if isinstance(_r, (int, float)) else _r[1]
    except AttributeError:
        tret = _find_return_iterative(luna_natal_lon, jd_start, swe.MOON)

    if lugar_retorno:
        lat, lon, lugar_nombre = _geocode(lugar_retorno)
    else:
        lat = carta_natal["debug"]["lat"]
        lon = carta_natal["debug"]["lon"]
        lugar_nombre = f"(lugar natal: {carta_natal['debug'].get('lugar_nombre', '?')})"

    planetas = calc_planets(tret)
    casas = calc_houses(tret, lat, lon)
    for nombre_p, pdata in planetas.items():
        if "error" not in pdata:
            pdata["casa"] = assign_planet_house(pdata["lon"], casas["cusps"])
    aspectos = calc_aspectos(planetas)

    fecha_str = swe.revjul(tret, swe.GREG_CAL)
    return {
        "debug": {
            "tipo":         "retorno_lunar",
            "jd_ut":        tret,
            "fecha_ut":     f"{int(fecha_str[0]):04d}-{int(fecha_str[1]):02d}-{int(fecha_str[2]):02d} {fecha_str[3]:.2f}h UTC",
            "lat":          lat,
            "lon":          lon,
            "lugar_nombre": lugar_nombre,
        },
        "planetas": planetas,
        "casas": {
            "cuspides": [{"numero": i + 1, "lon": casas["cusps"][i], "signo": casas["cusps_str"][i]} for i in range(12)],
            "asc":    {"lon": casas["asc"],    "signo": lon_to_sign(casas["asc"])},
            "mc":     {"lon": casas["mc"],     "signo": lon_to_sign(casas["mc"])},
            "ic":     {"lon": casas["ic"],     "signo": lon_to_sign(casas["ic"])},
            "dc":     {"lon": casas["dc"],     "signo": lon_to_sign(casas["dc"])},
        },
        "aspectos": aspectos,
    }


def _find_return_iterative(target_lon: float, jd_start: float, planet_id: int,
                            search_days: int = 400) -> float:
    """Fallback: busca iterativamente cuándo un planeta cruza target_lon.
    Usa bisección en ventana de search_days adelante."""
    from swisseph import calc_ut as _cu
    def lon_at(jd):
        xx, _ = _cu(jd, planet_id, MOSH)
        return xx[0]
    step = 0.5
    jd = jd_start
    prev = (lon_at(jd) - target_lon) % 360
    if prev > 180: prev -= 360
    while jd < jd_start + search_days:
        jd += step
        curr = (lon_at(jd) - target_lon) % 360
        if curr > 180: curr -= 360
        if prev < 0 and curr >= 0:
            # Bisección fina
            lo, hi = jd - step, jd
            for _ in range(40):
                mid = (lo + hi) / 2
                mv = (lon_at(mid) - target_lon) % 360
                if mv > 180: mv -= 360
                if mv < 0:
                    lo = mid
                else:
                    hi = mid
            return (lo + hi) / 2
        prev = curr
    raise ValueError(f"No se encontró retorno en {search_days} días desde jd={jd_start}")


# ── 12. Activaciones: aspectos entre dos cartas (ej. retorno vs natal) ───────────

def calc_activaciones(carta_base: dict, carta_activadora: dict,
                      orb: float = 3.0) -> list:
    """
    Aspectos entre los planetas de carta_activadora (retorno, progresión, etc.)
    y los planetas de carta_base (natal). Retorna lista ordenada por significancia.
    Orbes intermedios entre tránsitos (estrictos) y natal (amplios).
    """
    base_planetas = carta_base.get("planetas", {})
    act_planetas = carta_activadora.get("planetas", {})

    resultado = []
    for na, da in act_planetas.items():
        if "error" in da:
            continue
        for nb, db in base_planetas.items():
            if "error" in db:
                continue
            diff = abs(da["lon"] - db["lon"]) % 360
            diff = min(diff, 360 - diff)
            for angulo, nombre_asp, simbolo, orb_base in ASPECTOS_DEF:
                orb_max = min(orb_base, orb)
                orb_real = abs(diff - angulo)
                if orb_real <= orb_max:
                    peso = TRANSIT_WEIGHT.get(na, 1)
                    significancia = peso * (1 - orb_real / max(orb_max, 0.01))
                    resultado.append({
                        "planeta_activador": na,
                        "planeta_base":      nb,
                        "aspecto":           nombre_asp,
                        "simbolo":           simbolo,
                        "angulo":            angulo,
                        "orb":               round(orb_real, 2),
                        "peso":              peso,
                        "significancia":     round(significancia, 2),
                    })

    resultado.sort(key=lambda a: -a["significancia"])
    return resultado


def formatear_activaciones(activaciones: list, base_label: str, act_label: str,
                           top_n: int = 15) -> str:
    """Render de aspectos entre dos cartas."""
    lines = [f"⚡ *Activaciones: {act_label} → {base_label}*\n"]
    if not activaciones:
        lines.append("_(sin activaciones dentro del orb)_")
        return "\n".join(lines)
    for a in activaciones[:top_n]:
        lines.append(
            f"• *{a['planeta_activador']}* ({act_label}) {a['simbolo']} "
            f"*{a['planeta_base']}* ({base_label}) · orb {a['orb']}°"
        )
    if len(activaciones) > top_n:
        lines.append(f"\n_… y {len(activaciones) - top_n} más._")
    return "\n".join(lines)


# ── 13. Análisis triple-capa (natal + solar + lunar + tránsitos) ─────────────────

def calc_triple_capa(carta_natal: dict,
                     anio_solar: int = None,
                     lugar_solar: str = None,
                     fecha_lunar: str = None,
                     lugar_lunar: str = None) -> dict:
    """
    Análisis predictivo completo de 3 capas + tránsitos actuales:
      - Natal (identidad base)
      - Retorno Solar del año (tema del año)
      - Retorno Lunar del mes (tema del mes)
      - Tránsitos actuales sobre cada capa
      - Activaciones cruzadas (solar/lunar sobre natal, tránsitos sobre solar/lunar)

    Returns: dict con todas las capas + aspectos cruzados. Sirve como input
    para que el LLM arme la interpretación integrada.
    """
    solar = calc_retorno_solar(carta_natal, anio=anio_solar, lugar_retorno=lugar_solar)
    lunar = calc_retorno_lunar(carta_natal, fecha_ref=fecha_lunar, lugar_retorno=lugar_lunar)

    return {
        "natal": carta_natal,
        "solar": solar,
        "lunar": lunar,
        "transitos": {
            "sobre_natal": calc_transitos(carta_natal),
            "sobre_solar": calc_transitos(solar),
            "sobre_lunar": calc_transitos(lunar),
        },
        "activaciones": {
            "solar_sobre_natal": calc_activaciones(carta_natal, solar),
            "lunar_sobre_natal": calc_activaciones(carta_natal, lunar),
            "lunar_sobre_solar": calc_activaciones(solar, lunar),
        },
    }


def formatear_triple_capa(tc: dict, top_n: int = 8) -> str:
    """Resumen compacto de las 3 capas + sus aspectos más significativos."""
    lines = ["🔱 *Análisis triple-capa (natal + solar + lunar + tránsitos)*\n"]

    # Cabeceras
    lines.append(f"📍 *Natal:* {tc['natal']['debug'].get('lugar_nombre', '?')}")
    lines.append(f"☀️ *Retorno Solar {tc['solar']['debug']['anio']}:* {tc['solar']['debug']['fecha_ut']} · {tc['solar']['debug']['lugar_nombre']}")
    lines.append(f"🌙 *Retorno Lunar:* {tc['lunar']['debug']['fecha_ut']} · {tc['lunar']['debug']['lugar_nombre']}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ *Activaciones SOLAR → NATAL (tema del año):*")
    for a in tc["activaciones"]["solar_sobre_natal"][:top_n]:
        lines.append(f"  • {a['planeta_activador']} {a['simbolo']} {a['planeta_base']} natal · orb {a['orb']}°")

    lines.append("\n⚡ *Activaciones LUNAR → NATAL (tema del mes):*")
    for a in tc["activaciones"]["lunar_sobre_natal"][:top_n]:
        lines.append(f"  • {a['planeta_activador']} {a['simbolo']} {a['planeta_base']} natal · orb {a['orb']}°")

    lines.append("\n⚡ *Activaciones LUNAR → SOLAR (cómo el mes dispara el año):*")
    for a in tc["activaciones"]["lunar_sobre_solar"][:top_n]:
        lines.append(f"  • {a['planeta_activador']} {a['simbolo']} {a['planeta_base']} solar · orb {a['orb']}°")

    lines.append("\n🪐 *Tránsitos actuales sobre natal (top):*")
    for a in tc["transitos"]["sobre_natal"].get("aspectos", [])[:top_n]:
        lines.append(f"  • {a['planeta_transito']} {a['simbolo']} {a['planeta_natal']} · orb {a['orb']}°")

    lines.append("\n🪐 *Tránsitos sobre solar (activan el tema del año):*")
    for a in tc["transitos"]["sobre_solar"].get("aspectos", [])[:top_n]:
        lines.append(f"  • {a['planeta_transito']} {a['simbolo']} {a['planeta_natal']} solar · orb {a['orb']}°")

    lines.append("\n🪐 *Tránsitos sobre lunar (activan el tema del mes):*")
    for a in tc["transitos"]["sobre_lunar"].get("aspectos", [])[:top_n]:
        lines.append(f"  • {a['planeta_transito']} {a['simbolo']} {a['planeta_natal']} lunar · orb {a['orb']}°")

    return "\n".join(lines)
