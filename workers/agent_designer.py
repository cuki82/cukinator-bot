"""
workers/agent_designer.py — Agent Designer (FastAPI service :3340).

Recibe pedidos de diseño del bot y genera assets corporativos:
  - HTML/CSS/Tailwind (landings, dashboards, componentes)
  - PDFs con branding (brochures, reportes, propuestas)
  - PPTX (presentaciones con layouts corporativos)
  - Critique (revisar diseño existente: usabilidad, a11y, consistencia)

RAG:
  <tenant>.kb_documents namespace 'brand' con el manual de identidad
  (logos, paleta, tipografías, plantillas). Si hay material ingestado,
  todo output lo aplica.

No genera imágenes raster propias — para eso hay que integrar DALL-E 3
o Stable Diffusion aparte (tool image_gen pendiente).

Arrancar con:
    uvicorn workers.agent_designer:app --host 0.0.0.0 --port 3340
"""
import os
import sys
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Optional, List

# Cargar vault
try:
    sys.path.insert(0, os.environ.get("REPO_PATH", "/home/cukibot/cukinator-bot"))
    from services.vault import load_all_to_env
    load_all_to_env()
except Exception:
    pass

import anthropic
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Cukinator Agent Designer")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
DESIGNER_SECRET = os.environ.get("DESIGNER_SECRET") or os.environ.get("WORKER_SECRET", "cuki-designer-secret")
DEFAULT_MODEL = os.environ.get("DESIGNER_MODEL", "claude-sonnet-4-6")
OUTPUT_DIR = Path(os.environ.get("DESIGNER_OUTPUT", "/tmp/designer"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

log.info(
    "designer boot: ANTHROPIC_KEY=%s · DESIGNER_SECRET=%s · default_model=%s",
    "set" if ANTHROPIC_KEY else "MISSING",
    "set" if DESIGNER_SECRET else "MISSING",
    DEFAULT_MODEL,
)


# ── Models ────────────────────────────────────────────────────────────────

class DesignTask(BaseModel):
    task_id: str
    type: str              # 'html' | 'pdf' | 'pptx' | 'critique'
    tenant: str = "reamerica"
    chat_id: Optional[int] = None
    brief: str             # qué hay que diseñar
    target: Optional[str] = None      # audiencia / destino (ej. 'cliente', 'interno')
    reference: Optional[str] = None   # para critique: el HTML/texto a revisar
    model: Optional[str] = None       # override default


class DesignResult(BaseModel):
    task_id: str
    status: str            # 'ok' | 'error'
    type: str
    summary: str
    output_text: Optional[str] = None    # HTML / markdown / critique result
    output_file: Optional[str] = None    # path al PDF/PPTX generado
    brand_chunks_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_s: float = 0
    errors: list = []


# ── Brand context via RAG ─────────────────────────────────────────────────

def _fetch_brand_context(tenant: str, brief: str, top_k: int = 5) -> str:
    """Busca chunks del manual de identidad del tenant. Namespace 'brand'."""
    try:
        from modules.rag_kb import build_context
        return build_context(
            query=f"identidad visual branding {brief}",
            top_k=top_k, namespace="brand",
            tenant=tenant, with_citations=False,
        ) or ""
    except Exception as e:
        log.debug(f"brand rag skip: {e}")
        return ""


# ── Generators por tipo ───────────────────────────────────────────────────

HTML_SYSTEM = """Sos un diseñador web senior. Generás HTML semántico + Tailwind CSS.

Reglas:
- HTML5 válido, responsive mobile-first, accesible WCAG 2.1 AA.
- Tailwind v3 utility-first. Sin CSS custom salvo cuando sea necesario.
- Jerarquía visual clara, contraste suficiente, espaciado consistente.
- Tipografía: sans-serif moderna (Inter / Geist / system-ui).
- Si te paso un manual de identidad, APLICALO estrictamente: colores exactos
  (hex), tipografías, proporciones, tono. El diseño debe verse del tenant.
- Output SOLO el HTML completo (<!DOCTYPE html>...<html>...). Sin comentarios
  ni explicaciones fuera del código. Una sola respuesta con el archivo listo."""

PDF_SYSTEM = """Sos un diseñador de presentaciones/PDFs corporativos. Tu output
es un plan estructurado que luego se renderea con fpdf2.

Formato de respuesta (JSON estricto, sin texto fuera):
{
  "title": "...",
  "subtitle": "...",
  "brand": {"primary_color": "#hex", "secondary_color": "#hex", "font": "Helvetica"},
  "pages": [
    {"heading": "...", "body": "markdown con **bold** y bullets", "layout": "cover|text|two_col|quote"}
  ]
}

Si te paso un manual de identidad, usá SUS colores y tipografías.
Tono directo, profesional, en español argentino."""

CRITIQUE_SYSTEM = """Sos un crítico de diseño senior. Revisás HTML/mockups y
devolvés feedback accionable.

Estructura:
1. 🎯 Jerarquía visual — qué destaca, qué pierde el foco
2. 🎨 Consistencia de marca — alineación con el manual de identidad (si aplica)
3. ♿ Accesibilidad WCAG 2.1 AA — contraste, focus, aria, semántica
4. ✍️ UX writing — claridad, tono, microcopy
5. 📐 Layout y espaciado — grid, ritmo, respiración
6. 🔧 Acciones concretas — lista priorizada de cambios específicos

Directo, sin rodeos. No elogies por elogiar. Si algo está bien, decílo corto."""


def _run_claude(system: str, user: str, model: str) -> dict:
    """Llamada simple a Claude. Retorna dict con text, usage."""
    if not client:
        return {"error": "ANTHROPIC_KEY no configurado"}
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text += block.text
        usage = getattr(resp, "usage", None)
        return {
            "text": text,
            "tokens_in":  getattr(usage, "input_tokens", 0) or 0 if usage else 0,
            "tokens_out": getattr(usage, "output_tokens", 0) or 0 if usage else 0,
        }
    except Exception as e:
        return {"error": str(e)}


def _gen_html(task: DesignTask, brand_ctx: str) -> dict:
    user = f"Brief: {task.brief}\n"
    if task.target:
        user += f"Audiencia/destino: {task.target}\n"
    if brand_ctx:
        user += f"\nManual de identidad del tenant:\n{brand_ctx}\n"
    user += "\nDevolvé el HTML completo."
    return _run_claude(HTML_SYSTEM, user, task.model or DEFAULT_MODEL)


def _gen_pdf_plan(task: DesignTask, brand_ctx: str) -> dict:
    user = f"Brief: {task.brief}\n"
    if task.target:
        user += f"Audiencia/destino: {task.target}\n"
    if brand_ctx:
        user += f"\nManual de identidad:\n{brand_ctx}\n"
    user += "\nDevolvé SOLO el JSON del plan del PDF."
    return _run_claude(PDF_SYSTEM, user, task.model or DEFAULT_MODEL)


def _render_pdf_from_plan(plan: dict, output_path: Path) -> None:
    """Usa fpdf2 para renderear el plan JSON al path dado."""
    from fpdf import FPDF
    brand = plan.get("brand", {})
    primary = brand.get("primary_color", "#1e3a5f").lstrip("#")
    try:
        pr, pg, pb = int(primary[:2], 16), int(primary[2:4], 16), int(primary[4:6], 16)
    except Exception:
        pr, pg, pb = 30, 58, 95

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    for page in plan.get("pages", []):
        layout = page.get("layout", "text")
        pdf.add_page()
        if layout == "cover":
            pdf.set_fill_color(pr, pg, pb)
            pdf.rect(0, 0, 210, 297, "F")
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 24)
            pdf.set_y(100)
            pdf.multi_cell(0, 10, (page.get("heading") or plan.get("title") or "").encode("latin-1", errors="replace").decode("latin-1"))
            pdf.set_font("Helvetica", "", 14)
            pdf.multi_cell(0, 8, (page.get("body") or plan.get("subtitle") or "").encode("latin-1", errors="replace").decode("latin-1"))
        else:
            pdf.set_text_color(pr, pg, pb)
            pdf.set_font("Helvetica", "B", 16)
            heading = (page.get("heading") or "").encode("latin-1", errors="replace").decode("latin-1")
            pdf.cell(0, 10, heading, ln=1)
            pdf.ln(2)
            pdf.set_text_color(30, 30, 30)
            pdf.set_font("Helvetica", "", 11)
            body = (page.get("body") or "").encode("latin-1", errors="replace").decode("latin-1")
            for line in body.split("\n"):
                pdf.multi_cell(0, 6, line)
    pdf.output(str(output_path))


def _gen_critique(task: DesignTask, brand_ctx: str) -> dict:
    if not task.reference:
        return {"error": "critique requiere 'reference' con el HTML/texto a revisar"}
    user = f"Diseño a revisar:\n```\n{task.reference[:12000]}\n```\n\nBrief original: {task.brief}\n"
    if brand_ctx:
        user += f"\nManual de identidad:\n{brand_ctx}\n"
    return _run_claude(CRITIQUE_SYSTEM, user, task.model or DEFAULT_MODEL)


# ── FastAPI endpoints ─────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "anthropic": bool(ANTHROPIC_KEY),
        "default_model": DEFAULT_MODEL,
        "output_dir": str(OUTPUT_DIR),
        "types_supported": ["html", "pdf", "pptx", "critique"],
    }


@app.post("/design", response_model=DesignResult)
def design(task: DesignTask, x_designer_secret: str = Header(None)):
    if x_designer_secret != DESIGNER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    start = time.time()
    errors: list = []
    brand_ctx = _fetch_brand_context(task.tenant, task.brief)
    brand_chunks = brand_ctx.count("[C") if brand_ctx else 0

    out_text = None
    out_file = None
    tokens_in = 0
    tokens_out = 0

    try:
        if task.type == "html":
            r = _gen_html(task, brand_ctx)
            if "error" in r:
                errors.append(r["error"])
            else:
                out_text = r["text"]
                tokens_in = r["tokens_in"]
                tokens_out = r["tokens_out"]
                # Guardar también como archivo
                out_file = str(OUTPUT_DIR / f"{task.task_id}.html")
                Path(out_file).write_text(out_text, encoding="utf-8")

        elif task.type == "pdf":
            r = _gen_pdf_plan(task, brand_ctx)
            if "error" in r:
                errors.append(r["error"])
            else:
                tokens_in = r["tokens_in"]
                tokens_out = r["tokens_out"]
                try:
                    plan = json.loads(r["text"].strip().strip("`").replace("json\n", "", 1))
                    out_file = str(OUTPUT_DIR / f"{task.task_id}.pdf")
                    _render_pdf_from_plan(plan, Path(out_file))
                    out_text = json.dumps(plan, indent=2, ensure_ascii=False)
                except Exception as ex:
                    errors.append(f"pdf render error: {ex}")
                    out_text = r["text"]  # devolver texto raw como fallback

        elif task.type == "critique":
            r = _gen_critique(task, brand_ctx)
            if "error" in r:
                errors.append(r["error"])
            else:
                out_text = r["text"]
                tokens_in = r["tokens_in"]
                tokens_out = r["tokens_out"]

        elif task.type == "pptx":
            errors.append("pptx generation pending (agregar python-pptx + plan generator)")

        else:
            errors.append(f"type desconocido: {task.type}")

    except Exception as e:
        errors.append(str(e))

    summary = f"designer {task.type} · {len(out_text or '')} chars"
    if out_file:
        summary += f" · file={Path(out_file).name}"

    return DesignResult(
        task_id=task.task_id,
        status="ok" if not errors else ("partial" if (out_text or out_file) else "error"),
        type=task.type,
        summary=summary,
        output_text=out_text,
        output_file=out_file,
        brand_chunks_used=brand_chunks,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_s=round(time.time() - start, 1),
        errors=errors,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3340, log_level="info")
