from datetime import date, timedelta


def calcular_vencimiento(fecha_factura: date, dias: int, dia_fijo: int) -> date:
    """
    Suma los días de pago a la fecha de factura y ajusta al día fijo del mes.
    Si dia_fijo == 0, devuelve la fecha sin ajuste.
    Ejemplo: 15/05/2026 + 84 días = 07/08/2026 → ajuste día 25 = 25/08/2026
    """
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
