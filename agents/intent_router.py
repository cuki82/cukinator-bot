"""
agents/intent_router.py — Clasificador de intenciones. Zero latencia, zero costo.
Clasifica en 6 intents via keywords antes de llegar a cualquier LLM.
"""
import re
import logging

log = logging.getLogger(__name__)

# ── Keywords por intent ────────────────────────────────────────────────────────

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
        # Fallback amplio: si el mensaje menciona el dominio técnico del bot
        # + cualquier verbo de acción/investigación → coding. Mejor falso positivo
        # (va al worker Codex+ClaudeCode, que igual puede solo responder si no
        # hay nada que cambiar) que falso negativo (Haiku que no puede pushear).
        r"\b(?:revis|analiz|investig|propon|suger|fixe|arregl|mejor|mir|inspeccion|edit|cambi|modific|escrib|reescrib|implement|agreg|refactor|fij|corregi|ajust)\w*\b.*\b(?:c[oó]digo|handler|funci[oó]n|archivo|bot|worker|router|m[oó]dulo|endpoint|prompt|config|schema|logic|voice|audio|rag|kb|sistema|arquitectura|pipeline|feature|trace|token|intent|servicio)\b",
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
        r"busca[rn]?", r"investiga[rn]?", r"encontr[aá]", r"qu[eé] dice", r"qu[eé] es",
        r"c[oó]mo funciona", r"explic[aá]", r"compara[rn]?", r"an[aá]liz[aá]",
        r"resumen", r"noticias", r"último[s]?", r"informaci[oó]n sobre",
        r"documentaci[oó]n", r"normativa", r"regulaci[oó]n",
    ],
    "reinsurance": [
        r"reaseguro", r"reinsur", r"p[oó]liza", r"wording", r"cl[aá]usula",
        r"siniestro", r"prima", r"cobertura", r"cedente", r"reasegurador",
        r"treaty", r"facultativo", r"xs[lL]", r"excess of loss", r"quota share",
        r"retenci[oó]n", r"contrato de reaseguro", r"programa",
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

# ── Clasificador ───────────────────────────────────────────────────────────────

def classify(text: str) -> str:
    """
    Clasifica el intent del mensaje. Zero latencia, zero costo.
    Retorna uno de: coding, research, reinsurance, astrology, personal, conversational
    """
    t = text.lower()

    scores = {intent: 0 for intent in _PRIORITY}

    for intent, patterns in _PATTERNS.items():
        if intent not in scores:
            continue
        for p in patterns:
            if re.search(p, t):
                scores[intent] += 1

    # El intent con más matches gana (mínimo 1)
    best = max(scores, key=scores.get)
    result = best if scores[best] > 0 else "conversational"

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
        r"\ben detalle\b", r"\ba fondo\b", r"\bcompleto\b", r"\bexhaustivo\b",
        r"\bm[uú]ltiples\b", r"\btodos los\b", r"\bconsidera[rn]?\b", r"\bprofundiz[aá]\w*",
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
    ("simple",  "conversational"): "claude-haiku-4-5",
    ("medium",  "conversational"): "claude-haiku-4-5",
    ("complex", "conversational"): "claude-sonnet-4-6",
    ("simple",  "research"):       "claude-haiku-4-5",
    ("medium",  "research"):       "claude-sonnet-4-6",
    ("complex", "research"):       "claude-sonnet-4-6",
    ("simple",  "reinsurance"):    "claude-sonnet-4-6",
    ("medium",  "reinsurance"):    "claude-sonnet-4-6",
    ("complex", "reinsurance"):    "claude-opus-4-5",
    ("simple",  "astrology"):      "claude-sonnet-4-6",
    ("medium",  "astrology"):      "claude-sonnet-4-6",
    ("complex", "astrology"):      "claude-sonnet-4-6",
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
