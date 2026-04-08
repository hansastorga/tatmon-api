import os
import time
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MGR_BASE = "https://api.mygadgetrepairs.com/v1"
MGR_KEY  = os.environ.get("MGR_API_KEY", "")

VENTAS_TIPOS = {"venta de equipos.", "compra", "venta", "reserva"}

TIENDA_EMAIL_MAP = {
    "tiendacayala@tatmon.com":          "Tatmon Cayalá",
    "tiendakalu@tatmon.com":            "Tatmon Kalú",
    "tiendalavilla@tatmon.com":         "Tatmon La Villa",
    "tiendaquiche@tatmon.com":          "Tatmon Quiché",
    "tiendaquetzaltenango@tatmon.com":  "Tatmon Quetzaltenango",
}

TIENDA_NAME_MAP = {
    "tatmon cayalá":          "Tatmon Cayalá",
    "tatmon kalu":            "Tatmon Kalú",
    "tatmon kalú":            "Tatmon Kalú",
    "tatmon la villa":        "Tatmon La Villa",
    "tatmon quiché":          "Tatmon Quiché",
    "tatmon quiche":          "Tatmon Quiché",
    "tatmon quetzaltenango":  "Tatmon Quetzaltenango",
}

EST_TERM = {"Completado", "Finalizado / Entregado", "Facturado"}
EST_ACT  = {
    "Nuevo", "En progreso", "En espera de autorización del cliente",
    "Cliente ha autorizado la reparación", "Repuesto Local",
    "Paquete del interior", "Esta en taller Ajeno", "Recolección"
}

_cache = {"data": None, "ts": 0}
CACHE_TTL = 3600

def get_tienda(ticket):
    """Detecta tienda por email del técnico o nombre."""
    tec = ticket.get("technician") or {}
    email = (tec.get("email") or "").lower().strip()
    if email in TIENDA_EMAIL_MAP:
        return TIENDA_EMAIL_MAP[email]
    fname = (tec.get("first_name") or "").lower().strip()
    if fname in TIENDA_NAME_MAP:
        return TIENDA_NAME_MAP[fname]
    return "Sin tienda"

def get_tecnico_nombre(ticket):
    """Nombre completo del técnico. Vacío si es cuenta de tienda."""
    tec = ticket.get("technician") or {}
    email = (tec.get("email") or "").lower().strip()
    if email in TIENDA_EMAIL_MAP:
        return ""
    fname = tec.get("first_name") or ""
    lname = tec.get("last_name") or ""
    full  = f"{fname} {lname}".strip()
    return full

def is_venta(ticket):
    tipo = (ticket.get("issue_type") or {}).get("label") or ""
    return tipo.lower().strip() in VENTAS_TIPOS

def parse_total(ticket):
    inv = ticket.get("invoice") or {}
    try:
        return float(str(inv.get("total") or inv.get("amount") or 0)
                     .replace("Q","").replace(",","").strip())
    except:
        return 0.0

def cycle_time_hours(ticket):
    """Horas entre created_date y last_updated si está terminado."""
    estado = (ticket.get("status") or {}).get("label") or ""
    if estado not in EST_TERM:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        c = datetime.strptime(ticket["created_date"], fmt)
        u = datetime.strptime(ticket["last_updated"], fmt)
        delta = (u - c).total_seconds() / 3600
        return round(delta, 1)
    except:
        return None

def fetch_all_tickets():
    all_tickets = []
    page = 1
    headers = {"Authorization": MGR_KEY.strip(), "Accept": "application/json"}
    while True:
        try:
            r = requests.get(
                f"{MGR_BASE}/tickets",
                headers=headers,
                params={"page": page},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
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

def compute_kpis(tickets):
    tiendas = {}

    for t in tickets:
        tienda   = get_tienda(t)
        tecnico  = get_tecnico_nombre(t)
        estado   = (t.get("status") or {}).get("label") or ""
        tipo_lbl = (t.get("issue_type") or {}).get("label") or ""
        ref      = t.get("ticket_ref") or t.get("id") or ""
        total    = parse_total(t)
        ct       = cycle_time_hours(t)
        venta    = is_venta(t)

        if tienda not in tiendas:
            tiendas[tienda] = {
                "nombre": tienda, "total": 0, "completados": 0, "wip": 0,
                "revenue": 0.0, "sin_asignar_rep": 0, "sin_asignar_vta": 0,
                "cycle_times": [], "tecnicos": {}, "boletos_sin_asignar": []
            }

        td = tiendas[tienda]
        td["total"] += 1
        td["revenue"] += total

        if estado in EST_TERM:
            td["completados"] += 1
            if ct is not None:
                td["cycle_times"].append(ct)
        elif estado in EST_ACT:
            td["wip"] += 1

        if not tecnico:
            td["boletos_sin_asignar"].append({
                "ref": ref, "estado": estado,
                "tipo": tipo_lbl, "es_venta": venta
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
            tec["total"] += 1
            tec["revenue"] += total
            if estado in EST_TERM:
                tec["completados"] += 1
                if ct is not None:
                    tec["cycle_times"].append(ct)
            elif estado in EST_ACT:
                tec["wip"] += 1

    # Calcular promedios y limpiar
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

    return {
        "red": {
            "total": total_red,
            "completados": comp_red,
            "wip": sum(td["wip"] for td in tiendas.values()),
            "revenue": round(rev_red, 2),
            "eficiencia": round(comp_red/total_red*100) if total_red > 0 else 0,
            "cycle_time_avg_hrs": round(sum(all_cts)/len(all_cts), 1) if all_cts else None,
            "sin_asignar_rep": sa_rep,
            "sin_asignar_vta": sa_vta,
        },
        "tiendas": list(tiendas.values()),
        "total_tickets_raw": len(tickets),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def get_kpis_cached():
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL:
        print("[INFO] Actualizando cache...")
        tickets = fetch_all_tickets()
        _cache["data"] = compute_kpis(tickets)
        _cache["ts"] = now
        print(f"[INFO] {len(tickets)} tickets procesados")
    return _cache["data"]

@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "Tatmon Producción API", "version": "2.0"})

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
    _cache["ts"] = 0
    data = get_kpis_cached()
    return jsonify({"ok": True, "updated_at": data["updated_at"]})

@app.route("/tickets/raw")
def tickets_raw():
    if not MGR_KEY:
        return jsonify({"error": "MGR_API_KEY no configurada"}), 500
    headers = {"Authorization": MGR_KEY.strip(), "Accept": "application/json"}
    try:
        r = requests.get(f"{MGR_BASE}/tickets", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        sample = data[:3] if isinstance(data, list) else (data.get("tickets") or [])[:3]
        return jsonify({"sample": sample, "campos": list(sample[0].keys()) if sample else []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
