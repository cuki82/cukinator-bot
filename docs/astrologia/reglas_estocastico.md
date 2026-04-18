# Reglas de interpretación astrológica — Criterio estocástico

Fuente: el owner. Estas reglas son OBLIGATORIAS para toda lectura astrológica
que haga el bot (natal, retornos, tránsitos, análisis por rango). El motor
(`modules/swiss_engine.py`) y el system prompt aplican estos filtros.

---

## Aspectos válidos

- **Solo mayores:** ☌ (conjunción), ☍ (oposición), □ (cuadratura), △ (trígono), ⚹ (sextil).
- **Incluir SIEMPRE todos los aspectos válidos** — tensos **y** armónicos, nunca filtrar.
- **Plenivalencia obligatoria signo-vs-signo** (ver chequeos rápidos abajo).
- **No arrastrar aspectos** de días anteriores, salvo análisis de rango.

## Orbes (máximos)

| Cuerpo | Orbe máximo |
|--------|-------------|
| Lentos (♃ ♄ ♅ ♆ ♇) | ≤ 5° |
| Rápidos (☉ ☿ ♀ ♂) | ≤ 4° |
| Luna (☽) | ≤ 3° |

## Etiquetas obligatorias en cada aspecto

- **A / S**: aplicativo o separativo.
- **D / R**: planeta directo o retrógrado.
- **Casa natal implicada** (nunca cambia según tránsito).
- **Plenivalencia** (signo del planeta en tránsito + signo del planeta natal — validar).

## Tránsitos lentos — obligatorios siempre

- Incluir ♇, ♆, ♅, ♄, ♃ **aunque no cambien de aspecto** en el día.
- Detallar: grado, plenivalencia, orbe, D/R, A/S.
- Aunque un aspecto dure semanas, **figurar cada día**.
- Marcar **cambios de estado R→D, D→R** cuando ocurran.

## Tránsitos personales — obligatorios siempre

- Incluir ☉ ☿ ♀ ♂ y **especialmente ☽**.
- Son los **gatillos** de los lentos → integrarlos siempre en la lectura.
- **ALERTA si ☽ está ≤ 2° de cambio de signo** (avanza ~12–13°/día).

## Jerarquía de validación (en ese orden)

1. Tránsitos sobre la Natal.
2. Revolución Solar (**fáctico**, tema del año).
3. Revolución Lunar (**emocional**, tema del mes).
4. Aspectos internos rápidos de la Lunar.

## Orden de lectura dentro de un día

**Lentos → rápidos → Luna.**

## Estructura de salida por día

```
📅 Día [AAAA-MM-DD]

▶ Posiciones exactas (extraídas de Swiss Ephemeris)
▶ Tránsitos sobre la Natal
  • [planeta trans] [aspecto] [planeta natal] — orbe [x°xx] — (A/S) — (D/R) — Casa natal implicada.
▶ Redes de regentes activadas
  • Qué cúspide toca cada tránsito, cadenas y subredes.
▶ Activaciones desde la Lunar
  • Cruces de ángulos, casas sensibles, figuras duras.
▶ Lectura emocional gestalt
  • Integrar toda la red (tensos + armónicos).
  • Correlato real emocional/vincular.
  • Considerar el estado de los lentos (R/D).
▶ DESCARTES
  • Lista de aspectos rechazados con motivo.
```

## Bloque triple-chequeo (obligatorio)

1. Extraer del motor todas las posiciones ☉ … ⚷ (Quirón).
2. Generar candidatos por ángulo (≤ 5°).
3. Validar plenivalencia (ver chequeos rápidos).
4. Etiquetar A/S y D/R.
5. **Mostrar DESCARTES** (aspectos rechazados con motivo).

## Chequeos rápidos anti-error (plenivalencia)

- ♓ – ♐ = **□** (NO △). Piscis y Sagitario no forman trígono.
- ♈ – ♐ = △.
- ♎ – ♒ = △.
- ♎ – ♑ = □.
- **Signos contiguos distintos no forman aspecto mayor** (excepto conjunción dentro del mismo signo).
- Luna ≤ 2° del cambio de signo → **ALERTA**.

## Control NATAL vs. TRÁNSITO

- **Nunca cambiar la casa natal** de un planeta según el tránsito.
- Diferenciar **NATAL (permanente)** vs. **TRÁNSITO (dinámico)**.
- Aspectos natales usados → marcar "**MEMORIA NATAL**".
- Aspectos de tránsito → especificar planeta natal y **casa**.

## Tono y estilo

- **Directo, crudo, sin contención**.
- Lectura **gestalt completa** (tensos + armónicos integrados).
- Nada de "memoria entrenada" — siempre validar en efemérides / natal / lunar.

## Control final

- Validar que la salida comience con el bloque "**Posiciones exactas (extraídas de Swiss Ephemeris)**".
- Si no aparece primero, la salida es inválida → repetir desde el paso 1.

## Modo "análisis por rango"

Cuando el usuario pide `analízame en modo pista de AAAA-MM-DD a AAAA-MM-DD`:

- Dividir en **bloques de 5 días** para que cada día tenga detalle completo sin cortar.
- Cada día con la estructura completa arriba.
- Al final del rango, un resumen gestalt del período.
