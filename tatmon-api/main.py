import os
import time
import requests
from datetime import datetime, timezone, date
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MGR_BASE = "https://api.mygadgetrepairs.com/v1"
MGR_KEY  = os.environ.get("MGR_API_KEY", "")

VENTAS_TIPOS = {"venta de equipos.", "compra", "venta", "reserva"}
STORE_NAMES  = {
    "tatmon kalu", "tatmon la villa", "tatmon cayalá",
    "tatmon quiché", "tatmon quetzaltenango",
    "tatmon cayala", "tatmon quiche"
}
EST_TERM = {"Completado", "Finalizado / Entregado", "Facturado"}
EST_ACT  = {
    "Nuevo", "En progreso", "En espera de autorización del cliente",
    "Cliente ha autorizado la reparación", "Repuesto Local",
    "Paquete del interior", "Esta en taller Ajeno", "Recolección"
}
TIENDA_EMAIL_MAP = {
    "tiendacayala@tatmon.com":         "Tatmon Cayalá",
    "tiendakalu@tatmon.com":           "Tatmon Kalú",
    "tiendalavilla@tatmon.com":        "Tatmon La Villa",
    "tiendaquiche@tatmon.com":         "Tatmon Quiché",
    "tiendaquetzaltenango@tatmon.com": "Tatmon Quetzaltenango",
}

_cache      = {"data": None, "ts": 0}
_cache_all  = {"data": None, "ts": 0}
CACHE_TTL   = 3600

# ── helpers ──────────────────────────────────────────────

def get_tienda(ticket):
    tec   = ticket.get("technician") or {}
    email = (tec.get("email") or "").lower().strip()
    if email in TIENDA_EMAIL_MAP:
        return TIENDA_EMAIL_MAP[email]
    fname = (tec.get("first_name") or "").strip()
    lname = (tec.get("last_name") or "").strip()
    full  = f"{fname} {lname}".strip().lower()
    # Match by first name containing tienda keyword
    tienda_keywords = {
        "cayalá": "Tatmon Cayalá", "cayala": "Tatmon Cayalá",
        "kalú": "Tatmon Kalú",   "kalu": "Tatmon Kalú",
        "villa": "Tatmon La Villa",
        "quiché": "Tatmon Quiché", "quiche": "Tatmon Quiché",
        "quetzaltenango": "Tatmon Quetzaltenango", "xela": "Tatmon Quetzaltenango",
    }
    for kw, name in tienda_keywords.items():
        if kw in full:
            return name
    # If technician has no email and no recognizable name but has an id, mark as unassigned store
    if not email and not fname:
        return "Sin tienda"
    # Real technician — determine tienda from ticket_ref prefix
    ref = ticket.get("ticket_ref") or ""
    ref_map = {"CY": "Tatmon Cayalá", "K": "Tatmon Kalú", "L": "Tatmon La Villa",
               "CQ": "Tatmon Quiché", "Q": "Tatmon Quetzaltenango"}
    for prefix, name in ref_map.items():
        if ref.startswith(prefix + "-"):
            return name
    return "Sin tienda"

def get_tecnico_nombre(ticket):
    tec   = ticket.get("technician") or {}
    email = (tec.get("email") or "").lower().strip()
    if email in TIENDA_EMAIL_MAP:
        return ""
    fname = tec.get("first_name") or ""
    lname = tec.get("last_name") or ""
    return f"{fname} {lname}".strip()

def is_venta(ticket):
    tipo = (ticket.get("issue_type") or {}).get("label") or ""
    return tipo.lower().strip() in VENTAS_TIPOS

def parse_total(ticket):
    inv = ticket.get("invoice") or {}
    # MGR /tickets list returns: invoice.amount (the paid amount)
    for key in ("amount", "total", "grand_total", "subtotal"):
        v = inv.get(key)
        if v is not None and v != "" and v != 0:
            try:
                return float(str(v).replace("Q", "").replace(",", "").strip())
            except:
                pass
    return 0.0

def date_str(iso):
    """Extrae YYYY-MM-DD de una cadena ISO, tolerando offsets."""
    if not iso:
        return ""
    return str(iso)[:10]

def cycle_time_hours(ticket):
    estado = (ticket.get("status") or {}).get("label") or ""
    if estado not in EST_TERM:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        c = datetime.strptime(ticket["created_date"], fmt)
        u = datetime.strptime(ticket["last_updated"],  fmt)
        return round((u - c).total_seconds() / 3600, 1)
    except:
        return None

def classify_ticket(ticket, hoy_str):
    """
    Clasifica cada ticket en una de 3 categorías según
    fecha de creación vs fecha de último pago.
    """
    creado = date_str(ticket.get("created_date", ""))
    inv    = ticket.get("invoice") or {}
    pagado = date_str(inv.get("last_payment_date", ""))
    paid   = bool(pagado)

    if creado == hoy_str and paid and pagado == hoy_str:
        return "venta_limpia"       # ticket nuevo + cobrado hoy
    elif creado < hoy_str and paid and pagado == hoy_str:
        return "cobro_cartera"      # ticket anterior + cobrado hoy
    elif creado == hoy_str and not paid:
        return "pipeline_sin_cobrar"  # ticket nuevo + sin cobrar
    else:
        return "otro"

# ── fetch ─────────────────────────────────────────────────

def fetch_all_tickets():
    all_tickets = []
    page = 1
    headers = {"Authorization": MGR_KEY.strip(), "Accept": "application/json"}
    while True:
        try:
            r = requests.get(f"{MGR_BASE}/tickets", headers=headers,
                             params={"page": page}, timeout=15)
            r.raise_for_status()
            data  = r.json()
            batch = data if isinstance(data, list) else (data.get("tickets") or data.get("data") or [])
            if not batch:
                break
            all_tickets.extend(batch)
            if len(batch) < 50:
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"[ERROR] pág {page}: {e}")
            break
    return all_tickets

# ── KPI compute ───────────────────────────────────────────

def compute_kpis(tickets):
    hoy_str = date.today().isoformat()   # "YYYY-MM-DD" en UTC-6 aprox.
    tiendas = {}

    # Contadores de las 3 categorías (red completa)
    cats = {"venta_limpia": [], "cobro_cartera": [], "pipeline_sin_cobrar": []}

    for t in tickets:
        tienda  = get_tienda(t)
        tecnico = get_tecnico_nombre(t)
        estado  = (t.get("status") or {}).get("label") or ""
        total   = parse_total(t)
        ct      = cycle_time_hours(t)
        venta   = is_venta(t)
        ref     = t.get("ticket_ref") or t.get("id") or ""
        cat     = classify_ticket(t, hoy_str)

        if cat in cats:
            cats[cat].append(t)

        if tienda not in tiendas:
            tiendas[tienda] = {
                "nombre": tienda, "total": 0, "completados": 0, "wip": 0,
                "revenue": 0.0, "sin_asignar_rep": 0, "sin_asignar_vta": 0,
                "cycle_times": [], "tecnicos": {}, "boletos_sin_asignar": [],
                "venta_limpia": 0, "cobro_cartera": 0, "pipeline_sin_cobrar": 0
            }

        td = tiendas[tienda]
        td["total"] += 1
        td["revenue"] += total
        if cat in ("venta_limpia", "cobro_cartera", "pipeline_sin_cobrar"):
            td[cat] += 1

        if estado in EST_TERM:
            td["completados"] += 1
            if ct is not None:
                td["cycle_times"].append(ct)
        elif estado in EST_ACT:
            td["wip"] += 1

        real_tec = bool(tecnico)
        if not real_tec:
            td["boletos_sin_asignar"].append(
                {"ref": ref, "estado": estado,
                 "tipo": (t.get("issue_type") or {}).get("label") or "",
                 "es_venta": venta}
            )
            if venta:
                td["sin_asignar_vta"] += 1
            else:
                td["sin_asignar_rep"] += 1
        else:
            if tecnico not in td["tecnicos"]:
                td["tecnicos"][tecnico] = {
                    "nombre": tecnico, "total": 0, "completados": 0,
                    "wip": 0, "revenue": 0.0, "cycle_times": []
                }
            tec = td["tecnicos"][tecnico]
            tec["total"] += 1
            tec["revenue"] += total
            if estado in EST_TERM:
                tec["completados"] += 1
                if ct is not None:
                    tec["cycle_times"].append(ct)
            elif estado in EST_ACT:
                tec["wip"] += 1

    # Promedios
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

    # Resumen de categorías del día
    def cat_summary(tlist):
        rev = sum(parse_total(t) for t in tlist)
        return {
            "count": len(tlist),
            "revenue": round(rev, 2),
            "por_tienda": _count_by_tienda(tlist)
        }

    return {
        "hoy": hoy_str,
        "red": {
            "total": total_red, "completados": comp_red,
            "wip": sum(td["wip"] for td in tiendas.values()),
            "revenue": round(rev_red, 2),
            "eficiencia": round(comp_red/total_red*100) if total_red > 0 else 0,
            "cycle_time_avg_hrs": round(sum(all_cts)/len(all_cts), 1) if all_cts else None,
            "sin_asignar_rep": sa_rep, "sin_asignar_vta": sa_vta,
        },
        "tiendas": list(tiendas.values()),
        "categorias_dia": {
            "venta_limpia":         cat_summary(cats["venta_limpia"]),
            "cobro_cartera":        cat_summary(cats["cobro_cartera"]),
            "pipeline_sin_cobrar":  cat_summary(cats["pipeline_sin_cobrar"]),
        },
        "total_tickets_raw": len(tickets),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def _count_by_tienda(tlist):
    counts = {}
    for t in tlist:
        name = get_tienda(t)
        counts[name] = counts.get(name, 0) + 1
    return counts

def get_kpis_cached():
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL:
        print("[INFO] Actualizando cache KPIs...")
        tickets = fetch_all_tickets()
        _cache["data"] = compute_kpis(tickets)
        _cache["ts"] = now
        print(f"[INFO] {len(tickets)} tickets procesados")
    return _cache["data"]

def get_all_cached():
    now = time.time()
    if _cache_all["data"] is None or (now - _cache_all["ts"]) > CACHE_TTL:
        print("[INFO] Actualizando cache tickets/all...")
        tickets = fetch_all_tickets()
        _cache_all["data"] = tickets
        _cache_all["ts"] = now
    return _cache_all["data"]

# ── routes ────────────────────────────────────────────────

@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "Tatmon Producción API", "version": "3.0"})

@app.route("/kpis")
def kpis():
    if not MGR_KEY:
        return jsonify({"error": "MGR_API_KEY no configurada"}), 500
    try:
        return jsonify(get_kpis_cached())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/kpis/refresh")
def refresh():
    _cache["data"] = None
    _cache["ts"]   = 0
    _cache_all["data"] = None
    _cache_all["ts"]   = 0
    data = get_kpis_cached()
    return jsonify({"ok": True, "updated_at": data["updated_at"]})

@app.route("/tickets/all")
def tickets_all():
    if not MGR_KEY:
        return jsonify({"error": "MGR_API_KEY no configurada"}), 500
    try:
        tickets = get_all_cached()
        return jsonify({
            "tickets": tickets,
            "total": len(tickets),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/tickets/raw")
def tickets_raw():
    if not MGR_KEY:
        return jsonify({"error": "MGR_API_KEY no configurada"}), 500
    headers = {"Authorization": MGR_KEY.strip(), "Accept": "application/json"}
    try:
        r = requests.get(f"{MGR_BASE}/tickets", headers=headers, timeout=15)
        r.raise_for_status()
        data   = r.json()
        sample = data[:3] if isinstance(data, list) else (data.get("tickets") or [])[:3]
        return jsonify({"sample": sample, "campos": list(sample[0].keys()) if sample else []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
