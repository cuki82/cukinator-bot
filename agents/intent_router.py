"""
agents/intent_router.py — Clasificador de intenciones v2.
4 capas: keyword regex → embeddings → LLM con contexto → fallback.
"""
import os
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
        r"reinici[aá][rn]? (?:el )?(?:bot|servicio|worker|container)",
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


# ── Embeddings classifier (Layer B) ─────────────────────────────────────────────
# Ejemplos canónicos por intent. Editar acá para cubrir nuevos casos sin
# tocar regex. Cada ejemplo se embedea con text-embedding-3-small via LiteLLM
# y se cachea en memoria.

INTENT_EXAMPLES: dict[str, list[str]] = {
    "coding": [
        "agregá un endpoint /ping al server",
        "modificá el bot_core para que acepte X",
        "fijate por qué el worker está fallando",
        "configurá el bot en el grupo Humanos vs Bots",
        "deployá el código nuevo al VPS",
        "hacé un commit y push de los cambios",
        "investigá por qué el handler no responde",
        "refactorizá el módulo de astrología",
        "fixea el bug de logout",
        "instalá ffmpeg en el VPS",
        "reiniciá el servicio del worker",
        "qué pasa con el deploy",
        "mostrame los logs del bot",
        "abrí un PR con los cambios",
        "agregá la tool nueva al worker",
    ],
    "reinsurance": [
        "cuántas oportunidades tiene Ignacio este mes",
        "armame el dashboard de brokers de Reamerica",
        "qué dice la cláusula XYZ del wording",
        "cuál es la prima emitida total de junio",
        "listame los IBF activos del cliente",
        "buscá la póliza del contrato 1234",
        "cuántas cotizaciones colocadas hay",
        "explicame quota share vs excess of loss",
        "qué cubre el treaty de catástrofe",
        "performance del broker Martin Romanelli",
    ],
    "astrology": [
        "calculame la carta natal de Lara",
        "qué tránsitos tengo esta semana",
        "armame el retorno solar de este año",
        "cuál es mi luna y mi ascendente",
        "qué casa tiene Marte en mi natal",
        "explicame la conjunción Venus-Júpiter",
    ],
    "personal": [
        "acordate que mi cumpleaños es el 12 de marzo",
        "guardá que prefiero respuestas cortas",
        "qué te dije la semana pasada sobre el viaje",
        "cuál es el email de mi hermano",
        "anotá que tengo reunión el martes",
    ],
    "research": [
        "buscá noticias sobre LATAM Airlines",
        "qué dice el último decreto sobre seguros",
        "investigá la regulación de fintech en Argentina",
        "resumime el paper de embeddings de OpenAI",
        "compará GPT-5 con Claude Opus",
    ],
    "conversational": [
        "hola, qué onda",
        "qué hora es",
        "cómo está el clima",
        "gracias",
        "dale, listo",
        "cómo te llamás",
        "armame un PPT con la propuesta",
        "diseñame un flyer corporativo",
    ],
}

# Cache de embeddings por intent (lazy, lifetime del proceso)
_EMB_CACHE: dict[str, list] = {}     # intent → list[Float32Array]
_EMB_DIM = 1536
_EMB_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
_LITELLM_URL = os.environ.get("LITELLM_URL", "http://172.17.0.1:4000")
_LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "")


def _embed_via_litellm(texts: list[str]):
    """Devuelve list[list[float]]. Vacío si falla."""
    import urllib.request, json as _json
    if not texts:
        return []
    try:
        req = urllib.request.Request(
            f"{_LITELLM_URL}/v1/embeddings",
            data=_json.dumps({"model": _EMB_MODEL, "input": texts}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_LITELLM_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = _json.loads(r.read())
        return [item["embedding"] for item in d.get("data", [])]
    except Exception as e:
        log.debug(f"embed via litellm fail: {e}")
        return []


def _ensure_examples_embedded():
    """Idempotente — embedea los ejemplos canónicos en _EMB_CACHE si no están."""
    if _EMB_CACHE:
        return
    for intent, examples in INTENT_EXAMPLES.items():
        vecs = _embed_via_litellm(examples)
        if vecs and len(vecs) == len(examples):
            _EMB_CACHE[intent] = vecs


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _classify_with_embeddings(text: str) -> tuple[str, float, dict]:
    """Layer B: nearest-neighbor con embeddings.
    Returns (intent, confidence, scores_per_intent).
    confidence = top1_score - top2_score (margin); usar para decidir si escalar.
    """
    _ensure_examples_embedded()
    if not _EMB_CACHE:
        return "conversational", 0.0, {}
    qvecs = _embed_via_litellm([text[:2000]])
    if not qvecs:
        return "conversational", 0.0, {}
    qvec = qvecs[0]
    scores: dict[str, float] = {}
    for intent, vecs in _EMB_CACHE.items():
        sims = sorted((_cosine(qvec, v) for v in vecs), reverse=True)
        # promedio top-3 más similares — más estable que el top1
        topk = sims[:3] or [0.0]
        scores[intent] = sum(topk) / len(topk)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top1, score1 = ranked[0]
    score2 = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score1 - score2
    return top1, margin, scores


# ── LLM v2 con contexto (Layer C) ───────────────────────────────────────────────

def _get_recent_history(chat_id, n=3) -> list[dict]:
    """Lee los últimos N pares (user/assistant) del chat para dar contexto al LLM."""
    if not chat_id:
        return []
    try:
        import sqlite3
        db_path = os.environ.get("DB_PATH", os.path.expanduser("~/data/memory.db"))
        con = sqlite3.connect(db_path, timeout=5)
        rows = con.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, n * 2),
        ).fetchall()
        con.close()
        # Reverso para orden cronológico
        return [{"role": r, "content": c[:600]} for r, c in reversed(rows)]
    except Exception as e:
        log.debug(f"_get_recent_history fail: {e}")
        return []


def _classify_with_llm_v2(text: str, chat_id=None) -> tuple[str, float]:
    """Layer C: Haiku con últimos 3 turnos + structured-ish output.
    Returns (intent, confidence_0_to_1)."""
    try:
        import os as _os
        import anthropic
        key = _os.environ.get("ANTHROPIC_KEY") or _os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return "conversational", 0.0
        client = anthropic.Anthropic(api_key=key, timeout=10.0)
        history = _get_recent_history(chat_id, n=3)
        history_text = ""
        if history:
            lines = ["── HISTORIAL RECIENTE ──"]
            for h in history:
                role = "USER" if h["role"] == "user" else "BOT"
                lines.append(f"[{role}] {h['content']}")
            lines.append("── FIN HISTORIAL ──\n")
            history_text = "\n".join(lines)

        system = (
            "Sos un clasificador de intent para un bot de Telegram. "
            "Devolvés SOLO un JSON con dos campos: intent y confidence.\n\n"
            "Intents disponibles:\n"
            "- coding: pide modificar código/bot/handler/tool, deploy, push, refactor, debug, devops\n"
            "- reinsurance: pregunta CRM Salesforce, accounts, oportunidades, primas, brokers, IBF, Reamerica\n"
            "- astrology: carta natal, tránsitos, planetas, signos, retornos, ascendente\n"
            "- personal: recordar/guardar preferencias, historial, datos del usuario\n"
            "- research: buscar/investigar/resumir info externa, web, noticias, papers\n"
            "- conversational: charla general, saludos, confirmaciones, clima, hora, diseño, PPT\n\n"
            "REGLAS:\n"
            "1. Si el HISTORIAL muestra que el bot acaba de hacer una pregunta confirmable "
            "y el USER actual es una confirmación corta (sí/dale/configuralo/mandásela), "
            "el intent es el que la pregunta del bot estaba proponiendo (típicamente coding).\n"
            "2. confidence es 0-1: 1 = clarísimo, 0.5 = dudoso, <0.4 = NO sé (devolvé conversational).\n\n"
            "Formato de respuesta (JSON estricto, sin texto extra):\n"
            '{"intent": "coding", "confidence": 0.92}'
        )

        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            system=system,
            messages=[{
                "role": "user",
                "content": f"{history_text}\n[USER ACTUAL] {text[:800]}",
            }],
        )
        raw = ""
        for block in resp.content:
            if hasattr(block, "text"):
                raw += block.text
        raw = raw.strip()
        # Parsear JSON tolerante
        import json as _json, re as _re
        m = _re.search(r"\{[^}]+\}", raw)
        if not m:
            return "conversational", 0.0
        data = _json.loads(m.group(0))
        intent = str(data.get("intent", "conversational")).strip().lower()
        conf = float(data.get("confidence", 0.5))
        valid = {"coding", "reinsurance", "astrology", "personal", "research", "conversational"}
        if intent not in valid:
            return "conversational", 0.0
        log.info(f"llm_v2: {intent!r} conf={conf:.2f} for {text[:60]!r}")
        return intent, conf
    except Exception as e:
        log.debug(f"llm_v2 fail: {e}")
        return "conversational", 0.0


# ── Clasificador unificado (todas las capas) ────────────────────────────────────

def classify(text: str, use_llm_fallback: bool = True, chat_id=None) -> str:
    """Intent Router v2 — pipeline de 4 capas (Layer 0 vive en handle_message):
       Layer 1: regex keyword + design override (instantáneo)
       Layer 2: embeddings nearest-neighbor (~50ms, ~$0.00001)
       Layer 3: LLM Haiku con contexto del chat (~1s, ~$0.001)
    Cada capa loguea su decisión en intent_log para diagnóstico (Layer D).
    """
    import time as _t
    t0 = _t.time()
    t = text.lower()

    # ── Layer 1.A: Override de diseño ──
    for pat in _DESIGN_INTENT_PATTERNS:
        if re.search(pat, t):
            _log_layer(chat_id, text, "conversational", "design_override",
                       confidence=1.0, ms=int((_t.time()-t0)*1000),
                       meta={"matched_pattern": pat[:40]})
            return "conversational"

    # ── Layer 1.B: Keyword regex con scoring ──
    scores = {intent: 0 for intent in _PRIORITY}
    for intent, patterns in _PATTERNS.items():
        if intent not in scores:
            continue
        for p in patterns:
            if re.search(p, t):
                scores[intent] += 1
    best = max(scores, key=scores.get)
    keyword_result = best if scores[best] > 0 else "conversational"
    keyword_score = scores[best]

    # Si el regex dio un intent específico con alta confianza, return directo.
    # Específicos = reinsurance/astrology/personal con ≥2 matches.
    if keyword_result in ("reinsurance", "astrology", "personal") and keyword_score >= 2:
        _log_layer(chat_id, text, keyword_result, "keyword_strong",
                   confidence=min(1.0, keyword_score / 3),
                   ms=int((_t.time()-t0)*1000), meta={"score": keyword_score})
        return keyword_result

    # Texto muy corto → confiar en keyword o conversational, sin gastar APIs.
    if len(text.strip()) < 15:
        _log_layer(chat_id, text, keyword_result, "keyword_short",
                   confidence=0.6 if keyword_score > 0 else 0.4,
                   ms=int((_t.time()-t0)*1000), meta={"score": keyword_score})
        return keyword_result

    # ── Layer 2: Embeddings ──
    if use_llm_fallback:
        emb_intent, emb_margin, emb_scores = _classify_with_embeddings(text)
        # margin > 0.05 = decisión clara (top1 separa bien del top2)
        if emb_margin >= 0.05:
            # Si keyword y embedding coinciden → muy confiable
            if emb_intent == keyword_result and keyword_score > 0:
                _log_layer(chat_id, text, emb_intent, "embed_consensus",
                           confidence=min(1.0, 0.7 + emb_margin),
                           ms=int((_t.time()-t0)*1000),
                           meta={"margin": round(emb_margin, 4), "kw": keyword_result})
                return emb_intent
            # Si discrepan pero embedding tiene buen margin, embedding gana
            _log_layer(chat_id, text, emb_intent, "embed_wins",
                       confidence=min(1.0, 0.6 + emb_margin),
                       ms=int((_t.time()-t0)*1000),
                       meta={"margin": round(emb_margin, 4), "kw": keyword_result})
            return emb_intent

        # ── Layer 3: LLM con contexto si embeddings no se decide claramente ──
        llm_intent, llm_conf = _classify_with_llm_v2(text, chat_id=chat_id)
        if llm_conf >= 0.4:
            _log_layer(chat_id, text, llm_intent, "llm_v2",
                       confidence=llm_conf, ms=int((_t.time()-t0)*1000),
                       meta={"emb_top": emb_intent, "emb_margin": round(emb_margin, 4),
                             "kw": keyword_result})
            return llm_intent

    # Fallback final: lo que dijo regex (o conversational)
    _log_layer(chat_id, text, keyword_result, "fallback",
               confidence=0.4, ms=int((_t.time()-t0)*1000),
               meta={"score": keyword_score})
    return keyword_result


def _log_layer(chat_id, text, intent, layer, confidence, ms, meta=None):
    """Telemetría centralizada — Layer D."""
    log.info(f"intent[{layer}] → {intent!r} conf={confidence:.2f} ({ms}ms) for {text[:50]!r}")
    try:
        from services.intent_state import log_classification
        log_classification(chat_id, text, intent, layer=layer,
                           confidence=confidence, duration_ms=ms,
                           metadata=meta)
    except Exception:
        pass


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
