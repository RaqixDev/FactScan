from datetime import date, timedelta


def calcular_vencimiento(fecha_factura: date, dias, dia_fijo) -> date:
    """
    Suma los días de pago a la fecha de factura y ajusta al día fijo del mes.
    Si dia_fijo == 0, devuelve la fecha sin ajuste.
    Acepta int o str para dias/dia_fijo (la BD puede devolver strings si el
    valor fue almacenado incorrectamente).
    """
    try:
        dias     = int(dias)     if dias     not in (None, '') else 0
        dia_fijo = int(dia_fijo) if dia_fijo not in (None, '') else 0
    except (ValueError, TypeError):
        dias, dia_fijo = 0, 0
    fecha_raw = fecha_factura + timedelta(days=dias)
    if dia_fijo == 0:
        return fecha_raw
    if fecha_raw.day <= dia_fijo:
        return fecha_raw.replace(day=dia_fijo)
    else:
        if fecha_raw.month == 12:
            return date(fecha_raw.year + 1, 1, dia_fijo)
        else:
            return date(fecha_raw.year, fecha_raw.month + 1, dia_fijo)
