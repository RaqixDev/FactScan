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
    Extracción con PyMuPDF usando múltiples modos para máxima cobertura.
    - words: ensambla caracteres individuales, funciona con texto blanco sobre oscuro
    - text con FLAGS: captura texto ignorado por el modo por defecto
    Combina ambos para no perder nada.
    """
    try:
        import fitz
        doc = fitz.open(str(ruta_pdf))
        partes = []
        for pag in doc:
            tokens: set[str] = set()

            # Modo 1: words (ensambla chars individuales)
            for w in pag.get_text("words", sort=True):
                if w[4].strip():
                    tokens.add(w[4].strip())

            # Modo 2: text con flags para máxima cobertura
            # TEXT_INHIBIT_SPACES=1 evita falsos espacios que rompen números
            for flag in (0, 1, fitz.TEXT_PRESERVE_LIGATURES if hasattr(fitz,'TEXT_PRESERVE_LIGATURES') else 0):
                t = pag.get_text("text", flags=flag, sort=True).strip()
                for tok in t.split():
                    if tok.strip():
                        tokens.add(tok.strip())

            # Modo 3: rawdict — cada span, incluidos los de texto invisible/coloreado.
            # Si el campo "text" está vacío, reconstruye desde "chars" (carácter a carácter).
            d = pag.get_text("rawdict", flags=0)
            for block in d.get("blocks", []):
                if block.get("type") == 0:
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            t = (span.get("text") or "").strip()
                            if t:
                                tokens.add(t)
                            else:
                                # Reconstruir desde chars individuales
                                chars = span.get("chars", [])
                                if chars:
                                    word = ''.join(
                                        c.get("c", "") for c in chars
                                    ).strip()
                                    if word:
                                        tokens.add(word)

            partes.append(' '.join(sorted(tokens, key=lambda x: x)))
        doc.close()
        return '\n'.join(partes)
    except Exception:
        return ''


def texto_completo(ruta_pdf: str) -> str:
    """
    Combina tres métodos de extracción para maximizar cobertura:
    1. pdfplumber extract_text  — texto en orden de flujo
    2. pdfplumber extract_words — basado en posición bbox (mejor para celdas coloreadas)
    3. fitz words               — ensambla caracteres individuales, detecta texto blanco
    """
    partes: list[str] = []

    try:
        partes.append(extraer_texto_pdf(ruta_pdf))
    except Exception:
        pass

    # pdfplumber extract_words: algoritmo diferente a extract_text,
    # a veces captura números en celdas coloreadas que extract_text ignora
    try:
        import pdfplumber
        with pdfplumber.open(ruta_pdf) as pdf:
            for pag in pdf.pages:
                words = pag.extract_words(
                    x_tolerance=3, y_tolerance=3,
                    extra_attrs=['fontname'],
                )
                if words:
                    partes.append(' '.join(w['text'] for w in words))
    except Exception:
        pass

    partes.append(extraer_texto_fitz(ruta_pdf))

    # Unir sin duplicar tokens exactos
    vistos: set[str] = set()
    resultado: list[str] = []
    for linea in '\n'.join(p for p in partes if p).splitlines():
        l = linea.strip()
        if l and l not in vistos:
            vistos.add(l)
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


def sugerir_palabras_clave(ruta_pdf: str, nombre_proveedor: str = '') -> list[str]:
    """
    Extrae del PDF fragmentos candidatos a palabra clave del proveedor.
    Prioriza: nombre del proveedor en el texto, Registro Mercantil,
    teléfonos, emails, palabras propias (mínimo 5 chars).
    Incluye charset extendido para idiomas catalán/gallego/euskera.
    """
    from collections import Counter

    texto = texto_completo(ruta_pdf).upper()
    # Charset ampliado: incluye vocales con acento grave/circunflejo, Ç, Ü, etc.
    _LETRAS = r'[A-ZÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÄËÏÖÜÇÑ]'
    sugerencias: list[str] = []
    vistos: set[str] = set()

    def _add(s):
        s = s.strip()
        if s and s not in vistos:
            vistos.add(s); sugerencias.append(s)

    # ── 1. Palabras del nombre del proveedor que aparecen en el texto ─────────
    nombre_up = nombre_proveedor.upper()
    for palabra in re.split(r'[\s,;./\-()]+', nombre_up):
        if len(palabra) >= 4 and palabra not in _TERMINOS_GENERICOS and palabra in texto:
            _add(palabra)
    # También buscar el nombre completo limpio (sin forma jurídica) en el texto
    nombre_limpio = re.sub(
        r'\b(S\.?L\.?|S\.?A\.?|S\.?C\.?P\.?|A\.?I\.?E\.?|S\.?A\.?U\.?)\b',
        '', nombre_up
    ).strip()
    if len(nombre_limpio) >= 5 and nombre_limpio in texto:
        _add(nombre_limpio)

    # ── 2. Registro Mercantil ─────────────────────────────────────────────────
    for pat in (r'TOMO\s+\d+', r'HOJA\s+[A-Z]-?\d+', r'INSCRIPCI[OÓ]N\s+\d+'):
        for m in re.finditer(pat, texto):
            _add(m.group().strip())

    # ── 3. Teléfonos / fax (formato español) ─────────────────────────────────
    for m in re.finditer(r'\b[679]\d{8}\b|\b\d{3}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}\b', texto):
        _add(m.group().strip())

    # ── 4. Emails ─────────────────────────────────────────────────────────────
    for m in re.finditer(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}', texto):
        _add(m.group().strip())

    # ── 5. Códigos RGSI / RS ──────────────────────────────────────────────────
    for m in re.finditer(r'RGSI\s+[\d./]+|RS:\s*[\d./]+', texto):
        _add(m.group().strip())

    # ── 6. Palabras propias largas (>=5 chars, charset ampliado) ─────────────
    palabras = [
        w for w in re.findall(_LETRAS + r'{5,}', texto)
        if w not in _TERMINOS_GENERICOS and not w.startswith('MEDEMAR')
    ]
    conteo = Counter(palabras)
    # Priorizar las menos frecuentes (más específicas del proveedor)
    for palabra, n in sorted(conteo.items(), key=lambda x: x[1]):
        if n <= 3:
            _add(palabra)

    # ── 7. Frases de 2-3 palabras contiguas significativas ───────────────────
    tokens = re.findall(_LETRAS + r'{4,}', texto)
    for i in range(len(tokens) - 1):
        w1, w2 = tokens[i], tokens[i+1]
        if (w1 not in _TERMINOS_GENERICOS and w2 not in _TERMINOS_GENERICOS
                and len(w1) >= 4 and len(w2) >= 4):
            _add(f'{w1} {w2}')

    return sugerencias[:18]


# ── Localizar coordenadas de importes en el PDF ───────────────────────────────

def encontrar_coords_importes(ruta_pdf: str, factura: dict) -> list[dict]:
    """
    Busca en el PDF las posiciones (bounding boxes) donde aparecen los importes
    extraídos (base, cuota IVA, total).  Devuelve lista de dicts con las mismas
    claves que las entradas de plantilla: {campo, pagina, x0, y0, x1, y1}.
    Usa la ÚLTIMA ocurrencia de cada número (suele ser la del resumen final).
    """
    import fitz

    iva_ops = factura.get('iva_ops') or []
    if not iva_ops and factura.get('base', 0):
        iva_ops = [{'base': factura['base'],
                    'cuota_iva': factura.get('cuota_iva', 0)}]

    buscar: dict[str, float] = {'total': factura.get('total', 0)}
    for i, op in enumerate(iva_ops, 1):
        buscar[f'base_{i}']      = op.get('base', 0)
        buscar[f'cuota_iva_{i}'] = op.get('cuota_iva', 0)

    def _formatos(v: float) -> list[str]:
        """Genera las posibles representaciones textuales del valor (formato español)."""
        res = []
        if v >= 1000:
            e = int(v); d = round((v - e) * 100)
            m, r = divmod(e, 1000)
            if m >= 1000:
                mm, m2 = divmod(m, 1000)
                res.append(f"{mm}.{m2:03d}.{r:03d},{d:02d}")
            else:
                res.append(f"{m}.{r:03d},{d:02d}")
        # Sin separador de miles (también presente en algunas facturas)
        res.append(f"{v:.2f}".replace(".", ","))
        return list(dict.fromkeys(res))

    overlays: list[dict] = []
    try:
        doc = fitz.open(ruta_pdf)
        for pag_idx, pag in enumerate(doc):
            for campo, valor in buscar.items():
                if not valor:
                    continue
                for fmt in _formatos(valor):
                    rects = pag.search_for(fmt)
                    if rects:
                        r = rects[-1]   # última ocurrencia = tabla resumen
                        overlays.append({
                            'campo':  campo,
                            'pagina': pag_idx,
                            'x0': r.x0, 'y0': r.y0,
                            'x1': r.x1, 'y1': r.y1,
                        })
                        break
        doc.close()
    except Exception:
        pass
    return overlays


# ── Fallback numérico ─────────────────────────────────────────────────────────

_RATES_IVA = [21.0, 10.0, 5.0, 4.0]


def _total_keyword_en_texto(texto: str) -> bool:
    """Verdadero si alguna keyword TOTAL aparece en el stream de texto nativo."""
    t = texto.upper()
    return any(kw in t for kw in _KEYWORDS_TOTAL)


def extraer_fallback_numerico(ruta_pdf: str) -> dict:
    """
    Estrategia en tres niveles para extraer importes de cualquier formato:

    1. Texto nativo del PDF — rápido, sin coste, cubre el 80% de los casos.

    2. OCR de la imagen renderizada (Tesseract, gratis) — lee la página como
       la ve el usuario en pantalla. Se activa automáticamente cuando:
       a) el nivel 1 devuelve total=0, O
       b) el total fue elegido como máximo (fallback de último recurso) sin que
          ninguna keyword 'TOTAL' apareciese en el texto → probable celda coloreada.
       En ambos casos compara: si OCR encuentra un total mayor que el texto, usa OCR.

    3. Claude Vision (API) — se activa desde la UI cuando los dos anteriores fallan.
    """
    try:
        import fitz as _fitz
        with _fitz.open(ruta_pdf) as _doc:
            n_pags = len(_doc)
    except Exception:
        n_pags = 1

    # ── Nivel 1: texto nativo ────────────────────────────────────────────────
    texto_doc = ''
    if n_pags > 1:
        try:
            import pdfplumber
            with pdfplumber.open(ruta_pdf) as pdf:
                texto_ultima = (pdf.pages[-1].extract_text() or '').strip()
                if len(texto_ultima) < 80 and len(pdf.pages) >= 2:
                    texto_ultima = '\n'.join(
                        (p.extract_text() or '') for p in pdf.pages[-2:]
                    )
        except Exception:
            texto_ultima = ''

        if texto_ultima:
            r = _deducir_de_texto(texto_ultima, ruta_pdf)
            if r.get('total', 0) > 0 and r.get('iva_ops'):
                texto_doc = texto_ultima
                # Verificar si el total vino de keyword o de fallback max-value
                if _total_keyword_en_texto(texto_doc) or _buscar_total_search(ruta_pdf) > 0:
                    return r   # total confirmado por keyword → fiable
                # Si no hay keyword en texto → posible celda coloreada → verificar con OCR

    if not texto_doc:
        texto_doc = texto_completo(ruta_pdf)
    r = _deducir_de_texto(texto_doc, ruta_pdf)
    total_texto = r.get('total', 0.0)

    # Total confirmado por keyword en el texto nativo → fiable, no necesita OCR
    if total_texto > 0 and _total_keyword_en_texto(texto_doc):
        return r

    # ── Nivel 2: OCR de la imagen renderizada ────────────────────────────────
    # Se llega aquí cuando:
    #   a) total_texto == 0  (no se encontró nada), o
    #   b) total encontrado pero sin keyword TOTAL en texto (elegido como max-value fallback)
    #      → posible fila TOTAL en celda coloreada no legible por get_text()
    r_ocr = _deducir_via_ocr_imagen(ruta_pdf)
    total_ocr = r_ocr.get('total', 0.0)

    if total_ocr > 0:
        # OCR encontró algo; si es mayor que el texto (±2%) → usar OCR
        if total_ocr > total_texto * 1.02 or total_texto == 0:
            if not r_ocr.get('num_factura') and r.get('num_factura'):
                r_ocr['num_factura'] = r['num_factura']
            if not r_ocr.get('fecha') and r.get('fecha'):
                r_ocr['fecha'] = r['fecha']
            return r_ocr

    return r   # devolver resultado de texto aunque tenga total=0


def _deducir_via_ocr_imagen(ruta_pdf: str) -> dict:
    """
    Renderiza las páginas del PDF a imagen y aplica Tesseract OCR.
    Esto permite leer texto en celdas coloreadas, fondo oscuro, fuentes especiales,
    etc. — exactamente igual a como lo ve un humano en pantalla.
    Solo procesa la última página (totales) y si hay más de una, también la penúltima.
    """
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io

        doc = fitz.open(ruta_pdf)
        n = len(doc)
        # Páginas a analizar: últimas dos (resumen de totales)
        paginas = list(range(max(0, n - 2), n))
        textos = []
        for idx in paginas:
            pix = doc[idx].get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes('png')))
            t = pytesseract.image_to_string(img, lang='spa+eng',
                                            config='--psm 6')
            textos.append(t)
        doc.close()
        texto_ocr = '\n'.join(textos)
        # OCR no tiene PDF para search_for, solo texto
        return _deducir_de_texto(texto_ocr)
    except Exception:
        return {}


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
    # Frases específicas (mayor prioridad)
    'TOTAL FACTURA', 'IMPORTE TOTAL FACTURA', 'TOTAL INVOICE',
    'TOTAL A PAGAR', 'IMPORTE TOTAL', 'IMPORTE FACTURA',
    'TOTAL GENERAL', 'IMPORTE A PAGAR', 'TOTAL FACT.',
    'TOTAL FATURA', 'AMOUNT DUE', 'NET AMOUNT', 'TOTAL AMOUNT',
    'TOTAL A COBRAR',
    # Formatos compactos tipo "TOTAL: 1.323,41" (Laumar y similares)
    'TOTAL:', 'TOTAL :', 'IMPORTE:', 'IMPORTE :',
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


def _reconstruir_linea_words(words_linea: list) -> str:
    """
    Une tokens de una misma línea PDF respetando gaps:
    - gap <= 4 pts: chars adyacentes del mismo número → sin espacio
    - gap > 4 pts: palabras separadas → espacio

    Esto es crítico para PDFs que almacenan números carácter a carácter
    (p.ej. celdas con fondo coloreado): "1", ".", "323", ",", "41" → "1.323,41"
    """
    if not words_linea:
        return ''
    words_sorted = sorted(words_linea, key=lambda w: w[0])  # por x0
    texto = ''
    prev_x1 = -999.0
    for w in words_sorted:
        x0, x1, tok = w[0], w[2], w[4]
        gap = x0 - prev_x1
        if prev_x1 >= 0 and gap > 4:
            texto += ' '
        texto += tok
        prev_x1 = x1
    return texto


def _buscar_total_search(ruta_pdf: str) -> float:
    """
    Usa fitz search_for() para localizar el keyword 'TOTAL' en el PDF
    y extraer el importe que aparece a su derecha, incluso en celdas
    coloreadas donde get_text() falla.

    Tres intentos en orden de preferencia:
    1. get_textbox() — rápido, falla en celdas coloreadas
    2. words con unión por proximidad — captura chars almacenados carácter a carácter
    3. rawdict chars — máxima cobertura
    """
    import fitz
    _KW_TOTAL = ['TOTAL FACTURA', 'TOTAL:', 'TOTAL :', 'TOTAL FACT', 'TOTAL']
    try:
        doc = fitz.open(ruta_pdf)
        for pag in doc:
            pag_w = pag.rect.width
            for kw in _KW_TOTAL:
                for hit in pag.search_for(kw):
                    area = fitz.Rect(hit.x1, hit.y0 - 5, pag_w, hit.y1 + 5)

                    # Intento 1: get_textbox
                    texto_area = pag.get_textbox(area).strip()
                    nums = _extraer_importes(texto_area)
                    if nums:
                        doc.close()
                        return max(nums)

                    # Intento 2: words en el área con unión de chars adyacentes
                    words_area = [
                        w for w in pag.get_text("words", sort=True)
                        if fitz.Rect(w[:4]).intersects(area) and w[4].strip()
                    ]
                    if words_area:
                        texto_w = _reconstruir_linea_words(words_area)
                        nums = _extraer_importes(texto_w)
                        if nums:
                            doc.close()
                            return max(nums)

                    # Intento 3: rawdict chars en el área
                    chars_area: list[tuple[float, str]] = []
                    d = pag.get_text("rawdict", flags=0)
                    for block in d.get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                for c in span.get("chars", []):
                                    cbbox = c.get("bbox", [0, 0, 0, 0])
                                    if fitz.Rect(cbbox).intersects(area):
                                        chars_area.append((cbbox[0], c.get("c", "")))
                                if not span.get("chars") and span.get("text"):
                                    sbbox = span.get("bbox", [0, 0, 0, 0])
                                    if fitz.Rect(sbbox).intersects(area):
                                        chars_area.append((sbbox[0], span["text"]))
                    if chars_area:
                        chars_area.sort(key=lambda x: x[0])
                        texto_chars = ''.join(c[1] for c in chars_area)
                        nums = _extraer_importes(texto_chars)
                        if nums:
                            doc.close()
                            return max(nums)

        doc.close()
    except Exception:
        pass
    return 0.0


def _buscar_importes_search(ruta_pdf: str) -> list[float]:
    """
    Extrae importes monetarios del PDF usando múltiples modos de fitz.
    Captura texto de celdas coloreadas que get_text() básico ignora,
    incluyendo números almacenados carácter a carácter.
    """
    import fitz
    vals: set[float] = set()
    try:
        doc = fitz.open(ruta_pdf)
        for pag in doc:
            # Modo 1: get_text estándar
            for flags in (0,):
                t = pag.get_text("text", flags=flags)
                for v in _extraer_importes(t):
                    vals.add(v)

            # Modo 2: words agrupados por (block_no, line_no) + unión de chars adyacentes
            # Crítico para números almacenados carácter a carácter en celdas coloreadas
            words = pag.get_text("words", sort=False)
            lineas_dict: dict[tuple, list] = {}
            for w in words:
                key = (w[5], w[6])   # (block_no, line_no)
                lineas_dict.setdefault(key, []).append(w)
            for linea_words in lineas_dict.values():
                texto_linea = _reconstruir_linea_words(linea_words)
                for v in _extraer_importes(texto_linea):
                    vals.add(v)

            # Modo 3: rawdict chars agrupados por y-position aproximada
            d = pag.get_text("rawdict", flags=0)
            filas_chars: dict[int, list[tuple[float, str]]] = {}
            for block in d.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        chars = span.get("chars", [])
                        if chars:
                            for c in chars:
                                cbbox = c.get("bbox", [0, 0, 0, 0])
                                y_key = round(cbbox[1] / 3)
                                filas_chars.setdefault(y_key, []).append(
                                    (cbbox[0], c.get("c", ""))
                                )
                        elif span.get("text"):
                            sbbox = span.get("bbox", [0, 0, 0, 0])
                            y_key = round(sbbox[1] / 3)
                            filas_chars.setdefault(y_key, []).append(
                                (sbbox[0], span["text"])
                            )
            for fila in filas_chars.values():
                fila.sort(key=lambda x: x[0])
                texto_fila = ''.join(c[1] for c in fila)
                for v in _extraer_importes(texto_fila):
                    vals.add(v)

        doc.close()
    except Exception:
        pass
    return sorted(vals, reverse=True)


def _deducir_de_texto(texto: str, ruta_pdf: str = '') -> dict:
    cands = _extraer_importes(texto)   # mayor primero

    # Si tenemos el PDF, añadir candidatos vía fitz (captura celdas coloreadas)
    if ruta_pdf:
        extra = _buscar_importes_search(ruta_pdf)
        todos = sorted(set(cands) | set(extra), reverse=True)
    else:
        todos = cands

    if not todos:
        total = 0.0
    else:
        # Estrategia 1: keyword + número en texto extraído
        total = _total_por_contexto(texto, set(todos))
        # Estrategia 2: search_for directo en el PDF (para celdas coloreadas)
        if not total and ruta_pdf:
            total = _buscar_total_search(ruta_pdf)
        # Estrategia 3: el valor más alto de los candidatos combinados
        if not total:
            total = todos[0]

    cands = todos   # usar candidatos enriquecidos para deducción IVA
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
    Extrae el número de factura y la fecha del texto del PDF.

    Estrategia de dos pasos para el número de factura:
    1. CONTEXTO: busca lo que aparece justo después de labels tipo "Nº Factura",
       "Factura N°", etc. Esto evita confundir albaranes o NIFs con el nº real.
    2. REGEX puro (fallback): si el paso 1 falla, busca en todo el texto
       excluyendo patrones que coincidan con NIFs españoles (9 chars exactos).
    """
    from datetime import datetime

    PATRON_NUMFAC = re.compile(
        r'\b('
        r'\d{2}/[A-Z]{1,4}/\d{3,6}/\d{2}'    # 02/FA/7888/26
        r'|[A-Z]{1,4}\d{2}[/\-]\d{4,8}'       # FV26-02913 / F26/1034
        r'|[A-Z]{1,4}[/\-]\d{4,10}'            # FAC-12345 / G/016123
        r'|[A-Z]\d{4,12}'                       # A1751 / F000669
        r'|F[AVR]?\d{5,12}'                     # FV26002909
        r')\b'
    )
    _NIF_PAT = re.compile(r'^[A-Z]\d{7}[A-Z0-9]$')   # NIF español 9 chars

    # ── Paso 1: extracción por contexto (más fiable) ─────────────────────────
    _LABELS_NUMFAC = [
        'Nº FACTURA', 'N° FACTURA', 'FACTURA Nº', 'FACTURA N°',
        'NÚMERO FACTURA', 'NUMERO FACTURA', 'Nº DE FACTURA', 'N DE FACTURA',
        'NUM. FACTURA', 'NÚM. FACTURA', 'FACTURA NUM', 'INVOICE NO',
        'INVOICE NUMBER', 'FACTURA:', 'Nº FACT', 'NUM FACTURA',
    ]
    texto_up = texto.upper()
    num_factura = None

    for label in _LABELS_NUMFAC:
        pos = texto_up.find(label)
        if pos < 0:
            continue
        # Buscar el primer candidato en los 80 chars después del label
        fragmento = texto[pos + len(label): pos + len(label) + 80]
        for m in PATRON_NUMFAC.finditer(fragmento):
            c = m.group()
            if not _NIF_PAT.match(c):
                num_factura = c
                break
        if num_factura:
            break

    # ── Paso 2: regex en texto completo (fallback) ────────────────────────────
    if not num_factura:
        for m in PATRON_NUMFAC.finditer(texto):
            c = m.group()
            if not _NIF_PAT.match(c):
                num_factura = c
                break

    # ── Fecha ─────────────────────────────────────────────────────────────────
    # Paso 1: buscar la fecha justo después de un label "Fecha", "Data", etc.
    # Esto evita confundir la fecha de factura con las de vencimiento/albarán.
    _LABELS_FECHA = [
        'FECHA FACTURA', 'FECHA REGISTRO', 'FECHA EMISION', 'DATA FACTURA',
        'FECHA:', 'FECHA ', 'DATE:', 'DATE ',
    ]
    PATRON_FECHA2 = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{2})\b')
    PATRON_FECHA4 = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{4})\b')
    fecha = None

    for label_f in _LABELS_FECHA:
        pos_f = texto_up.find(label_f)
        if pos_f < 0:
            continue
        fragmento_f = texto[pos_f + len(label_f): pos_f + len(label_f) + 60]
        # Recoger todas las fechas del fragmento con su posición en el texto
        fechas_pos: list = []
        for patron, fmt in [(PATRON_FECHA4, '%d/%m/%Y'), (PATRON_FECHA2, '%d/%m/%y')]:
            for m in patron.finditer(fragmento_f):
                try:
                    d = datetime.strptime(m.group(), fmt).date()
                    if 2015 <= d.year <= 2040:
                        fechas_pos.append((m.start(), d))
                except ValueError:
                    pass
        if fechas_pos:
            # Tomar la que aparece PRIMERO en el texto (más cercana al label)
            fecha = min(fechas_pos, key=lambda x: x[0])[1]
            break

    # Paso 2: buscar la fecha más CERCANA al número de factura en el texto.
    # La fecha de la factura siempre aparece junto al número de factura.
    if not fecha and num_factura:
        pos_nfac = texto.find(num_factura)
        if pos_nfac >= 0:
            inicio = max(0, pos_nfac - 30)
            fin    = min(len(texto), pos_nfac + len(num_factura) + 120)
            fragmento_nfac = texto[inicio:fin]
            offset_nfac = pos_nfac - inicio + len(num_factura)
            candidatas: list = []
            for patron, fmt in [(PATRON_FECHA4, '%d/%m/%Y'), (PATRON_FECHA2, '%d/%m/%y')]:
                for m in patron.finditer(fragmento_nfac):
                    try:
                        d = datetime.strptime(m.group(), fmt).date()
                        if 2015 <= d.year <= 2040:
                            dist = abs(m.start() - offset_nfac)
                            candidatas.append((dist, d))
                    except ValueError:
                        pass
            if candidatas:
                fecha = min(candidatas, key=lambda x: x[0])[1]   # más cercana

    # Paso 3: última opción — tomar la fecha más antigua del documento completo
    if not fecha:
        todas: list = []
        for patron, fmt in [(PATRON_FECHA4, '%d/%m/%Y'), (PATRON_FECHA2, '%d/%m/%y')]:
            for m in patron.finditer(texto):
                try:
                    d = datetime.strptime(m.group(), fmt).date()
                    if 2015 <= d.year <= 2040:
                        todas.append(d)
                except ValueError:
                    pass
        if todas:
            fecha = min(todas)


    return fecha, num_factura


# ── Extracción por plantilla de coordenadas ───────────────────────────────────

def extraer_por_plantilla(ruta_pdf: str, plantilla: list) -> dict:
    """
    Extrae texto de zonas específicas del PDF usando coordenadas guardadas.
    Intenta primero pdfplumber; si el campo queda vacío (p.ej. celdas con fondo
    de color donde pdfplumber falla) reintenta con PyMuPDF rawdict.
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

    # Fallback con PyMuPDF para campos vacíos:
    # 1. words con unión de chars adyacentes (crucial para celdas coloreadas)
    # 2. rawdict chars como último recurso
    vacios = [item for item in plantilla if not resultado.get(item['campo'])]
    if vacios:
        try:
            import fitz
            doc = fitz.open(ruta_pdf)
            for item in vacios:
                pag  = doc[item['pagina']]
                rect = fitz.Rect(item['x0'], item['y0'], item['x1'], item['y1'])

                # Intento 1: words con unión por proximidad (reconstruye chars individuales)
                words_en_rect = [
                    w for w in pag.get_text("words", sort=True)
                    if w[4].strip() and fitz.Rect(w[:4]).intersects(rect)
                ]
                if words_en_rect:
                    resultado[item['campo']] = _reconstruir_linea_words(words_en_rect)
                    if resultado[item['campo']]:
                        continue

                # Intento 2: rawdict chars en el rect
                chars_rect: list[tuple[float, str]] = []
                d = pag.get_text("rawdict", flags=0)
                for block in d.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            for c in span.get("chars", []):
                                cbbox = c.get("bbox", [0, 0, 0, 0])
                                if fitz.Rect(cbbox).intersects(rect):
                                    chars_rect.append((cbbox[0], c.get("c", "")))
                            if not span.get("chars") and span.get("text"):
                                sbbox = span.get("bbox", [0, 0, 0, 0])
                                if fitz.Rect(sbbox).intersects(rect):
                                    chars_rect.append((sbbox[0], span["text"]))
                if chars_rect:
                    chars_rect.sort(key=lambda x: x[0])
                    resultado[item['campo']] = ''.join(c[1] for c in chars_rect)
            doc.close()
        except Exception:
            pass

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
