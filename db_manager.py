"""
db_manager.py -- Base de datos para historial de contribuciones
Soporta PostgreSQL (Railway/produccion) y SQLite (local)
"""
import os
import json
from datetime import datetime
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    _PG = True
else:
    import sqlite3
    _PG = False

DB_PATH = Path(os.environ.get("DATABASE_PATH", Path(__file__).parent / "contribuciones.db"))


# -- Helpers -------------------------------------------------------------------

def get_conn():
    if _PG:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def _q(sql):
    """Convierte ? a %s para PostgreSQL."""
    if _PG:
        return sql.replace("?", "%s")
    return sql


def _cur(conn):
    """Cursor con filas tipo dict."""
    if _PG:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def _fetchall(conn, sql, params=()):
    cur = _cur(conn)
    cur.execute(_q(sql), params)
    return [dict(r) for r in cur.fetchall()]


def _fetchone(conn, sql, params=()):
    cur = _cur(conn)
    cur.execute(_q(sql), params)
    row = cur.fetchone()
    return dict(row) if row else None


def _execute(conn, sql, params=()):
    cur = _cur(conn)
    cur.execute(_q(sql), params)
    return cur


# -- INIT ----------------------------------------------------------------------

def init_db():
    """Crea las tablas si no existen."""
    conn = get_conn()
    cur = _cur(conn)

    if _PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS propiedades (
                id              SERIAL PRIMARY KEY,
                rol             TEXT    NOT NULL,
                subrol          TEXT    NOT NULL DEFAULT '0',
                region_code     TEXT    NOT NULL DEFAULT '',
                comuna          TEXT    NOT NULL DEFAULT '',
                codigo_comuna   TEXT    NOT NULL DEFAULT '',
                descripcion     TEXT    DEFAULT '',
                encargado       TEXT    DEFAULT '',
                activa          INTEGER DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_prop_rol
                ON propiedades(rol, subrol, codigo_comuna)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consultas (
                id              SERIAL PRIMARY KEY,
                propiedad_id    INTEGER NOT NULL REFERENCES propiedades(id),
                fecha_consulta  TIMESTAMP DEFAULT NOW(),
                deuda_total     INTEGER DEFAULT 0,
                exento          INTEGER DEFAULT 0,
                destino         TEXT    DEFAULT '',
                direccion       TEXT    DEFAULT '',
                propietario     TEXT    DEFAULT '',
                tiene_cuotas    INTEGER DEFAULT 0,
                cuotas_json     TEXT    DEFAULT '[]',
                cambios_json    TEXT    DEFAULT '[]',
                fuente          TEXT    DEFAULT 'SII.cl'
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_consultas_prop
                ON consultas(propiedad_id, fecha_consulta DESC)
        """)
        conn.commit()
    else:
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
        # Migracion SQLite: agregar encargado si no existe
        try:
            conn.execute("SELECT encargado FROM propiedades LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE propiedades ADD COLUMN encargado TEXT DEFAULT ''")
            conn.commit()

    conn.close()
    db_name = "PostgreSQL" if _PG else str(DB_PATH)
    print(f"[OK] Base de datos lista ({db_name})")


# -- PROPIEDADES ---------------------------------------------------------------

def upsert_propiedad(rol, subrol, region_code, comuna, codigo_comuna="", descripcion="", encargado=""):
    """Inserta o retorna la propiedad. Devuelve el id."""
    conn = get_conn()
    row = _fetchone(conn, "SELECT id FROM propiedades WHERE rol=? AND subrol=? AND codigo_comuna=?",
                    (rol, subrol, codigo_comuna))
    if row:
        prop_id = row["id"]
        _execute(conn,
            "UPDATE propiedades SET descripcion=?, region_code=?, comuna=?, encargado=COALESCE(NULLIF(?,''),(SELECT encargado FROM propiedades WHERE id=?)), activa=1 WHERE id=?",
            (descripcion, region_code, comuna, encargado, prop_id, prop_id))
        conn.commit()
    else:
        if _PG:
            r = _fetchone(conn,
                "INSERT INTO propiedades(rol,subrol,region_code,comuna,codigo_comuna,descripcion,encargado) VALUES(?,?,?,?,?,?,?) RETURNING id",
                (rol, subrol, region_code, comuna, codigo_comuna, descripcion, encargado))
            prop_id = r["id"]
        else:
            cur = _execute(conn,
                "INSERT INTO propiedades(rol,subrol,region_code,comuna,codigo_comuna,descripcion,encargado) VALUES(?,?,?,?,?,?,?)",
                (rol, subrol, region_code, comuna, codigo_comuna, descripcion, encargado))
            prop_id = cur.lastrowid
        conn.commit()
    conn.close()
    return prop_id


def get_todas_propiedades():
    conn = get_conn()
    rows = _fetchall(conn, """
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
    """)
    conn.close()
    return rows


def get_propiedad(prop_id):
    conn = get_conn()
    row = _fetchone(conn, "SELECT * FROM propiedades WHERE id=?", (prop_id,))
    conn.close()
    return row


def eliminar_propiedad(prop_id):
    conn = get_conn()
    _execute(conn, "UPDATE propiedades SET activa=0 WHERE id=?", (prop_id,))
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
        _execute(conn, f"UPDATE propiedades SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
    conn.close()


# -- CONSULTAS / HISTORIAL -----------------------------------------------------

def guardar_consulta(propiedad_id, datos: dict) -> dict:
    """Guarda una consulta y compara con la anterior para detectar cambios."""
    conn = get_conn()

    ultima = _fetchone(conn,
        "SELECT cuotas_json, deuda_total FROM consultas WHERE propiedad_id=? ORDER BY fecha_consulta DESC LIMIT 1",
        (propiedad_id,))

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
                    "de": estado_ant, "a": estado_nuevo,
                    "fecha": datetime.now().isoformat(),
                    "monto": cuota.get("monto", 0),
                })
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

    _execute(conn, """
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
        cuotas_json, cambios_json,
        datos.get("fuente", "SII.cl"),
    ))
    conn.commit()
    conn.close()

    datos["cambios"] = cambios
    return datos


def get_historial(propiedad_id, limite=20):
    """Retorna las ultimas N consultas de una propiedad."""
    conn = get_conn()
    rows = _fetchall(conn, """
        SELECT * FROM consultas
        WHERE propiedad_id = ?
        ORDER BY fecha_consulta DESC
        LIMIT ?
    """, (propiedad_id, limite))
    conn.close()
    historial = []
    for d in rows:
        d["cuotas"] = json.loads(d.pop("cuotas_json", "[]"))
        d["cambios"] = json.loads(d.pop("cambios_json", "[]"))
        historial.append(d)
    return historial


def get_resumen_global():
    """Estadisticas generales del portafolio."""
    conn = get_conn()
    row = _fetchone(conn, "SELECT COUNT(*) as cnt FROM propiedades WHERE activa=1")
    total = row["cnt"] if row else 0

    ultima_data = _fetchall(conn, """
        SELECT c.deuda_total, c.exento
        FROM propiedades p
        JOIN consultas c ON c.id = (
            SELECT id FROM consultas WHERE propiedad_id=p.id ORDER BY fecha_consulta DESC LIMIT 1
        )
        WHERE p.activa=1
    """)
    conn.close()

    deuda_total = sum(r["deuda_total"] for r in ultima_data if r["deuda_total"])
    exentas = sum(1 for r in ultima_data if r["exento"])
    con_deuda = sum(1 for r in ultima_data if r["deuda_total"] and r["deuda_total"] > 0)
    al_dia = sum(1 for r in ultima_data if r["deuda_total"] == 0 and not r["exento"])
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
    if _PG:
        print("Usando PostgreSQL")
    else:
        print(f"Archivo: {DB_PATH}")
