"""
Extrae texto de ficheros PDF y localiza el proveedor mediante estrategia multicapa:
  1. NIF por regex en el texto extraído
  2. Palabras clave guardadas en la BD del proveedor
  3. Nombre del fichero contra nombres y claves de proveedores
  4. Palabras del nombre del proveedor que aparecen en el texto del PDF
"""
import re
from pathlib import Path
from typing import Optional

MIN_CHARS_NATIVO = 50

# Términos genéricos de factura que no sirven para identificar a nadie
_TERMINOS_GENERICOS = {
    'MEDEMARI', 'BARCELONA', 'FACTURA', 'FECHA', 'IMPORTE', 'TOTAL', 'BASE',
    'EUROS', 'IMPORTE', 'PRECIO', 'FORMA', 'PAGO', 'CLIENTE', 'HOJA', 'PORTES',
    'VARIOS', 'DESCRIPCIÓN', 'AGENTE', 'TRANSPORTISTA', 'MATRÍCULA', 'CAJAS',
    'KILOS', 'BOLSAS', 'CÓDIGO', 'ALBARÁN', 'PEDIDO', 'RECIBO', 'DELEGACIÓN',
    'DIRECCIÓN', 'ENVÍO', 'EFECTIVO', 'TRANSFERENCIA', 'VENCIMIENTO',
    'TIPO', 'CUOTA', 'RECARGO', 'RETENCIÓN', 'BRUTO', 'NETO',
    'S.L.', 'S.A.', 'S.L', 'S.A', 'SL', 'SA', 'DE', 'DEL', 'LA', 'EL',
    'LOS', 'LAS', 'Y', 'E', 'NIF', 'CIF', 'IVA',
}


# ── Extracción de texto ────────────────────────────────────────────────────────

def extraer_texto_pdf(ruta_pdf: str) -> str:
    """Extrae texto con pdfplumber; cae a Tesseract si es escaneado."""
    ruta = Path(ruta_pdf)
    if not ruta.exists():
        raise FileNotFoundError(f"PDF no encontrado: {ruta_pdf}")
    texto = _extraer_nativo(ruta)
    if len(texto.strip()) < MIN_CHARS_NATIVO:
        texto = _extraer_ocr(ruta)
    return texto


def extraer_texto_fitz(ruta_pdf: str) -> str:
    """
    Extracción alternativa con PyMuPDF.
    A veces recupera bloques que pdfplumber no alcanza.
    """
    try:
        import fitz
        doc = fitz.open(str(ruta_pdf))
        partes = [pag.get_text("text") for pag in doc]
        doc.close()
        return '\n'.join(partes)
    except Exception:
        return ''


def texto_completo(ruta_pdf: str) -> str:
    """Une la salida de ambos extractores para maximizar cobertura."""
    t1 = ''
    t2 = ''
    try:
        t1 = extraer_texto_pdf(ruta_pdf)
    except Exception:
        pass
    t2 = extraer_texto_fitz(ruta_pdf)
    # Unir sin duplicar líneas idénticas
    lineas = set()
    resultado = []
    for linea in (t1 + '\n' + t2).splitlines():
        l = linea.strip()
        if l and l not in lineas:
            lineas.add(l)
            resultado.append(l)
    return '\n'.join(resultado)


# ── Identificación del proveedor ──────────────────────────────────────────────

def identificar_proveedor_en_pdf(ruta_pdf: str) -> Optional[str]:
    """
    Estrategia multicapa para localizar el NIF del emisor:
      1. Regex de NIF en el texto combinado  → retorno inmediato, máxima certeza
      2. Palabras clave guardadas en la BD   → +5 pts por coincidencia
      3. Palabras del nombre del proveedor que aparecen en el texto → +2 pts

    Umbral de auto-identificación: 5 puntos.
    Esto requiere al menos UNA palabra clave (+5) o TRES palabras del nombre (+6).
    Con menos puntuación devuelve None y la UI mostrará el diálogo de selección.

    NOTA: La capa "nombre de fichero" fue eliminada porque "002" en "002-factura.pdf"
    coincidía como subcadena con "2002" en "Jamones Artesan 2002 S.L." causando
    identificaciones erróneas. Las palabras clave explícitas son más fiables.
    """
    import proveedores as prov_db

    t_completo  = texto_completo(ruta_pdf).upper()
    t_busqueda  = t_completo

    # ── 1. NIF por regex ─────────────────────────────────────────────────────
    patron_nif = re.compile(r'[A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z]')
    for nif in patron_nif.findall(t_busqueda):
        if prov_db.buscar_por_nif(nif):
            return nif

    # ── 2-3. Puntuación por claves y nombre ───────────────────────────────────
    scores: dict[str, int] = {}
    todos = prov_db.listar_proveedores()

    for prov in todos:
        nif    = prov['nif']
        nombre = prov['nombre'].upper()
        score  = 0

        # 2. Palabras clave explícitas (alta confianza)
        claves_raw = (prov.get('palabras_clave') or '').strip()
        for clave in (c.strip().upper() for c in claves_raw.split(',') if c.strip()):
            if clave and clave in t_busqueda:
                score += 5

        # 3. Palabras significativas del nombre en el texto (≥5 chars, no genéricas)
        for w in re.split(r'[\s,.;]+', nombre):
            if len(w) >= 5 and w not in _TERMINOS_GENERICOS and w in t_busqueda:
                score += 2

        if score > 0:
            scores[nif] = score

    if not scores:
        return None

    mejor_nif   = max(scores, key=lambda n: scores[n])
    mejor_score = scores[mejor_nif]
    # Umbral 5: requiere al menos una clave explícita o tres palabras del nombre
    return mejor_nif if mejor_score >= 5 else None


def sugerir_palabras_clave(ruta_pdf: str, nif_excluir: str = '') -> list[str]:
    """
    Extrae del PDF fragmentos de texto que podrían servir como palabras clave
    para identificar automáticamente al proveedor en el futuro.
    Prioriza: Registro Mercantil, teléfonos, emails, códigos únicos.
    """
    texto = texto_completo(ruta_pdf).upper()
    sugerencias: list[str] = []

    # Registro Mercantil
    for m in re.finditer(r'TOMO\s+\d+', texto):
        sugerencias.append(m.group().strip())
    for m in re.finditer(r'HOJA\s+[A-Z]-?\d+', texto):
        sugerencias.append(m.group().strip())
    for m in re.finditer(r'INSCRIPCI[OÓ]N\s+\d+', texto):
        sugerencias.append(m.group().strip())

    # Teléfonos / fax (formato español)
    for m in re.finditer(r'\b[679]\d{8}\b|\b\d{3}[\s-]\d{3}[\s-]\d{2}[\s-]\d{2}\b', texto):
        sugerencias.append(m.group().strip())

    # Emails
    for m in re.finditer(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}', texto):
        sugerencias.append(m.group().strip())

    # Códigos RGSI / RS / similares
    for m in re.finditer(r'RGSI\s+[\d./]+|RS:\s*[\d./]+', texto):
        sugerencias.append(m.group().strip())

    # Palabras largas y únicas (probablemente nombres propios o marcas)
    palabras_largas = [
        w for w in re.findall(r'\b[A-ZÁÉÍÓÚÑ]{7,}\b', texto)
        if w not in _TERMINOS_GENERICOS and not w.startswith('MEDEMAR')
    ]
    # Solo las que aparecen una o dos veces (más específicas)
    from collections import Counter
    conteo = Counter(palabras_largas)
    for palabra, n in conteo.items():
        if n <= 2:
            sugerencias.append(palabra)

    # Eliminar duplicados manteniendo orden y limitar
    vistos: set[str] = set()
    resultado: list[str] = []
    for s in sugerencias:
        s = s.strip()
        if s and s not in vistos:
            vistos.add(s); resultado.append(s)
    return resultado[:15]


# ── Fallback numérico ─────────────────────────────────────────────────────────

_RATES_IVA = [21.0, 10.0, 5.0, 4.0]


def extraer_fallback_numerico(ruta_pdf: str) -> dict:
    """
    Extrae importes del PDF cuando la plantilla no funciona.
    Para PDFs multipágina, prueba primero la ÚLTIMA página (donde suele estar
    el resumen de totales e IVA) — evita que subtotales intermedios de páginas
    anteriores confundan al algoritmo.
    Si la última página no da resultado, usa el documento completo.
    """
    try:
        import fitz as _fitz
        with _fitz.open(ruta_pdf) as _doc:
            n_pags = len(_doc)
    except Exception:
        n_pags = 1

    if n_pags > 1:
        try:
            import pdfplumber
            with pdfplumber.open(ruta_pdf) as pdf:
                # Texto de la última página
                texto_ultima = (pdf.pages[-1].extract_text() or '').strip()
                # Si hay muy poco texto en la última (p.ej. solo firma/pie),
                # ampliar a las dos últimas páginas
                if len(texto_ultima) < 80 and len(pdf.pages) >= 2:
                    texto_ultima = '\n'.join(
                        (p.extract_text() or '') for p in pdf.pages[-2:]
                    )
        except Exception:
            texto_ultima = ''

        if texto_ultima:
            r = _deducir_de_texto(texto_ultima, ruta_pdf)
            if r.get('total', 0) > 0 and r.get('iva_ops'):
                return r   # la última página tiene todo lo necesario

    # Fallback: documento completo
    return _deducir_de_texto(texto_completo(ruta_pdf), ruta_pdf)


def _extraer_importes(texto: str) -> list[float]:
    """
    Extrae importes monetarios del texto manejando formatos españoles:
    - Con separador de miles: 1.743,95 → 1743.95  (p.ej. facturas Aribau, Makro)
    - Sin separador de miles: 743,95  → 743.95
    - Con punto decimal:      743.95  → 743.95
    El orden de prioridad evita que "1.441,28" se capture solo como "441,28".
    """
    vals: set[float] = set()

    # 1. Miles españoles: 1.234,56 o 1.234.567,89
    for m in re.finditer(r'\b(\d{1,3}(?:\.\d{3})+),(\d{2})\b', texto):
        try:
            v = float(m.group(1).replace('.', '') + '.' + m.group(2))
            if v > 0:
                vals.add(round(v, 2))
        except ValueError:
            pass

    # 2. Decimal con coma sin miles: 743,95  (no precedido de dígito+punto)
    for m in re.finditer(r'(?<![.\d])(\d{1,6}),(\d{2})\b', texto):
        try:
            v = float(m.group(1) + '.' + m.group(2))
            if v > 0:
                vals.add(round(v, 2))
        except ValueError:
            pass

    # 3. Decimal con punto: 743.95  (no precedido de dígito, no seguido de dígito)
    for m in re.finditer(r'(?<![,\d])(\d{1,6})\.(\d{2})(?!\d)', texto):
        try:
            v = float(m.group(1) + '.' + m.group(2))
            if v > 0:
                vals.add(round(v, 2))
        except ValueError:
            pass

    return sorted(vals, reverse=True)


# Keywords que preceden al importe total de la factura (orden de prioridad)
_KEYWORDS_TOTAL = [
    'TOTAL FACTURA', 'TOTAL INVOICE', 'TOTAL A PAGAR', 'IMPORTE TOTAL',
    'IMPORTE FACTURA', 'TOTAL GENERAL', 'IMPORTE A PAGAR', 'TOTAL FACT.',
    'TOTAL FATURA', 'AMOUNT DUE', 'NET AMOUNT', 'TOTAL AMOUNT',
    'TOTAL A COBRAR', 'IMPORTE TOTAL FACTURA',
]


def _total_por_contexto(texto: str, cands_set: set) -> float:
    """
    Busca keywords de 'TOTAL FACTURA' en el texto y devuelve el importe que
    aparece justo después. Más fiable que 'el número más alto' cuando hay
    acumulados o subtotales parciales en el documento.
    """
    t = texto.upper()
    for kw in _KEYWORDS_TOTAL:
        pos = t.find(kw)
        while pos >= 0:
            # Buscar el primer número monetario en los 200 chars siguientes
            fragmento = t[pos + len(kw): pos + len(kw) + 200]
            nums = _extraer_importes(fragmento)
            for v in nums:
                if v in cands_set and v > 0.01:
                    return v
            pos = t.find(kw, pos + 1)
    return 0.0


def _deducir_de_texto(texto: str, ruta_pdf: str = '') -> dict:
    cands = _extraer_importes(texto)   # mayor primero

    if not cands:
        total = 0.0
    else:
        # Estrategia 1: buscar total por contexto (keyword + número siguiente)
        total = _total_por_contexto(texto, set(cands))
        # Estrategia 2: si no hay keyword, usar el valor más alto
        if not total:
            total = cands[0]

    iva_ops = _buscar_combinacion_iva(cands, total) if total > 0 else []

    # Extraer fecha y número de factura por regex
    fecha, num_factura = _extraer_fecha_y_numfac(texto)

    res = {
        'total':       total,
        'iva_ops':     iva_ops,
        'num_factura': num_factura,
        'fecha':       fecha,
        'base':        iva_ops[0]['base']      if iva_ops else 0.0,
        'tipo_iva':    iva_ops[0]['tipo_iva']  if iva_ops else 0.0,
        'cuota_iva':   iva_ops[0]['cuota_iva'] if iva_ops else 0.0,
        'recargo': 0.0, 'retencion': 0.0,
    }
    return res


def _buscar_combinacion_iva(cands: list[float], total: float) -> list[dict]:
    """
    Busca 1, 2 o 3 bases imponibles que con su IVA sumen al total.

    ESTRATEGIA en dos pasos:
    ─ Paso 1 (pares confirmados): busca bases cuya cuota calculada TAMBIÉN
      aparece explícitamente en el texto del PDF. Si base Y cuota están en el
      PDF es porque la factura los imprimió: son los valores reales.
      Combina hasta 3 pares confirmados y elige el grupo que cuadre con el total.
    ─ Paso 2 (fallback algebraico): si el paso 1 no resuelve, calcula
      algebraicamente. Más propenso a falsos positivos.
    """
    from itertools import combinations, product as iprod

    EPS      = 0.03
    cands_set = set(cands)
    sub      = [n for n in cands if 0 < n < total][:20]

    def _op(base, rate):
        return {'base': base, 'tipo_iva': rate,
                'cuota_iva': round(base * rate / 100, 2),
                'recargo': 0.0, 'retencion': 0.0}

    # ═══════════════════════════════════════════════════════════════════════
    # PASO 1 — Pares confirmados: base y cuota AMBOS presentes en el PDF
    # ═══════════════════════════════════════════════════════════════════════
    pares_conf = []
    for base in sub:
        for rate in _RATES_IVA:
            cuota = round(base * rate / 100, 2)
            if cuota in cands_set:
                pares_conf.append(_op(base, rate))

    # Buscar 1, 2 o 3 pares confirmados que sumen al total
    for n in range(1, min(4, len(pares_conf) + 1)):
        for combo in combinations(pares_conf, n):
            suma = sum(p['base'] + p['cuota_iva'] for p in combo)
            if abs(suma - total) < EPS:
                return list(combo)

    # ═══════════════════════════════════════════════════════════════════════
    # PASO 2 — Fallback algebraico (cuando los valores exactos no están en texto)
    # ═══════════════════════════════════════════════════════════════════════

    # 1 base calculada desde el total
    for rate in _RATES_IVA:
        base = round(total / (1 + rate / 100), 2)
        op   = _op(base, rate)
        if abs(base + op['cuota_iva'] - total) < EPS:
            return [op]

    # 1 base que aparece en el texto
    for base in sub:
        for rate in _RATES_IVA:
            op = _op(base, rate)
            if abs(base + op['cuota_iva'] - total) < EPS:
                return [op]

    # 2 bases en texto
    for b1, b2 in combinations(sub, 2):
        if b1 + b2 >= total:
            continue
        for r1, r2 in iprod(_RATES_IVA, repeat=2):
            o1, o2 = _op(b1, r1), _op(b2, r2)
            if abs(b1 + o1['cuota_iva'] + b2 + o2['cuota_iva'] - total) < EPS:
                return [o1, o2]

    # 3 bases en texto (espacio de búsqueda limitado)
    for b1, b2, b3 in combinations(sub[:8], 3):
        if b1 + b2 + b3 >= total:
            continue
        for r1, r2, r3 in iprod(_RATES_IVA, repeat=3):
            o1, o2, o3 = _op(b1, r1), _op(b2, r2), _op(b3, r3)
            if abs(b1 + o1['cuota_iva'] + b2 + o2['cuota_iva'] +
                   b3 + o3['cuota_iva'] - total) < EPS:
                return [o1, o2, o3]

    return []


def _extraer_fecha_y_numfac(texto: str) -> tuple:
    """
    Extrae la fecha de la factura (prefiere formato DD/MM/YY de 2 dígitos)
    y el número de factura (patrones comunes españoles).
    """
    from datetime import datetime

    # Número de factura — patrones más comunes
    PATRON_NUMFAC = re.compile(
        r'\b('
        r'\d{2}/[A-Z]{1,4}/\d{3,6}/\d{2}'    # 02/FA/7888/26
        r'|[A-Z]{1,4}\d{2}[/\-]\d{4,8}'       # FV26-02913 / FV26002909
        r'|[A-Z]{1,4}[/\-]\d{4,10}'            # FAC-12345
        r'|[A-Z]\d{4,12}'                       # A1751 / F000669
        r'|F[AVR]?\d{5,12}'                     # FV26002909
        r')\b'
    )
    num_factura = None
    for m in PATRON_NUMFAC.finditer(texto):
        num_factura = m.group(); break

    # Fecha — preferir DD/MM/YY (año 2 dígitos = fecha factura)
    # sobre DD/MM/YYYY (año 4 dígitos = vencimiento u otras)
    PATRON_FECHA2 = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{2})\b')
    PATRON_FECHA4 = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{4})\b')
    fecha = None

    for patron, fmt in [(PATRON_FECHA2, '%d/%m/%y'), (PATRON_FECHA4, '%d/%m/%Y')]:
        for m in patron.finditer(texto):
            try:
                fecha = datetime.strptime(m.group(), fmt).date()
                break
            except ValueError:
                pass
        if fecha:
            break

    return fecha, num_factura


# ── Extracción por plantilla de coordenadas ───────────────────────────────────

def extraer_por_plantilla(ruta_pdf: str, plantilla: list) -> dict:
    """
    Extrae texto de zonas específicas del PDF usando coordenadas guardadas.
    plantilla = [{'campo': str, 'pagina': int, 'x0', 'y0', 'x1', 'y1': float}]
    """
    import pdfplumber
    resultado = {}
    with pdfplumber.open(ruta_pdf) as pdf:
        for item in plantilla:
            try:
                pag  = pdf.pages[item['pagina']]
                crop = pag.crop((item['x0'], item['y0'], item['x1'], item['y1']))
                resultado[item['campo']] = (crop.extract_text() or '').strip()
            except Exception:
                resultado[item['campo']] = ''
    return resultado


def parsear_campos_plantilla(raw: dict) -> dict:
    """Convierte texto crudo extraído por plantilla a los tipos correctos."""
    from datetime import datetime
    resultado = {}

    texto_fecha = raw.get('fecha', '').strip()
    fecha = None
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%Y-%m-%d', '%d.%m.%Y'):
        try:
            fecha = datetime.strptime(texto_fecha, fmt).date(); break
        except ValueError:
            pass
    resultado['fecha'] = fecha
    resultado['num_factura'] = raw.get('num_factura', '').strip()

    def _num(t: str) -> float:
        t = t.replace('€', '').replace('%', '').strip()
        if ',' in t and '.' in t:
            t = t.replace('.', '').replace(',', '.')
        elif ',' in t:
            t = t.replace(',', '.')
        t = re.sub(r'[^\d.]', '', t)
        try:
            return float(t)
        except (ValueError, TypeError):
            return 0.0

    iva_ops = []
    for i in ('1', '2', '3'):
        base = _num(raw.get(f'base_{i}', ''))
        if base > 0:
            iva_ops.append({
                'base':      base,
                'tipo_iva':  _num(raw.get(f'tipo_iva_{i}', '')),
                'cuota_iva': _num(raw.get(f'cuota_iva_{i}', '')),
                'recargo': 0.0, 'retencion': 0.0,
            })
    if not iva_ops:
        base = _num(raw.get('base', ''))
        if base > 0:
            iva_ops.append({
                'base': base,
                'tipo_iva':  _num(raw.get('tipo_iva', '')),
                'cuota_iva': _num(raw.get('cuota_iva', '')),
                'recargo': 0.0, 'retencion': 0.0,
            })

    resultado['iva_ops']  = iva_ops
    resultado['base']     = iva_ops[0]['base']      if iva_ops else 0.0
    resultado['tipo_iva'] = iva_ops[0]['tipo_iva']  if iva_ops else 0.0
    resultado['cuota_iva']= iva_ops[0]['cuota_iva'] if iva_ops else 0.0
    resultado['total']    = _num(raw.get('total', ''))
    return resultado


# ── Internos ──────────────────────────────────────────────────────────────────

def _extraer_nativo(ruta: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(ruta) as pdf:
            return '\n'.join(p.extract_text() or '' for p in pdf.pages)
    except Exception as e:
        raise RuntimeError(f"Error extrayendo texto de {ruta}: {e}") from e


def _extraer_ocr(ruta: Path) -> str:
    try:
        import fitz, pytesseract
        from PIL import Image
        import io
        doc = fitz.open(str(ruta))
        textos = []
        for pag in doc:
            pix = pag.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes('png')))
            textos.append(pytesseract.image_to_string(img, lang='spa'))
        doc.close()
        return '\n'.join(textos)
    except Exception as e:
        raise RuntimeError(f"Error en OCR de {ruta}: {e}") from e
