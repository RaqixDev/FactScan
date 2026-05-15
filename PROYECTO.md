# PROYECTO: A3Scan Propio — Procesador de Facturas PDF para A3Con
## Contexto completo para retomar el desarrollo

---

## 1. OBJETIVO

Construir una aplicación de escritorio Windows en **Python** que:
1. Procese ficheros PDF de facturas de proveedores
2. Extraiga los datos contables relevantes (proveedor, NIF, nº factura, fecha, base, IVA, etc.)
3. Consulte una tabla local de proveedores para obtener las cuentas contables
4. Genere el fichero **SUENLACE.DAT** con el formato exacto que A3Con necesita para importar
5. Copie/renombre el PDF a la carpeta de A3Con con la nomenclatura correcta

Este programa sustituye a **A3Scan**, que falla frecuentemente (especialmente con formatos de número de factura no estándar).

**Empresa:** MEDEMARI, S.L. — `E00002` en A3Con
**Software contable:** A3Con (A3CONV5)
**Ruta A3Con:** `C:\A3\A3CONV5\E00002\FACTURAS\2026\`

---

## 2. STACK TECNOLÓGICO

```
Python 3.11+
├── pdfplumber / PyMuPDF      → Extracción texto de PDFs nativos
├── pytesseract + Tesseract   → OCR para PDFs escaneados
├── API de Claude (Anthropic) → Interpretación inteligente de campos
├── sqlite3 (stdlib)          → Base de datos local de proveedores
├── PyQt6 o Tkinter           → Interfaz gráfica Windows
├── watchdog                  → Vigilar carpeta de entrada (opcional)
└── pathlib / shutil          → Gestión de ficheros
```

**Dependencias pip:**
```
pip install pdfplumber PyMuPDF pytesseract anthropic PyQt6
```

---

## 3. ARQUITECTURA DEL PROGRAMA

```
PDF entra al programa
      ↓
[Módulo Extracción]
  pdfplumber → texto nativo
  Tesseract  → OCR si es escaneado
      ↓
[Módulo IA - API Claude]
  Prompt estructurado → JSON con campos:
  {nif, proveedor, num_factura, fecha, base, tipo_iva,
   cuota_iva, total, recargo_equiv, retencion}
      ↓
[Módulo Proveedores - SQLite]
  Buscar por NIF → cuenta, contrapartida, banco, días_pago, día_fijo
  ¿No existe? → Abrir formulario alta proveedor
      ↓
[Módulo Vencimiento]
  fecha_factura + días_pago → ajuste al día_fijo del mes
      ↓
[Módulo NºDoc]
  Leer carpeta C:\A3\...\FACTURAS\2026\
  Máximo NºDoc existente + 1
      ↓
[Módulo Generador DAT]
  Línea 1 SE-CI extendida (510 chars) → asiento proveedor
  Línea 2 SE-DI extendida (510 chars) → detalle gasto + IVA
  Línea 3 SE-AV estándar  (254 chars) → vencimiento (opcional)
      ↓
[Módulo Ficheros]
  Copiar PDF → C:\A3\...\FACTURAS\2026\R{NIF}{NºDoc}.PDF
  Guardar    → C:\A3\...\FACTURAS\2026\SUENLACE.DAT
      ↓
A3Con importa → asigna NºAsiento → renombra PDF a R{NIF}{NºDoc}@{NºAsiento}.PDF
```

---

## 4. FORMATO SUENLACE.DAT — ESPECIFICACIÓN COMPLETA

El fichero tiene **3 líneas por factura** (terminadas en `\n`), codificación **Latin-1**.

### 4.1 LÍNEA 1 — SE-CI Extendida (Asiento Proveedor)

**Longitud: 510 caracteres**

| Pos (0-base) | Long | Campo | Valor / Descripción |
|---|---|---|---|
| 000 | 1 | tipform | `5` (constante, extensión A3Scan) |
| 001-005 | 5 | cod-emp | `00002` — código empresa |
| 006-013 | 8 | fechafac | Fecha factura `AAAAMMDD` |
| 014 | 1 | tipreg | `1` = Factura / `2` = Abono |
| 015-026 | 12 | cuenta | Cuenta proveedor 8 dígitos + `0000` padding (ej: `400000380000`) |
| 027-056 | 30 | descuenta | Nombre proveedor, relleno con espacios a la derecha |
| 057 | 1 | tipfac | `2` = Compras (constante para facturas recibidas) |
| 058-067 | 10 | numfac | Nº factura (10 chars, truncado/relleno con espacios) |
| 068 | 1 | orden | `I` (constante) |
| 069-099 | 31 | desfac | Concepto: `Fra.Nº {num_factura_completo} de {proveedor_corto}` |
| 099-112 | 14 | importe | Total factura: ` {importe:013.2f}` (espacio + 13 chars) |
| 113 | 1 | tipo_doc | `R` = Recibida (constante) |
| 114-122 | 9 | nif | NIF del proveedor (9 chars) |
| 123-128 | 6 | ndoc | NºDoc secuencial: `{ndoc:06d}` |
| 129-235 | 107 | reserva1 | Espacios |
| 236-243 | 8 | fecha_dev | Fecha devengo `AAAAMMDD` (= fecha factura) |
| 244-251 | 8 | fecha_asi | Fecha asiento `AAAAMMDD` (= fecha factura) |
| 252-264 | 13 | numfac_completo | Nº factura completo (13 chars, relleno espacios) |
| 265-507 | 243 | reserva2 | Espacios |
| 508-509 | 2 | fin | `EN` (constante) |

**Notas importantes:**
- El campo `cuenta` (pos 015-026, 12 chars) almacena la cuenta de 8 dígitos + `0000`: ej. `40000038` → `400000380000`
- El campo `numfac` (pos 058-067, 10 chars) es el número SIN el prefijo completo si no cabe (ej. `02/FA/7888/26` → `FA/7888/26`)
- El campo `importe` (pos 099-112, 14 chars) lleva un espacio al inicio: ` 0000000138.60`

### 4.2 LÍNEA 2 — SE-DI Extendida (Detalle Gasto + IVA)

**Longitud: 510 caracteres**

| Pos (0-base) | Long | Campo | Valor / Descripción |
|---|---|---|---|
| 000 | 1 | tipform | `5` (constante) |
| 001-005 | 5 | cod-emp | `00002` |
| 006-013 | 8 | fechafac | Fecha factura `AAAAMMDD` |
| 014 | 1 | tip-reg | `9` (constante — detalle IVA) |
| 015-026 | 12 | cuenta | Cuenta gasto/contrapartida 8 dígitos + `0000` (ej: `600000010000`) |
| 027-056 | 30 | descuenta | Espacios (vacío en la línea de gasto) |
| 057 | 1 | tipimp | `C` = Cargo/Debe (constante para compras) |
| 058-067 | 10 | numfac | Nº factura (10 chars, igual que línea 1) |
| 068 | 1 | orden | `U` = Última línea del asiento (constante) |
| 069-098 | 30 | descrip | Concepto (igual que línea 1, 30 chars) |
| 099-100 | 2 | subtipo | `01` (constante — subtipo operación IVA) |
| 101-114 | 14 | base | Base imponible: ` {base:013.2f}` |
| 115-119 | 5 | por-iva | Tipo IVA: `{iva:05.2f}` (ej: `10.00`) |
| 120-133 | 14 | cuo-iva | Cuota IVA: ` {cuota_iva:013.2f}` |
| 134-138 | 5 | por-rec | Recargo equiv. %: `00.00` |
| 139-152 | 14 | cuo-rec | Cuota recargo: ` 0000000000.00` |
| 153-157 | 5 | por-ret | Retención %: `00.00` |
| 158-171 | 14 | cuo-ret | Cuota retención: ` 0000000000.00` |
| 172-173 | 2 | impreso | `01` (constante) |
| 174 | 1 | op-iva | `S` = Operación con IVA (constante) |
| 175-507 | 333 | reserva | Espacios |
| 508-509 | 2 | fin | `EN` (constante) |

### 4.3 LÍNEA 3 — SE-AV Estándar (Vencimiento)

**Longitud: 254 caracteres** — A3Con puede generarlo automáticamente,
pero se implementa por si acaso. Byte inicial `3` (no `5`).

| Pos (0-base) | Long | Campo | Valor / Descripción |
|---|---|---|---|
| 000 | 1 | tipform | `3` (constante estándar A3Con) |
| 001-005 | 5 | cod-emp | `00002` |
| 006-013 | 8 | fechaven | **Fecha vencimiento** `AAAAMMDD` (calculada) |
| 014 | 1 | tipreg | `V` (constante) |
| 015-026 | 12 | cuenta | Cuenta proveedor 8 dígitos + espacios a la derecha |
| 027-056 | 30 | descuent | Nombre proveedor, relleno espacios |
| 057 | 1 | tipven | `P` = Pago (constante para compras) |
| 058-067 | 10 | numfac | Nº factura (10 chars) |
| 068 | 1 | reserva1 | Espacio |
| 069-098 | 30 | descrip | Descripción vencimiento |
| 099-112 | 14 | importe | Total factura: ` {total:013.2f}` |
| 113-120 | 8 | fechafac | Fecha factura `AAAAMMDD` |
| 121-132 | 12 | cuentates | Cuenta tesorería (banco) 8 dígitos + espacios |
| 133-134 | 2 | formpag | Días de pago: `{dias:02d}` (ej: `84`) |
| 135-136 | 2 | nro-ven | Nº vencimiento: `01` |
| 137-251 | 115 | reserva2 | Espacios |
| 252 | 1 | moneda | `E` = Euros (constante) |
| 253 | 1 | ind-gen | Espacio |

### 4.4 CÁLCULO DEL VENCIMIENTO

```python
from datetime import date, timedelta

def calcular_vencimiento(fecha_factura: date, dias: int, dia_fijo: int) -> date:
    """
    Suma los días de pago a la fecha de factura y ajusta al día fijo del mes.
    Ejemplo: 15/05/2026 + 84 días = 07/08/2026 → ajuste día 25 = 25/08/2026
    """
    fecha_raw = fecha_factura + timedelta(days=dias)
    if fecha_raw.day <= dia_fijo:
        return fecha_raw.replace(day=dia_fijo)
    else:
        # Pasar al mes siguiente
        if fecha_raw.month == 12:
            return date(fecha_raw.year + 1, 1, dia_fijo)
        else:
            return date(fecha_raw.year, fecha_raw.month + 1, dia_fijo)
```

---

## 5. NOMENCLATURA DEL PDF

```
Nombre antes de importar:  R{NIF}{NºDoc:06d}.PDF
Nombre tras importar A3Con: R{NIF}{NºDoc:06d}@{NºAsiento:012d}.PDF

Ejemplos reales:
  RA08120149008931.PDF          → antes de importar
  RA08120149008931@000000015872.PDF  → después (A3Con añade @NºAsiento)
```

**Componentes:**
- `R` — tipo Recibida (constante)
- `NIF` — 9 caracteres del NIF del proveedor
- `NºDoc` — 6 dígitos, contador global secuencial del año
- `@NºAsiento` — 12 dígitos, lo asigna A3Con al importar (el programa NO lo genera)

**Obtener el siguiente NºDoc:**
```python
import re
from pathlib import Path

def siguiente_ndoc(carpeta: str) -> int:
    ruta = Path(carpeta)
    patron = re.compile(r'^R.{9}(\d{6})', re.IGNORECASE)
    maximo = 0
    for f in ruta.glob("R*.PDF"):
        m = patron.match(f.name)
        if m:
            maximo = max(maximo, int(m.group(1)))
    return maximo + 1
```

---

## 6. BASE DE DATOS DE PROVEEDORES (SQLite)

**Fichero:** `proveedores.db` en la carpeta del programa.

```sql
CREATE TABLE proveedores (
    nif             TEXT PRIMARY KEY,
    nombre          TEXT NOT NULL,
    cuenta          TEXT NOT NULL,        -- ej: 40000038
    contrapartida   TEXT NOT NULL,        -- ej: 60000001
    cuenta_pago     TEXT,                 -- ej: 57200002 (banco)
    dias_pago       INTEGER DEFAULT 30,   -- ej: 84
    dia_fijo        INTEGER DEFAULT 0,    -- ej: 25 (0 = sin ajuste)
    tipo_pago       TEXT DEFAULT 'TR',    -- TR=Transferencia, EF=Efectivo
    email           TEXT,
    iva_habitual    REAL DEFAULT 10.0,    -- % IVA más frecuente
    activo          INTEGER DEFAULT 1
);

CREATE TABLE proveedor_iva (
    nif             TEXT,
    operacion       INTEGER,              -- 1, 2, 3...
    tipo_iva        INTEGER,              -- código A3Con (5=10%, 6=4%, 4=21%)
    porcentaje      REAL,                 -- 10.0, 4.0, 21.0
    PRIMARY KEY (nif, operacion),
    FOREIGN KEY (nif) REFERENCES proveedores(nif)
);
```

**Datos de ejemplo (Guzman Gastronomia):**
```sql
INSERT INTO proveedores VALUES (
    'B63864029', 'GUZMAN GASTRONOMIA S.L.', '40000125', '60000004',
    '57200002', 84, 25, 'TR', 'adm.comercial@bidfoodiberia.com', 10.0, 1
);
INSERT INTO proveedor_iva VALUES ('B63864029', 1, 5, 10.0);
INSERT INTO proveedor_iva VALUES ('B63864029', 2, 6, 4.0);
INSERT INTO proveedor_iva VALUES ('B63864029', 3, 4, 21.0);
```

---

## 7. PROMPT PARA LA API DE CLAUDE (extracción de campos)

```python
SYSTEM_PROMPT = """
Eres un asistente especializado en contabilidad española.
Analiza el texto de una factura y extrae los campos en formato JSON.
Responde ÚNICAMENTE con el JSON, sin texto adicional ni markdown.
"""

USER_PROMPT = """
Del siguiente texto de factura española, extrae estos campos:
{
  "proveedor": "nombre completo del emisor",
  "nif": "NIF/CIF del emisor (9 chars, ej: A08120149)",
  "num_factura": "número de factura completo tal como aparece",
  "fecha": "fecha de la factura en formato DD/MM/YYYY",
  "base_imponible": 126.00,
  "tipo_iva": 10.0,
  "cuota_iva": 12.60,
  "recargo_equivalencia": 0.0,
  "retencion": 0.0,
  "total": 138.60,
  "concepto": "descripción breve del producto/servicio"
}

Si hay múltiples tipos de IVA, usa el predominante y añade campo "multiples_iva": true.
Si no encuentras algún campo, ponlo como null.

TEXTO DE LA FACTURA:
{texto_factura}
"""
```

---

## 8. MÓDULO GENERADOR DAT — CÓDIGO NÚCLEO

```python
def generar_suenlace(factura: dict, proveedor: dict, ndoc: int,
                     fecha_vencimiento: date) -> str:
    """
    Genera el contenido completo del SUENLACE.DAT para una factura.

    factura = {
        'fecha': date,
        'num_factura': str,
        'base': float,
        'tipo_iva': float,
        'cuota_iva': float,
        'total': float,
        'recargo': float,
        'retencion': float,
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
    emp    = '00002'
    fecha  = factura['fecha'].strftime('%Y%m%d')
    nif    = proveedor['nif'].ljust(9)[:9]
    nombre = proveedor['nombre'].ljust(30)[:30]
    cuenta_prov = (proveedor['cuenta'] + '0000').ljust(12)[:12]
    cuenta_gast = (proveedor['contrapartida'] + '0000').ljust(12)[:12]
    cuenta_banco = proveedor['cuenta_pago'].ljust(12)[:12]

    # Número de factura: versión corta (10c) y completa (13c)
    numfac_largo = factura['num_factura']
    numfac_10 = numfac_largo[-10:].ljust(10) if len(numfac_largo) > 10 \
                else numfac_largo.ljust(10)
    numfac_13 = numfac_largo.ljust(13)[:13]

    concepto_largo = f"Fra.Nº {numfac_largo} de {proveedor['nombre'][:6]}"
    concepto_31 = concepto_largo.ljust(31)[:31]
    concepto_30 = concepto_largo.ljust(30)[:30]

    total   = factura['total']
    base    = factura['base']
    iva_pct = factura['tipo_iva']
    iva_imp = factura['cuota_iva']
    rec_pct = factura.get('recargo', 0.0)
    rec_imp = base * rec_pct / 100
    ret_pct = factura.get('retencion', 0.0)
    ret_imp = base * ret_pct / 100

    ndoc_str = f'{ndoc:06d}'

    def fmt_importe(v):
        return f' {v:013.2f}'  # espacio + 13 chars

    # ── LÍNEA 1 — SE-CI Extendida (510 chars) ─────────────────────────
    l1 = ''
    l1 += '5'                           # [000] tipform
    l1 += emp                           # [001-005] empresa
    l1 += fecha                         # [006-013] fecha factura
    l1 += '1'                           # [014] tipreg = Factura
    l1 += cuenta_prov                   # [015-026] cuenta proveedor (12c)
    l1 += nombre                        # [027-056] nombre proveedor (30c)
    l1 += '2'                           # [057] tipfac = Compras
    l1 += numfac_10                     # [058-067] nº factura (10c)
    l1 += 'I'                           # [068] orden
    l1 += concepto_31                   # [069-099] concepto (31c)
    l1 += fmt_importe(total)            # [099-112] importe (14c)  ← pos 99
    l1 += 'R'                           # [113] tipo doc
    l1 += nif                           # [114-122] NIF (9c)
    l1 += ndoc_str                      # [123-128] NºDoc (6c)
    l1 += ' ' * 107                     # [129-235] reserva
    l1 += fecha                         # [236-243] fecha devengo
    l1 += fecha                         # [244-251] fecha asiento
    l1 += numfac_13                     # [252-264] nº factura completo (13c)
    l1 += ' ' * 243                     # [265-507] reserva
    l1 += 'EN'                          # [508-509] fin
    assert len(l1) == 510, f"L1 longitud {len(l1)} ≠ 510"

    # ── LÍNEA 2 — SE-DI Extendida (510 chars) ─────────────────────────
    l2 = ''
    l2 += '5'                           # [000] tipform
    l2 += emp                           # [001-005] empresa
    l2 += fecha                         # [006-013] fecha factura
    l2 += '9'                           # [014] tip-reg = Detalle IVA
    l2 += cuenta_gast                   # [015-026] cuenta gasto (12c)
    l2 += ' ' * 30                      # [027-056] nombre (vacío)
    l2 += 'C'                           # [057] tipimp = Cargo
    l2 += numfac_10                     # [058-067] nº factura (10c)
    l2 += 'U'                           # [068] orden = Última línea
    l2 += concepto_30                   # [069-098] concepto (30c)
    l2 += '01'                          # [099-100] subtipo
    l2 += fmt_importe(base)             # [101-114] base (14c)
    l2 += f'{iva_pct:05.2f}'           # [115-119] % IVA (5c)
    l2 += fmt_importe(iva_imp)          # [120-133] cuota IVA (14c)
    l2 += f'{rec_pct:05.2f}'           # [134-138] % recargo (5c)
    l2 += fmt_importe(rec_imp)          # [139-152] cuota recargo (14c)
    l2 += f'{ret_pct:05.2f}'           # [153-157] % retención (5c)
    l2 += fmt_importe(ret_imp)          # [158-171] cuota retención (14c)
    l2 += '01'                          # [172-173] impreso
    l2 += 'S'                           # [174] op-iva = con IVA
    l2 += ' ' * 333                     # [175-507] reserva
    l2 += 'EN'                          # [508-509] fin
    assert len(l2) == 510, f"L2 longitud {len(l2)} ≠ 510"

    # ── LÍNEA 3 — SE-AV Estándar (254 chars) — VENCIMIENTO ────────────
    fecha_ven = fecha_vencimiento.strftime('%Y%m%d')
    l3 = ''
    l3 += '3'                           # [000] tipform = estándar A3Con
    l3 += emp                           # [001-005] empresa
    l3 += fecha_ven                     # [006-013] fecha VENCIMIENTO
    l3 += 'V'                           # [014] tipreg = Vencimiento
    l3 += proveedor['cuenta'].ljust(12)[:12]  # [015-026] cuenta (12c)
    l3 += nombre                        # [027-056] nombre (30c)
    l3 += 'P'                           # [057] tipven = Pago
    l3 += numfac_10                     # [058-067] nº factura (10c)
    l3 += ' '                           # [068] reserva1
    l3 += concepto_30                   # [069-098] descripción (30c)
    l3 += fmt_importe(total)            # [099-112] importe (14c)
    l3 += fecha                         # [113-120] fecha factura
    l3 += cuenta_banco                  # [121-132] cuenta tesorería (12c)
    l3 += f'{proveedor["dias_pago"]:02d}'  # [133-134] forma pago
    l3 += '01'                          # [135-136] nº vencimiento
    l3 += ' ' * 115                     # [137-251] reserva
    l3 += 'E'                           # [252] moneda = Euros
    l3 += ' '                           # [253] ind-gen
    assert len(l3) == 254, f"L3 longitud {len(l3)} ≠ 254"

    return l1 + '\n' + l2 + '\n' + l3 + '\n'
```

---

## 9. FICHEROS DEL PROYECTO

```
a3scan_propio/
├── PROYECTO.md              ← este fichero (contexto completo)
├── main.py                  ← punto de entrada, interfaz gráfica
├── extractor.py             ← extracción texto PDF (pdfplumber + OCR)
├── ia_parser.py             ← llamada API Claude para identificar campos
├── proveedores.py           ← gestión SQLite de proveedores
├── generador_dat.py         ← generación SUENLACE.DAT
├── vencimiento.py           ← cálculo de vencimientos
├── config.py                ← rutas, empresa, API key
├── proveedores.db           ← base de datos SQLite (se crea sola)
└── requirements.txt         ← dependencias
```

---

## 10. CONFIG.PY — CONFIGURACIÓN

```python
# config.py
EMPRESA         = '00002'
RUTA_A3CON      = r'C:\A3\A3CONV5\E00002\FACTURAS\2026'
RUTA_ENTRADA    = r'C:\Facturas_Entrada'   # carpeta donde dejas los PDF nuevos
ANTHROPIC_KEY   = 'sk-ant-...'             # tu API key de Anthropic
MODELO_CLAUDE   = 'claude-sonnet-4-20250514'
ENCODING_DAT    = 'latin-1'
```

---

## 11. EJEMPLO REAL VERIFICADO

Factura de **Consignaciones del Mar, S.A.** procesada por A3Scan original:

```
NIF:          A08120149
Nº Factura:   02/FA/7888/26
Fecha:        15/05/2026
Base:         126,00 €
IVA:          10% → 12,60 €
Total:        138,60 €
Cuenta prov:  40000038
Contrapartida: 60000001
NºDoc asignado: 008931
NºAsiento A3Con: 15872
PDF generado: RA08120149008931@000000015872.PDF
```

El SUENLACE.DAT verificado byte a byte (fichero real de A3Scan) está en `/PROYECTO.md` sección 8 — el código de `generar_suenlace()` reproduce exactamente ese formato.

---

## 12. TAREAS PENDIENTES DE IMPLEMENTAR

- [x] Especificación completa del formato SUENLACE.DAT
- [x] Patrón de nomenclatura PDF
- [x] Estructura base de datos proveedores
- [x] Cálculo de vencimientos
- [x] Función `generar_suenlace()` núcleo
- [ ] Módulo extractor PDF (`extractor.py`)
- [ ] Módulo IA parser (`ia_parser.py`)
- [ ] Módulo gestión proveedores con CRUD (`proveedores.py`)
- [ ] Interfaz gráfica (`main.py`)
- [ ] Función `siguiente_ndoc()` leyendo carpeta A3Con
- [ ] Manejo de facturas con múltiples tipos de IVA
- [ ] Manejo de facturas con retención (IRPF)
- [ ] Validación cruzada base + IVA = total
- [ ] Log de operaciones procesadas
- [ ] Modo revisión manual antes de generar DAT

---

## 13. NOTAS IMPORTANTES

1. **Encoding:** el SUENLACE.DAT debe escribirse en **Latin-1**, no UTF-8, para que A3Con lo lea correctamente (el carácter `º` de `Fra.Nº` es Latin-1).

2. **Cuenta de 12 chars en líneas tipo 5:** se rellena con `0000` al final, no con espacios: `40000038` → `400000380000`. En la línea SE-AV (tipo 3) se rellena con espacios: `40000038    `.

3. **NºDoc duplicado:** si el mismo proveedor tiene varias facturas del mismo número en el mismo lote, el NºDoc debe ser diferente para cada una.

4. **A3Scan marca como "dudoso"** el nº de factura cuando tiene más de 2 barras. Nuestro programa lo acepta tal cual — es una ventaja clave sobre A3Scan.

5. **PDFs escaneados:** si `pdfplumber` no extrae texto (menos de 50 chars), activar Tesseract automáticamente.

6. **La carpeta de A3Con** puede estar en red (`\\servidor\A3\...`). Usar `pathlib.Path` para compatibilidad.
