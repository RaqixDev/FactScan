"""
Llama a la API de Claude para extraer campos contables de texto de factura.
Devuelve un dict con los campos normalizados.
"""
import json
from datetime import date

import anthropic

from config import ANTHROPIC_KEY, MODELO_CLAUDE


SYSTEM_PROMPT = """
Eres un asistente especializado en contabilidad española.
Analiza el texto de una factura y extrae los campos en formato JSON.
Responde ÚNICAMENTE con el JSON, sin texto adicional ni markdown.
"""

USER_PROMPT_TPL = """
Del siguiente texto de factura española, extrae estos campos.
IMPORTANTE: el "proveedor" y "nif" son los del EMISOR de la factura (quien la emite/vende),
NO los del destinatario (quien la recibe/compra). La empresa receptora es MEDEMARI S.L.
{{
  "proveedor": "nombre completo del EMISOR (vendedor, proveedor)",
  "nif": "NIF/CIF del EMISOR (9 chars, ej: A08120149)",
  "num_factura": "número de factura completo tal como aparece",
  "fecha": "fecha de la factura en formato DD/MM/YYYY",
  "base_imponible": 126.00,
  "tipo_iva": 10.0,
  "cuota_iva": 12.60,
  "recargo_equivalencia": 0.0,
  "retencion": 0.0,
  "total": 138.60,
  "concepto": "descripción breve del producto/servicio"
}}

Si hay múltiples tipos de IVA, usa el predominante y añade campo "multiples_iva": true.
Si no encuentras algún campo, ponlo como null.

TEXTO DE LA FACTURA:
{texto_factura}
"""


def parsear_factura(texto_factura: str) -> dict:
    """
    Envía el texto a Claude y devuelve el dict con los campos extraídos.
    Normaliza los tipos de datos (fecha → date, floats, etc.).
    Lanza ValueError si la respuesta no es JSON válido.
    """
    cliente = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = USER_PROMPT_TPL.format(texto_factura=texto_factura)

    mensaje = cliente.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    texto_respuesta = mensaje.content[0].text.strip()
    # Quitar bloques markdown ```json ... ``` si el modelo los incluye
    if texto_respuesta.startswith('```'):
        texto_respuesta = texto_respuesta.split('```', 2)[1]
        if texto_respuesta.startswith('json'):
            texto_respuesta = texto_respuesta[4:]
        texto_respuesta = texto_respuesta.strip()
    try:
        datos = json.loads(texto_respuesta)
    except json.JSONDecodeError as e:
        raise ValueError(f"Respuesta de Claude no es JSON válido: {e}\n{texto_respuesta}") from e

    return _normalizar(datos)


def _normalizar(datos: dict) -> dict:
    """Convierte los campos al tipo correcto para el resto del sistema."""
    from datetime import datetime

    if datos.get('fecha'):
        try:
            datos['fecha'] = datetime.strptime(datos['fecha'], '%d/%m/%Y').date()
        except ValueError:
            datos['fecha'] = None

    for campo in ('base_imponible', 'tipo_iva', 'cuota_iva',
                  'recargo_equivalencia', 'retencion', 'total'):
        if datos.get(campo) is not None:
            datos[campo] = float(datos[campo])
        else:
            datos[campo] = 0.0

    # Renombrar claves para que coincidan con la interfaz de generador_dat
    datos['base']    = datos.pop('base_imponible')
    datos['recargo'] = datos.pop('recargo_equivalencia')

    return datos
