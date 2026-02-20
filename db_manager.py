"""
db_manager.py -- Base de datos SQLite para historial de contribuciones
Ejecutar una vez para inicializar: python db_manager.py
"""
import os
import sqlite3
import json
from datetime import datetime
from pathlib import Path

# Si hay volumen Railway, usar ese path; sino usar local
DB_PATH = Path(os.environ.get("DATABASE_PATH", Path(__file__).parent / "contribuciones.db"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Crea las tablas si no existen."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS propiedades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            rol             TEXT    NOT NULL,
            subrol          TEXT    NOT NULL DEFAULT '0',
            region_code     TEXT    NOT NULL DEFAULT '',
            comuna          TEXT    NOT NULL DEFAULT '',
            codigo_comuna   TEXT    NOT NULL DEFAULT '',
            descripcion     TEXT    DEFAULT '',
            encargado       TEXT    DEFAULT '',
            activa          INTEGER DEFAULT 1,
            created_at      TEXT    DEFAULT (datetime('now','localtime'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_prop_rol
            ON propiedades(rol, subrol, codigo_comuna);

        CREATE TABLE IF NOT EXISTS consultas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            propiedad_id    INTEGER NOT NULL REFERENCES propiedades(id),
            fecha_consulta  TEXT    DEFAULT (datetime('now','localtime')),
            deuda_total     INTEGER DEFAULT 0,
            exento          INTEGER DEFAULT 0,
            destino         TEXT    DEFAULT '',
            direccion       TEXT    DEFAULT '',
            propietario     TEXT    DEFAULT '',
            tiene_cuotas    INTEGER DEFAULT 0,
            cuotas_json     TEXT    DEFAULT '[]',
            cambios_json    TEXT    DEFAULT '[]',
            fuente          TEXT    DEFAULT 'SII.cl'
        );

        CREATE INDEX IF NOT EXISTS idx_consultas_prop
            ON consultas(propiedad_id, fecha_consulta DESC);
    """)
    conn.commit()

    # Migracion: agregar columna encargado si no existe (para DBs existentes)
    try:
        conn.execute("SELECT encargado FROM propiedades LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE propiedades ADD COLUMN encargado TEXT DEFAULT ''")
        conn.commit()

    conn.close()
    print(f"[OK] Base de datos lista en: {DB_PATH}")


# -- PROPIEDADES ---------------------------------------------------------------

def upsert_propiedad(rol, subrol, region_code, comuna, codigo_comuna="", descripcion="", encargado=""):
    """Inserta o retorna la propiedad. Devuelve el id."""
    conn = get_conn()
    cur = conn.execute(
        "SELECT id FROM propiedades WHERE rol=? AND subrol=? AND codigo_comuna=?",
        (rol, subrol, codigo_comuna)
    )
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE propiedades SET descripcion=?, region_code=?, comuna=?, encargado=COALESCE(NULLIF(?,''),(SELECT encargado FROM propiedades WHERE id=?)), activa=1 WHERE id=?",
            (descripcion, region_code, comuna, encargado, row["id"], row["id"])
        )
        conn.commit()
        prop_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO propiedades(rol,subrol,region_code,comuna,codigo_comuna,descripcion,encargado) VALUES(?,?,?,?,?,?,?)",
            (rol, subrol, region_code, comuna, codigo_comuna, descripcion, encargado)
        )
        conn.commit()
        prop_id = cur.lastrowid
    conn.close()
    return prop_id


def get_todas_propiedades():
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.*,
               c.fecha_consulta as ultima_consulta,
               c.deuda_total as ultima_deuda,
               c.exento as ultimo_exento,
               c.direccion as ultima_direccion,
               c.propietario as ultimo_propietario,
               c.destino as ultimo_destino,
               c.tiene_cuotas as ultimo_tiene_cuotas,
               c.cuotas_json
        FROM propiedades p
        LEFT JOIN consultas c ON c.id = (
            SELECT id FROM consultas WHERE propiedad_id=p.id ORDER BY fecha_consulta DESC LIMIT 1
        )
        WHERE p.activa = 1
        ORDER BY p.id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_propiedad(prop_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM propiedades WHERE id=?", (prop_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def eliminar_propiedad(prop_id):
    conn = get_conn()
    conn.execute("UPDATE propiedades SET activa=0 WHERE id=?", (prop_id,))
    conn.commit()
    conn.close()


def update_propiedad(prop_id, campos: dict):
    """Actualiza campos permitidos de una propiedad."""
    conn = get_conn()
    allowed = ["encargado", "descripcion"]
    sets = []
    vals = []
    for k in allowed:
        if k in campos:
            sets.append(f"{k}=?")
            vals.append(campos[k])
    if sets:
        vals.append(prop_id)
        conn.execute(f"UPDATE propiedades SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
    conn.close()


# -- CONSULTAS / HISTORIAL -----------------------------------------------------

def guardar_consulta(propiedad_id, datos: dict) -> dict:
    """
    Guarda una consulta y compara con la anterior para detectar cambios.
    Retorna los datos enriquecidos con el campo 'cambios'.
    """
    conn = get_conn()

    # Obtener ultima consulta anterior
    ultima = conn.execute(
        "SELECT cuotas_json, deuda_total FROM consultas WHERE propiedad_id=? ORDER BY fecha_consulta DESC LIMIT 1",
        (propiedad_id,)
    ).fetchone()

    # Detectar cambios entre consultas
    cambios = []
    if ultima:
        cuotas_anterior = json.loads(ultima["cuotas_json"])
        cuotas_nueva = datos.get("cuotas", [])
        estados_ant = {c["numero"]: c["estado"] for c in cuotas_anterior}
        for cuota in cuotas_nueva:
            n = cuota["numero"]
            estado_ant = estados_ant.get(n)
            estado_nuevo = cuota["estado"]
            if estado_ant and estado_ant != estado_nuevo:
                cambios.append({
                    "cuota": n,
                    "nombre": cuota.get("nombre", f"Cuota {n}"),
                    "de": estado_ant,
                    "a": estado_nuevo,
                    "fecha": datetime.now().isoformat(),
                    "monto": cuota.get("monto", 0),
                })

        # Comparar deuda
        deuda_ant = ultima["deuda_total"] or 0
        deuda_nueva = datos.get("deuda_total", 0)
        if deuda_nueva < deuda_ant and deuda_ant > 0:
            cambios.append({
                "tipo": "pago_registrado",
                "monto_pagado": deuda_ant - deuda_nueva,
                "fecha": datetime.now().isoformat(),
            })

    cuotas_json = json.dumps(datos.get("cuotas", []), ensure_ascii=False)
    cambios_json = json.dumps(cambios, ensure_ascii=False)

    conn.execute("""
        INSERT INTO consultas
            (propiedad_id, deuda_total, exento, destino, direccion, propietario,
             tiene_cuotas, cuotas_json, cambios_json, fuente)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        propiedad_id,
        datos.get("deuda_total", 0),
        1 if datos.get("exento") else 0,
        datos.get("destino", ""),
        datos.get("direccion", ""),
        datos.get("propietario", ""),
        datos.get("tiene_cuotas", 0),
        cuotas_json,
        cambios_json,
        datos.get("fuente", "SII.cl"),
    ))
    conn.commit()
    conn.close()

    datos["cambios"] = cambios
    return datos


def get_historial(propiedad_id, limite=20):
    """Retorna las ultimas N consultas de una propiedad."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM consultas
        WHERE propiedad_id = ?
        ORDER BY fecha_consulta DESC
        LIMIT ?
    """, (propiedad_id, limite)).fetchall()
    conn.close()
    historial = []
    for r in rows:
        d = dict(r)
        d["cuotas"] = json.loads(d.pop("cuotas_json", "[]"))
        d["cambios"] = json.loads(d.pop("cambios_json", "[]"))
        historial.append(d)
    return historial


def get_resumen_global():
    """Estadisticas generales del portafolio."""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM propiedades WHERE activa=1").fetchone()[0]

    ultima_data = conn.execute("""
        SELECT c.deuda_total, c.exento
        FROM propiedades p
        JOIN consultas c ON c.id = (
            SELECT id FROM consultas WHERE propiedad_id=p.id ORDER BY fecha_consulta DESC LIMIT 1
        )
        WHERE p.activa=1
    """).fetchall()
    conn.close()

    deuda_total = sum(r[0] for r in ultima_data if r[0])
    exentas = sum(1 for r in ultima_data if r[1])
    con_deuda = sum(1 for r in ultima_data if r[0] and r[0] > 0)
    al_dia = sum(1 for r in ultima_data if r[0] == 0 and not r[1])
    sin_consultar = total - len(ultima_data)

    return {
        "total": total,
        "consultadas": len(ultima_data),
        "al_dia": al_dia,
        "con_deuda": con_deuda,
        "exentas": exentas,
        "sin_consultar": sin_consultar,
        "deuda_total_clp": deuda_total,
    }


if __name__ == "__main__":
    init_db()
    print("Base de datos inicializada correctamente.")
    print(f"Archivo: {DB_PATH}")
