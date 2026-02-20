"""
backend_con_historial.py — Servidor Flask con API REST SII + historial SQLite
Uso: python backend_con_historial.py
"""
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from datetime import datetime
from pathlib import Path
import os
import requests as http_requests
import json
import uuid

from db_manager import (
    init_db, upsert_propiedad, get_todas_propiedades,
    get_propiedad, eliminar_propiedad, update_propiedad,
    guardar_consulta, get_historial, get_resumen_global
)

app = Flask(__name__)
CORS(app)

# ── Cargar comunas SII ───────────────────────────────────────────────────────
COMUNAS_JSON = Path(__file__).parent / "sii_comunas.json"
COMUNAS_SII = {}        # {"TEMUCO": "9201", ...}
COMUNAS_POR_REGION = {} # {"9": [{"codigo":"9201","nombre":"TEMUCO"}, ...]}
REGIONES_SII = []       # [{"codigo":"15","nombre":"REGION DE ARICA..."}]

REGIONES_NOMBRES = {
    "15": "XV - Arica y Parinacota", "1": "I - Tarapaca", "2": "II - Antofagasta",
    "3": "III - Atacama", "4": "IV - Coquimbo", "5": "V - Valparaiso",
    "13": "RM - Metropolitana", "6": "VI - O'Higgins", "7": "VII - Maule",
    "16": "XVI - Nuble", "8": "VIII - Biobio", "9": "IX - Araucania",
    "14": "XIV - Los Rios", "10": "X - Los Lagos", "11": "XI - Aysen",
    "12": "XII - Magallanes",
}

def cargar_comunas():
    global COMUNAS_SII, COMUNAS_POR_REGION, REGIONES_SII
    if not COMUNAS_JSON.exists():
        print("[WARN] sii_comunas.json no encontrado. Ejecutar captura inicial.")
        return
    with open(COMUNAS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    COMUNAS_POR_REGION = data
    for reg_code, comunas in data.items():
        for c in comunas:
            nombre = c["nombre"].strip().title()
            COMUNAS_SII[nombre] = c["codigo"]
            COMUNAS_SII[c["nombre"].strip()] = c["codigo"]  # uppercase too
    REGIONES_SII = [
        {"codigo": k, "nombre": REGIONES_NOMBRES.get(k, f"Region {k}")}
        for k in ["15","1","2","3","4","5","13","6","7","16","8","9","14","10","11","12"]
        if k in data
    ]
    print(f"[OK] {len(COMUNAS_SII)//2} comunas cargadas de {len(data)} regiones")


# ── Cliente SII API ──────────────────────────────────────────────────────────
SII_BASE = "https://www4.sii.cl/cuotaanualbienesraicespubinternetui"
SII_API = f"{SII_BASE}/services/data"

class SIIClient:
    """Wrapper para la API REST interna del SII."""

    def __init__(self):
        self.session = http_requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Accept-Language": "es-CL,es;q=0.9",
            "Referer": f"{SII_BASE}/",
        })
        self.conv_id = None
        self._init_session()

    def _init_session(self):
        """Establece cookies visitando la pagina principal."""
        try:
            r = self.session.get(f"{SII_BASE}/", timeout=15)
            self.conv_id = "PY_" + uuid.uuid4().hex[:10].upper()
            print(f"[OK] Sesion SII establecida (status={r.status_code})")
        except Exception as e:
            print(f"[WARN] No se pudo conectar al SII: {e}")
            self.conv_id = "OFFLINE_" + uuid.uuid4().hex[:8].upper()

    def _post(self, endpoint, namespace, data):
        payload = {
            "metaData": {
                "namespace": f"cl.sii.sdi.lob.bbrr.cuotaanualbienesraices.data.impl.{namespace}",
                "conversationId": self.conv_id,
                "transactionId": str(uuid.uuid4()),
                "page": None,
            },
            "data": data,
        }
        r = self.session.post(f"{SII_API}/{endpoint}", json=payload, timeout=20)
        resp = r.json()
        errors = resp.get("metaData", {}).get("errors")
        if errors:
            raise Exception(f"SII error: {errors}")
        return resp.get("data", [])

    def buscar_propiedad(self, codigo_comuna: str, manzana: str, predio: str) -> dict:
        """Busca una propiedad y sus cuotas pendientes en el SII."""
        data = self._post(
            "propiedadesService/buscarPropiedades",
            "PropiedadesApplicationService/buscarPropiedades",
            {"codigoComuna": codigo_comuna, "manzana": manzana, "predio": predio},
        )
        if not data:
            return {"error": "Propiedad no encontrada en SII. Verificar ROL y comuna."}

        prop = data[0]
        resultado = self._parsear_propiedad(prop)
        return resultado

    def get_historial_pagos(self, codigo_comuna: str, manzana: str, predio: str) -> list:
        """Obtiene historial de pagos anteriores."""
        try:
            data = self._post(
                "pagoService/getPagos",
                "PagoApplicationService/getPagos",
                {"codigoComuna": codigo_comuna, "manzana": manzana, "predio": predio},
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _parsear_propiedad(self, prop: dict) -> dict:
        """Convierte la respuesta SII al formato interno."""
        cuotas_raw = prop.get("cuotas", {})
        tiene_cuotas = prop.get("tieneCuotas", 0)

        nombres_cuota = {
            1: "1a Cuota (Abril)",
            2: "2a Cuota (Junio)",
            3: "3a Cuota (Septiembre)",
            4: "4a Cuota (Noviembre)",
        }

        cuotas = []
        deuda_total = 0

        if tiene_cuotas > 0 and cuotas_raw:
            for key, c in cuotas_raw.items():
                num_cuota = c.get("cuota", int(key))
                condicion = c.get("condicion", "").upper()

                if condicion == "VENCIDA":
                    estado = "vencida"
                    deuda_total += c.get("totalPago", 0)
                elif condicion == "VIGENTE":
                    estado = "pendiente"
                else:
                    estado = "sin_datos"

                cuotas.append({
                    "numero": num_cuota,
                    "nombre": nombres_cuota.get(num_cuota, f"Cuota {num_cuota}"),
                    "monto": c.get("valor", 0),
                    "total_pago": c.get("totalPago", 0),
                    "contribucion_neta": c.get("contribucionNeta", 0),
                    "reajustes": c.get("reajustes", 0),
                    "intereses": c.get("intereses", 0),
                    "condonacion": c.get("condonacion", 0),
                    "vencimiento": c.get("fechaVencimiento", ""),
                    "estado": estado,
                    "agno": c.get("agno", datetime.now().year),
                })

        # Si no tiene cuotas pendientes, crear placeholders con estado "pagada" o "exento"
        if not cuotas:
            y = datetime.now().year
            vcts = [f"30-04-{y}", f"30-06-{y}", f"30-09-{y}", f"30-11-{y}"]
            estado_base = "exento" if tiene_cuotas == 0 else "pagada"
            cuotas = [
                {"numero": i+1, "nombre": nombres_cuota[i+1], "monto": 0,
                 "total_pago": 0, "contribucion_neta": 0, "reajustes": 0,
                 "intereses": 0, "condonacion": 0,
                 "vencimiento": vcts[i], "estado": estado_base, "agno": y}
                for i in range(4)
            ]

        cuotas.sort(key=lambda c: c["numero"])

        return {
            "rol": prop.get("rol", f"{prop.get('manzana','')}-{prop.get('predio','')}"),
            "direccion": prop.get("direccion", "").strip(),
            "propietario": prop.get("nombrePropietario", "").strip(),
            "destino": prop.get("destino", "").strip(),
            "comuna_descripcion": prop.get("descripcionComuna", "").strip(),
            "exento": tiene_cuotas == 0,
            "cuotas": cuotas,
            "deuda_total": deuda_total,
            "tiene_cuotas": tiene_cuotas,
            "codigo_tgr": prop.get("codigoTgr", 0),
            "ultima_actualizacion": datetime.now().isoformat(),
            "fuente": "SII.cl (API directa)",
        }


# Instancia global del cliente SII
sii_client = None

def get_sii_client():
    global sii_client
    if sii_client is None:
        sii_client = SIIClient()
    return sii_client


# ── Mock para testing sin SII ────────────────────────────────────────────────

def mock_scrape(rol, subrol, region, comuna):
    """Datos simulados para testing sin conexion al SII."""
    seed = (int(rol) if rol.isdigit() else sum(ord(c) for c in rol)) * 7
    avaluo = 25_000_000 + (seed % 17) * 8_500_000
    cuota_base = round(avaluo * 0.0056 / 4)
    y = datetime.now().year
    estados_prop = ["al_dia", "al_dia", "al_dia", "deuda_1", "deuda_2", "exento"]
    ep = estados_prop[seed % len(estados_prop)]
    noms = {1: "1a Cuota (Abril)", 2: "2a Cuota (Junio)", 3: "3a Cuota (Septiembre)", 4: "4a Cuota (Noviembre)"}
    vcts = [f"30-04-{y}", f"30-06-{y}", f"30-09-{y}", f"30-11-{y}"]
    cuotas = []
    for i in range(4):
        monto = 0 if ep == "exento" else round(cuota_base * (1 + i * 0.025))
        if ep == "exento": estado = "exento"
        elif ep == "al_dia": estado = "pagada" if i < 2 else "pendiente"
        elif ep == "deuda_1": estado = "pagada" if i == 0 else ("vencida" if i == 1 else "pendiente")
        else: estado = "pagada" if i == 0 else "vencida"
        cuotas.append({
            "numero": i+1, "nombre": noms[i+1], "monto": monto,
            "total_pago": monto, "contribucion_neta": monto,
            "reajustes": 0, "intereses": 0, "condonacion": 0,
            "vencimiento": vcts[i], "estado": estado, "agno": y,
        })
    destinos = ["Habitacional", "No Habitacional", "Agricola"]
    return {
        "rol": f"{rol}-{subrol}",
        "direccion": "Direccion simulada 123",
        "propietario": "Propietario Demo",
        "destino": destinos[seed % 3],
        "comuna_descripcion": comuna,
        "exento": ep == "exento",
        "cuotas": cuotas,
        "deuda_total": sum(c["monto"] for c in cuotas if c["estado"] == "vencida"),
        "tiene_cuotas": 0 if ep == "exento" else 4,
        "ultima_actualizacion": datetime.now().isoformat(),
        "fuente": "Demo (datos simulados)",
    }


# ── RUTAS ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Sirve la app web."""
    return send_file("contribuciones_con_historial.html")


@app.route("/health")
def health():
    client = get_sii_client()
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "db": "contribuciones.db",
        "sii_session": client.conv_id is not None,
    })


@app.route("/propiedades", methods=["GET"])
def listar_propiedades():
    return jsonify(get_todas_propiedades())


@app.route("/propiedades", methods=["POST"])
def agregar_propiedad():
    d = request.json
    prop_id = upsert_propiedad(
        d["rol"], d.get("subrol", "0"),
        d.get("region_code", ""), d["comuna"],
        d.get("codigo_comuna", ""),
        d.get("descripcion", ""),
        d.get("encargado", "")
    )
    return jsonify({"id": prop_id, "status": "ok"})


@app.route("/propiedades/<int:prop_id>", methods=["PATCH"])
def actualizar_propiedad(prop_id):
    d = request.json
    update_propiedad(prop_id, d)
    return jsonify({"status": "ok"})


@app.route("/propiedades/<int:prop_id>", methods=["DELETE"])
def borrar_propiedad(prop_id):
    eliminar_propiedad(prop_id)
    return jsonify({"status": "ok"})


@app.route("/consultar")
def consultar():
    rol = request.args.get("rol", "").strip()
    subrol = request.args.get("subrol", "0").strip()
    comuna = request.args.get("comuna", "").strip()
    codigo_comuna = request.args.get("codigo_comuna", "").strip()
    region_code = request.args.get("region", "").strip()
    descripcion = request.args.get("descripcion", "").strip()
    encargado = request.args.get("encargado", "").strip()
    demo = request.args.get("demo", "false").lower() == "true"

    if not rol:
        return jsonify({"error": "Parametro requerido: rol"}), 400

    # Resolver codigo_comuna si no viene directo
    if not codigo_comuna and comuna:
        codigo_comuna = COMUNAS_SII.get(comuna, COMUNAS_SII.get(comuna.upper(), ""))
    if not codigo_comuna and not demo:
        return jsonify({"error": f"Comuna '{comuna}' no reconocida. Usar codigo_comuna directo."}), 400

    # Registrar propiedad en BD
    prop_id = upsert_propiedad(rol, subrol, region_code, comuna, codigo_comuna, descripcion, encargado)

    if demo:
        resultado = mock_scrape(rol, subrol, region_code, comuna)
    else:
        try:
            client = get_sii_client()
            resultado = client.buscar_propiedad(codigo_comuna, rol, subrol)
        except Exception as e:
            return jsonify({"error": f"Error consultando SII: {str(e)}"}), 502

    if "error" not in resultado:
        resultado = guardar_consulta(prop_id, resultado)

    resultado["propiedad_id"] = prop_id
    return jsonify(resultado)


@app.route("/historial/<int:prop_id>")
def historial(prop_id):
    limite = int(request.args.get("limite", 20))
    prop = get_propiedad(prop_id)
    if not prop:
        return jsonify({"error": "Propiedad no encontrada"}), 404
    return jsonify({
        "propiedad": prop,
        "historial": get_historial(prop_id, limite),
    })


@app.route("/pagos-sii")
def pagos_sii():
    """Obtiene historial de pagos directamente desde SII."""
    codigo_comuna = request.args.get("codigo_comuna", "").strip()
    manzana = request.args.get("rol", "").strip()
    predio = request.args.get("subrol", "0").strip()
    if not codigo_comuna or not manzana:
        return jsonify({"error": "Parametros requeridos: codigo_comuna, rol"}), 400
    try:
        client = get_sii_client()
        pagos = client.get_historial_pagos(codigo_comuna, manzana, predio)
        return jsonify({"data": pagos})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/resumen")
def resumen():
    return jsonify(get_resumen_global())


@app.route("/regiones")
def regiones():
    return jsonify(REGIONES_SII)


@app.route("/comunas")
def comunas():
    region = request.args.get("region", "").strip()
    if region:
        return jsonify(COMUNAS_POR_REGION.get(region, []))
    # Retornar todas
    flat = []
    for reg, coms in COMUNAS_POR_REGION.items():
        for c in coms:
            flat.append({"region": reg, **c})
    return jsonify(flat)


@app.route("/reset-session", methods=["POST"])
def reset_session():
    """Reinicia la sesion con SII si expira."""
    global sii_client
    sii_client = SIIClient()
    return jsonify({"status": "ok", "conv_id": sii_client.conv_id})


if __name__ == "__main__":
    init_db()
    cargar_comunas()
    get_sii_client()
    print()
    print("=" * 55)
    print("  CONTRIBUCIONES CL -- Backend v3 API REST SII")
    print("=" * 55)
    print(f"  Servidor:   http://localhost:5000")
    print(f"  Base datos: contribuciones.db")
    print(f"  Comunas:    {len(COMUNAS_SII)//2} cargadas")
    print()
    print("  Endpoints:")
    print("  GET  /health")
    print("  GET  /propiedades")
    print("  POST /propiedades")
    print("  GET  /consultar?rol=X&subrol=0&codigo_comuna=9201")
    print("  GET  /consultar?...&demo=true  (sin SII)")
    print("  GET  /historial/:id")
    print("  GET  /pagos-sii?codigo_comuna=9201&rol=13&subrol=1")
    print("  GET  /resumen")
    print("  GET  /regiones")
    print("  GET  /comunas?region=9")
    print("  POST /reset-session")
    print("=" * 55)
    print()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
