"""
Suite de regression tests para Intent Router v2.

Cubre:
- Layer 0 (pending state): confirmaciones cortas heredan intent del turno previo
- Layer 1 (regex + design override): casos clásicos
- Casos reales que fallaron en producción

Ejecutar:
    cd cukinator-bot && python -m pytest tests/test_intent_router_v2.py -v
o sin pytest:
    cd cukinator-bot && python tests/test_intent_router_v2.py
"""
from __future__ import annotations
import os
import sys
import tempfile

# Path setup — permite ejecutar desde tests/ o desde root del repo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# DB temporal aislada para no contaminar la real
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB

# Master key dummy para vault si lo importa
os.environ.setdefault("MASTER_KEY", "test-master-key-not-real")

from agents.intent_router import classify
from services.intent_state import (
    remember_pending, get_pending, clear_pending,
    is_short_confirmation, resolve_with_pending,
    extract_pending_tag, log_classification,
)


# ─── Helpers ──────────────────────────────────────────────────────────

_failures: list[str] = []
_passed = 0


def check(name: str, condition: bool, detail: str = ""):
    global _passed
    if condition:
        _passed += 1
        print(f"  [OK]{name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  [FAIL]{name} — {detail}")


def case_intent(text: str, expected: str, use_llm=False, note: str = ""):
    actual = classify(text, use_llm_fallback=use_llm)
    check(f"intent: {text[:50]!r} ->{expected}", actual == expected,
          f"got {actual} (expected {expected}). {note}")


# ─── Layer 0 — pending state ──────────────────────────────────────────

def test_pending_state():
    print("\n[Layer 0] pending state + confirmation detector")
    chat_id = -999999999  # chat ficticio

    clear_pending(chat_id)

    # Sin pending ->resolve devuelve None
    check("no pending: resolve_with_pending('si') ->None",
          resolve_with_pending(chat_id, "si") is None)

    # Con pending y confirmación corta ->resuelve
    remember_pending(chat_id, "coding", "agregá un endpoint /ping al bot")
    p = resolve_with_pending(chat_id, "si dale")
    check("con pending: 'si dale' ->resuelve coding",
          p is not None and p.intent == "coding",
          f"got {p}")
    check("con pending: 'si dale' ->action correcta",
          p is not None and "endpoint /ping" in p.action,
          f"got action={p.action if p else None!r}")

    # resolve_with_pending no debería borrar (eso lo hace el caller)
    p2 = get_pending(chat_id)
    # Después de mi diseño actual, get_pending todavía debería devolver el pending
    # hasta que el caller llame clear_pending explícitamente
    # (resolve_with_pending solo lo identifica)
    clear_pending(chat_id)

    # Sin confirmación corta ->no resuelve aunque haya pending
    remember_pending(chat_id, "coding", "agregá un endpoint")
    p3 = resolve_with_pending(chat_id, "no, mejor armemos otra cosa primero")
    check("con pending pero msg largo no-confirmación ->None",
          p3 is None)
    clear_pending(chat_id)


def test_short_confirmation_detector():
    print("\n[Layer 0] is_short_confirmation()")
    positives = ["si", "sí", "ok", "dale", "listo", "perfecto", "mandásela",
                 "configuralo", "sí, dale", "dale hacelo", "go"]
    for p in positives:
        check(f"confirmación: {p!r} ->True", is_short_confirmation(p))
    negatives = [
        "agregá un endpoint nuevo al bot",
        "calculá la carta natal de Lara",
        "no quiero hacer eso",
        "buscá noticias sobre LATAM",
        "hola, ¿cómo estás?",
    ]
    for n in negatives:
        check(f"NO confirmación: {n!r} ->False",
              not is_short_confirmation(n))


def test_pending_tag_extraction():
    print("\n[Layer 0] extract_pending_tag()")
    cases = [
        ("¿Lo armo? [PENDING:coding:agregá endpoint /ping]",
         "¿Lo armo?",
         ("coding", "agregá endpoint /ping")),
        ("Te paso la carta. Sin tag.",
         "Te paso la carta. Sin tag.",
         None),
        ("Ok dale [PENDING:reinsurance:listame brokers de Junio]",
         "Ok dale",
         ("reinsurance", "listame brokers de Junio")),
    ]
    for raw, expected_clean, expected_tag in cases:
        clean, tag = extract_pending_tag(raw)
        check(f"tag-extract clean: {raw[:40]!r}",
              clean.strip() == expected_clean.strip(),
              f"got {clean!r}")
        if expected_tag is None:
            check(f"tag-extract no-tag: {raw[:40]!r}", tag is None)
        else:
            check(f"tag-extract has-tag: {raw[:40]!r}",
                  tag == expected_tag, f"got {tag}")


# ─── Layer 1 — regex + design override ─────────────────────────────────

def test_layer1_regex():
    print("\n[Layer 1] regex sin LLM fallback (use_llm=False)")
    # Coding: bug crítico que originó el rework
    case_intent("Agregá un endpoint /ping al bot que devuelva pong", "coding")
    case_intent("modificá bot_core para que acepte X", "coding")
    case_intent("fijate por qué el worker está fallando",  "coding")
    case_intent("hacé un commit y push", "coding")
    case_intent("reiniciá el servicio cukinator-worker", "coding")

    # Astrology
    case_intent("calcular mi carta natal", "astrology")
    case_intent("qué tránsitos tengo este mes", "astrology")
    case_intent("conjunción venus marte", "astrology")

    # Reinsurance
    case_intent("cuánta prima emitida hay este mes", "reinsurance")
    case_intent("listame los IBF de Junio", "reinsurance")

    # Diseño override ->conversational (no coding)
    case_intent("armame un PPT corporativo con la propuesta", "conversational",
                note="design override debería ganar sobre 'arquitectura/stack'")
    case_intent("generá un PDF para cliente reaseguros", "conversational")

    # Conversational
    case_intent("hola, qué onda", "conversational")
    case_intent("qué hora es", "conversational")
    case_intent("dale", "conversational",
                note="confirmación suelta sin pending")


# ─── Telemetry ─────────────────────────────────────────────────────────

def test_telemetry():
    print("\n[Layer D] log_classification escribe a intent_log")
    log_classification(-999, "test telemetry msg", "coding",
                       layer="rule", confidence=0.9,
                       duration_ms=12, metadata={"test": True})
    # Verificar lectura
    import sqlite3
    con = sqlite3.connect(_TMP_DB)
    rows = con.execute(
        "SELECT chat_id, user_text, intent, layer, confidence FROM intent_log WHERE chat_id=?",
        (-999,),
    ).fetchall()
    con.close()
    check("telemetry: row escrita", len(rows) >= 1)
    if rows:
        cid, txt, intent, layer, conf = rows[-1]
        check("telemetry: intent guardado", intent == "coding")
        check("telemetry: layer guardado", layer == "rule")


# ─── Ejecución ─────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Intent Router v2 — regression tests")
    print(f"DB temporal: {_TMP_DB}")
    print("=" * 70)

    test_pending_state()
    test_short_confirmation_detector()
    test_pending_tag_extraction()
    test_layer1_regex()
    test_telemetry()

    print("\n" + "=" * 70)
    if _failures:
        print(f"FAIL: {len(_failures)} fallaron, {_passed} pasaron")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"OK — {_passed} tests pasaron")


if __name__ == "__main__":
    main()
