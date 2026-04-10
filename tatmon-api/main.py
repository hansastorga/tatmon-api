import os, time, requests
from datetime import datetime, timezone, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request
from flask_cors import CORS

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

VENTAS_TIPOS = {"venta de equipos.", "compra", "venta", "reserva"}
EST_TERM = {"Completado", "Finalizado / Entregado", "Facturado"}
EST_ACT  = {
    "Nuevo", "En progreso", "En espera de autorización del cliente",
    "Cliente ha autorizado la reparación", "Repuesto Local",
    "Paquete del interior", "Esta en taller Ajeno", "Recolección"
}

# Solo mostramos tickets de los últimos N días
DIAS_VENTANA = int(os.environ.get("DIAS_VENTANA", "30"))

_cache     = {"data": None, "ts": 0}
_cache_all = {"data": None, "ts": 0}
CACHE_TTL  = 3600

# ── helpers ──────────────────────────────────────────────

def get_tecnico_nombre(ticket):
    tec   = ticket.get("technician") or {}
    email = (tec.get("email") or "").lower().strip()
    fname = (tec.get("first_name") or "").strip()
    lname = (tec.get("last_name") or "").strip()
    full  = f"{fname} {lname}".strip()
    tienda_kw = ["cayalá","cayala","kalú","kalu","villa","quiché",
                 "quiche","quetzaltenango","xela","tatmon"]
    if any(kw in email for kw in tienda_kw):
        return ""
    if full.lower().startswith("tatmon"):
        return ""
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
                if f > 0:
                    return f
            except:
                pass
    return 0.0

def date_str(iso):
    return str(iso or "")[:10]

def cycle_time_hours(ticket):
    estado = (ticket.get("status") or {}).get("label") or ""
    if estado not in EST_TERM:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        c = datetime.strptime(ticket["created_date"], fmt)
        u = datetime.strptime(ticket["last_updated"],  fmt)
        diff = (u - c).total_seconds() / 3600
        return round(diff, 1) if 0 < diff < 720 else None
    except:
        return None

def classify_ticket(ticket, hoy_str):
    creado = date_str(ticket.get("created_date", ""))
    inv    = ticket.get("invoice") or {}
    pagado = date_str(inv.get("last_payment_date", ""))
    paid   = bool(pagado)
    if creado == hoy_str and paid and pagado == hoy_str:
        return "venta_limpia"
    elif creado < hoy_str and paid and pagado == hoy_str:
        return "cobro_cartera"
    elif creado == hoy_str and not paid:
        return "pipeline_sin_cobrar"
    return "otro"

def dentro_ventana(ticket, desde_str):
    creado = date_str(ticket.get("created_date", ""))
    return creado >= desde_str

# ── fetch ─────────────────────────────────────────────────

def fetch_tickets_for_tienda_rango(nombre, api_key, desde_str, hasta_str):
    if not api_key:
        return nombre, []
    all_tickets = []
    page = 1
    headers = {"Authorization": api_key.strip(), "Accept": "application/json"}
    while True:
        try:
            r = requests.get(f"{MGR_BASE}/tickets", headers=headers,
                             params={"page": page}, timeout=15)
            r.raise_for_status()
            data  = r.json()
            batch = data if isinstance(data, list) else (data.get("tickets") or data.get("data") or [])
            if not batch:
                break
            for t in batch:
                t["_tienda"] = nombre
            all_tickets.extend(batch)
            fechas = [date_str(t.get("created_date","")) for t in batch if t.get("created_date")]
            if fechas and min(fechas) < desde_str:
                break
            if len(batch) < 50:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[ERROR] {nombre} pag {page}: {e}")
            break
    en_ventana = [t for t in all_tickets
                  if t.get("created_date") and
                  desde_str <= date_str(t["created_date"]) <= hasta_str]
    print(f"[INFO] {nombre}: {len(en_ventana)} tickets ({desde_str} to {hasta_str})")
    return nombre, en_ventana

def fetch_tickets_for_tienda(nombre, api_key):
    _, tickets = fetch_tickets_for_tienda_rango(nombre, api_key,
        (date.today() - timedelta(days=DIAS_VENTANA)).isoformat(),
        date.today().isoformat())
    return nombre, tickets

def fetch_all_parallel(dias=None, desde=None, hasta=None):
    # Calcular ventana efectiva
    if desde and hasta:
        desde_str = desde
        hasta_str = hasta
    else:
        d = int(dias) if dias else DIAS_VENTANA
        hasta_str = date.today().isoformat()
        desde_str = (date.today() - timedelta(days=d)).isoformat()

    all_tickets = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_tickets_for_tienda_rango, n, k, desde_str, hasta_str): n
                   for n, k in TIENDAS_CONFIG.items() if k}
        for f in as_completed(futures):
            _, tickets = f.result()
            all_tickets.extend(tickets)
    print(f"[INFO] Red completa: {len(all_tickets)} tickets ({desde_str} → {hasta_str})")
    return all_tickets, desde_str, hasta_str

# ── compute ───────────────────────────────────────────────

def compute_kpis(tickets, desde_str=None, hasta_str=None):
    hoy_str  = date.today().isoformat()
    desde    = desde_str or (date.today() - timedelta(days=DIAS_VENTANA)).isoformat()
    hasta    = hasta_str or hoy_str
    tiendas  = {}
    cats     = {"venta_limpia":[], "cobro_cartera":[], "pipeline_sin_cobrar":[]}

    for t in tickets:
        tienda  = t.get("_tienda") or "Sin tienda"
        tecnico = get_tecnico_nombre(t)
        estado  = (t.get("status") or {}).get("label") or ""
        total   = parse_total(t)
        ct      = cycle_time_hours(t)
        venta   = is_venta(t)
        ref     = t.get("ticket_ref") or ""
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
        td["total"]   += 1
        td["revenue"] += total
        if cat in cats:
            td[cat] += 1

        if estado in EST_TERM:
            td["completados"] += 1
            if ct is not None:
                td["cycle_times"].append(ct)
        elif estado in EST_ACT:
            td["wip"] += 1

        if not tecnico:
            td["boletos_sin_asignar"].append({
                "ref": ref, "estado": estado,
                "tipo": (t.get("issue_type") or {}).get("label") or "",
                "es_venta": venta
            })
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
            tec["total"]   += 1
            tec["revenue"] += total
            if estado in EST_TERM:
                tec["completados"] += 1
                if ct is not None:
                    tec["cycle_times"].append(ct)
            elif estado in EST_ACT:
                tec["wip"] += 1

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
        return {
            "count":   len(lst),
            "revenue": round(sum(parse_total(t) for t in lst), 2),
            "por_tienda": {
                n: sum(1 for t in lst if t.get("_tienda")==n)
                for n in TIENDAS_CONFIG if any(t.get("_tienda")==n for t in lst)
            }
        }

    return {
        "hoy":       hoy_str,
        "ventana":   f"{desde} → {hasta or hoy_str}",
        "red": {
            "total":              total_red,
            "completados":        comp_red,
            "wip":                sum(td["wip"] for td in tiendas.values()),
            "revenue":            round(rev_red, 2),
            "eficiencia":         round(comp_red/total_red*100) if total_red > 0 else 0,
            "cycle_time_avg_hrs": round(sum(all_cts)/len(all_cts), 1) if all_cts else None,
            "sin_asignar_rep":    sa_rep,
            "sin_asignar_vta":    sa_vta,
        },
        "tiendas": list(tiendas.values()),
        "categorias_dia": {
            "venta_limpia":        cat_sum(cats["venta_limpia"]),
            "cobro_cartera":       cat_sum(cats["cobro_cartera"]),
            "pipeline_sin_cobrar": cat_sum(cats["pipeline_sin_cobrar"]),
        },
        "tiendas_status": {
            n: "ok" if k else "sin_key"
            for n, k in TIENDAS_CONFIG.items()
        },
        "tiendas_activas": [n for n, k in TIENDAS_CONFIG.items() if k],
        "total_tickets_raw": len(tickets),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def get_kpis_cached(dias=None, desde=None, hasta=None):
    # Si hay params de fecha, no usar cache — recalcular siempre
    if dias or desde:
        tickets, desde_str, hasta_str = fetch_all_parallel(dias=dias, desde=desde, hasta=hasta)
        return compute_kpis(tickets, desde_str, hasta_str)
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL:
        print(f"[INFO] Jalando tickets (ventana {DIAS_VENTANA} dias)...")
        tickets, desde_str, hasta_str = fetch_all_parallel()
        _cache["data"] = compute_kpis(tickets, desde_str, hasta_str)
        _cache["ts"]   = now
        print(f"[INFO] Total: {len(tickets)} tickets")
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

# ── routes ────────────────────────────────────────────────

@app.route("/")
def health():
    keys_ok = sum(1 for k in TIENDAS_CONFIG.values() if k)
    return jsonify({
        "status": "ok", "service": "Tatmon API", "version": "4.1",
        "tiendas_configuradas": keys_ok,
        "ventana_dias": DIAS_VENTANA,
        "tiendas": {n: "✓" if k else "✗" for n, k in TIENDAS_CONFIG.items()}
    })

@app.route("/kpis")
def kpis():
    try:
        dias  = request.args.get("dias")
        desde = request.args.get("desde")
        hasta = request.args.get("hasta")
        return jsonify(get_kpis_cached(dias=dias, desde=desde, hasta=hasta))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/kpis/refresh")
def refresh():
    _cache["data"] = _cache_all["data"] = None
    _cache["ts"]   = _cache_all["ts"]   = 0
    data = get_kpis_cached()
    return jsonify({
        "ok": True,
        "total_tickets": data["total_tickets_raw"],
        "ventana": data["ventana"],
        "tiendas_activas": data["tiendas_activas"],
        "updated_at": data["updated_at"]
    })

@app.route("/tickets/all")
def tickets_all():
    try:
        dias  = request.args.get("dias")
        desde = request.args.get("desde")
        hasta = request.args.get("hasta")
        tickets = get_all_cached(dias=dias, desde=desde, hasta=hasta)
        return jsonify({"tickets": tickets, "total": len(tickets),
                        "updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/debug/tiendas")
def debug_tiendas():
    results = {}
    for nombre, key in TIENDAS_CONFIG.items():
        if not key:
            results[nombre] = {"error": "sin key"}
            continue
        headers = {"Authorization": key.strip(), "Accept": "application/json"}
        try:
            r = requests.get(f"{MGR_BASE}/tickets", headers=headers,
                             params={"page": 1}, timeout=10)
            data = r.json()
            batch = data if isinstance(data, list) else (data.get("tickets") or data.get("data") or [])
            sample = batch[0] if batch else {}
            # Mostrar fechas de todos los tickets del primer batch
            fechas = [date_str(t.get("created_date","")) for t in batch]
            results[nombre] = {
                "status":        r.status_code,
                "count_pag1":    len(batch),
                "primera_fecha": fechas[0] if fechas else None,
                "ultima_fecha":  fechas[-1] if fechas else None,
                "todas_fechas":  fechas,
                "invoice_keys":  list((sample.get("invoice") or {}).keys()),
                "invoice_sample": sample.get("invoice"),
                "primer_ref":    sample.get("ticket_ref"),
                "ultimo_ref":    batch[-1].get("ticket_ref") if batch else None,
            }
        except Exception as e:
            results[nombre] = {"error": str(e)}
    return jsonify(results)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
