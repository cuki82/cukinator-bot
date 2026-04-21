"""
agents/intent_router.py — Clasificador de intenciones. Zero latencia, zero costo.
Clasifica en 6 intents via keywords antes de llegar a cualquier LLM.
"""
import re
import logging

log = logging.getLogger(__name__)

# ── Keywords por intent ────────────────────────────────────────────────────────

# Patrones de DISEÑO que NO deben caer en coding aunque mencionen palabras
# técnicas. Ejemplos: "armame una PPT con todo el stack", "generame un PDF
# corporativo", "diseñá un mockup HTML del workspace". Esto va al LLM bot
# (intent=conversational) que tiene la tool generar_diseno disponible.
_DESIGN_INTENT_PATTERNS = [
    r"\b(?:arm[aá]|gener[aá]|hac[eé]|cre[aá]|prepar[aá]|diseñ[aá]|necesito|quiero)(?:me|le|nos)?\s+(?:un[ao]?|el|la|los|las|el?\s+)?\s*(?:ppt|pptx|presentaci[oó]n|pdf|html|mockup|landing|email|template|diseño|pieza|brochure|flyer|catalogo|cat[aá]logo)\b",
    r"\b(?:ppt|pptx|presentaci[oó]n)\s+(?:corporativ|formal|para|sobre|de|del)\b",
    r"\b(?:flyer|brochure|landing|mockup|catalogo|cat[aá]logo)\s+(?:para|de|del|sobre)\b",
]

_PATTERNS = {
    "coding": [
        # Modificaciones de codigo
        r"modific[aá] el bot", r"cambi[aá] el c[oó]digo", r"edit[aá] el archivo",
        r"nuevo m[oó]dulo", r"nueva funci[oó]n", r"nuevo handler", r"nuevo tool",
        r"push a github", r"hac[eé] un commit", r"abr[ií] un pr", r"pull request",
        r"refactoriz[aá]", r"reescrib[ií]", r"agregá? un tool", r"agregá? un handler",
        r"cambi[aá] el system prompt", r"modific[aá] bot_core", r"nueva integraci[oó]n al bot",
        r"deploy", r"dockerfile", r"requirements\.txt", r"github",
        # Code review / investigación / propuesta de cambios — estas tareas van
        # al worker porque típicamente terminan con "proponé", "hacé el cambio"
        # o "pusheá". Si caen en Claude directo, Haiku puede LEER el código con
        # las tools vps_* pero NO pushear (git_commit_push solo existe en el
        # worker) — y se queda preguntando "¿hacemos el push?" sin poder ejecutar.
        r"\brevis[aá]\s+(?:el\s+|la\s+)?(?:c[oó]digo|handler|funci[oó]n|archivo|bot|m[oó]dulo|l[oó]gica)",
        r"\banaliz[aá]\s+(?:el\s+|la\s+)?(?:c[oó]digo|handler|funci[oó]n|archivo|flujo|l[oó]gica)",
        r"\binspeccion[aá]\w*\s+(?:el\s+|la\s+|los\s+|las\s+)?(?:c[oó]digo|handler|funci[oó]n|archivo|bot|repo)",
        r"\binvestig[aá]\s+(?:el\s+|la\s+|c[oó]mo\s+)?(?:c[oó]digo|funci[oó]n|handler|funciona|est[aá]\s+hech[oa]|anda)",
        r"\bpropone[mr]?e?\s+(?:un\s+|una\s+)?(?:cambio|mejora|modificaci[oó]n|fix|implementaci[oó]n|c[oó]mo)",
        r"\bsuger[ií]\w*\s+(?:un\s+|una\s+)?(?:cambio|modificaci[oó]n|mejora|c[oó]mo)",
        r"\bfixe[aá]?r?\s+(?:el\s+|la\s+|un\s+|los\s+|las\s+)?(?:bug|error|c[oó]digo|handler|funci[oó]n|archivo|bot)",
        r"\barreglar?\s+(?:el\s+|la\s+)?(?:bug|c[oó]digo|funci[oó]n|handler|archivo|bot|error|m[oó]dulo)",
        r"\bmejorar?\s+(?:el\s+|la\s+)?(?:c[oó]digo|funci[oó]n|bot|handler|performance|flujo)",
        r"\bc[oó]mo est[aá]\s+(?:implementad[ao]|el\s+c[oó]digo|hech[oa]|el\s+handler|la\s+funci[oó]n)",
        r"\bimplement[aá]\s+(?:el\s+|un[ao]?\s+)?(?:feature|funci[oó]n|handler|m[oó]dulo|tool|cambio|fix)",
        r"\bmir[aá]\s+(?:el\s+|la\s+)?(?:c[oó]digo|handler|funci[oó]n|archivo|bot)",
        # Fallback amplio coding: verbo de acción + OBJETO TÉCNICO ESPECÍFICO del
        # repo/bot (no objetos genéricos como "menú" o "botón" que pueden ser
        # de Reamerica/astrología/otra empresa). Los casos ambiguos (menú,
        # botón, comando sin contexto) se resuelven con LLM fallback, no con
        # más keywords que hacen over-fitting.
        r"\b(?:revis|analiz|investig|propon|suger|fixe|arregl|mejor|mir|inspeccion|edit|cambi|modific|escrib|reescrib|implement|agreg|refactor|fij|corregi|ajust|pon[ée]r?|meter|sumar|integrar|unificar|combin)\w*\b.*\b(?:c[oó]digo|handler|funci[oó]n|archivo|bot_core|handlers/|services/|workers/|modules/|agents/|worker|router|m[oó]dulo|endpoint|schema|pipeline|trace|intent_router|message_handler|systemctl|docker|nginx|railway|deploy|commit|push|merge|branch|repo)\b",
        # "/comando dentro de /otrocomando" → modificación de estructura
        r"/\w+\s+(?:dentro|en|al)\s+/\w+",
        r"\bpor qu[eé]\s+.*(?:no\s+)?(?:funciona|anda|falla|devuelve|retorna|crashea|rompe)",
        r"\bhacemos?\s+(?:el\s+)?push",
        r"\b(?:qu[eé]\s+)?(?:est[aá]s?|estuvo|estaba)\s+(?:pasando|fallando|rompiendo)\b",
        # DevOps fallback — cualquier verbo de estado/acción sobre un servicio/infra
        # va al worker. El user lo quiere así: "no es solo código, también cualquier
        # decisión que tenga que ver con DevOps".
        r"\b(?:pas|anda|funcion|fall|crash|crashe|rompe|devuelv|retorn|responde|est[aá]|qued[oó])\w*\b.*\b(?:bot|worker|mcp|servicio|service|vps|railway|deploy|systemd|docker|container|endpoint|api)\b",
        r"\b(?:actualiz|updat|upgradear?|downgradear?|bumpear?|moverl[oa]?|migrar?)\w*\b",
        r"\b(?:qu[eé]\s+pasa|qu[eé]\s+tiene|qu[eé]\s+est[aá]\s+pasando)\s+(?:con\s+)?(?:el|la|los|las)?\s*(?:bot|worker|mcp|servicio|vps|deploy|sistema|arquitectura|endpoint|railway|docker)",
        r"\bno\s+(?:anda|funciona|responde|conecta|levanta|arranca|llega|procesa|transcribe|clasifica)",
        # DevOps / VPS / servidor
        r"entra[rn]? al servidor", r"accede[rn]? al vps", r"conect[aá]te al",
        r"fijate en el vps", r"ver[ií]? en el servidor", r"chequea[rn]? el vps",
        r"corr[eé] en el vps", r"ejecut[aá] en el (?:vps|servidor)",
        r"ssh", r"docker", r"systemctl", r"systemd", r"container(?:es)?",
        r"reinicia[rn]? (?:el )?(?:bot|servicio|worker|container)",
        r"restart (?:bot|service|worker|container)",
        r"ver logs?", r"mostr[aá] logs?", r"journalctl", r"tail -f",
        r"estado del (?:vps|servidor|bot)", r"status del", r"uptime",
        r"instal[aá] (?:en|el)", r"instal[aá]r (?:en|el)",
        r"configur[aá] el", r"\.service", r"nginx", r"uvicorn",
        r"agent.?worker", r"worker del bot",
        r"arrancar? (?:el )?(?:servicio|worker|bot)",
        r"parar? (?:el )?(?:servicio|worker|bot)",
    ],
    "research": [
        r"\bbusc[aáoó]\w*", r"\binvestig[aáoó]\w*", r"\bencontr[aá]\w*",
        r"qu[eé] dice", r"qu[eé] es", r"c[oó]mo funciona",
        r"\bexplic[aá]\w*", r"\bcompar[aáoó]\w*", r"\ban[aá]liz[aá]\w*",
        r"\bresumen\b", r"\bnoticias\b", r"\búltim[oa]s?\b", r"\binformaci[oó]n sobre\b",
        r"\bdocumentaci[oó]n\b", r"\bnormativa\b", r"\bregulaci[oó]n\b",
    ],
    "reinsurance": [
        r"reaseguro", r"reinsur", r"p[oó]liza", r"wording", r"cl[aá]usula",
        r"siniestro", r"prima", r"cobertura", r"cedente", r"reasegurador",
        r"treaty", r"facultativo", r"xs[lL]", r"excess of loss", r"quota share",
        r"retenci[oó]n", r"contrato de reaseguro", r"programa",
        # Salesforce — toda interaccion SF cae en este intent (regla owner)
        r"\bsalesforce\b", r"\bsf\b", r"\bcrm\b", r"\bsoql\b",
        r"\baccount(s)?\b(?!\s+(?:a|de)\s+(?:gmail|google|github|telegram|instagram|twitter|facebook))",
        r"\boportunidad(es)?\b", r"\bopportunity\b", r"\bopportunities\b",
        r"\blead(s)?\b", r"\bpipeline\b", r"\bendoso(s)?\b",
        r"\bibf\b", r"\bbordereaux\b", r"\bcedente(s)?\b",
        r"\breasegurador(es)?\b", r"\bbroker(s)?\b",
        r"\bprima\s+(?:emitida|cedida|total|del?\s+ano|por\s+mes)\b",
        r"\bcontrato(s)?\s+de\s+reaseguro\b",
        # Vocabulario comercial Reamerica — el user usa estos terminos para hablar de SF
        r"\bnegocio(s)?\b", r"\bconcretad[oa]s?\b", r"\bganad[oa]s?\b",
        r"\bcerrad[oa]s?\b(?!\s+(?:la|el)\s+(?:bot|sesion|chat))",  # cerrado pero no del bot
        r"\bcotizad[oa]s?\b", r"\bcotizaci[oó]n(es)?\b",
        r"\bcolocad[oa]s?\b", r"\bcolocaci[oó]n(es)?\b",
        r"\bcomisi[oó]n(es)?\b", r"\bcomisionad[oa]s?\b",
        r"\bintermedi(?:o|aci[oó]n|ad[oa]s?)\b",
        r"\bmaterializad[oa]s?\b", r"\bbajad[oa]s?\b",
        r"\bemitid[oa]s?\b",  # prima emitida, polizas emitidas
        r"\bcedid[oa]s?\b",   # prima cedida
        r"\bvigent(?:e|es)\b", r"\bvencid[oa]s?\b",
        # Personas conocidas Reamerica → asumir contexto SF
        r"\bromanelli\b", r"\bignacio\b\s+\w+", r"\bcarlos\s+(?:martin\s+)?romanelli\b",
        r"\bmartin\s+romanelli\b",
    ],
    "astrology": [
        r"astro", r"carta natal", r"tr[aá]nsito", r"planeta", r"signo",
        r"ascendente", r"luna", r"mercurio", r"venus", r"marte", r"j[uú]piter",
        r"saturno", r"ur[aá]no", r"neptuno", r"plut[oó]n", r"h[oó]roscopo",
        r"casa [0-9]", r"conjunci[oó]n", r"oposici[oó]n", r"trígono",
    ],
    "personal": [
        r"recorda[rn]?", r"guard[aá]", r"mi preferencia", r"mis datos",
        r"cu[aá]ndo dije", r"qu[eé] dijiste", r"mi historial", r"acord[aá]te",
        r"anot[aá]", r"mi perfil", r"mis contactos", r"agenda personal",
    ],
}

# Orden de prioridad (primero gana)
_PRIORITY = ["coding", "reinsurance", "astrology", "personal", "research", "conversational"]

# ── Señales de ambigüedad ─────────────────────────────────────────────────────
# Si el keyword router devuelve 'conversational' PERO detectamos estas señales,
# escalamos a un LLM Haiku para que clasifique con contexto semántico. Esto
# evita over-fitting con keywords genéricas (ej. "menú" puede ser del bot,
# de Reamerica, o de astrología — el regex no discrimina, el LLM sí).

_AMBIGUITY_SIGNALS = [
    r"/\w+",                          # cualquier slash command mencionado
    r"\b(?:pon[ée]r?|agrega|meter?|integrar?|unificar?|combin\w*|modific|cambi|edita|borra|elimin|refactor|implement|reescrib)\w*\b",
    r"\b(?:archivo|handler|comando|menú|menu|submenu|submenú|botón|boton|callback|tool|endpoint|module|módulo|función|funcion|servicio|service)\b",
    r"\b(?:del bot|del worker|del repo|del código|del codigo|del archivo|del handler|del sistema)\b",
    r"\bperformance\b", r"\bdashboard\b", r"\bmétric\w+\b", r"\bmetric\w+\b",
]

def _has_ambiguity(t: str) -> bool:
    return any(re.search(p, t) for p in _AMBIGUITY_SIGNALS)


def _classify_with_llm(text: str) -> str:
    """Clasifica con Haiku 4.5 cuando el keyword router no alcanza. Zero-shot.
    ~$0.0005 por call. Fallback a 'conversational' si Claude no responde o falla."""
    try:
        import os
        import anthropic
        key = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return "conversational"
        client = anthropic.Anthropic(api_key=key, timeout=10.0)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=20,
            system=(
                "Clasificá el mensaje del usuario en UNO de estos intents. Respondé SOLO la palabra, sin puntuación ni explicación:\n"
                "- coding: pide modificar código/handler/tool/archivo del bot/repo, push/commit, refactor, bug del código.\n"
                "- reinsurance: pregunta por CRM Salesforce, accounts, oportunidades, primas, brokers, IBF, endosos, Reamerica.\n"
                "- astrology: carta natal, tránsitos, planetas, signos, retornos, ascendente.\n"
                "- personal: recordar/guardar preferencias, historial, mis datos, mis contactos del usuario.\n"
                "- research: buscar/investigar/resumir algo externo, web, noticias, documentación.\n"
                "- conversational: charla general, saludos, confirmaciones, clima, hora, preguntas que no caen en otra."
            ),
            messages=[{"role": "user", "content": text[:800]}],
        )
        content = ""
        for block in resp.content:
            if hasattr(block, "text"):
                content += block.text
        out = content.strip().lower().split()[0] if content.strip() else ""
        out = out.strip("`'\"., ")
        valid = {"coding", "reinsurance", "astrology", "personal", "research", "conversational"}
        if out in valid:
            log.info(f"llm_classifier: {out!r} for {text[:60]!r}")
            return out
        log.warning(f"llm_classifier unexpected output: {content[:60]!r}")
        return "conversational"
    except Exception as e:
        log.debug(f"llm_classifier fail: {e}")
        return "conversational"


# ── Clasificador ───────────────────────────────────────────────────────────────

def classify(text: str, use_llm_fallback: bool = True) -> str:
    """Clasifica el intent. 2 pasadas:
    1. Keyword regex (zero latencia, zero costo) — cubre la gran mayoría.
    2. Si regex dio 'conversational' PERO hay señales de ambigüedad, escala a
       Haiku 4.5 (~$0.0005, ~1s). Esto evita keywords genéricas que pueden
       aplicar a múltiples dominios (ej. 'menú' = bot/astrología/reamerica).

    Override de DISEÑO: si el texto es claramente un pedido de PPT/PDF/HTML/
    mockup/diseño, fuerza conversational (donde el LLM bot tiene la tool
    generar_diseno disponible). Esto evita que palabras técnicas en el brief
    ('stack', 'arquitectura', 'pipeline') ruteen al worker de coding por
    error."""
    t = text.lower()

    # Override de diseño — alta prioridad, antes del scoring de coding
    for pat in _DESIGN_INTENT_PATTERNS:
        if re.search(pat, t):
            log.info(f"intent: design override → conversational (matched {pat[:40]!r})")
            return "conversational"

    scores = {intent: 0 for intent in _PRIORITY}
    for intent, patterns in _PATTERNS.items():
        if intent not in scores:
            continue
        for p in patterns:
            if re.search(p, t):
                scores[intent] += 1

    best = max(scores, key=scores.get)
    result = best if scores[best] > 0 else "conversational"

    # Fallback LLM: si hay señales de ambigüedad (verbo + objeto genérico como
    # "menú/botón/comando" que puede ser del bot O de astrología O de Reamerica),
    # escalamos a Haiku 4.5 para que decida con contexto semántico — NO importa
    # si el keyword dijo coding o conversational; queremos la verdad, no la
    # primera respuesta de regex.
    # No escalamos: texto corto (<15 chars) o intents específicos confiables
    # (reinsurance/astrology/personal) con score alto.
    if (use_llm_fallback
            and len(text.strip()) >= 15
            and _has_ambiguity(t)
            and not (result in ("reinsurance", "astrology", "personal") and scores[result] >= 2)):
        llm_result = _classify_with_llm(text)
        if llm_result != result:
            log.info(f"intent: keyword={result!r} → llm={llm_result!r} for {text[:60]!r}")
        result = llm_result

    log.debug(f"Intent '{result}' para: {text[:60]}")
    return result


def classify_complexity(text: str) -> str:
    """
    Estima la complejidad de la tarea para elegir el modelo.
    Retorna: simple | medium | complex
    """
    t = text.lower()
    word_count = len(text.split())

    # Complex: frases y verbos largos. Word boundaries para no matchear substrings.
    complex_signals = [
        r"\banali[zs][aá]\w*", r"\bcompar[aá]\w*", r"\bredact[aá]\w*", r"\belabor[aá]\w*",
        r"\ben detalle\b", r"\ba fondo\b", r"\bcomplet[ao]s?\b", r"\bexhaustiv[ao]s?\b",
        r"\bm[uú]ltiples\b", r"\btodos los\b", r"\bconsidera[rn]?\b",
        r"\bprofund\w*",  # profundo, profundamente, profundización, profundizá
        r"\bdetallad\w*", r"\bintegrad\w*", r"\bintegral\w*",
    ]
    # Simple: interjecciones / confirmaciones breves. Word boundaries obligatorios —
    # sin ellos 'no' matchea 'no proporcional', 'ok' matchea 'okupa', etc., y una query
    # compleja cae a Haiku/Sonnet por error. Eso ya nos pasó: usó Sonnet para
    # reaseguros complejo porque "no proporcional" disparó el pattern "no".
    simple_signals = [
        r"\bqu[eé] hora\b", r"\bclima\b", r"\btemperatura\b", r"\bhola\b",
        r"\bgr[aá]cias\b", r"\bok\b", r"\blisto\b", r"\bdale\b",
        r"\bperfecto\b", r"\bs[ií]\b",
    ]
    # Nota: 'no' suelto se removió a propósito — es demasiado ambiguo (matchea
    # 'no proporcional', 'no se', 'no puedo'). Si querés interpretar 'no' como
    # confirmación corta, hay que hacerlo a nivel de mensaje entero (len<=3).

    if word_count < 8 or any(re.search(p, t) for p in simple_signals):
        return "simple"
    if word_count > 40 or any(re.search(p, t) for p in complex_signals):
        return "complex"
    return "medium"


# Model mapping
MODEL_MAP = {
    # Conversacional: Haiku para frases cortas (qué hora, hola, ok),
    # Sonnet para mensajes medios/largos (prefiere calidad sobre costo
    # cuando el user está realmente conversando).
    ("simple",  "conversational"): "claude-haiku-4-5",
    ("medium",  "conversational"): "claude-sonnet-4-6",
    ("complex", "conversational"): "claude-sonnet-4-6",
    ("simple",  "research"):       "claude-haiku-4-5",
    ("medium",  "research"):       "claude-sonnet-4-6",
    ("complex", "research"):       "claude-sonnet-4-6",
    # Reinsurance: Haiku para queries simples (armar SOQL básico, "cuántos accounts hay").
    # Sonnet para análisis medio (filtros multi-criterio, comparaciones).
    # Opus para análisis cross-object profundo / textos largos / interpretaciones.
    # Las tools sf_consultar y sf_broker_performance encapsulan logica → el LLM
    # gasta menos razonamiento, Haiku alcanza para la mayoria.
    ("simple",  "reinsurance"):    "claude-haiku-4-5",
    ("medium",  "reinsurance"):    "claude-haiku-4-5",
    ("complex", "reinsurance"):    "claude-sonnet-4-6",
    ("simple",  "astrology"):      "claude-sonnet-4-6",
    ("medium",  "astrology"):      "claude-sonnet-4-6",
    ("complex", "astrology"):      "claude-opus-4-5",  # interpretaciones profundas merecen Opus
    ("simple",  "personal"):       "claude-haiku-4-5",
    ("medium",  "personal"):       "claude-haiku-4-5",
    ("complex", "personal"):       "claude-haiku-4-5",
    ("simple",  "coding"):         "claude-sonnet-4-6",
    ("medium",  "coding"):         "claude-sonnet-4-6",
    ("complex", "coding"):         "claude-opus-4-5",
}

def select_model(text: str, intent: str = None) -> str:
    """Elige el modelo óptimo según intent y complejidad."""
    if intent is None:
        intent = classify(text)
    complexity = classify_complexity(text)
    return MODEL_MAP.get((complexity, intent), "claude-haiku-4-5")
