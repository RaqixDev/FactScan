import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / 'proveedores.db'

SCHEMA = """
CREATE TABLE IF NOT EXISTS proveedor_plantilla (
    nif     TEXT,
    campo   TEXT,
    pagina  INTEGER DEFAULT 0,
    x0      REAL,
    y0      REAL,
    x1      REAL,
    y1      REAL,
    PRIMARY KEY (nif, campo),
    FOREIGN KEY (nif) REFERENCES proveedores(nif)
);

CREATE TABLE IF NOT EXISTS proveedores (
    nif             TEXT PRIMARY KEY,
    nombre          TEXT NOT NULL,
    cuenta          TEXT NOT NULL,
    contrapartida   TEXT NOT NULL,
    cuenta_pago     TEXT,
    dias_pago       INTEGER DEFAULT 30,
    dia_fijo        INTEGER DEFAULT 0,
    tipo_pago       TEXT DEFAULT 'TR',
    email           TEXT,
    iva_habitual    REAL DEFAULT 10.0,
    activo          INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS proveedor_iva (
    nif             TEXT,
    operacion       INTEGER,
    tipo_iva        INTEGER,
    porcentaje      REAL,
    PRIMARY KEY (nif, operacion),
    FOREIGN KEY (nif) REFERENCES proveedores(nif)
);
"""


def _conexion() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrar() -> None:
    """Añade columnas nuevas a BDs existentes sin perder datos."""
    with _conexion() as conn:
        try:
            conn.execute("ALTER TABLE proveedores ADD COLUMN palabras_clave TEXT DEFAULT ''")
        except Exception:
            pass  # Ya existe


def inicializar_db() -> None:
    """Crea las tablas si no existen y aplica migraciones."""
    with _conexion() as conn:
        conn.executescript(SCHEMA)
    _migrar()


def buscar_por_nif(nif: str) -> Optional[dict]:
    """Devuelve el proveedor como dict o None si no existe."""
    with _conexion() as conn:
        row = conn.execute(
            "SELECT * FROM proveedores WHERE nif = ? AND activo = 1", (nif,)
        ).fetchone()
    return dict(row) if row else None


def listar_proveedores() -> list[dict]:
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM proveedores WHERE activo = 1 ORDER BY nombre"
        ).fetchall()
    return [dict(r) for r in rows]


def insertar_proveedor(datos: dict) -> None:
    campos = ('nif', 'nombre', 'cuenta', 'contrapartida', 'cuenta_pago',
              'dias_pago', 'dia_fijo', 'tipo_pago', 'email', 'iva_habitual')
    valores = tuple(datos.get(c) for c in campos)
    sql = f"INSERT INTO proveedores ({','.join(campos)}) VALUES ({','.join('?'*len(campos))})"
    with _conexion() as conn:
        conn.execute(sql, valores)


def actualizar_proveedor(nif: str, datos: dict) -> None:
    campos = [k for k in datos if k != 'nif']
    sets = ', '.join(f'{c} = ?' for c in campos)
    valores = [datos[c] for c in campos] + [nif]
    with _conexion() as conn:
        conn.execute(f"UPDATE proveedores SET {sets} WHERE nif = ?", valores)


def eliminar_proveedor(nif: str) -> None:
    """Borrado lógico."""
    with _conexion() as conn:
        conn.execute("UPDATE proveedores SET activo = 0 WHERE nif = ?", (nif,))


def buscar_por_nombre(fragmento: str) -> list[dict]:
    """Búsqueda parcial por nombre (LIKE). Devuelve lista de coincidencias."""
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM proveedores WHERE nombre LIKE ? AND activo = 1 ORDER BY nombre",
            (f'%{fragmento.upper()}%',),
        ).fetchall()
    return [dict(r) for r in rows]


def buscar_candidatos(texto: str) -> list[dict]:
    """
    Busca proveedores que podrían coincidir con este texto de factura.
    Multicapa: palabras clave guardadas > nombre del proveedor en texto > texto en nombre.
    """
    import re
    IGNORAR = {
        'S.L.', 'S.A.', 'S.L', 'S.A', 'SL', 'SA', 'DE', 'DEL', 'LA', 'EL',
        'LOS', 'LAS', 'Y', 'E', 'NIF', 'CIF', 'IVA', 'TOTAL', 'BASE', 'FACTURA',
        'FECHA', 'EUROS', 'IMPORTE', 'PRECIO', 'FORMA', 'PAGO', 'HOJA',
        'MEDEMARI', 'BARCELONA', 'PORTES', 'VARIOS',
    }
    t = texto.upper()
    todos = listar_proveedores()
    scores: dict[str, int] = {}
    objs:   dict[str, dict] = {}

    for prov in todos:
        nif    = prov['nif']
        nombre = prov['nombre'].upper()
        score  = 0

        # Capa 1: palabras clave del proveedor (mayor peso)
        claves = (prov.get('palabras_clave') or '').strip()
        for clave in (c.strip().upper() for c in claves.split(',') if c.strip()):
            if clave and clave in t:
                score += 5

        # Capa 2: palabras del nombre del proveedor que aparecen en el texto
        palabras_nombre = [
            w for w in re.split(r'[\s,.;]+', nombre)
            if len(w) >= 4 and w not in IGNORAR
        ]
        for p in palabras_nombre:
            if p in t:
                score += 2

        # Capa 3: palabras del texto que aparecen en el nombre del proveedor
        for token in re.split(r'[\s.,;:()/]+', t):
            if len(token) >= 4 and token not in IGNORAR and token in nombre:
                score += 1

        if score > 0:
            scores[nif]  = score
            objs[nif]    = prov

    return sorted(objs.values(), key=lambda p: scores[p['nif']], reverse=True)


def guardar_palabras_clave(nif: str, palabras: list[str]) -> None:
    """Guarda las palabras clave de identificación del proveedor."""
    claves = ', '.join(p.strip() for p in palabras if p.strip())
    with _conexion() as conn:
        conn.execute(
            "UPDATE proveedores SET palabras_clave = ? WHERE nif = ?",
            (claves, nif)
        )


def tipos_iva_proveedor(nif: str) -> list[dict]:
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM proveedor_iva WHERE nif = ? ORDER BY operacion", (nif,)
        ).fetchall()
    return [dict(r) for r in rows]


def obtener_plantilla(nif: str) -> list[dict]:
    with _conexion() as conn:
        rows = conn.execute(
            "SELECT * FROM proveedor_plantilla WHERE nif = ? ORDER BY campo", (nif,)
        ).fetchall()
    return [dict(r) for r in rows]


def tiene_plantilla(nif: str) -> bool:
    with _conexion() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM proveedor_plantilla WHERE nif = ?", (nif,)
        ).fetchone()[0]
    return n > 0


def guardar_plantilla(nif: str, campos: list[dict]) -> None:
    """
    Guarda/actualiza coordenadas de la plantilla para un proveedor.
    campos = [{'campo': str, 'pagina': int, 'x0', 'y0', 'x1', 'y1': float}]
    """
    with _conexion() as conn:
        for c in campos:
            conn.execute(
                """INSERT OR REPLACE INTO proveedor_plantilla
                   (nif, campo, pagina, x0, y0, x1, y1)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (nif, c['campo'], c.get('pagina', 0),
                 c['x0'], c['y0'], c['x1'], c['y1']),
            )


def cargar_datos_ejemplo() -> None:
    """Inserta datos de ejemplo de Guzman Gastronomia si no existe."""
    if buscar_por_nif('B63864029'):
        return
    insertar_proveedor({
        'nif': 'B63864029',
        'nombre': 'GUZMAN GASTRONOMIA S.L.',
        'cuenta': '40000125',
        'contrapartida': '60000004',
        'cuenta_pago': '57200002',
        'dias_pago': 84,
        'dia_fijo': 25,
        'tipo_pago': 'TR',
        'email': 'adm.comercial@bidfoodiberia.com',
        'iva_habitual': 10.0,
    })
    with _conexion() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO proveedor_iva VALUES (?,?,?,?)",
            [('B63864029', 1, 5, 10.0),
             ('B63864029', 2, 6, 4.0),
             ('B63864029', 3, 4, 21.0)],
        )


if __name__ == '__main__':
    inicializar_db()
    cargar_datos_ejemplo()
    print("Base de datos inicializada.")
    for p in listar_proveedores():
        print(p)
