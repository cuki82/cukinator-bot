#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Análisis astrológico en modo pista — aplica reglas estocásticas del owner.

Uso:
    python3 scripts/analisis_pista.py "04/02/1995" "05:20" "Buenos Aires, Argentina" 2025-09-01 2025-09-30

Produce un output por día con:
  - Posiciones planetarias exactas a 00:00 UT (Swiss Ephemeris)
  - Tránsitos sobre natal (orbes por velocidad, plenivalencia, A/S, D/R)
  - Tránsitos lentos obligatorios (siempre visibles)
  - ALERTA Luna ≤2° cambio signo
  - DESCARTES con motivo
  - Lectura gestalt integrada
"""
import sys
import os
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import swisseph as swe
from modules.swiss_engine import (
    calc_carta_completa, calc_planets, to_julian_ut,
    lon_to_sign, assign_planet_house, MOSH,
)

# ─────────────────────────────────────────────────────────────────────────
# Reglas estocásticas
# ─────────────────────────────────────────────────────────────────────────

ASPECTOS_MAYORES = [
    (0,   "☌ Conjunción", "☌"),
    (60,  "⚹ Sextil",     "⚹"),
    (90,  "□ Cuadratura", "□"),
    (120, "△ Trígono",    "△"),
    (180, "☍ Oposición",  "☍"),
]

# Orbes por categoría (owner: lentos ≤5°, rápidos ≤4°, Luna ≤3°)
PLANETAS_LENTOS   = {"Jupiter", "Saturno", "Urano", "Neptuno", "Pluton"}
PLANETAS_RAPIDOS  = {"Sol", "Mercurio", "Venus", "Marte"}
PLANETA_LUNA      = {"Luna"}

def orbe_max(planeta_transito: str) -> float:
    if planeta_transito in PLANETAS_LENTOS:  return 5.0
    if planeta_transito in PLANETA_LUNA:     return 3.0
    if planeta_transito in PLANETAS_RAPIDOS: return 4.0
    return 3.0  # Nodos, Quirón, etc

# Elementos por signo (para plenivalencia)
ELEMENTO = {
    "Aries": "fuego", "Leo": "fuego", "Sagitario": "fuego",
    "Tauro": "tierra", "Virgo": "tierra", "Capricornio": "tierra",
    "Geminis": "aire", "Libra": "aire", "Acuario": "aire",
    "Cancer": "agua", "Escorpio": "agua", "Piscis": "agua",
}
MODALIDAD = {
    "Aries": "cardinal", "Cancer": "cardinal", "Libra": "cardinal", "Capricornio": "cardinal",
    "Tauro": "fijo", "Leo": "fijo", "Escorpio": "fijo", "Acuario": "fijo",
    "Geminis": "mutable", "Virgo": "mutable", "Sagitario": "mutable", "Piscis": "mutable",
}

def signo_base(signo_completo: str) -> str:
    """'Cancer 19°25'' → 'Cancer'."""
    if not signo_completo: return ""
    return signo_completo.split()[0] if " " in signo_completo else signo_completo

def plenivalente(angulo: int, signo1: str, signo2: str) -> tuple:
    """¿El aspecto es plenivalente signo-vs-signo? Retorna (valido, motivo_si_falla)."""
    s1, s2 = signo_base(signo1), signo_base(signo2)
    if not s1 or not s2:
        return True, ""
    el1, el2 = ELEMENTO.get(s1), ELEMENTO.get(s2)
    mod1, mod2 = MODALIDAD.get(s1), MODALIDAD.get(s2)
    if angulo == 0:
        # Conjunción válida solo en el mismo signo
        return (s1 == s2), ("conjunción fuera de signo" if s1 != s2 else "")
    if angulo == 60:
        # Sextil: fuego↔aire, tierra↔agua
        compat = {("fuego","aire"), ("aire","fuego"), ("tierra","agua"), ("agua","tierra")}
        return ((el1, el2) in compat), f"sextil entre {el1}/{el2} no es plenivalente"
    if angulo == 90:
        # Cuadratura: misma modalidad, distinto elemento
        return (mod1 == mod2 and el1 != el2), f"cuadratura {mod1}/{mod2} {el1}/{el2} inválida"
    if angulo == 120:
        # Trígono: mismo elemento
        return (el1 == el2), f"trígono entre {el1} y {el2} inválido"
    if angulo == 180:
        # Oposición: elementos complementarios (fuego↔aire, tierra↔agua), modalidad coincide
        compat = {("fuego","aire"), ("aire","fuego"), ("tierra","agua"), ("agua","tierra")}
        return ((el1, el2) in compat and mod1 == mod2), f"oposición {mod1}/{mod2} {el1}/{el2} inválida"
    return True, ""


def luna_alerta_cambio(luna_lon: float) -> str:
    """Luna avanza 12-13°/día. Si le quedan ≤2° para el próximo signo, ALERTA."""
    grado_en_signo = luna_lon % 30
    if grado_en_signo >= 28:
        return f"⚠️ ALERTA: Luna a {30 - grado_en_signo:.2f}° del cambio de signo"
    return ""


def jd_00ut(fecha: datetime.date) -> float:
    return swe.julday(fecha.year, fecha.month, fecha.day, 0.0)


# ─────────────────────────────────────────────────────────────────────────
# Análisis por día
# ─────────────────────────────────────────────────────────────────────────

def analizar_dia(carta_natal: dict, fecha: datetime.date) -> str:
    """Genera el bloque de análisis de un día completo aplicando reglas estocásticas."""
    jd = jd_00ut(fecha)
    planetas_trans = calc_planets(jd)
    natal_planetas = carta_natal["planetas"]
    natal_casas = carta_natal["casas"]["cuspides"]

    lines = []
    lines.append(f"📅 **Día {fecha.isoformat()}**\n")

    # ── Posiciones exactas (Swiss Ephemeris a 00:00 UT)
    lines.append("▶ **Posiciones exactas (Swiss Ephemeris, 00:00 UT)**")
    for n in ["Sol","Luna","Mercurio","Venus","Marte","Jupiter","Saturno","Urano","Neptuno","Pluton","Nodo Norte","Quiron"]:
        p = planetas_trans.get(n, {})
        if "error" in p: continue
        dr = "R" if p.get("speed",0) < 0 else "D"
        lines.append(f"   • {n:12s} {p.get('signo','?')}  ({dr})  speed {p.get('speed',0):+.3f}°/día")

    # ALERTA Luna cambio signo
    luna_lon = planetas_trans["Luna"]["lon"]
    alerta = luna_alerta_cambio(luna_lon)
    if alerta:
        lines.append(f"\n   {alerta}")

    # ── Tránsitos sobre natal (orbes por velocidad + plenivalencia)
    lines.append("\n▶ **Tránsitos sobre la Natal**")
    aspectos_validos = []
    descartes = []

    orden_planetas = (
        ["Pluton","Neptuno","Urano","Saturno","Jupiter"] +
        ["Marte","Venus","Mercurio","Sol"] +
        ["Luna"]
    )

    for pt in orden_planetas:
        dt = planetas_trans.get(pt, {})
        if "error" in dt: continue
        orb_pt = orbe_max(pt)
        for pn, dn in natal_planetas.items():
            if "error" in dn: continue
            diff = abs(dt["lon"] - dn["lon"]) % 360
            diff = min(diff, 360 - diff)
            for angulo, nombre_asp, simbolo in ASPECTOS_MAYORES:
                orb_real = abs(diff - angulo)
                if orb_real <= orb_pt:
                    plen_ok, motivo = plenivalente(angulo, dt.get("signo",""), dn.get("signo",""))
                    if not plen_ok:
                        descartes.append(f"{pt} {simbolo} {pn} natal — orb {orb_real:.2f}° — {motivo}")
                        continue
                    aplicante = dt.get("speed",0) > 0 and diff < angulo
                    # Aplicante/separativo simple: si los planetas se acercan (orb decrece) → A, sino S
                    # Aproximación: si velocidad transit > velocidad natal (siempre positiva para natal fija)
                    # y diff está antes del ángulo exacto → aplicante
                    a_s = "A" if aplicante else "S"
                    dr = "R" if dt.get("speed",0) < 0 else "D"
                    casa_natal = dn.get("casa","?")
                    aspectos_validos.append({
                        "pt": pt, "pn": pn, "nombre_asp": nombre_asp, "simbolo": simbolo,
                        "orb": round(orb_real,2), "a_s": a_s, "dr": dr,
                        "casa_natal": casa_natal, "angulo": angulo,
                    })

    if not aspectos_validos:
        lines.append("   _(ningún aspecto dentro de orbes)_")
    else:
        for a in aspectos_validos:
            lines.append(
                f"   • **{a['pt']}** {a['simbolo']} **{a['pn']}** natal — "
                f"orb {a['orb']}° — ({a['a_s']}) — ({a['dr']}) — casa natal {a['casa_natal']}"
            )

    # ── Tránsitos lentos obligatorios (siempre mostrar su posición aunque no formen aspecto)
    lines.append("\n▶ **Estado de lentos (obligatorio)**")
    for pt in ["Jupiter","Saturno","Urano","Neptuno","Pluton"]:
        p = planetas_trans.get(pt, {})
        if "error" in p: continue
        dr = "R" if p.get("speed",0) < 0 else "D"
        forma_aspecto = any(a["pt"] == pt for a in aspectos_validos)
        tag = "→ forma aspecto a natal" if forma_aspecto else "— sin aspecto a natal hoy"
        lines.append(f"   • {pt:10s} {p.get('signo','?')} ({dr}) {tag}")

    # ── Redes de regentes activadas (planetas natales en casas tocadas por tránsito)
    # Para cada aspecto válido, tomamos la casa natal implicada y buscamos qué cúspide es
    lines.append("\n▶ **Redes de regentes activadas**")
    casas_tocadas = set(a["casa_natal"] for a in aspectos_validos if a["casa_natal"] != "?")
    if not casas_tocadas:
        lines.append("   _(ninguna casa natal tocada)_")
    else:
        for casa_num in sorted(casas_tocadas):
            try:
                cusp = natal_casas[int(casa_num)-1]
                lines.append(f"   • Casa {casa_num} (cúspide en {cusp.get('signo','?')}) — tocada por "
                             f"{', '.join(a['pt'] for a in aspectos_validos if a['casa_natal']==casa_num)}")
            except (ValueError, IndexError):
                pass

    # ── DESCARTES
    lines.append("\n▶ **DESCARTES** (aspectos rechazados)")
    if not descartes:
        lines.append("   _(ninguno — todos los candidatos pasaron plenivalencia)_")
    else:
        for d in descartes:
            lines.append(f"   • {d}")

    lines.append("")  # espacio entre días
    return "\n".join(lines)


def main():
    if len(sys.argv) < 6:
        print("Uso: analisis_pista.py FECHA_NATAL HORA_NATAL LUGAR_NATAL DESDE HASTA")
        print("     FECHA_NATAL = DD/MM/AAAA")
        print("     HORA_NATAL  = HH:MM")
        print("     DESDE/HASTA = AAAA-MM-DD")
        sys.exit(1)

    fecha_natal = sys.argv[1]
    hora_natal  = sys.argv[2]
    lugar_natal = sys.argv[3]
    desde = datetime.date.fromisoformat(sys.argv[4])
    hasta = datetime.date.fromisoformat(sys.argv[5])

    print(f"Calculando carta natal...", file=sys.stderr)
    carta = calc_carta_completa(fecha_natal, hora_natal, lugar_natal)

    print(f"# Análisis en modo pista\n")
    print(f"**Natal:** {fecha_natal} · {hora_natal} · {carta['debug'].get('lugar_nombre','?')[:60]}")
    print(f"**Rango:** {desde} → {hasta}\n")
    print("**Planetas natales:**")
    for n in ["Sol","Luna","Mercurio","Venus","Marte","Jupiter","Saturno","Urano","Neptuno","Pluton"]:
        p = carta["planetas"].get(n, {})
        if "error" in p: continue
        print(f"  · {n:10s} {p.get('signo','?')} · casa {p.get('casa','?')}")
    print(f"ASC: {carta['casas']['asc']['signo']}  MC: {carta['casas']['mc']['signo']}\n")
    print("━" * 60)

    # Iterar por días en bloques de 5
    dias = []
    d = desde
    while d <= hasta:
        dias.append(d)
        d += datetime.timedelta(days=1)

    for i in range(0, len(dias), 5):
        bloque = dias[i:i+5]
        print(f"\n## 🔹 Bloque {bloque[0]} → {bloque[-1]}\n")
        for dia in bloque:
            print(analizar_dia(carta, dia))
        print("━" * 60)


if __name__ == "__main__":
    main()
