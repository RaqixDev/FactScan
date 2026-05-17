"""
Historial de facturas procesadas por proveedor y ejercicio fiscal.

Permite validar que el número de factura extraído sea coherente con el historial:
  - Si fecha nueva > fecha histórica → número nuevo debe ser mayor
  - Si fecha nueva < fecha histórica → número nuevo debe ser menor

Esto detecta capturas erróneas (albaranes, NIFs, etc.) que rompan la secuencia.
"""
import re
import sqlite3
from datetime import date
from pathlib import Path

_DB = Path(__file__).parent / 'proveedores.db'


def _conn():
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    return c


def _migrar():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS historial_facturas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nif         TEXT    NOT NULL,
                ejercicio   INTEGER NOT NULL,
                num_factura TEXT    NOT NULL,
                fecha       TEXT    NOT NULL,
                ndoc        INTEGER,
                UNIQUE(nif, ejercicio, num_factura)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_hist_nif "
            "ON historial_facturas(nif, ejercicio)"
        )


def _descomponer(num: str) -> tuple[str, int] | None:
    """
    Descompone el número de factura en (prefijo, parte_secuencial).

    Ejemplos:
      "FV26-02913" → ("FV26-", 2913)
      "F26/1034"   → ("F26/",  1034)
      "826034-ALP" → ("",      826034)  ← toma el primer grupo numérico largo

    Retorna None si no hay dígitos.
    """
    grupos = re.findall(r'\d+', num)
    if not grupos:
        return None
    # El grupo numérico secuencial es el ÚLTIMO (o el único)
    seq = int(grupos[-1])
    m = re.search(r'^(.*?)(\d+)$', num)
    if not m:
        return None
    return m.group(1), seq   # (prefijo, número)


# ── Escritura ─────────────────────────────────────────────────────────────────

def registrar_factura(nif: str, num_factura: str,
                      fecha: date, ndoc: int = None) -> None:
    """Guarda (o actualiza) una factura procesada en el historial."""
    _migrar()
    if not nif or not num_factura or not fecha:
        return
    with _conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO historial_facturas
                (nif, ejercicio, num_factura, fecha, ndoc)
            VALUES (?, ?, ?, ?, ?)
        """, (nif, fecha.year, num_factura, fecha.isoformat(), ndoc))


# ── Consulta ──────────────────────────────────────────────────────────────────

def obtener_historial(nif: str, ejercicio: int) -> list[dict]:
    """Devuelve el historial del proveedor en ese ejercicio, ordenado por fecha."""
    _migrar()
    with _conn() as con:
        rows = con.execute("""
            SELECT num_factura, fecha, ndoc
            FROM   historial_facturas
            WHERE  nif=? AND ejercicio=?
            ORDER  BY fecha, num_factura
        """, (nif, ejercicio)).fetchall()
    return [
        {'num_factura': r['num_factura'],
         'fecha':       date.fromisoformat(r['fecha']),
         'ndoc':        r['ndoc']}
        for r in rows
    ]


def ultimo_conocido(nif: str, ejercicio: int) -> dict | None:
    """Última factura registrada del proveedor en ese ejercicio."""
    h = obtener_historial(nif, ejercicio)
    return h[-1] if h else None


# ── Validación ────────────────────────────────────────────────────────────────

def validar_secuencia(nif: str, num_factura: str,
                      fecha: date) -> tuple[str, str]:
    """
    Comprueba que el número de factura sea coherente con el historial.

    Retorna (estado, mensaje):
      ('ok',          '')   → coherente con el historial
      ('sin_datos',   '')   → no hay historial para este proveedor/año
      ('incomparable','')   → prefijo diferente al historial (otra serie, no comparable)
      ('sospechoso',  msg)  → rompe la secuencia cronológica esperada
    """
    historial = obtener_historial(nif, fecha.year)
    if not historial:
        return 'sin_datos', ''

    nuevo = _descomponer(num_factura)
    if not nuevo:
        return 'incomparable', ''
    nuevo_prefijo, nuevo_seq = nuevo

    # Solo comparar con registros que tengan el mismo prefijo de numeración
    comparables = []
    for h in historial:
        d = _descomponer(h['num_factura'])
        if d and d[0] == nuevo_prefijo:
            comparables.append((h['fecha'], h['num_factura'], d[1]))

    if not comparables:
        return 'incomparable', ''

    # Verificar coherencia temporal: fecha mayor ↔ número mayor
    problemas: list[str] = []
    for h_fecha, h_num, h_seq in comparables:
        if h_fecha < fecha and h_seq >= nuevo_seq:
            problemas.append(
                f'El {h_fecha.strftime("%d/%m/%Y")} se procesó {h_num} '
                f'(nº {h_seq:,}) — fecha anterior pero número ≥ {num_factura}'
            )
        elif h_fecha > fecha and h_seq <= nuevo_seq:
            problemas.append(
                f'El {h_fecha.strftime("%d/%m/%Y")} se procesó {h_num} '
                f'(nº {h_seq:,}) — fecha posterior pero número ≤ {num_factura}'
            )

    if problemas:
        return 'sospechoso', '\n'.join(problemas)

    return 'ok', ''


def sugerir_num_factura(nif: str, fecha: date) -> str | None:
    """
    A partir del historial, devuelve una descripción del rango esperado.
    Útil para mostrarlo en la UI como ayuda visual.
    Ej: "Se esperaba un número > FV26-02913 (última del 12/05/2026)"
    """
    historial = obtener_historial(nif, fecha.year)
    if not historial:
        return None
    # Buscar el registro más próximo en fecha
    anterior = [h for h in historial if h['fecha'] <= fecha]
    posterior = [h for h in historial if h['fecha'] > fecha]
    partes = []
    if anterior:
        ult = anterior[-1]
        partes.append(
            f'> {ult["num_factura"]} (del {ult["fecha"].strftime("%d/%m/%Y")})'
        )
    if posterior:
        sig = posterior[0]
        partes.append(
            f'< {sig["num_factura"]} (del {sig["fecha"].strftime("%d/%m/%Y")})'
        )
    return '  y  '.join(partes) if partes else None
