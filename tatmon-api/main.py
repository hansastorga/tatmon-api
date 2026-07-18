import os, time, io, smtplib, requests
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.platypus import PageBreak
from reportlab.graphics.shapes import Drawing, Rect, String

app = Flask(__name__)
CORS(app)

MGR_BASE = "https://api.mygadgetrepairs.com/v1"

TIENDAS_CONFIG = {
    "Tatmon La Villa":       os.environ.get("MGR_API_KEY_LAVILLA", ""),
    "Tatmon Kalú":           os.environ.get("MGR_API_KEY_KALU", ""),
    "Tatmon Cayalá":         os.environ.get("MGR_API_KEY_CAYALA", ""),
    "Tatmon Quiché":         os.environ.get("MGR_API_KEY_QUICHE", ""),
    "Tatmon Quetzaltenango": os.environ.get("MGR_API_KEY_XELA", ""),
}

# Calendario de operación por tienda. weekday(): Lunes=0 ... Domingo=6
# Kalú y La Villa no aperturan los domingos.
TIENDA_CALENDARIO = {
    "Tatmon La Villa":       {"cerrado_dias": [6]},
    "Tatmon Kalú":           {"cerrado_dias": [6]},
    "Tatmon Cayalá":         {"cerrado_dias": []},
    "Tatmon Quiché":         {"cerrado_dias": []},
    "Tatmon Quetzaltenango": {"cerrado_dias": []},
}

def tienda_cerrada(nombre_tienda, fecha_str):
    """True si la tienda no opera ese día según TIENDA_CALENDARIO. fecha_str: 'YYYY-MM-DD'."""
    try:
        fecha = date.fromisoformat(fecha_str)
    except (TypeError, ValueError):
        return False
    dias_cerrados = TIENDA_CALENDARIO.get(nombre_tienda, {}).get("cerrado_dias", [])
    return fecha.weekday() in dias_cerrados

TZ_GT = ZoneInfo("America/Guatemala")

def hoy_gt():
    """Fecha actual en horario de Guatemala. Railway corre el contenedor en UTC,
    así que date.today() a las 9pm GT (03:00 UTC) ya devuelve el día siguiente.
    El cron nocturno de GitHub Actions llama /reporte/enviar sin ?fecha=, por lo
    que depende de esta función para calcular "hoy" correctamente."""
    return datetime.now(TZ_GT).date()

VENTAS_TIPOS = {"venta de equipos.", "compra", "venta", "reserva"}
EST_TERM = {"Completado", "Finalizado / Entregado", "Facturado"}
EST_ACT  = {
    "Nuevo", "En progreso", "En espera de autorización del cliente",
    "Cliente ha autorizado la reparación", "Repuesto Local",
    "Paquete del interior", "Esta en taller Ajeno", "Recolección"
}

DIAS_VENTANA = int(os.environ.get("DIAS_VENTANA", "30"))
_cache     = {"data": None, "ts": 0}
_cache_all = {"data": None, "ts": 0}
CACHE_TTL  = 3600

EMAIL_USER          = os.environ.get("EMAIL_USER", "")
EMAIL_APP_PASSWORD  = os.environ.get("EMAIL_APP_PASSWORD", "")
REPORT_RECIPIENTS   = [r.strip() for r in os.environ.get("REPORT_RECIPIENTS", "").split(",") if r.strip()]
REPORT_SECRET       = os.environ.get("REPORT_SECRET", "")

AZUL    = colors.HexColor("#00A7E1")
NARANJA = colors.HexColor("#FF6B00")
NEGRO   = colors.HexColor("#1A1A1A")
GRIS    = colors.HexColor("#F2F2F2")
VERDE   = colors.HexColor("#1E8E3E")
ROJO    = colors.HexColor("#D93025")
AMARILLO   = colors.HexColor("#FFF8E1")
AZUL_CLARO = colors.HexColor("#E3F4FB")
VERDE_CLARO = colors.HexColor("#E8F5E9")
GRIS_CERRADO = colors.HexColor("#888888")

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.jpg")

def get_tecnico_nombre(ticket):
    tec   = ticket.get("technician") or {}
    email = (tec.get("email") or "").lower().strip()
    fname = (tec.get("first_name") or "").strip()
    lname = (tec.get("last_name") or "").strip()
    full  = f"{fname} {lname}".strip()
    tienda_kw = ["cayalá","cayala","kalú","kalu","villa","quiché","quiche","quetzaltenango","xela","tatmon"]
    if any(kw in email for kw in tienda_kw): return ""
    if full.lower().startswith("tatmon"): return ""
    return full

def is_venta(ticket):
    tipo = (ticket.get("issue_type") or {}).get("label") or ""
    return tipo.lower().strip() in VENTAS_TIPOS

def parse_total(ticket):
    inv = ticket.get("invoice") or {}
    for key in ("amount", "total", "grand_total", "subtotal", "paid"):
        v = inv.get(key)
        if v is not None and v != "":
            try:
                f = float(str(v).replace("Q","").replace(",","").strip())
                if f > 0: return f
            except: pass
    import re
    desc = ticket.get("description") or ""
    m = re.search(r'[Pp]recio\s+pactado\s*:\s*Q?\s*([\d,]+(?:\.\d+)?)', desc)
    if m:
        try: return float(m.group(1).replace(",", ""))
        except: pass
    return 0.0

def date_str(iso): return str(iso or "")[:10]

def cycle_time_hours(ticket):
    estado = (ticket.get("status") or {}).get("label") or ""
    if estado not in EST_TERM: return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        c = datetime.strptime(ticket["created_date"], fmt)
        u = datetime.strptime(ticket["last_updated"],  fmt)
        diff = (u - c).total_seconds() / 3600
        return round(diff, 1) if 0 < diff < 720 else None
    except: return None

def fecha_pago_efectiva(ticket):
    """Fecha efectiva de pago de un ticket.

    Usa invoice.last_payment_date como fuente principal. Algunas tiendas
    (confirmado en Tatmon Quetzaltenango/Xela vía /debug/tiendas, donde
    invoice_keys llega vacío) no exponen ese campo en el invoice aunque el
    ticket ya esté facturado/completado y cobrado. En ese caso, si el ticket
    está en un estado terminal (EST_TERM), se usa last_updated como
    aproximación de la fecha de cobro. Es un fallback heurístico: monitorear
    que no sobre-clasifique tickets simplemente actualizados (no cobrados).
    """
    inv    = ticket.get("invoice") or {}
    pagado = date_str(inv.get("last_payment_date", ""))
    if not pagado:
        estado = (ticket.get("status") or {}).get("label") or ""
        if estado in EST_TERM:
            pagado = date_str(ticket.get("last_updated", ""))
    return pagado

def classify_ticket(ticket, fecha_str):
    creado = date_str(ticket.get("created_date", ""))
    pagado = fecha_pago_efectiva(ticket)
    paid   = bool(pagado)
    if creado == fecha_str and paid and pagado == fecha_str: return "venta_limpia"
    elif creado < fecha_str and paid and pagado == fecha_str: return "cobro_cartera"
    elif creado == fecha_str and not paid: return "pipeline_sin_cobrar"
    return "otro"

def fetch_tickets_for_tienda_rango(nombre, api_key, desde_str, hasta_str):
    if not api_key: return nombre, []
    all_tickets = []
    page = 1
    headers = {"Authorization": api_key.strip(), "Accept": "application/json"}
    while True:
        try:
            r = requests.get(f"{MGR_BASE}/tickets", headers=headers, params={"page": page}, timeout=15)
            r.raise_for_status()
            data  = r.json()
            batch = data if isinstance(data, list) else (data.get("tickets") or data.get("data") or [])
            if not batch: break
            for t in batch: t["_tienda"] = nombre
            all_tickets.extend(batch)
            fechas = [date_str(t.get("created_date","")) for t in batch if t.get("created_date")]
            if fechas and min(fechas) < desde_str: break
            if len(batch) < 50: break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[ERROR] {nombre} pag {page}: {e}")
            break
    en_ventana = [t for t in all_tickets if t.get("created_date") and desde_str <= date_str(t["created_date"]) <= hasta_str]
    print(f"[INFO] {nombre}: {len(en_ventana)} tickets ({desde_str} to {hasta_str})")
    return nombre, en_ventana

def fetch_all_parallel(dias=None, desde=None, hasta=None):
    if desde and hasta:
        desde_str = desde; hasta_str = hasta
    else:
        d = int(dias) if dias else DIAS_VENTANA
        hasta_str = hoy_gt().isoformat()
        desde_str = (hoy_gt() - timedelta(days=d)).isoformat()
    all_tickets = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_tickets_for_tienda_rango, n, k, desde_str, hasta_str): n
                   for n, k in TIENDAS_CONFIG.items() if k}
        for f in as_completed(futures):
            _, tickets = f.result()
            all_tickets.extend(tickets)
    print(f"[INFO] Red completa: {len(all_tickets)} tickets ({desde_str} → {hasta_str})")
    return all_tickets, desde_str, hasta_str

def compute_kpis(tickets, desde_str=None, hasta_str=None):
    fecha_ref = desde_str or hoy_gt().isoformat()
    desde     = desde_str or (hoy_gt() - timedelta(days=DIAS_VENTANA)).isoformat()
    hasta     = hasta_str or hoy_gt().isoformat()
    tiendas   = {}
    cats      = {"venta_limpia":[], "cobro_cartera":[], "pipeline_sin_cobrar":[]}
    for t in tickets:
        tienda  = t.get("_tienda") or "Sin tienda"
        tecnico = get_tecnico_nombre(t)
        estado  = (t.get("status") or {}).get("label") or ""
        total   = parse_total(t)
        ct      = cycle_time_hours(t)
        venta   = is_venta(t)
        ref     = t.get("ticket_ref") or ""
        cat     = classify_ticket(t, fecha_ref)
        if cat in cats: cats[cat].append(t)
        if tienda not in tiendas:
            tiendas[tienda] = {"nombre": tienda, "total": 0, "completados": 0, "wip": 0,
                "revenue": 0.0, "sin_asignar_rep": 0, "sin_asignar_vta": 0,
                "tecnicos": {}, "boletos_sin_asignar": [],
                "venta_limpia": 0, "cobro_cartera": 0, "pipeline_sin_cobrar": 0,
                "cycle_times": []}
        td = tiendas[tienda]
        td["total"] += 1; td["revenue"] += total
        if cat in cats: td[cat] += 1
        if estado in EST_TERM:
            td["completados"] += 1
            if ct is not None: td["cycle_times"].append(ct)
        elif estado in EST_ACT: td["wip"] += 1
        if not tecnico:
            td["boletos_sin_asignar"].append({"ref": ref, "estado": estado,
                "tipo": (t.get("issue_type") or {}).get("label") or "", "es_venta": venta})
            if venta: td["sin_asignar_vta"] += 1
            else:     td["sin_asignar_rep"] += 1
        else:
            if tecnico not in td["tecnicos"]:
                td["tecnicos"][tecnico] = {"nombre": tecnico, "total": 0, "completados": 0,
                    "wip": 0, "revenue": 0.0, "cycle_times": []}
            tec = td["tecnicos"][tecnico]
            tec["total"] += 1; tec["revenue"] += total
            if estado in EST_TERM:
                tec["completados"] += 1
                if ct is not None: tec["cycle_times"].append(ct)
            elif estado in EST_ACT: tec["wip"] += 1
    for td in tiendas.values():
        cts = td.pop("cycle_times", [])
        td["cycle_time_avg_hrs"] = round(sum(cts)/len(cts), 1) if cts else None
        td["eficiencia"] = round(td["completados"]/td["total"]*100) if td["total"] > 0 else 0
        for tec in td["tecnicos"].values():
            tcts = tec.pop("cycle_times", [])
            tec["cycle_time_avg_hrs"] = round(sum(tcts)/len(tcts), 1) if tcts else None
            tec["eficiencia"] = round(tec["completados"]/tec["total"]*100) if tec["total"] > 0 else 0
        td["tecnicos"] = sorted(td["tecnicos"].values(), key=lambda x: x["total"], reverse=True)
    total_red = sum(td["total"] for td in tiendas.values())
    comp_red  = sum(td["completados"] for td in tiendas.values())
    rev_red   = sum(td["revenue"] for td in tiendas.values())
    sa_rep    = sum(td["sin_asignar_rep"] for td in tiendas.values())
    sa_vta    = sum(td["sin_asignar_vta"] for td in tiendas.values())
    all_cts   = [td["cycle_time_avg_hrs"] for td in tiendas.values() if td["cycle_time_avg_hrs"]]
    def cat_sum(lst):
        return {"count": len(lst), "revenue": round(sum(parse_total(t) for t in lst), 2),
                "por_tienda": {n: sum(1 for t in lst if t.get("_tienda")==n)
                    for n in TIENDAS_CONFIG if any(t.get("_tienda")==n for t in lst)}}
    return {
        "hoy": fecha_ref, "ventana": f"{desde} → {hasta}",
        "red": {"total": total_red, "completados": comp_red,
            "wip": sum(td["wip"] for td in tiendas.values()),
            "revenue": round(rev_red, 2),
            "eficiencia": round(comp_red/total_red*100) if total_red > 0 else 0,
            "cycle_time_avg_hrs": round(sum(all_cts)/len(all_cts), 1) if all_cts else None,
            "sin_asignar_rep": sa_rep, "sin_asignar_vta": sa_vta},
        "tiendas": list(tiendas.values()),
        "categorias_dia": {
            "venta_limpia":        cat_sum(cats["venta_limpia"]),
            "cobro_cartera":       cat_sum(cats["cobro_cartera"]),
            "pipeline_sin_cobrar": cat_sum(cats["pipeline_sin_cobrar"]),
        },
        "issue_types": list({(t.get("issue_type") or {}).get("label","") for t in tickets if (t.get("issue_type") or {}).get("label")}),
        "tiendas_status": {n: "ok" if k else "sin_key" for n, k in TIENDAS_CONFIG.items()},
        "tiendas_activas": [n for n, k in TIENDAS_CONFIG.items() if k],
        "total_tickets_raw": len(tickets),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def get_kpis_cached(dias=None, desde=None, hasta=None):
    if dias or desde:
        tickets, desde_str, hasta_str = fetch_all_parallel(dias=dias, desde=desde, hasta=hasta)
        return compute_kpis(tickets, desde_str, hasta_str)
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL:
        tickets, desde_str, hasta_str = fetch_all_parallel()
        _cache["data"] = compute_kpis(tickets, desde_str, hasta_str)
        _cache["ts"]   = now
    return _cache["data"]

def get_all_cached(dias=None, desde=None, hasta=None):
    if dias or desde:
        tickets, _, _ = fetch_all_parallel(dias=dias, desde=desde, hasta=hasta)
        return tickets
    now = time.time()
    if _cache_all["data"] is None or (now - _cache_all["ts"]) > CACHE_TTL:
        tickets, _, _ = fetch_all_parallel()
        _cache_all["data"] = tickets
        _cache_all["ts"]   = now
    return _cache_all["data"]

def fetch_payments_dia(fecha_str):
    resultados = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key:
            resultados[nombre] = {"realizados": 0.0, "advances": 0.0, "total": 0.0, "count": 0}
            continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        realizados = advances = 0.0
        count = 0; page = 1
        while True:
            try:
                r = requests.get(f"{MGR_BASE}/payments", headers=headers,
                                 params={"page": page}, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not isinstance(batch, list):
                    batch = batch.get("payments") or batch.get("data") or []
                if not batch: break
                del_dia = [p for p in batch if date_str(p.get("date","")) == fecha_str]
                for p in del_dia:
                    monto = float(p.get("amount") or 0)
                    if p.get("is_advance"): advances   += monto
                    else:                   realizados += monto
                    count += 1
                fechas = [date_str(p.get("date","")) for p in batch if p.get("date")]
                if fechas and min(fechas) < fecha_str: break
                if len(batch) < 50: break
                page += 1; time.sleep(0.2)
            except Exception as e:
                print(f"[ERROR] payments {nombre} pag {page}: {e}")
                break
        resultados[nombre] = {"realizados": round(realizados, 2),
            "advances": round(advances, 2), "total": round(realizados + advances, 2), "count": count}
        print(f"[INFO] payments {nombre} {fecha_str}: realizados={realizados} advances={advances} count={count}")
    return resultados

def fetch_payments_dia_raw(fecha_str):
    """Como fetch_payments_dia pero retorna los registros crudos (no solo sumas), para
    poder conciliar cada pago contra el ticket que lo originó."""
    resultados = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key:
            resultados[nombre] = []
            continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        pagos_dia = []
        page = 1
        while True:
            try:
                r = requests.get(f"{MGR_BASE}/payments", headers=headers,
                                 params={"page": page}, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not isinstance(batch, list):
                    batch = batch.get("payments") or batch.get("data") or []
                if not batch: break
                del_dia = [p for p in batch if date_str(p.get("date","")) == fecha_str]
                for p in del_dia: p["_tienda"] = nombre
                pagos_dia.extend(del_dia)
                fechas = [date_str(p.get("date","")) for p in batch if p.get("date")]
                if fechas and min(fechas) < fecha_str: break
                if len(batch) < 50: break
                page += 1; time.sleep(0.2)
            except Exception as e:
                print(f"[ERROR] payments_raw {nombre} pag {page}: {e}")
                break
        resultados[nombre] = pagos_dia
    return resultados

def reconciliar_pagos_tickets(fecha_str, ventana_dias=90):
    """Concilia cada pago de /payments contra el ticket cuyo invoice.last_payment_date
    coincide exactamente (misma tienda, mismo timestamp al segundo). No existe un campo
    de ID compartido entre /payments e invoice.number — se confirmó vía /debug/tiendas y
    /debug/payments que /payments no trae number/ticket_id/invoice_id, solo id y payment_id
    en formatos que no corresponden a invoice.number. El timestamp es el único cruce viable.

    Clasifica cada pago conciliado como:
      - venta_dia: el ticket asociado fue creado el mismo fecha_str
      - cartera:   el ticket asociado fue creado antes de fecha_str
      - sin_match: no se encontró ningún ticket con ese timestamp exacto (revisar manualmente)
    """
    pagos_por_tienda = fetch_payments_dia_raw(fecha_str)
    desde = (date.fromisoformat(fecha_str) - timedelta(days=ventana_dias)).isoformat()
    tickets, _, _ = fetch_all_parallel(desde=desde, hasta=fecha_str)

    idx = {}
    for t in tickets:
        ts = (t.get("invoice") or {}).get("last_payment_date")
        if not ts: continue
        idx.setdefault((t.get("_tienda"), ts), []).append(t)

    venta_dia = {"count": 0, "revenue": 0.0}
    cartera   = {"count": 0, "revenue": 0.0}
    sin_match = {"count": 0, "revenue": 0.0, "detalle": []}

    for tienda, pagos in pagos_por_tienda.items():
        for p in pagos:
            monto = float(p.get("amount") or 0)
            candidatos = idx.get((tienda, p.get("date")), [])
            if not candidatos:
                sin_match["count"]   += 1
                sin_match["revenue"] += monto
                sin_match["detalle"].append({"tienda": tienda, "payment_id": p.get("payment_id"),
                    "monto": monto, "date": p.get("date"), "is_advance": p.get("is_advance")})
                continue
            creado = date_str(candidatos[0].get("created_date", ""))
            destino = venta_dia if creado == fecha_str else cartera
            destino["count"] += 1; destino["revenue"] += monto

    cxc_tickets = [t for t in tickets
                   if date_str(t.get("created_date", "")) == fecha_str
                   and not (t.get("invoice") or {}).get("last_payment_date")]

    for cat in (venta_dia, cartera, sin_match): cat["revenue"] = round(cat["revenue"], 2)
    return {
        "fecha": fecha_str,
        "venta_dia": venta_dia,
        "cartera_recuperada": cartera,
        "sin_match": sin_match,
        "cuentas_por_cobrar": {
            "count": len(cxc_tickets),
            "revenue_estimado": round(sum(parse_total(t) for t in cxc_tickets), 2),
            "nota": "revenue_estimado usa parse_total() (regex sobre descripción) porque invoice no trae monto — no es confiable, solo referencia"
        },
        "total_pagos_del_dia": sum(len(p) for p in pagos_por_tienda.values()),
    }

def fetch_invoice_details_for_tickets(tickets):
    """Para cada ticket con invoice, llama GET /ticketInvoices/{invoice.id} (threaded) y
    devuelve (idx_pago_a_ticket, detalle_por_ticket_id).

    Confirmado que este es el único camino de conciliación real: /ticketInvoices/{id}
    trae un array 'payments' embebido cuyos id/payment_id coinciden exactamente con los
    registros de /payments — algo que /tickets y /payments no comparten directamente
    (ver /debug/invoice, /debug/payment_detail). No existe /payments/{id} (404) ni ningún
    otro atajo inverso, así que hay que recorrer las facturas de los tickets en la ventana.
    """
    idx_pago_ticket = {}
    detalle_por_ticket = {}
    tareas = []
    for t in tickets:
        inv_id = (t.get("invoice") or {}).get("id")
        tienda = t.get("_tienda")
        key = TIENDAS_CONFIG.get(tienda, "")
        if inv_id and key:
            tareas.append((t, tienda, key, inv_id))

    def _fetch(item):
        t, tienda, key, inv_id = item
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        try:
            r = requests.get(f"{MGR_BASE}/ticketInvoices/{inv_id}", headers=headers, timeout=15)
            r.raise_for_status()
            body = r.json()
            facturas = body if isinstance(body, list) else [body]
            return t, (facturas[0] if facturas else {})
        except Exception as e:
            print(f"[ERROR] ticketInvoices {tienda} {inv_id}: {e}")
            return t, {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_fetch, item) for item in tareas]
        for f in as_completed(futures):
            t, factura = f.result()
            if not factura: continue
            detalle_por_ticket[t.get("id")] = factura
            for p in factura.get("payments") or []:
                pid = p.get("id")
                if pid: idx_pago_ticket[pid] = t
    return idx_pago_ticket, detalle_por_ticket

def reconciliar_pagos_tickets_v2(fecha_str, ventana_dias=60):
    """Clasifica los pagos reales del día contra el ticket que los originó, usando el
    índice construido desde /ticketInvoices/{id} (ver fetch_invoice_details_for_tickets),
    en vez de adivinar por timestamp (reconciliar_pagos_tickets, que falla ~78% de las
    veces — ver /debug/reconciliacion)."""
    t0 = time.time()
    pagos_por_tienda = fetch_payments_dia_raw(fecha_str)
    desde = (date.fromisoformat(fecha_str) - timedelta(days=ventana_dias)).isoformat()
    tickets, _, _ = fetch_all_parallel(desde=desde, hasta=fecha_str)
    idx_pago_ticket, detalle_por_ticket = fetch_invoice_details_for_tickets(tickets)

    venta_dia = {"count": 0, "revenue": 0.0}
    cartera   = {"count": 0, "revenue": 0.0}
    sin_match = {"count": 0, "revenue": 0.0, "detalle": []}

    for tienda, pagos in pagos_por_tienda.items():
        for p in pagos:
            monto  = float(p.get("amount") or 0)
            ticket = idx_pago_ticket.get(p.get("id"))
            if not ticket:
                sin_match["count"]   += 1
                sin_match["revenue"] += monto
                sin_match["detalle"].append({"tienda": tienda, "payment_id": p.get("payment_id"),
                    "id": p.get("id"), "monto": monto, "date": p.get("date"), "is_advance": p.get("is_advance")})
                continue
            creado  = date_str(ticket.get("created_date", ""))
            destino = venta_dia if creado == fecha_str else cartera
            destino["count"] += 1; destino["revenue"] += monto

    cxc_tickets = [t for t in tickets if date_str(t.get("created_date", "")) == fecha_str]
    cxc_pendientes = []
    for t in cxc_tickets:
        factura = detalle_por_ticket.get(t.get("id"), {})
        if not factura or factura.get("status") != "Paid":
            cxc_pendientes.append((t, factura))

    for cat in (venta_dia, cartera, sin_match): cat["revenue"] = round(cat["revenue"], 2)
    return {
        "fecha": fecha_str,
        "venta_dia": venta_dia,
        "cartera_recuperada": cartera,
        "sin_match": sin_match,
        "cuentas_por_cobrar": {
            "count": len(cxc_pendientes),
            "revenue": round(sum(float((f.get("amount_total") or 0)) for _, f in cxc_pendientes), 2),
        },
        "total_pagos_del_dia": sum(len(p) for p in pagos_por_tienda.values()),
        "tickets_en_ventana": len(tickets),
        "segundos": round(time.time() - t0, 1),
    }

def get_dia_kpis(fecha_str):
    payments = fetch_payments_dia(fecha_str)
    desde_30 = (date.fromisoformat(fecha_str) - timedelta(days=30)).isoformat()
    tickets, _, _ = fetch_all_parallel(desde=desde_30, hasta=fecha_str)
    tickets_del_dia = [t for t in tickets if fecha_pago_efectiva(t) == fecha_str]
    kpis = compute_kpis(tickets_del_dia, fecha_str, fecha_str)
    total_realizados = sum(v["realizados"] for v in payments.values())
    total_advances   = sum(v["advances"]   for v in payments.values())
    total_cobrado    = total_realizados + total_advances
    kpis["red"]["revenue"]           = round(total_cobrado, 2)
    kpis["red"]["revenue_realizado"]  = round(total_realizados, 2)
    kpis["red"]["revenue_advance"]    = round(total_advances, 2)
    kpis["payments"] = payments
    for td in kpis["tiendas"]:
        nombre = td["nombre"]
        if nombre in payments:
            td["revenue"]           = payments[nombre]["total"]
            td["revenue_realizado"]  = payments[nombre]["realizados"]
            td["revenue_advance"]    = payments[nombre]["advances"]
    kpis["categorias_dia"]["advances"] = {
        "count":   sum(v["count"] for v in payments.values() if v["advances"] > 0),
        "revenue": round(total_advances, 2)
    }
    # Para categorización: incluir tickets de venta creados ese día
    # aunque no tengan last_payment_date en el invoice
    tickets_venta_dia = [
        t for t in tickets
        if date_str(t.get("created_date", "")) == fecha_str
        and ((t.get("issue_type") or {}).get("label") or "").strip() == "Venta de equipos."
    ]
    ids_ya = {t.get("id") for t in tickets_del_dia}
    tickets_extra = [t for t in tickets_venta_dia if t.get("id") not in ids_ya]
    kpis["_tickets_raw"] = tickets_del_dia + tickets_extra
    # Ventas POS del día
    pos = fetch_pos_dia(fecha_str)
    kpis["pos_dia"]   = pos
    kpis["pos_total"] = round(sum(v["total"] for v in pos.values()), 2)
    kpis["pos_count"] = sum(v["count"] for v in pos.values())
    return kpis

def fetch_pos_dia(fecha_str):
    """Obtiene ventas POS del día desde /posOrders por tienda."""
    resultados = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key:
            resultados[nombre] = {"total": 0.0, "count": 0}
            continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        total = 0.0; count = 0; page = 1
        while True:
            try:
                r = requests.get(f"{MGR_BASE}/posOrders", headers=headers,
                                 params={"page": page}, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not isinstance(batch, list):
                    batch = batch.get("orders") or batch.get("data") or []
                if not batch: break
                del_dia = [o for o in batch
                           if date_str(o.get("created_date","")) == fecha_str
                           and o.get("status") == "Paid"]
                for o in del_dia:
                    try: total += float(o.get("amount_total") or 0)
                    except: pass
                    count += 1
                fechas = [date_str(o.get("created_date","")) for o in batch if o.get("created_date")]
                if fechas and min(fechas) < fecha_str: break
                if len(batch) < 50: break
                page += 1; time.sleep(0.2)
            except Exception as e:
                print(f"[ERROR] posOrders {nombre} pag {page}: {e}")
                break
        resultados[nombre] = {"total": round(total, 2), "count": count}
        print(f"[INFO] posOrders {nombre} {fecha_str}: total={total} count={count}")
    return resultados

def fmt_q(valor): return f"Q {valor:,.2f}"

def categorizar_tickets_dia(tickets, fecha_str):
    """Clasifica tickets del día en Teléfonos y Reparaciones por issue_type."""
    telefonos    = {"count": 0, "revenue": 0.0, "tiendas": {}}
    reparaciones = {"count": 0, "revenue": 0.0, "tiendas": {}}
    for t in tickets:
        tipo   = ((t.get("issue_type") or {}).get("label") or "").strip()
        total  = parse_total(t)
        tienda = t.get("_tienda") or "Sin tienda"
        if tipo == "Venta de equipos.": cat = telefonos
        elif tipo:                      cat = reparaciones
        else:                           continue
        cat["count"]   += 1
        cat["revenue"] += total
        cat["tiendas"][tienda] = cat["tiendas"].get(tienda, 0) + 1
    telefonos["revenue"]    = round(telefonos["revenue"], 2)
    reparaciones["revenue"] = round(reparaciones["revenue"], 2)
    return {"telefonos": telefonos, "reparaciones": reparaciones}

def generar_analisis(tiendas_hoy, tiendas_ayer, rev_hoy, rev_ayer, var_pct):
    lineas = []
    if rev_ayer == 0 and rev_hoy == 0:
        lineas.append("Sin ventas registradas hoy ni ayer en la red.")
        return lineas
    tendencia = "creció" if var_pct >= 0 else "cayó"
    lineas.append(f"La red {tendencia} {abs(var_pct):.1f}% respecto a ayer ({fmt_q(rev_ayer)} → {fmt_q(rev_hoy)}).")
    if tiendas_hoy:
        mejor = max(tiendas_hoy.values(), key=lambda t: t.get("revenue", 0))
        peor  = min(tiendas_hoy.values(), key=lambda t: t.get("revenue", 0))
        lineas.append(f"{mejor['nombre']} lideró el día con {fmt_q(mejor.get('revenue',0))}.")
        if peor["nombre"] != mejor["nombre"]:
            lineas.append(f"{peor['nombre']} tuvo el resultado más bajo con {fmt_q(peor.get('revenue',0))}.")
    sin_hoy = [n for n in tiendas_ayer if n not in tiendas_hoy or tiendas_hoy[n].get("revenue",0)==0]
    if sin_hoy:
        lineas.append("Sin ventas registradas hoy en: " + ", ".join(sin_hoy) + ".")
    return lineas

def generar_pdf_reporte(data_hoy, data_ayer, fecha_str):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=15*mm, bottomMargin=15*mm, leftMargin=15*mm, rightMargin=15*mm)
    styles = getSampleStyleSheet()
    titulo_style   = ParagraphStyle("titulo",    parent=styles["Title"],   textColor=NEGRO,  fontSize=16)
    subtitulo_style = ParagraphStyle("subtitulo", parent=styles["Heading2"], textColor=AZUL,  fontSize=12)
    cuerpo_style   = ParagraphStyle("cuerpo",    parent=styles["Normal"],  fontSize=10, leading=14)
    pie_style      = ParagraphStyle("pie",        parent=styles["Normal"],  textColor=NARANJA, fontSize=9, alignment=TA_CENTER)
    elementos = []

    ayer_str = (date.fromisoformat(fecha_str) - timedelta(days=1)).isoformat()

    # ─── PÁGINA 1 ─────────────────────────────────────────────────────────────
    if os.path.exists(LOGO_PATH):
        elementos.append(Image(LOGO_PATH, width=55*mm, height=26.3*mm))
        elementos.append(Spacer(1, 4*mm))
    elementos.append(Paragraph(f"Reporte Diario de Ventas — {fecha_str}", titulo_style))
    elementos.append(Spacer(1, 6*mm))

    rev_hoy  = data_hoy["red"]["revenue"]
    rev_ayer = data_ayer["red"]["revenue"]
    var_pct  = ((rev_hoy - rev_ayer) / rev_ayer * 100) if rev_ayer else (100.0 if rev_hoy else 0.0)
    tickets_hoy = data_hoy["red"]["total"]

    kpi_data = [
        ["INGRESOS HOY", "INGRESOS AYER", "VARIACIÓN", "TICKETS HOY"],
        [fmt_q(rev_hoy), fmt_q(rev_ayer), f"{var_pct:+.1f}%", str(tickets_hoy)],
    ]
    kpi_table = Table(kpi_data, colWidths=[42*mm]*4)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), NEGRO), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTSIZE", (0,0), (-1,0), 8), ("FONTSIZE", (0,1), (-1,1), 14),
        ("FONTNAME", (0,1), (-1,1), "Helvetica-Bold"),
        ("TEXTCOLOR", (2,1), (2,1), VERDE if var_pct >= 0 else ROJO),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("BOTTOMPADDING", (0,0), (-1,0), 6), ("TOPPADDING", (0,1), (-1,1), 8),
        ("BOTTOMPADDING", (0,1), (-1,1), 8),
        ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
        ("LINEBELOW", (0,0), (-1,0), 0.5, colors.HexColor("#DDDDDD")),
        ("BACKGROUND", (0,1), (-1,1), GRIS),
    ]))
    elementos.append(kpi_table)
    elementos.append(Spacer(1, 8*mm))

    elementos.append(Paragraph("Desglose de ingresos", subtitulo_style))
    elementos.append(Spacer(1, 3*mm))
    cats_hoy      = data_hoy.get("categorias_dia", {})
    venta_hoy     = cats_hoy.get("venta_limpia",        {}).get("revenue", 0.0)
    cartera_hoy   = cats_hoy.get("cobro_cartera",       {}).get("revenue", 0.0)
    pipeline_hoy  = cats_hoy.get("pipeline_sin_cobrar", {}).get("count",   0)
    rev_real      = data_hoy["red"].get("revenue_realizado", venta_hoy + cartera_hoy)
    advances_real = data_hoy["red"].get("revenue_advance",   0.0)
    total_cobrado = data_hoy["red"]["revenue"]
    desglose_data = [
        ["Categoría", "Monto", "Descripción"],
        ["Venta del día",        fmt_q(venta_hoy),     "Órdenes creadas y cobradas hoy"],
        ["Cobro de cartera",     fmt_q(cartera_hoy),   "Órdenes anteriores cobradas hoy"],
        ["INGRESO REALIZADO",    fmt_q(rev_real),       "Servicios completados"],
        ["Advances / Anticipos", fmt_q(advances_real),  "Pagos anticipados (trabajo pendiente)"],
        ["TOTAL COBRADO",        fmt_q(total_cobrado),  ""],
        ["Pipeline sin cobrar",  f"{pipeline_hoy} tickets", "Creados hoy, pago pendiente"],
    ]
    desglose_table = Table(desglose_data, colWidths=[52*mm, 33*mm, 85*mm])
    desglose_table.setStyle(TableStyle([
        ("BACKGROUND",     (0,0), (-1,0),  NEGRO), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME",       (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0,0), (-1,-1), 9), ("ALIGN", (1,0), (1,-1), "RIGHT"),
        ("GRID",           (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
        ("ROWBACKGROUNDS", (0,1), (-1,2),  [colors.white, GRIS]),
        ("BACKGROUND",     (0,3), (-1,3),  AMARILLO), ("FONTNAME", (0,3), (-1,3), "Helvetica-Bold"),
        ("BACKGROUND",     (0,4), (-1,4),  AZUL_CLARO),
        ("BACKGROUND",     (0,5), (-1,5),  NEGRO), ("TEXTCOLOR", (0,5), (-1,5), colors.white),
        ("FONTNAME",       (0,5), (-1,5),  "Helvetica-Bold"),
        ("BACKGROUND",     (0,6), (-1,6),  GRIS), ("TEXTCOLOR", (0,6), (-1,6), colors.HexColor("#888888")),
    ]))
    elementos.append(desglose_table)
    elementos.append(Spacer(1, 8*mm))

    elementos.append(Paragraph("Ingresos por sucursal", subtitulo_style))
    elementos.append(Spacer(1, 3*mm))
    tiendas_hoy_d  = {t["nombre"]: t for t in data_hoy["tiendas"]}
    tiendas_ayer_d = {t["nombre"]: t for t in data_ayer["tiendas"]}
    nombres = sorted(set(TIENDAS_CONFIG) | set(tiendas_hoy_d) | set(tiendas_ayer_d))
    tabla_data = [["Sucursal", "Hoy", "Ayer", "Variación"]]
    filas_color = []
    for i, nombre in enumerate(nombres, start=1):
        cerrado_hoy  = tienda_cerrada(nombre, fecha_str)
        cerrado_ayer = tienda_cerrada(nombre, ayer_str)
        rh = tiendas_hoy_d.get(nombre, {}).get("revenue", 0.0)
        ra = tiendas_ayer_d.get(nombre, {}).get("revenue", 0.0)
        hoy_txt  = "Cerrado" if cerrado_hoy  else fmt_q(rh)
        ayer_txt = "Cerrado" if cerrado_ayer else fmt_q(ra)
        if cerrado_hoy or cerrado_ayer:
            var_txt = "-"
            color = GRIS_CERRADO
        else:
            var = ((rh - ra) / ra * 100) if ra else (100.0 if rh else 0.0)
            var_txt = f"{var:+.1f}%"
            color = VERDE if var >= 0 else ROJO
        tabla_data.append([nombre, hoy_txt, ayer_txt, var_txt])
        filas_color.append((i, color))
    tabla = Table(tabla_data, colWidths=[70*mm, 35*mm, 35*mm, 30*mm])
    estilo = [
        ("BACKGROUND", (0,0), (-1,0), AZUL), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("ALIGN", (1,0), (-1,-1), "CENTER"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, GRIS]),
    ]
    for fila, color in filas_color:
        estilo.append(("TEXTCOLOR", (3,fila), (3,fila), color))
        estilo.append(("FONTNAME", (3,fila), (3,fila), "Helvetica-Bold"))
    tabla.setStyle(TableStyle(estilo))
    elementos.append(tabla)
    elementos.append(Spacer(1, 8*mm))

    elementos.append(Paragraph("Análisis rápido", subtitulo_style))
    elementos.append(Spacer(1, 3*mm))
    analisis = generar_analisis(tiendas_hoy_d, tiendas_ayer_d, rev_hoy, rev_ayer, var_pct)
    for linea in analisis:
        elementos.append(Paragraph(f"•  {linea}", cuerpo_style))
        elementos.append(Spacer(1, 1.5*mm))
    elementos.append(Spacer(1, 10*mm))
    elementos.append(Paragraph("Te lo dejo ¡Niiiitiiiidoooo!", pie_style))

    # ─── PÁGINA 2: Desglose por categoría ─────────────────────────────────────
    elementos.append(PageBreak())
    elementos.append(Paragraph(f"Desglose por Categoría — {fecha_str}", titulo_style))
    elementos.append(Spacer(1, 6*mm))

    tickets_raw    = data_hoy.get("_tickets_raw", [])
    cats2          = categorizar_tickets_dia(tickets_raw, fecha_str)
    rep            = cats2["reparaciones"]
    rev_rep        = rep["revenue"]
    rev_total_real = data_hoy["red"]["revenue"]
    rev_pos        = data_hoy.get("pos_total", 0.0)
    cnt_pos        = data_hoy.get("pos_count", 0)
    rev_adv        = data_hoy["red"].get("revenue_advance", 0.0)
    pct_pos = round(rev_pos / rev_total_real * 100, 1) if rev_total_real else 0
    pct_rep = round(rev_rep / rev_total_real * 100, 1) if rev_total_real else 0
    pct_adv = round(max(100 - pct_pos - pct_rep, 0), 1)
    cat_data = [
        ["Categoría", "Órdenes", "Ingresos", "% del total cobrado"],
        ["Ventas POS (Teléfonos/Accesorios)", str(cnt_pos),       fmt_q(rev_pos),       f"{pct_pos}%"],
        ["Reparaciones",                      str(rep["count"]),  fmt_q(rev_rep),       f"{pct_rep}%"],
        ["Advances / Anticipos",              "—",               fmt_q(rev_adv),        f"{pct_adv}%"],
        ["TOTAL COBRADO",                     "—",               fmt_q(rev_total_real), ""],
    ]
    cat_table = Table(cat_data, colWidths=[65*mm, 22*mm, 48*mm, 45*mm])
    cat_table.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0),  NEGRO), ("TEXTCOLOR",  (0,0), (-1,0),  colors.white),
        ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,-1), 9), ("ALIGN", (1,0), (-1,-1), "CENTER"),
        ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
        ("BACKGROUND",  (0,1), (-1,1),  AZUL_CLARO),
        ("BACKGROUND",  (0,2), (-1,2),  VERDE_CLARO),
        ("BACKGROUND",  (0,3), (-1,3),  GRIS), ("TEXTCOLOR", (0,3), (-1,3), colors.HexColor("#888888")),
        ("BACKGROUND",  (0,4), (-1,4),  NEGRO), ("TEXTCOLOR", (0,4), (-1,4), colors.white),
        ("FONTNAME",    (0,4), (-1,4),  "Helvetica-Bold"),
    ]))
    elementos.append(Paragraph("Composición de ingresos", subtitulo_style))
    elementos.append(Spacer(1, 3*mm))
    elementos.append(cat_table)
    elementos.append(Spacer(1, 8*mm))

    # Barra visual
    elementos.append(Paragraph("Distribución visual", subtitulo_style))
    elementos.append(Spacer(1, 3*mm))
    BAR_W = 170*mm; BAR_H = 14*mm
    d = Drawing(BAR_W, BAR_H + 20)
    x = 0
    segmentos = [
        (pct_pos, colors.HexColor("#00A7E1"), f"Ventas POS {pct_pos}%"),
        (pct_rep, colors.HexColor("#1E8E3E"), f"Reparaciones {pct_rep}%"),
        (pct_adv, colors.HexColor("#FF6B00"), f"Advances {pct_adv}%"),    ]
    for pct, col, label in segmentos:
        w = BAR_W * pct / 100
        if w < 1: continue
        d.add(Rect(x, 6, w, BAR_H, fillColor=col, strokeColor=colors.white, strokeWidth=1))
        if w > 25:
            d.add(String(x + w/2, 10, label, fontName="Helvetica-Bold", fontSize=7,
                         fillColor=colors.white, textAnchor="middle"))
        x += w
    elementos.append(d)
    elementos.append(Spacer(1, 8*mm))

    # Detalle por tienda
    elementos.append(Paragraph("Desglose por sucursal", subtitulo_style))
    elementos.append(Spacer(1, 3*mm))
    pos_por_tienda = {n: data_hoy.get("pos_dia", {}).get(n, {}) for n in TIENDAS_CONFIG}
    det_data = [["Sucursal", "POS (Teléfonos/Acc.)", "Reparaciones", "Total POS"]]
    for tn in sorted(TIENDAS_CONFIG.keys()):
        if tienda_cerrada(tn, fecha_str):
            det_data.append([tn, "Cerrado", "-", "-"])
            continue
        p = pos_por_tienda.get(tn, {})
        det_data.append([tn, str(p.get("count", 0)), str(rep["tiendas"].get(tn, 0)), fmt_q(p.get("total", 0.0))])
    det_table = Table(det_data, colWidths=[65*mm, 40*mm, 40*mm, 35*mm])
    det_table.setStyle(TableStyle([
        ("BACKGROUND",     (0,0), (-1,0),  AZUL), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME",       (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0,0), (-1,-1), 9), ("ALIGN", (1,0), (-1,-1), "CENTER"),
        ("GRID",           (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, GRIS]),
    ]))
    elementos.append(det_table)
    elementos.append(Spacer(1, 10*mm))
    elementos.append(Paragraph("Te lo dejo ¡Niiiitiiiidoooo!", pie_style))

    doc.build(elementos)
    buf.seek(0)
    return buf

def enviar_reporte_email(pdf_buffer, fecha_str):
    if not EMAIL_USER or not EMAIL_APP_PASSWORD: raise RuntimeError("Faltan credenciales")
    if not REPORT_RECIPIENTS: raise RuntimeError("REPORT_RECIPIENTS vacío")
    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER; msg["To"] = ", ".join(REPORT_RECIPIENTS)
    msg["Subject"] = f"Reporte Diario de Ventas Tatmon — {fecha_str}"
    msg.attach(MIMEText("Adjunto reporte diario.\n\nTe lo dejo ¡Niiiitiiiidoooo!", "plain"))
    adjunto = MIMEBase("application", "pdf")
    adjunto.set_payload(pdf_buffer.read())
    encoders.encode_base64(adjunto)
    adjunto.add_header("Content-Disposition", f'attachment; filename="Reporte_Ventas_Tatmon_{fecha_str}.pdf"')
    msg.attach(adjunto)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(); server.login(EMAIL_USER, EMAIL_APP_PASSWORD); server.send_message(msg)

@app.route("/")
def health():
    keys_ok = sum(1 for k in TIENDAS_CONFIG.values() if k)
    return jsonify({"status": "ok", "service": "Tatmon API", "version": "4.11",
                    "tiendas_configuradas": keys_ok, "ventana_dias": DIAS_VENTANA,
                    "tiendas": {n: "✓" if k else "✗" for n, k in TIENDAS_CONFIG.items()}})

@app.route("/kpis")
def kpis():
    try: return jsonify(get_kpis_cached(dias=request.args.get("dias"), desde=request.args.get("desde"), hasta=request.args.get("hasta")))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/kpis/refresh")
def refresh():
    _cache["data"] = _cache_all["data"] = None; _cache["ts"] = _cache_all["ts"] = 0
    data = get_kpis_cached()
    return jsonify({"ok": True, "total_tickets": data["total_tickets_raw"], "ventana": data["ventana"],
                    "tiendas_activas": data["tiendas_activas"], "updated_at": data["updated_at"]})

@app.route("/tickets/all")
def tickets_all():
    try:
        tickets = get_all_cached(dias=request.args.get("dias"), desde=request.args.get("desde"), hasta=request.args.get("hasta"))
        return jsonify({"tickets": tickets, "total": len(tickets), "updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/debug/tiendas")
def debug_tiendas():
    results = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key: results[nombre] = {"error": "sin key"}; continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        try:
            r = requests.get(f"{MGR_BASE}/tickets", headers=headers, params={"page": 1}, timeout=10)
            data = r.json()
            batch = data if isinstance(data, list) else (data.get("tickets") or data.get("data") or [])
            sample = batch[0] if batch else {}
            fechas = [date_str(t.get("created_date","")) for t in batch]
            issue_types = list({(t.get("issue_type") or {}).get("label","") for t in batch if (t.get("issue_type") or {}).get("label")})
            results[nombre] = {"status": r.status_code, "count_pag1": len(batch),
                "primera_fecha": fechas[0] if fechas else None, "issue_types": issue_types,
                "invoice_keys": list((sample.get("invoice") or {}).keys())}
        except Exception as e: results[nombre] = {"error": str(e)}
    return jsonify(results)

@app.route("/debug/payments")
def debug_payments():
    fecha = request.args.get("fecha", "2026-07-11")
    results = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key: results[nombre] = {"error": "sin key"}; continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        try:
            r = requests.get(f"{MGR_BASE}/payments", headers=headers, params={"page": 1}, timeout=15)
            data = r.json()
            batch = data if isinstance(data, list) else (data.get("payments") or data.get("data") or [])
            sample = batch[0] if batch else {}
            del_dia = [p for p in batch if fecha in str(p.get("date","") or "")]
            results[nombre] = {"status": r.status_code, "total_pag1": len(batch),
                "del_dia": len(del_dia), "keys": list(sample.keys()) if sample else [], "sample": sample}
        except Exception as e: results[nombre] = {"error": str(e)}
    return jsonify(results)

@app.route("/debug/pos")
def debug_pos():
    """Explora /posOrders de MGR. Acepta ?fecha=YYYY-MM-DD"""
    fecha = request.args.get("fecha", "2026-07-12")
    results = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key: results[nombre] = {"error": "sin key"}; continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        try:
            r = requests.get(f"{MGR_BASE}/posOrders", headers=headers,
                             params={"page": 1}, timeout=15)
            data = r.json()
            batch = data if isinstance(data, list) else (data.get("orders") or data.get("data") or [])
            sample = batch[0] if batch else {}
            del_dia = [o for o in batch if fecha in str(o.get("date","") or o.get("created_at","") or o.get("sale_date","") or "")]
            results[nombre] = {
                "status": r.status_code, "total_pag1": len(batch),
                "del_dia": len(del_dia), "keys": list(sample.keys()) if sample else [],
                "sample": sample, "del_dia_sample": del_dia[:2],
                "raw_type": "array" if isinstance(data, list) else list(data.keys()) if isinstance(data, dict) else str(type(data))
            }
        except Exception as e: results[nombre] = {"error": str(e)}
    return jsonify(results)

@app.route("/debug/invoice")
def debug_invoice():
    """Explora GET /ticketInvoices/{invoiceId} de MGR — endpoint sin probar hasta ahora.
    Puede traer el detalle de pagos asociados a una factura (el campo de conciliación
    que /tickets y /payments no comparten directamente). Uso: ?tienda=NOMBRE&invoice_id=UUID"""
    tienda = request.args.get("tienda", "")
    invoice_id = request.args.get("invoice_id", "")
    key = TIENDAS_CONFIG.get(tienda, "")
    if not key: return jsonify({"error": f"tienda desconocida o sin key: {tienda}", "tiendas_validas": list(TIENDAS_CONFIG.keys())}), 400
    if not invoice_id: return jsonify({"error": "falta invoice_id"}), 400
    headers = {"Authorization": key.strip(), "Accept": "application/json"}
    try:
        r = requests.get(f"{MGR_BASE}/ticketInvoices/{invoice_id}", headers=headers, timeout=15)
        return jsonify({"status": r.status_code, "body": r.json() if r.content else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug/payment_detail")
def debug_payment_detail():
    """Prueba si GET /payments/{paymentId} existe y si retorna un link directo a
    ticket/invoice (más eficiente que recorrer facturas una por una)."""
    tienda = request.args.get("tienda", "")
    payment_id = request.args.get("payment_id", "")
    key = TIENDAS_CONFIG.get(tienda, "")
    if not key: return jsonify({"error": f"tienda desconocida o sin key: {tienda}"}), 400
    headers = {"Authorization": key.strip(), "Accept": "application/json"}
    intentos = {}
    for path in (f"/payments/{payment_id}", f"/ticketPayments/{payment_id}"):
        try:
            r = requests.get(f"{MGR_BASE}{path}", headers=headers, timeout=15)
            intentos[path] = {"status": r.status_code, "body": r.json() if r.content else None}
        except Exception as e:
            intentos[path] = {"error": str(e)}
    return jsonify(intentos)

@app.route("/debug/invoices_list")
def debug_invoices_list():
    """Explora GET /ticketInvoices/ (lista, sin ID) — para ver si ya trae el array
    'payments' embebido por factura sin necesidad de N+1 llamadas por ticket."""
    tienda = request.args.get("tienda", "")
    page = request.args.get("page", "1")
    key = TIENDAS_CONFIG.get(tienda, "")
    if not key: return jsonify({"error": f"tienda desconocida o sin key: {tienda}", "tiendas_validas": list(TIENDAS_CONFIG.keys())}), 400
    headers = {"Authorization": key.strip(), "Accept": "application/json"}
    try:
        r = requests.get(f"{MGR_BASE}/ticketInvoices/", headers=headers, params={"page": page}, timeout=15)
        data = r.json() if r.content else None
        batch = data if isinstance(data, list) else (data.get("invoices") or data.get("data") or []) if isinstance(data, dict) else []
        sample = batch[0] if batch else {}
        return jsonify({"status": r.status_code, "count": len(batch),
            "raw_type": "array" if isinstance(data, list) else list(data.keys()) if isinstance(data, dict) else str(type(data)),
            "sample_keys": list(sample.keys()) if sample else [], "sample": sample})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug/reconciliacion_v2")
def debug_reconciliacion_v2():
    """Versión validada: conciliación por /ticketInvoices/{id} en vez de timestamp."""
    fecha = request.args.get("fecha", "2026-07-16")
    ventana = int(request.args.get("ventana_dias", "60"))
    try:
        return jsonify(reconciliar_pagos_tickets_v2(fecha, ventana_dias=ventana))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug/reconciliacion")
def debug_reconciliacion():
    """Compara el método viejo (clasificación por ticket, monto vía parse_total) contra
    el método nuevo (clasificación por pago real de /payments, conciliado por timestamp
    contra el ticket). Usar para validar antes de reemplazar get_dia_kpis()."""
    fecha = request.args.get("fecha", "2026-07-16")
    ventana = int(request.args.get("ventana_dias", "90"))
    try:
        nuevo = reconciliar_pagos_tickets(fecha, ventana_dias=ventana)
        viejo = get_dia_kpis(fecha)
        return jsonify({
            "fecha": fecha,
            "metodo_nuevo": nuevo,
            "metodo_viejo": {
                "tickets_total": viejo["red"]["total"],
                "venta_limpia":        viejo["categorias_dia"]["venta_limpia"],
                "cobro_cartera":       viejo["categorias_dia"]["cobro_cartera"],
                "pipeline_sin_cobrar": viejo["categorias_dia"]["pipeline_sin_cobrar"],
                "revenue_total": viejo["red"]["revenue"],
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reporte/preview")
def reporte_preview():
    try:
        fecha_param = request.args.get("fecha")
        hoy_str  = fecha_param or hoy_gt().isoformat()
        ayer_str = (date.fromisoformat(hoy_str) - timedelta(days=1)).isoformat()
        data_hoy  = get_dia_kpis(hoy_str)
        data_ayer = get_dia_kpis(ayer_str)
        pdf_buffer = generar_pdf_reporte(data_hoy, data_ayer, hoy_str)
        return send_file(pdf_buffer, mimetype="application/pdf",
                          as_attachment=False, download_name=f"reporte_{hoy_str}.pdf")
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/reporte/enviar")
def reporte_enviar():
    if REPORT_SECRET and request.args.get("secret") != REPORT_SECRET:
        return jsonify({"ok": False, "error": "no autorizado"}), 401
    try:
        fecha_param = request.args.get("fecha")
        hoy_str  = fecha_param or hoy_gt().isoformat()
        ayer_str = (date.fromisoformat(hoy_str) - timedelta(days=1)).isoformat()
        data_hoy  = get_dia_kpis(hoy_str)
        data_ayer = get_dia_kpis(ayer_str)
        pdf_buffer = generar_pdf_reporte(data_hoy, data_ayer, hoy_str)
        enviar_reporte_email(pdf_buffer, hoy_str)
        return jsonify({"ok": True, "fecha": hoy_str, "destinatarios": REPORT_RECIPIENTS})
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
