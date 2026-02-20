"""
monitor_contribuciones.py — Chequeo automatico de contribuciones + alerta por email y WhatsApp
Uso local:  python monitor_contribuciones.py
GitHub Actions: se ejecuta via cron, usa secrets para credenciales
"""
import json
import os
import smtplib
import sys
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# -- Config --------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "propiedades_monitor.json"

# Credenciales: vienen de env vars (GitHub Secrets) o se pueden hardcodear local
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO", "")

# WhatsApp Business API (Meta)
WA_TOKEN = os.environ.get("WA_TOKEN", "")
WA_PHONE_ID = os.environ.get("WA_PHONE_ID", "988131834391187")
WA_DESTINO = os.environ.get("WA_DESTINO", "56944084156")

# -- SII API Client -----------------------------------------------------------

SII_BASE = "https://www4.sii.cl/cuotaanualbienesraicespubinternetui"
SII_API = f"{SII_BASE}/services/data"


class SIIClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Accept-Language": "es-CL,es;q=0.9",
            "Referer": f"{SII_BASE}/",
        })
        self.conv_id = "MON_" + uuid.uuid4().hex[:10].upper()
        r = self.session.get(f"{SII_BASE}/", timeout=15)
        print(f"[SII] Sesion establecida (status={r.status_code})")

    def buscar(self, codigo_comuna, manzana, predio, retries=2):
        payload = {
            "metaData": {
                "namespace": "cl.sii.sdi.lob.bbrr.cuotaanualbienesraices.data.impl.PropiedadesApplicationService/buscarPropiedades",
                "conversationId": self.conv_id,
                "transactionId": str(uuid.uuid4()),
                "page": None,
            },
            "data": {
                "codigoComuna": str(codigo_comuna),
                "manzana": str(manzana),
                "predio": str(predio),
            },
        }
        import time
        for attempt in range(retries + 1):
            r = self.session.post(
                f"{SII_API}/propiedadesService/buscarPropiedades",
                json=payload, timeout=20,
            )
            if r.text and r.text.strip().startswith("{"):
                data = r.json().get("data", [])
                return data[0] if data else None
            # Respuesta vacia, reiniciar sesion y reintentar
            if attempt < retries:
                time.sleep(2)
                self.session.get(f"{SII_BASE}/", timeout=15)
                self.conv_id = "MON_" + uuid.uuid4().hex[:10].upper()
        return None


# -- Analisis ------------------------------------------------------------------

def analizar_propiedad(prop_raw):
    """Extrae info relevante de la respuesta SII."""
    cuotas_raw = prop_raw.get("cuotas", {})
    tiene_cuotas = prop_raw.get("tieneCuotas", 0)

    cuotas_vencidas = []
    cuotas_vigentes = []
    deuda_total = 0

    for key, c in cuotas_raw.items():
        condicion = c.get("condicion", "").upper()
        total_pago = c.get("totalPago", 0)
        info = {
            "cuota": c.get("cuota", 0),
            "agno": c.get("agno", 0),
            "vencimiento": c.get("fechaVencimiento", ""),
            "valor": c.get("valor", 0),
            "total_pago": total_pago,
            "intereses": c.get("intereses", 0),
            "reajustes": c.get("reajustes", 0),
        }
        if condicion == "VENCIDA":
            cuotas_vencidas.append(info)
            deuda_total += total_pago
        elif condicion == "VIGENTE":
            cuotas_vigentes.append(info)

    return {
        "rol": prop_raw.get("rol", ""),
        "direccion": prop_raw.get("direccion", "").strip(),
        "propietario": prop_raw.get("nombrePropietario", "").strip(),
        "destino": prop_raw.get("destino", "").strip(),
        "comuna": prop_raw.get("descripcionComuna", "").strip(),
        "exento": tiene_cuotas == 0,
        "deuda_total": deuda_total,
        "cuotas_vencidas": cuotas_vencidas,
        "cuotas_vigentes": cuotas_vigentes,
        "n_vencidas": len(cuotas_vencidas),
        "n_vigentes": len(cuotas_vigentes),
    }


# -- Chequeo de umbrales ------------------------------------------------------

def chequear_alertas(resultados, config):
    """Revisa cada propiedad contra los umbrales configurados."""
    umbral_deuda = config.get("umbral_deuda", 500000)
    umbral_cuotas = config.get("umbral_cuotas_vencidas", 2)
    alertas = []

    for r in resultados:
        motivos = []
        if r["deuda_total"] > umbral_deuda:
            motivos.append(
                f"Deuda ${r['deuda_total']:,.0f} supera umbral ${umbral_deuda:,.0f}"
            )
        if r["n_vencidas"] >= umbral_cuotas:
            motivos.append(
                f"{r['n_vencidas']} cuotas vencidas (umbral: {umbral_cuotas})"
            )
        if motivos:
            alertas.append({"propiedad": r, "motivos": motivos})

    return alertas


# -- Email ---------------------------------------------------------------------

def construir_html(alertas, resultados, config):
    """Genera el HTML del correo de alerta."""
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Resumen rapido
    total_props = len(resultados)
    total_deuda = sum(r["deuda_total"] for r in resultados)
    con_deuda = sum(1 for r in resultados if r["deuda_total"] > 0)
    exentas = sum(1 for r in resultados if r["exento"])

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;background:#fafafa;padding:0;">
      <!-- Header -->
      <div style="background:#0a0a0a;color:#fff;padding:24px 28px;">
        <h1 style="margin:0;font-size:20px;font-weight:600;">
          Contribuciones CL
          <span style="color:#22c55e;font-weight:400;"> | Alerta</span>
        </h1>
        <p style="margin:6px 0 0;font-size:12px;color:#888;">{fecha} | {total_props} propiedades monitoreadas</p>
      </div>

      <!-- Alertas -->
      {"".join(f'''
      <div style="margin:16px 20px;background:#fff;border:1px solid #fecaca;border-left:4px solid #ef4444;border-radius:8px;padding:16px 20px;">
        <div style="font-size:14px;font-weight:600;color:#111;">
          ROL {a["propiedad"]["rol"]} | {a["propiedad"]["comuna"]}
        </div>
        <div style="font-size:12px;color:#666;margin:4px 0 10px;">
          {a["propiedad"]["direccion"]} | {a["propiedad"]["destino"]}
        </div>
        {"".join(f'<div style="font-size:13px;color:#dc2626;margin:3px 0;">&#9888; {m}</div>' for m in a["motivos"])}
        <div style="margin-top:12px;display:flex;gap:16px;">
          <div style="background:#fef2f2;border-radius:6px;padding:10px 16px;flex:1;">
            <div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:1px;">Deuda Total</div>
            <div style="font-size:18px;font-weight:700;color:#dc2626;font-family:monospace;">
              ${a["propiedad"]["deuda_total"]:,.0f}
            </div>
          </div>
          <div style="background:#fef2f2;border-radius:6px;padding:10px 16px;">
            <div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:1px;">Cuotas Vencidas</div>
            <div style="font-size:18px;font-weight:700;color:#dc2626;font-family:monospace;">
              {a["propiedad"]["n_vencidas"]}
            </div>
          </div>
        </div>
        {"".join(f"""
        <div style="margin-top:8px;font-size:11px;color:#666;font-family:monospace;background:#f9f9f9;padding:6px 10px;border-radius:4px;">
          C{cv["cuota"]} {cv["agno"]} | Vto: {cv["vencimiento"]} | Base: ${cv["valor"]:,.0f} | Total: ${cv["total_pago"]:,.0f} (int: ${cv["intereses"]:,.0f})
        </div>
        """ for cv in a["propiedad"]["cuotas_vencidas"])}
      </div>
      ''' for a in alertas) if alertas else '<div style="margin:20px;padding:20px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;text-align:center;color:#166534;font-size:14px;">Todas las propiedades dentro de los umbrales configurados.</div>'}

      <!-- Resumen -->
      <div style="margin:16px 20px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;">
        <div style="font-size:12px;font-weight:600;color:#111;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px;">Resumen del portafolio</div>
        <table style="width:100%;font-size:12px;border-collapse:collapse;">
          <tr style="background:#f8f8f8;">
            <td style="padding:8px 12px;border-bottom:1px solid #eee;"><b>Total propiedades</b></td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-family:monospace;">{total_props}</td>
          </tr>
          <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;"><b>Con deuda</b></td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-family:monospace;color:#dc2626;">{con_deuda}</td>
          </tr>
          <tr style="background:#f8f8f8;">
            <td style="padding:8px 12px;border-bottom:1px solid #eee;"><b>Exentas</b></td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-family:monospace;">{exentas}</td>
          </tr>
          <tr>
            <td style="padding:8px 12px;"><b>Deuda total portafolio</b></td>
            <td style="padding:8px 12px;text-align:right;font-family:monospace;font-size:14px;font-weight:700;color:#dc2626;">${total_deuda:,.0f}</td>
          </tr>
        </table>
      </div>

      <!-- Detalle todas -->
      <div style="margin:16px 20px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;">
        <div style="font-size:12px;font-weight:600;color:#111;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px;">Detalle por propiedad</div>
        <table style="width:100%;font-size:11px;border-collapse:collapse;">
          <tr style="background:#0a0a0a;color:#fff;">
            <th style="padding:8px;text-align:left;">ROL</th>
            <th style="padding:8px;text-align:left;">Comuna</th>
            <th style="padding:8px;text-align:left;">Destino</th>
            <th style="padding:8px;text-align:right;">Deuda</th>
            <th style="padding:8px;text-align:center;">Vencidas</th>
            <th style="padding:8px;text-align:center;">Estado</th>
          </tr>
          {"".join(f'''
          <tr style="background:{'#fef2f2' if r['deuda_total']>0 else '#f0fdf4' if not r['exento'] else '#f8f8f8'};">
            <td style="padding:8px;font-family:monospace;font-weight:600;border-bottom:1px solid #eee;">{r['rol']}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;">{r['comuna']}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;">{r['destino']}</td>
            <td style="padding:8px;text-align:right;font-family:monospace;border-bottom:1px solid #eee;color:{'#dc2626' if r['deuda_total']>0 else '#166534'};">
              {"$"+f"{r['deuda_total']:,.0f}" if r['deuda_total']>0 else "$0"}
            </td>
            <td style="padding:8px;text-align:center;font-family:monospace;border-bottom:1px solid #eee;color:{'#dc2626' if r['n_vencidas']>0 else '#666'};">
              {r['n_vencidas']}
            </td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #eee;">
              <span style="background:{'#fecaca' if r['deuda_total']>0 else '#bbf7d0' if not r['exento'] else '#e5e7eb'};color:{'#991b1b' if r['deuda_total']>0 else '#166534' if not r['exento'] else '#666'};padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">
                {'DEUDA' if r['deuda_total']>0 else 'AL DIA' if not r['exento'] else 'EXENTO'}
              </span>
            </td>
          </tr>
          ''' for r in resultados)}
        </table>
      </div>

      <!-- Footer -->
      <div style="padding:16px 20px;font-size:10px;color:#aaa;text-align:center;">
        Contribuciones CL Monitor | Umbrales: deuda &gt; ${config.get('umbral_deuda',0):,.0f} | cuotas vencidas &ge; {config.get('umbral_cuotas_vencidas',0)}
      </div>
    </div>
    """
    return html


def enviar_email(destinatario, asunto, html_body):
    """Envia email via Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("[EMAIL] GMAIL_USER o GMAIL_APP_PASSWORD no configurados.")
        print("[EMAIL] Guardando email como HTML local...")
        out = Path(__file__).parent / "ultimo_reporte.html"
        out.write_text(html_body, encoding="utf-8")
        print(f"[EMAIL] Guardado en: {out}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = GMAIL_USER
    msg["To"] = destinatario
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"[EMAIL] Enviado a {destinatario}")
        return True
    except Exception as e:
        print(f"[EMAIL] Error: {e}")
        return False


def enviar_whatsapp(alertas, resultados):
    """Envia resumen por WhatsApp via Meta Business API."""
    if not WA_TOKEN:
        print("[WA] WA_TOKEN no configurado. Omitiendo WhatsApp.")
        return False

    # Construir mensaje de texto
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    total_deuda = sum(r["deuda_total"] for r in resultados)
    lineas = [f"*CONTRIBUCIONES CL* | {fecha}", ""]

    if alertas:
        lineas.append(f"⚠ *{len(alertas)} ALERTA(S):*")
        for a in alertas:
            p = a["propiedad"]
            lineas.append(f"")
            lineas.append(f"*ROL {p['rol']}* | {p['comuna']}")
            lineas.append(f"{p['direccion']} | {p['destino']}")
            for m in a["motivos"]:
                lineas.append(f"  → {m}")
            for cv in p["cuotas_vencidas"]:
                lineas.append(f"  C{cv['cuota']} {cv['agno']} | Vto: {cv['vencimiento']} | ${cv['total_pago']:,.0f}")
    else:
        lineas.append("✅ Todas las propiedades dentro de los umbrales.")

    lineas.append("")
    lineas.append(f"*Portafolio:* {len(resultados)} propiedades | Deuda total: ${total_deuda:,.0f}")

    texto = "\n".join(lineas)

    try:
        r = requests.post(
            f"https://graph.facebook.com/v22.0/{WA_PHONE_ID}/messages",
            headers={
                "Authorization": f"Bearer {WA_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": WA_DESTINO,
                "type": "text",
                "text": {"body": texto},
            },
            timeout=15,
        )
        data = r.json()
        if "messages" in data:
            print(f"[WA] Enviado a +{WA_DESTINO}")
            return True
        else:
            print(f"[WA] Error: {data.get('error', {}).get('message', data)}")
            return False
    except Exception as e:
        print(f"[WA] Error: {e}")
        return False


# -- Main ----------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  CONTRIBUCIONES CL -- Monitor Automatico")
    print("=" * 55)
    print(f"  Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print()

    # Cargar config
    if not CONFIG_FILE.exists():
        print(f"[ERROR] No se encontro {CONFIG_FILE}")
        sys.exit(1)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    config = cfg["config"]
    propiedades = cfg["propiedades"]
    email_dest = EMAIL_DESTINO or config.get("email_destino", "")

    print(f"  Propiedades: {len(propiedades)}")
    print(f"  Umbral deuda: ${config['umbral_deuda']:,.0f}")
    print(f"  Umbral cuotas vencidas: {config['umbral_cuotas_vencidas']}")
    print(f"  Email destino: {email_dest}")
    print()

    # Conectar SII
    client = SIIClient()
    print()

    # Consultar cada propiedad
    resultados = []
    for prop in propiedades:
        desc = prop.get("descripcion", f"ROL {prop['rol']}-{prop['subrol']}")
        print(f"  Consultando: {desc}...", end=" ")
        try:
            raw = client.buscar(prop["codigo_comuna"], prop["rol"], prop["subrol"])
            if raw:
                info = analizar_propiedad(raw)
                info["descripcion"] = desc
                resultados.append(info)
                estado = "DEUDA" if info["deuda_total"] > 0 else "EXENTO" if info["exento"] else "AL DIA"
                print(f"{estado} | ${info['deuda_total']:,.0f} | {info['n_vencidas']} vencidas")
            else:
                print("NO ENCONTRADA")
        except Exception as e:
            print(f"ERROR: {e}")

    print()

    # Chequear alertas
    alertas = chequear_alertas(resultados, config)

    if alertas:
        print(f"  *** {len(alertas)} ALERTA(S) DETECTADA(S) ***")
        for a in alertas:
            print(f"    ROL {a['propiedad']['rol']}: {', '.join(a['motivos'])}")
    else:
        print("  [OK] Todas las propiedades dentro de los umbrales.")

    print()

    # Construir y enviar email
    n_alertas = len(alertas)
    if n_alertas > 0:
        asunto = f"[ALERTA] Contribuciones CL: {n_alertas} propiedad(es) con problemas"
    else:
        asunto = f"[OK] Contribuciones CL: {len(resultados)} propiedades monitoreadas"

    html = construir_html(alertas, resultados, config)
    enviar_email(email_dest, asunto, html)

    # Enviar WhatsApp
    enviar_whatsapp(alertas, resultados)

    print()
    print("  Monitor finalizado.")
    print("=" * 55)

    # Exit code para GitHub Actions
    sys.exit(0)


if __name__ == "__main__":
    main()
