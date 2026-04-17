"""
handlers/kb_handler.py — Comandos de knowledge base de reaseguros.

/kb list       — lista documentos indexados
/kb search X   — busca en la KB
/kb ingest     — indexa un documento enviado
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)


async def cmd_kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args or []
    subcmd = args[0].lower() if args else "help"

    from modules.rag_kb import search, list_sources, build_context

    if subcmd == "list":
        sources = list_sources()
        if not sources:
            await update.message.reply_text("KB vacía. Enviá un PDF con /kb ingest.")
            return
        lines = ["📚 *Knowledge Base — Reaseguros*\n"]
        for s in sources:
            lines.append(f"• `{s['source']}` — {s['chunks']} chunks ({s['updated']})")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif subcmd == "search" and len(args) > 1:
        query = " ".join(args[1:])
        results = search(query, top_k=3)
        if not results:
            await update.message.reply_text("Sin resultados para esa búsqueda.")
            return
        lines = [f"🔍 *Resultados para:* `{query}`\n"]
        for r in results:
            pct = int(r["score"] * 100)
            snippet = r["content"][:200].replace("\n", " ")
            lines.append(f"*{r['source']}* ({pct}%)\n_{snippet}..._\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif subcmd == "context" and len(args) > 1:
        query = " ".join(args[1:])
        ctx = build_context(query)
        if not ctx:
            await update.message.reply_text("No hay contexto disponible para esa query.")
            return
        await update.message.reply_text(ctx[:3000])

    else:
        await update.message.reply_text(
            "Uso:\n"
            "/kb list — documentos indexados\n"
            "/kb search <consulta> — buscar en la KB\n"
            "/kb context <consulta> — ver contexto RAG\n"
            "\nPara indexar: enviá un PDF al bot con el caption `/kb ingest`"
        )
