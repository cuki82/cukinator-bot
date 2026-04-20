"""
services/sf_broker_perf.py — Dashboard de performance de un broker en SF.

Dado un nombre/email/Id de broker (User), calcula:
  - Volumen de pipeline (creadas, ganadas, perdidas, abiertas)
  - Hit ratio (won / closed)
  - Velocidad media de cierre (días entre Created y Close)
  - Diversificación de clientes (Accounts únicas, top 5)
  - Mix por país / industria
  - Pipeline activo (abiertas) y estancadas (>60 días sin movimiento)
  - Distribución mensual de los últimos 12 meses
  - Prima estimada via cruce Opp.AccountId → Contract → IBF__c (advertencia
    de sobre-estimación en el mensaje, no es prima atribuible 1:1 al broker)

Uso:
    from services.sf_broker_perf import resolve_broker, compute, format_dashboard
    user = resolve_broker("Ignacio Romanelli")
    metrics = compute(user["Id"], year=2025)
    print(format_dashboard(user, metrics))
"""
import logging
from datetime import datetime
from typing import Optional

from services.salesforce import sf_query

log = logging.getLogger(__name__)


def resolve_broker(needle: str) -> Optional[dict]:
    """Busca User por nombre, email o Id. Devuelve dict con Id+Name+Email o None."""
    needle = needle.strip()
    if needle.startswith("005") and len(needle) in (15, 18):
        # Parece un User Id
        rows = sf_query(f"SELECT Id, Name, Email, IsActive FROM User WHERE Id = '{needle}'")
        return rows[0] if rows else None
    # Buscar por nombre / email (escape simple para SOQL: solo escapar comillas)
    safe = needle.replace("'", "\\'")
    rows = sf_query(
        f"SELECT Id, Name, Email, IsActive FROM User "
        f"WHERE Name LIKE '%{safe}%' OR Email LIKE '%{safe}%' "
        f"ORDER BY IsActive DESC, Name LIMIT 5"
    )
    return rows[0] if rows else None


def _safe_query(soql: str, default=None):
    """Ejecuta sf_query devolviendo default si falla."""
    try:
        return sf_query(soql, max_records=200)
    except Exception as e:
        log.warning(f"broker_perf query fail: {e}: {soql[:100]}")
        return default if default is not None else []


def compute(broker_id: str, year: Optional[int] = None) -> dict:
    """Calcula todas las métricas para un OwnerId. year=None → todo el histórico."""
    bid = broker_id.replace("'", "")
    yfilter = f" AND CALENDAR_YEAR(CreatedDate) = {year}" if year else ""
    yfilter_close = f" AND CALENDAR_YEAR(CloseDate) = {year}" if year else ""

    metrics: dict = {"broker_id": bid, "year": year}

    # 1. Volumen total
    r = _safe_query(f"SELECT COUNT(Id) c FROM Opportunity WHERE OwnerId = '{bid}'{yfilter}")
    metrics["total_opps"] = r[0]["c"] if r else 0

    # 2. Ganadas / perdidas / abiertas
    r = _safe_query(
        f"SELECT IsClosed, IsWon, COUNT(Id) c FROM Opportunity "
        f"WHERE OwnerId = '{bid}'{yfilter} GROUP BY IsClosed, IsWon"
    )
    won = lost = open_ = 0
    for row in r:
        if not row["IsClosed"]:
            open_ += row["c"]
        elif row["IsWon"]:
            won += row["c"]
        else:
            lost += row["c"]
    metrics["won"]  = won
    metrics["lost"] = lost
    metrics["open"] = open_
    closed = won + lost
    metrics["hit_ratio"] = (won / closed) if closed else None

    # 3. Velocidad media de cierre (won)
    r = _safe_query(
        f"SELECT CreatedDate, CloseDate FROM Opportunity "
        f"WHERE OwnerId = '{bid}'{yfilter} AND IsClosed=true AND IsWon=true "
        f"AND CreatedDate != null AND CloseDate != null"
    )
    deltas = []
    for row in r:
        try:
            cd = datetime.fromisoformat(row["CreatedDate"].replace("Z", "+00:00"))
            cl = datetime.fromisoformat(row["CloseDate"] + "T00:00:00+00:00") if "T" not in row["CloseDate"] else datetime.fromisoformat(row["CloseDate"])
            d = (cl - cd.replace(tzinfo=cl.tzinfo)).days if cl else None
            if d is not None and d >= 0:
                deltas.append(d)
        except Exception:
            continue
    metrics["avg_days_to_close"] = round(sum(deltas) / len(deltas), 1) if deltas else None

    # 4. Diversificación: cuentas únicas
    r = _safe_query(f"SELECT COUNT_DISTINCT(AccountId) c FROM Opportunity WHERE OwnerId = '{bid}'{yfilter}")
    metrics["unique_accounts"] = r[0]["c"] if r else 0

    # 5. Top 5 accounts por # opps
    r = _safe_query(
        f"SELECT Account.Name acct, COUNT(Id) c FROM Opportunity "
        f"WHERE OwnerId = '{bid}'{yfilter} AND AccountId != null "
        f"GROUP BY Account.Name ORDER BY COUNT(Id) DESC LIMIT 5"
    )
    metrics["top_accounts"] = [(x.get("acct") or "(sin nombre)", x.get("c", 0)) for x in r]

    # 6. Mix por país / industria del Account
    r = _safe_query(
        f"SELECT Account.BillingCountry pais, COUNT(Id) c FROM Opportunity "
        f"WHERE OwnerId = '{bid}'{yfilter} AND Account.BillingCountry != null "
        f"GROUP BY Account.BillingCountry ORDER BY COUNT(Id) DESC LIMIT 10"
    )
    metrics["by_country"] = [(x.get("pais"), x.get("c", 0)) for x in r]

    r = _safe_query(
        f"SELECT Account.Industry ind, COUNT(Id) c FROM Opportunity "
        f"WHERE OwnerId = '{bid}'{yfilter} AND Account.Industry != null "
        f"GROUP BY Account.Industry ORDER BY COUNT(Id) DESC LIMIT 5"
    )
    metrics["by_industry"] = [(x.get("ind"), x.get("c", 0)) for x in r]

    # 7. Pipeline activo (top 10 abiertas más viejas)
    # NOTA: SOQL solo permite alias en campos agregados, no en SELECT regulares.
    r = _safe_query(
        f"SELECT Name, StageName, CreatedDate, Account.Name FROM Opportunity "
        f"WHERE OwnerId = '{bid}' AND IsClosed = false "
        f"ORDER BY CreatedDate ASC LIMIT 10"
    )
    metrics["open_oldest"] = [
        {"name": x.get("Name"), "stage": x.get("StageName"),
         "created": (x.get("CreatedDate") or "")[:10],
         "account": ((x.get("Account") or {}).get("Name") if x.get("Account") else "—") or "—"}
        for x in r
    ]

    # 8. Estancadas: abiertas hace >60 días
    r = _safe_query(
        f"SELECT COUNT(Id) c FROM Opportunity "
        f"WHERE OwnerId = '{bid}' AND IsClosed = false AND CreatedDate < LAST_N_DAYS:60"
    )
    metrics["stalled"] = r[0]["c"] if r else 0

    # 9. Distribución mensual últimos 12 meses (created)
    r = _safe_query(
        f"SELECT CALENDAR_YEAR(CreatedDate) y, CALENDAR_MONTH(CreatedDate) m, COUNT(Id) c "
        f"FROM Opportunity WHERE OwnerId = '{bid}' AND CreatedDate = LAST_N_MONTHS:12 "
        f"GROUP BY CALENDAR_YEAR(CreatedDate), CALENDAR_MONTH(CreatedDate) "
        f"ORDER BY CALENDAR_YEAR(CreatedDate), CALENDAR_MONTH(CreatedDate)"
    )
    metrics["monthly"] = [(x.get("y"), x.get("m"), x.get("c", 0)) for x in r]

    # 10. Prima estimada via cruce Account → Contract → IBF
    # Limitación: Salesforce SOQL no soporta subqueries arbitrarias en WHERE IN
    # con multi-nivel. Pero sí podemos hacer: traer AccountIds del broker, después
    # consultar IBF__c con Contrato__r.AccountId IN ese set.
    accts = _safe_query(
        f"SELECT AccountId FROM Opportunity WHERE OwnerId='{bid}'{yfilter} "
        f"AND IsWon=true AND AccountId != null"
    )
    acct_ids = list({x["AccountId"] for x in accts if x.get("AccountId")})
    metrics["accounts_won"] = len(acct_ids)
    if acct_ids and len(acct_ids) <= 200:
        ids_str = ",".join(f"'{a}'" for a in acct_ids[:200])
        prima = _safe_query(
            f"SELECT COUNT(Id) c, SUM(Prima_periodo_100__c) p100, SUM(Prima_cedida__c) pced, "
            f"SUM(Comision_total__c) com FROM IBF__c "
            f"WHERE Contrato__r.AccountId IN ({ids_str})"
        )
        if prima:
            metrics["prima_100_estimated"] = float(prima[0].get("p100") or 0)
            metrics["prima_ced_estimated"] = float(prima[0].get("pced") or 0)
            metrics["comision_estimated"]  = float(prima[0].get("com") or 0)
            metrics["ibf_count_estimated"] = int(prima[0].get("c") or 0)

    return metrics


def format_dashboard(broker: dict, metrics: dict) -> str:
    """Renderea las métricas como markdown para Telegram."""
    name = broker.get("Name", "?")
    email = broker.get("Email", "—")
    active = "✅" if broker.get("IsActive") else "❌ inactivo"
    year = metrics.get("year")
    period = f" — *{year}*" if year else " — *histórico*"

    out = [f"📊 *Performance: {name}*{period}", f"_{email} {active}_", ""]

    out.append("*PIPELINE*")
    tot = metrics.get("total_opps", 0)
    won = metrics.get("won", 0)
    lost = metrics.get("lost", 0)
    op = metrics.get("open", 0)
    out.append(f"• Total opps: *{tot}* · Ganadas: *{won}* · Bajas: *{lost}* · Abiertas: *{op}*")
    hr = metrics.get("hit_ratio")
    if hr is not None:
        out.append(f"• Hit ratio: *{hr*100:.1f}%* ({won}/{won+lost} cerradas)")
    avg = metrics.get("avg_days_to_close")
    if avg is not None:
        out.append(f"• Velocidad media de cierre: *{avg:.0f} días*")
    if metrics.get("stalled", 0):
        out.append(f"• ⚠️ Estancadas (>60 días abiertas): *{metrics['stalled']}*")
    out.append("")

    out.append("*ALCANCE*")
    out.append(f"• Cuentas únicas: *{metrics.get('unique_accounts',0)}*")
    if metrics.get("top_accounts"):
        out.append("• Top clientes:")
        for nm, c in metrics["top_accounts"]:
            out.append(f"   – {nm[:40]} ({c})")
    if metrics.get("by_country"):
        partes = ", ".join(f"{p}({c})" for p, c in metrics["by_country"][:6])
        out.append(f"• Países: {partes}")
    if metrics.get("by_industry"):
        partes = ", ".join(f"{(i or '—')[:25]}({c})" for i, c in metrics["by_industry"][:5])
        out.append(f"• Industrias: {partes}")
    out.append("")

    if metrics.get("monthly"):
        out.append("*MENSUAL (últ 12 meses)*")
        line = " · ".join(f"{int(y)%100:02d}/{int(m):02d}:{c}" for y, m, c in metrics["monthly"])
        out.append(line[:200])
        out.append("")

    if metrics.get("open_oldest"):
        out.append("*PIPELINE ABIERTO (top 5 más viejas)*")
        for o in metrics["open_oldest"][:5]:
            out.append(f"• {o['created']} · {o['name'][:30]} · {o['stage'][:20]} · {o['account'][:20]}")
        out.append("")

    if "prima_100_estimated" in metrics:
        out.append("*PRIMA ASOCIADA (estimada — cruce Account)*")
        out.append(f"• IBFs vinculados: *{metrics['ibf_count_estimated']}* "
                   f"(de {metrics.get('accounts_won',0)} cuentas ganadas)")
        out.append(f"• Prima 100%: *${metrics['prima_100_estimated']:,.0f}*")
        out.append(f"• Prima cedida: *${metrics['prima_ced_estimated']:,.0f}*")
        out.append(f"• Comisiones: *${metrics['comision_estimated']:,.0f}*")
        out.append("_⚠️ Sobreestima: incluye toda la prima de las cuentas ganadas, "
                   "no solo lo que el broker trajo. Para precisión real se necesita "
                   "campo `Broker__c`/`Opportunity__c` en IBF__c._")

    return "\n".join(out)
