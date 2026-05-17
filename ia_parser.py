"""
Llama a la API de Claude para extraer campos contables de facturas.
- parsear_factura(texto): para PDFs con texto extraíble
- parsear_factura_vision(ruta_pdf): para PDFs imagen (sin texto), usa la API de visión
"""
import base64
import json
from pathlib import Path

import anthropic

from config import ANTHROPIC_KEY, MODELO_CLAUDE


SYSTEM_PROMPT = """
Eres un asistente especializado en contabilidad española.
Analiza la factura y extrae los campos en formato JSON.
Responde ÚNICAMENTE con el JSON, sin texto adicional ni markdown.
"""

_CAMPOS_JSON = """
{
  "proveedor": "nombre completo del EMISOR (vendedor)",
  "nif": "NIF/CIF del EMISOR (9 chars, ej: A08120149)",
  "num_factura": "número de factura completo",
  "fecha": "fecha de la factura en formato DD/MM/YYYY",
  "base_imponible": 126.00,
  "tipo_iva": 10.0,
  "cuota_iva": 12.60,
  "recargo_equivalencia": 0.0,
  "retencion": 0.0,
  "total": 138.60,
  "concepto": "descripción breve del producto/servicio"
}
"""

USER_PROMPT_TPL = (
    "Del siguiente texto de factura española, extrae estos campos.\n"
    "IMPORTANTE: 'proveedor' y 'nif' son del EMISOR, NO del destinatario "
    "(la empresa receptora es MEDEMARI S.L.).\n"
    "Si hay múltiples tipos de IVA, usa el predominante y añade "
    "\"multiples_iva\": true.\n"
    "Si no encuentras algún campo, ponlo como null.\n\n"
    f"{_CAMPOS_JSON}\n\n"
    "TEXTO DE LA FACTURA:\n{texto_factura}"
)

USER_PROMPT_VISION = (
    "Analiza esta factura española y extrae los campos indicados.\n"
    "IMPORTANTE: 'proveedor' y 'nif' son del EMISOR (quien emite la factura), "
    "NO de MEDEMARI S.L. (la empresa receptora).\n"
    "Si hay múltiples tipos de IVA, usa el predominante y añade "
    "\"multiples_iva\": true.\n"
    "Si no encuentras algún campo, ponlo como null.\n\n"
    f"{_CAMPOS_JSON}"
)


def ocr_pdf(ruta_pdf: str, ruta_salida: str = None,
            idioma: str = 'spa+eng') -> str:
    """
    Convierte un PDF imagen en un PDF con capa de texto buscable usando OCR.
    Usa ocrmypdf + Tesseract (gratuito, sin coste de API).

    ruta_salida: si es None, guarda como <nombre>_ocr.pdf junto al original.
    idioma: código Tesseract, 'spa+eng' para español+inglés.
    Devuelve la ruta del PDF generado.
    """
    from pathlib import Path
    import ocrmypdf

    if ruta_salida is None:
        p = Path(ruta_pdf)
        ruta_salida = str(p.parent / (p.stem + '_ocr.pdf'))

    # Intentar con idioma preferido; si falla (no instalado), usar inglés
    for lang in (idioma, 'eng'):
        try:
            ocrmypdf.ocr(
                ruta_pdf,
                ruta_salida,
                language=lang,
                progress_bar=False,
                force_ocr=True,
                skip_text=False,
                optimize=0,
            )
            return ruta_salida
        except ocrmypdf.exceptions.MissingDependencyError:
            continue
        except Exception as e:
            if lang == 'eng':
                raise RuntimeError(f'OCR fallido: {e}') from e

    raise RuntimeError('No hay ningún idioma Tesseract disponible')


def es_pdf_imagen(ruta_pdf: str) -> bool:
    """Devuelve True si el PDF no tiene texto extraíble (es imagen pura)."""
    try:
        import pdfplumber
        with pdfplumber.open(ruta_pdf) as pdf:
            for pag in pdf.pages:
                if (pag.extract_text() or '').strip():
                    return False
        return True
    except Exception:
        return False


def parsear_factura(texto_factura: str) -> dict:
    """Extrae campos de factura a partir del texto (PDF con texto)."""
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt  = USER_PROMPT_TPL.format(texto_factura=texto_factura)

    msg = cliente.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return _normalizar(_parse_json(msg.content[0].text))


def parsear_factura_vision(ruta_pdf: str) -> dict:
    """
    Extrae campos de factura enviando las páginas como imágenes a Claude.
    Necesario cuando el PDF es imagen pura (cero texto extraíble).
    """
    import fitz

    cliente = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    doc     = fitz.open(ruta_pdf)

    # Construir el mensaje con imágenes de todas las páginas (máx 3)
    content = []
    for i, pag in enumerate(doc):
        if i >= 3:
            break
        pix  = pag.get_pixmap(dpi=150)   # 150 dpi: buena calidad sin exceder tokens
        data = base64.standard_b64encode(pix.tobytes("png")).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        })
    doc.close()

    content.append({"type": "text", "text": USER_PROMPT_VISION})

    msg = cliente.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return _normalizar(_parse_json(msg.content[0].text))


def _parse_json(texto: str) -> dict:
    texto = texto.strip()
    if texto.startswith('```'):
        texto = texto.split('```', 2)[1]
        if texto.startswith('json'):
            texto = texto[4:]
        texto = texto.strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError as e:
        raise ValueError(f"Respuesta de Claude no es JSON válido: {e}\n{texto}") from e


def _normalizar(datos: dict) -> dict:
    from datetime import datetime

    if datos.get('fecha'):
        for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d'):
            try:
                datos['fecha'] = datetime.strptime(datos['fecha'], fmt).date()
                break
            except ValueError:
                pass
        else:
            datos['fecha'] = None

    for campo in ('base_imponible', 'tipo_iva', 'cuota_iva',
                  'recargo_equivalencia', 'retencion', 'total'):
        v = datos.get(campo)
        datos[campo] = float(v) if v is not None else 0.0

    datos['base']    = datos.pop('base_imponible')
    datos['recargo'] = datos.pop('recargo_equivalencia')
    return datos
