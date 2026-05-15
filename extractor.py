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
      1. Regex de NIF en el texto combinado
      2. Palabras clave guardadas en la BD del proveedor
      3. Palabras del nombre del proveedor en el texto
      4. Nombre del fichero contra claves/nombres
    Devuelve el NIF con mayor puntuación (si supera el umbral), o None.
    """
    import proveedores as prov_db

    t_completo = texto_completo(ruta_pdf).upper()
    nombre_fich = Path(ruta_pdf).stem.upper()   # p.ej. "26-05-02-FA-007888-V30-CMAR"
    t_busqueda  = t_completo + '\n' + nombre_fich

    # ── 1. Buscar NIF por regex ───────────────────────────────────────────────
    patron_nif = re.compile(r'[A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z]')
    for nif in patron_nif.findall(t_busqueda):
        if prov_db.buscar_por_nif(nif):
            return nif

    # ── 2-4. Puntuación por nombre / claves / fichero ─────────────────────────
    scores: dict[str, int] = {}
    todos   = prov_db.listar_proveedores()

    for prov in todos:
        nif    = prov['nif']
        nombre = prov['nombre'].upper()
        score  = 0

        # 2. Palabras clave guardadas (peso alto)
        claves_raw = (prov.get('palabras_clave') or '').strip()
        if claves_raw:
            for clave in (c.strip().upper() for c in claves_raw.split(',') if c.strip()):
                if clave and clave in t_busqueda:
                    score += 5

        # 3. Palabras del nombre del proveedor en el texto (peso medio)
        palabras_nombre = [
            w for w in re.split(r'[\s,.;]+', nombre)
            if len(w) >= 4 and w not in _TERMINOS_GENERICOS
        ]
        for palabra in palabras_nombre:
            if palabra in t_busqueda:
                score += 2

        # 4. Nombre/clave contra nombre del fichero (peso bajo pero útil)
        for fragmento in [nombre] + (claves_raw.upper().split(',') if claves_raw else []):
            fragmento = fragmento.strip()
            if len(fragmento) >= 3:
                # Comprueba fragmento corto (tipo sigla) en el nombre del fichero
                for parte in nombre_fich.split('-'):
                    if parte and len(parte) >= 3 and parte in fragmento.replace(' ', ''):
                        score += 3
                        break

        if score > 0:
            scores[nif] = score

    if not scores:
        return None

    # Devolver el más puntuado si supera umbral mínimo
    mejor_nif   = max(scores, key=lambda n: scores[n])
    mejor_score = scores[mejor_nif]
    return mejor_nif if mejor_score >= 3 else None


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
    Recoge todos los valores monetarios del texto y deduce algebraicamente
    qué combinación de base(s) + IVA cuadra con el total.
    Devuelve el mismo dict que parsear_campos_plantilla.
    """
    t = texto_completo(ruta_pdf)
    return _deducir_de_texto(t, ruta_pdf)


def _deducir_de_texto(texto: str, ruta_pdf: str = '') -> dict:
    # Extraer valores con exactamente 2 decimales (importes monetarios)
    # Acepta tanto punto como coma como separador decimal
    vals: set[float] = set()
    for m in re.finditer(r'\b(\d{1,7})[,.](\d{2})\b', texto):
        try:
            v = float(f'{m.group(1)}.{m.group(2)}')
            if v > 0:
                vals.add(round(v, 2))
        except ValueError:
            pass

    cands = sorted(vals, reverse=True)   # mayor primero

    # El total es el importe más alto (el que aparece al final de la factura)
    # Si hay varios iguales elegimos el primero; si no hay nada, salimos
    total = cands[0] if cands else 0.0

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
        r'\d{2}/[A-Z]{1,4}/\d{3,6}/\d{2}'   # 02/FA/7888/26
        r'|[A-Z]{1,4}[/\-]\d{4,10}'           # FAC-12345 / FV26002909
        r'|F[AVR]?\d{6,12}'                   # FV26002909
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
