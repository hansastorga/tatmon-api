import os
import json
import time
import requests
from datetime import datetime, timezone
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

_cache = {"data": None, "ts": 0}
CACHE_TTL = 3600

def is_real_tec(name):
    if not name:
        return False
    return name.lower().strip() not in STORE_NAMES and name.strip() != ""

def is_venta(tipo):
    return (tipo or "").lower().strip() in VENTAS_TIPOS

def parse_total(s):
    try:
        return float(str(s).replace("Q", "").replace(",", "").strip())
    except:
        return 0.0

def mgr_headers():
    """Construye headers para MGR — prueba múltiples formatos."""
    key = MGR_KEY.strip()
    return {
        "Authorization": key,          # formato 1: key sola
        "Accept": "application/json"
    }

def fetch_all_tickets():
    all_tickets = []
    page = 1
    while True:
        try:
            r = requests.get(
                f"{MGR_BASE}/tickets",
                headers=mgr_headers(),
                params={"page": page},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get("tickets") or data.get("data") or []
            else:
                batch = []
            if not batch:
                break
            all_tickets.extend(batch)
            if len(batch) < 50:
                break
            page += 1
            time.sleep(0.5)
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Página {page}: {e}")
            break
    return all_tickets

def compute_kpis(tickets):
    tiendas = {}
    for t in tickets:
        tienda  = t.get("tienda") or t.get("location") or t.get("store") or "Sin tienda"
        tecnico = t.get("Técnico") or t.get("technician") or t.get("assignedTo") or ""
        estado  = t.get("Estado") or t.get("status") or ""
        tipo    = t.get("Tipo de problema") or t.get("issueType") or t.get("type") or ""
        total   = parse_total(t.get("Total") or t.get("total") or 0)
        ref     = t.get("Ref. Boleto") or t.get("ref") or t.get("id") or ""

        if tienda not in tiendas:
            tiendas[tienda] = {
                "nombre": tienda, "total": 0, "completados": 0, "wip": 0,
                "revenue": 0.0, "sin_asignar_rep": 0, "sin_asignar_vta": 0,
                "tecnicos": {}, "boletos_sin_asignar": []
            }
        td = tiendas[tienda]
        td["total"] += 1
        td["revenue"] += total
        if estado in EST_TERM:
            td["completados"] += 1
        elif estado in EST_ACT:
            td["wip"] += 1

        real_tec = is_real_tec(tecnico)
        es_venta = is_venta(tipo)
        if not real_tec:
            td["boletos_sin_asignar"].append({"ref": ref, "estado": estado, "tipo": tipo, "es_venta": es_venta})
            if es_venta:
                td["sin_asignar_vta"] += 1
            else:
                td["sin_asignar_rep"] += 1
        else:
            if tecnico not in td["tecnicos"]:
                td["tecnicos"][tecnico] = {"nombre": tecnico, "total": 0, "completados": 0, "wip": 0, "revenue": 0.0}
            tec = td["tecnicos"][tecnico]
            tec["total"] += 1
            tec["revenue"] += total
            if estado in EST_TERM:
                tec["completados"] += 1
            elif estado in EST_ACT:
                tec["wip"] += 1

    for td in tiendas.values():
        td["tecnicos"] = sorted(td["tecnicos"].values(), key=lambda x: x["total"], reverse=True)
        td["eficiencia"] = round(td["completados"] / td["total"] * 100) if td["total"] > 0 else 0

    total_red = sum(td["total"] for td in tiendas.values())
    comp_red  = sum(td["completados"] for td in tiendas.values())
    wip_red   = sum(td["wip"] for td in tiendas.values())
    rev_red   = sum(td["revenue"] for td in tiendas.values())
    sa_rep    = sum(td["sin_asignar_rep"] for td in tiendas.values())
    sa_vta    = sum(td["sin_asignar_vta"] for td in tiendas.values())

    return {
        "red": {
            "total": total_red, "completados": comp_red, "wip": wip_red,
            "revenue": round(rev_red, 2),
            "eficiencia": round(comp_red / total_red * 100) if total_red > 0 else 0,
            "sin_asignar_rep": sa_rep, "sin_asignar_vta": sa_vta
        },
        "tiendas": list(tiendas.values()),
        "total_tickets_raw": len(tickets),
        "campos_detectados": list(tickets[0].keys()) if tickets else [],
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def get_kpis_cached():
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL:
        tickets = fetch_all_tickets()
        _cache["data"] = compute_kpis(tickets)
        _cache["ts"] = now
    return _cache["data"]

@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "Tatmon Producción API", "version": "1.0"})

@app.route("/debug")
def debug():
    """Muestra configuración sin exponer la key completa."""
    key = MGR_KEY.strip()
    return jsonify({
        "key_length": len(key),
        "key_empty": key == "",
        "key_preview": key[:4] + "..." + key[-4:] if len(key) > 8 else "MUY_CORTA",
        "key_has_spaces": key != MGR_KEY,
        "header_sent": f"Authorization: {key[:4]}...{key[-4:]}",
        "env_vars": [k for k in os.environ.keys() if "MGR" in k or "API" in k]
    })

@app.route("/auth-test")
def auth_test():
    """Prueba 3 formatos de autenticación distintos contra MGR."""
    key = MGR_KEY.strip()
    results = {}

    formats = {
        "bare_key":      {"Authorization": key},
        "apiKey_prefix": {"Authorization": f"apiKey {key}"},
        "Bearer_prefix": {"Authorization": f"Bearer {key}"},
    }

    for name, headers in formats.items():
        headers["Accept"] = "application/json"
        try:
            r = requests.get(f"{MGR_BASE}/tickets", headers=headers, params={"page": 1}, timeout=10)
            results[name] = {"status": r.status_code, "ok": r.status_code == 200}
        except Exception as e:
            results[name] = {"status": "error", "msg": str(e)}

    return jsonify(results)

@app.route("/kpis")
def kpis():
    if not MGR_KEY:
        return jsonify({"error": "MGR_API_KEY no configurada"}), 500
    try:
        data = get_kpis_cached()
        return jsonify(data)
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
    key = MGR_KEY.strip()
    headers = {"Authorization": key, "Accept": "application/json"}
    try:
        r = requests.get(f"{MGR_BASE}/tickets", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            sample = data[:10]
        else:
            sample = (data.get("tickets") or data.get("data") or [])[:10]
        return jsonify({"sample": sample, "campos": list(sample[0].keys()) if sample else []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
