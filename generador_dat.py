import re
from datetime import date
from pathlib import Path

from config import EMPRESA, RUTA_A3CON, ENCODING_DAT


def siguiente_ndoc(carpeta: str = RUTA_A3CON) -> int:
    """
    Lee la carpeta de A3Con y devuelve el siguiente NºDoc secuencial.
    Busca ficheros R*.PDF y extrae los 6 dígitos del NºDoc.
    """
    ruta = Path(carpeta)
    patron = re.compile(r'^R.{9}(\d{6})', re.IGNORECASE)
    maximo = 0
    for f in ruta.glob("R*.PDF"):
        m = patron.match(f.name)
        if m:
            maximo = max(maximo, int(m.group(1)))
    return maximo + 1


def nombre_pdf(nif: str, ndoc: int) -> str:
    """Devuelve el nombre del PDF antes de importar en A3Con."""
    return f'R{nif[:9]}{ndoc:06d}.PDF'


def generar_suenlace(factura: dict, proveedor: dict, ndoc: int,
                     fecha_vencimiento: date) -> str:
    """
    Genera el contenido completo del SUENLACE.DAT para una factura.
    Retorna un str que debe guardarse en Latin-1.

    factura = {
        'fecha': date,
        'num_factura': str,
        'base': float,
        'tipo_iva': float,
        'cuota_iva': float,
        'total': float,
        'recargo': float,   (opcional, default 0.0)
        'retencion': float, (opcional, default 0.0)
    }
    proveedor = {
        'nif': str (9 chars),
        'nombre': str,
        'cuenta': str (8 dígitos),
        'contrapartida': str (8 dígitos),
        'cuenta_pago': str (8 dígitos),
        'dias_pago': int,
        'dia_fijo': int,
    }
    """
    emp    = EMPRESA
    fecha  = factura['fecha'].strftime('%Y%m%d')
    nif    = proveedor['nif'].ljust(9)[:9]
    nombre = proveedor['nombre'].ljust(30)[:30]

    # Cuentas: tipo 5 → 8 dígitos + '0000' (12c); tipo 3 → ljust(12) con espacios
    cuenta_prov  = (proveedor['cuenta'] + '0000').ljust(12)[:12]
    cuenta_gast  = (proveedor['contrapartida'] + '0000').ljust(12)[:12]
    cuenta_banco = proveedor['cuenta_pago'].ljust(12)[:12]
    cuenta_prov_av = proveedor['cuenta'].ljust(12)[:12]  # línea SE-AV: espacios

    # Número de factura: versión corta (10c) y completa (13c)
    numfac_largo = factura['num_factura']
    numfac_10 = (numfac_largo[-10:] if len(numfac_largo) > 10
                 else numfac_largo).ljust(10)
    numfac_13 = numfac_largo.ljust(13)[:13]

    concepto_largo = f"Fra.Nº {numfac_largo} de {proveedor['nombre'][:6]}"
    concepto_30 = concepto_largo.ljust(30)[:30]

    total = factura['total']
    ndoc_str = f'{ndoc:06d}'

    # Operaciones IVA: lista de {base, tipo_iva, cuota_iva, recargo, retencion}
    # Compatibilidad con facturas de IVA único (campos planos)
    iva_ops = factura.get('iva_ops')
    if not iva_ops:
        iva_ops = [{
            'base':      factura.get('base', 0.0),
            'tipo_iva':  factura.get('tipo_iva', 0.0),
            'cuota_iva': factura.get('cuota_iva', 0.0),
            'recargo':   factura.get('recargo', 0.0),
            'retencion': factura.get('retencion', 0.0),
        }]

    def fmt_importe(v: float) -> str:
        return f' {v:013.2f}'  # espacio + 13 chars = 14 chars total

    # ── LÍNEA 1 — SE-CI Extendida (510 chars) ─────────────────────────────
    l1 = ''
    l1 += '5'                            # [000]     tipform
    l1 += emp                            # [001-005] empresa
    l1 += fecha                          # [006-013] fecha factura
    l1 += '1'                            # [014]     tipreg = Factura
    l1 += cuenta_prov                    # [015-026] cuenta proveedor (12c)
    l1 += nombre                         # [027-056] nombre proveedor (30c)
    l1 += '2'                            # [057]     tipfac = Compras
    l1 += numfac_10                      # [058-067] nº factura (10c)
    l1 += 'I'                            # [068]     orden
    l1 += concepto_30                    # [069-098] concepto (30c) — spec dice 31 pero es 30
    l1 += fmt_importe(total)             # [099-112] importe (14c)
    l1 += 'R'                            # [113]     tipo doc
    l1 += nif                            # [114-122] NIF (9c)
    l1 += ndoc_str                       # [123-128] NºDoc (6c)
    l1 += ' ' * 107                      # [129-235] reserva
    l1 += fecha                          # [236-243] fecha devengo
    l1 += fecha                          # [244-251] fecha asiento
    l1 += numfac_13                      # [252-264] nº factura completo (13c)
    l1 += ' ' * 243                      # [265-507] reserva
    l1 += 'EN'                           # [508-509] fin
    assert len(l1) == 510, f"L1 longitud {len(l1)} ≠ 510"

    # ── LÍNEAS 2 — SE-DI Extendidas (510 chars c/u) — una por operación IVA
    lineas_l2 = []
    for idx, op in enumerate(iva_ops):
        es_ultima = (idx == len(iva_ops) - 1)
        base    = op.get('base', 0.0)
        iva_pct = op.get('tipo_iva', 0.0)
        iva_imp = op.get('cuota_iva', 0.0)
        rec_pct = op.get('recargo', 0.0)
        rec_imp = base * rec_pct / 100
        ret_pct = op.get('retencion', 0.0)
        ret_imp = base * ret_pct / 100

        l2 = ''
        l2 += '5'                            # [000]     tipform
        l2 += emp                            # [001-005] empresa
        l2 += fecha                          # [006-013] fecha factura
        l2 += '9'                            # [014]     tip-reg = Detalle IVA
        l2 += cuenta_gast                    # [015-026] cuenta gasto (12c)
        l2 += ' ' * 30                       # [027-056] nombre (vacío)
        l2 += 'C'                            # [057]     tipimp = Cargo
        l2 += numfac_10                      # [058-067] nº factura (10c)
        l2 += 'U' if es_ultima else 'I'      # [068]     U=última, I=intermedia
        l2 += concepto_30                    # [069-098] concepto (30c)
        l2 += '01'                           # [099-100] subtipo
        l2 += fmt_importe(base)              # [101-114] base (14c)
        l2 += f'{iva_pct:05.2f}'            # [115-119] % IVA (5c)
        l2 += fmt_importe(iva_imp)           # [120-133] cuota IVA (14c)
        l2 += f'{rec_pct:05.2f}'            # [134-138] % recargo (5c)
        l2 += fmt_importe(rec_imp)           # [139-152] cuota recargo (14c)
        l2 += f'{ret_pct:05.2f}'            # [153-157] % retención (5c)
        l2 += fmt_importe(ret_imp)           # [158-171] cuota retención (14c)
        l2 += '01'                           # [172-173] impreso
        l2 += 'S'                            # [174]     op-iva = con IVA
        l2 += ' ' * 333                      # [175-507] reserva
        l2 += 'EN'                           # [508-509] fin
        assert len(l2) == 510, f"L2[{idx}] longitud {len(l2)} ≠ 510"
        lineas_l2.append(l2)

    # ── LÍNEA 3 — SE-AV Estándar (254 chars) — VENCIMIENTO ────────────────
    fecha_ven = fecha_vencimiento.strftime('%Y%m%d')
    l3 = ''
    l3 += '3'                            # [000]     tipform = estándar A3Con
    l3 += emp                            # [001-005] empresa
    l3 += fecha_ven                      # [006-013] fecha VENCIMIENTO
    l3 += 'V'                            # [014]     tipreg = Vencimiento
    l3 += cuenta_prov_av                 # [015-026] cuenta proveedor (12c, espacios)
    l3 += nombre                         # [027-056] nombre (30c)
    l3 += 'P'                            # [057]     tipven = Pago
    l3 += numfac_10                      # [058-067] nº factura (10c)
    l3 += ' '                            # [068]     reserva1
    l3 += concepto_30                    # [069-098] descripción (30c)
    l3 += fmt_importe(total)             # [099-112] importe (14c)
    l3 += fecha                          # [113-120] fecha factura
    l3 += cuenta_banco                   # [121-132] cuenta tesorería (12c)
    l3 += f'{proveedor["dias_pago"]:02d}'  # [133-134] forma pago
    l3 += '01'                           # [135-136] nº vencimiento
    l3 += ' ' * 115                      # [137-251] reserva
    l3 += 'E'                            # [252]     moneda = Euros
    l3 += ' '                            # [253]     ind-gen
    assert len(l3) == 254, f"L3 longitud {len(l3)} ≠ 254"

    return l1 + '\n' + '\n'.join(lineas_l2) + '\n' + l3 + '\n'


def guardar_dat(contenido: str, ruta_destino: str) -> None:
    """Escribe el SUENLACE.DAT en Latin-1."""
    Path(ruta_destino).write_text(contenido, encoding=ENCODING_DAT)


def copiar_pdf(pdf_origen: str, nif: str, ndoc: int,
               carpeta_destino: str = RUTA_A3CON) -> Path:
    """
    Copia el PDF de factura a la carpeta de A3Con con la nomenclatura correcta.
    Devuelve la ruta del fichero copiado.
    """
    import shutil
    destino = Path(carpeta_destino) / nombre_pdf(nif, ndoc)
    shutil.copy2(pdf_origen, destino)
    return destino
