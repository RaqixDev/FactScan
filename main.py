"""
FactScan — Procesador de Facturas A3Con
Workflow: importar PDFs → procesar todo (auto si NIF conocido) → guardar DAT
"""
import sys
from pathlib import Path
from typing import Optional

import fitz

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRect, QPoint, QTimer
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPen, QBrush, QColor, QWheelEvent
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox, QDialog,
    QDialogButtonBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QFormLayout, QSpinBox, QDoubleSpinBox,
    QAbstractItemView, QSplitter, QGroupBox, QScrollArea, QFrame,
    QSizePolicy, QProgressBar,
)

import proveedores
from extractor import (
    extraer_texto_pdf, identificar_proveedor_en_pdf,
    extraer_por_plantilla, parsear_campos_plantilla,
    extraer_fallback_numerico, sugerir_palabras_clave, texto_completo,
)
from ia_parser import parsear_factura
from vencimiento import calcular_vencimiento
from generador_dat import siguiente_ndoc, generar_suenlace, guardar_dat, copiar_pdf
from config import RUTA_A3CON


CAMPOS_PLANTILLA = [
    ('num_factura', 'Nº Factura'),
    ('fecha',       'Fecha'),
    ('base_1',      'Base 1'),
    ('tipo_iva_1',  '% IVA 1'),
    ('cuota_iva_1', 'Cuota 1'),
    ('base_2',      'Base 2  (opcional)'),
    ('tipo_iva_2',  '% IVA 2  (opcional)'),
    ('cuota_iva_2', 'Cuota 2  (opcional)'),
    ('base_3',      'Base 3  (opcional)'),
    ('tipo_iva_3',  '% IVA 3  (opcional)'),
    ('cuota_iva_3', 'Cuota 3  (opcional)'),
    ('total',       'Total'),
]
COLORES_CAMPO = {
    'num_factura': '#E91E63', 'fecha': '#9C27B0',
    'base_1': '#0D47A1', 'tipo_iva_1': '#1565C0', 'cuota_iva_1': '#42A5F5',
    'base_2': '#1B5E20', 'tipo_iva_2': '#2E7D32', 'cuota_iva_2': '#66BB6A',
    'base_3': '#BF360C', 'tipo_iva_3': '#E64A19', 'cuota_iva_3': '#FF8A65',
    'total':  '#C62828',
}

# Columnas de la tabla principal
COL_ESTADO, COL_FICHERO, COL_PROV, COL_NUMFAC, COL_TOTAL, COL_VEN = 0,1,2,3,4,5
_USERDATA = Qt.ItemDataRole.UserRole   # guarda la ruta del PDF


# ── _PaginaPDF ────────────────────────────────────────────────────────────────

class _PaginaPDF(QWidget):
    hovering  = pyqtSignal(float, float)
    capturado = pyqtSignal(float, float, float, float)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self._pixmap:    Optional[QPixmap] = None
        self._zoom:      float = 1.0
        self._capturando: bool = False
        self._inicio:    Optional[QPoint] = None
        self._actual:    Optional[QPoint] = None
        self._overlays:  list[dict] = []

    def actualizar(self, pixmap, zoom):
        self._pixmap = pixmap; self._zoom = zoom
        self.setFixedSize(pixmap.size())
        self._inicio = self._actual = None; self.update()

    def set_captura(self, activo):
        self._capturando = activo; self._inicio = self._actual = None
        self.setCursor(Qt.CursorShape.CrossCursor if activo else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_overlays(self, overlays):
        self._overlays = overlays; self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if self._pixmap: p.drawPixmap(0, 0, self._pixmap)
        z = self._zoom
        for ov in self._overlays:
            r = QRect(int(ov['x0']*z), int(ov['y0']*z),
                      int((ov['x1']-ov['x0'])*z), int((ov['y1']-ov['y0'])*z))
            c = QColor(ov.get('color','#2196F3'))
            p.setPen(QPen(c, 2))
            c2 = QColor(c); c2.setAlpha(50); p.setBrush(QBrush(c2))
            p.drawRect(r)
            p.setPen(QPen(c, 1)); p.drawText(r.left()+2, r.top()-3, ov.get('label',''))
        if self._capturando and self._inicio and self._actual:
            rect = QRect(self._inicio, self._actual).normalized()
            p.setPen(QPen(QColor(220,30,30), 2))
            p.setBrush(QBrush(QColor(220,30,30,50))); p.drawRect(rect)
        p.end()

    def mousePressEvent(self, e):
        if self._capturando and e.button() == Qt.MouseButton.LeftButton:
            self._inicio = e.pos(); self._actual = e.pos()

    def mouseMoveEvent(self, e):
        self.hovering.emit(e.pos().x()/self._zoom, e.pos().y()/self._zoom)
        if self._capturando and self._inicio: self._actual = e.pos(); self.update()

    def mouseReleaseEvent(self, e):
        if self._capturando and self._inicio and e.button() == Qt.MouseButton.LeftButton:
            rect = QRect(self._inicio, e.pos()).normalized()
            if rect.width() > 4 and rect.height() > 4:
                z = self._zoom
                self.capturado.emit(rect.left()/z, rect.top()/z,
                                    rect.right()/z, rect.bottom()/z)
            self._inicio = self._actual = None; self.update()


# ── VisorPDF ──────────────────────────────────────────────────────────────────

class VisorPDF(QWidget):
    capturado = pyqtSignal(float, float, float, float)

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._doc: Optional[fitz.Document] = None
        self._pagina = 0; self._zoom = 1.0

        barra = QHBoxLayout(); barra.setContentsMargins(4,2,4,2)
        self._btn_m  = QPushButton('−'); self._btn_m.setFixedSize(28,26)
        self._lbl_z  = QLabel('―', alignment=Qt.AlignmentFlag.AlignCenter)
        self._lbl_z.setFixedWidth(46)
        self._btn_p  = QPushButton('+'); self._btn_p.setFixedSize(28,26)
        self._btn_f  = QPushButton('Encajar'); self._btn_f.setFixedHeight(26)
        self._lbl_xy = QLabel('', alignment=Qt.AlignmentFlag.AlignRight)
        self._lbl_xy.setStyleSheet('color:#888;font-size:10px;')
        self._btn_m.clicked.connect(lambda: self._zoom_rel(0.8))
        self._btn_p.clicked.connect(lambda: self._zoom_rel(1.25))
        self._btn_f.clicked.connect(self.fit_page)
        for w in (self._btn_m, self._lbl_z, self._btn_p, self._btn_f):
            barra.addWidget(w)
        barra.addStretch(); barra.addWidget(self._lbl_xy)

        self._pw = _PaginaPDF()
        self._pw.hovering.connect(lambda x,y: self._lbl_xy.setText(f'x={x:.0f} y={y:.0f}'))
        self._pw.capturado.connect(self.capturado)

        self._sc = QScrollArea()
        self._sc.setWidget(self._pw); self._sc.setWidgetResizable(False)
        self._sc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._sc.setStyleSheet('background:#505050;')

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        lay.addLayout(barra); lay.addWidget(sep); lay.addWidget(self._sc, 1)

    def cargar(self, ruta):
        self._doc = fitz.open(ruta); self._pagina = 0
        self._renderizar(); QTimer.singleShot(150, self.fit_page)

    def fit_page(self):
        if not self._doc: return
        pag = self._doc[self._pagina]
        vp  = self._sc.viewport()
        w = max(vp.width()-6, 100); h = max(vp.height()-6, 100)
        self._set_zoom(min(w/pag.rect.width, h/pag.rect.height))

    def _zoom_rel(self, f): self._set_zoom(self._zoom * f)

    def _set_zoom(self, z):
        self._zoom = max(0.15, min(5.0, z)); self._renderizar()

    def _renderizar(self):
        if not self._doc: return
        pag = self._doc[self._pagina]
        mat = fitz.Matrix(self._zoom, self._zoom)
        pix = pag.get_pixmap(matrix=mat, alpha=False)
        img = QImage(pix.samples, pix.width, pix.height,
                     pix.stride, QImage.Format.Format_RGB888)
        self._pw.actualizar(QPixmap.fromImage(img), self._zoom)
        self._lbl_z.setText(f'{int(self._zoom*100)}%')

    def set_modo_captura(self, a): self._pw.set_captura(a)
    def set_overlays(self, ov):    self._pw.set_overlays(ov)

    def wheelEvent(self, e: QWheelEvent):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_rel(1.15 if e.angleDelta().y() > 0 else 1/1.15)
        else:
            super().wheelEvent(e)


# ── DialogPlantilla ───────────────────────────────────────────────────────────

class DialogPlantilla(QDialog):
    def __init__(self, ruta_pdf, prov, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Plantilla — {prov["nombre"]}')
        self.setMinimumSize(1100, 700)
        self._ruta_pdf = ruta_pdf; self._prov = prov
        self._capturando: Optional[str] = None; self._filas = {}

        sp = QSplitter(Qt.Orientation.Horizontal)
        self._visor = VisorPDF(); self._visor.cargar(ruta_pdf)
        self._visor.capturado.connect(self._on_area)
        sp.addWidget(self._visor)

        sc = QScrollArea(); sc.setWidgetResizable(True)
        sc.setMinimumWidth(340); sc.setMaximumWidth(400)
        cw = QWidget(); pl = QVBoxLayout(cw); pl.setSpacing(4)
        pl.addWidget(QLabel(f'<b>{prov["nombre"]}</b> <small>{prov["nif"]}</small>'))
        pl.addWidget(QLabel('<small><i>Clic en ⊕ Capturar → dibuja sobre el PDF</i></small>'))

        existentes = {r['campo']: r for r in proveedores.obtener_plantilla(prov['nif'])}
        for campo, etiq in CAMPOS_PLANTILLA:
            color = COLORES_CAMPO.get(campo,'#888')
            grp = QGroupBox(etiq)
            grp.setStyleSheet(f'QGroupBox{{border:1px solid {color};border-left:4px solid {color};'
                              f'margin-top:6px;padding:4px;font-size:10px;}}')
            gl = QVBoxLayout(grp); gl.setSpacing(2); gl.setContentsMargins(4,4,4,4)
            fc = QHBoxLayout(); spins = {}
            for nc in ('x0','y0','x1','y1'):
                sb = QDoubleSpinBox(); sb.setRange(0,3000); sb.setDecimals(0)
                sb.setFixedWidth(62); sb.setPrefix(f'{nc}=')
                if campo in existentes: sb.setValue(existentes[campo][nc])
                spins[nc] = sb; fc.addWidget(sb)
            gl.addLayout(fc)
            fb = QHBoxLayout()
            btn = QPushButton('⊕ Capturar'); btn.setFixedHeight(22)
            btn.setStyleSheet(f'background:{color};color:white;font-size:10px;')
            btn.clicked.connect(lambda _,c=campo: self._iniciar(c))
            lp = QLabel('―'); lp.setStyleSheet('color:#666;font-size:10px;')
            fb.addWidget(btn); fb.addWidget(lp, 1); gl.addLayout(fb)
            self._filas[campo] = {'spins': spins, 'preview': lp}
            pl.addWidget(grp)
        pl.addStretch()
        bts = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                               QDialogButtonBox.StandardButton.Cancel)
        bts.accepted.connect(self._guardar); bts.rejected.connect(self.reject)
        pl.addWidget(bts); sc.setWidget(cw); sp.addWidget(sc); sp.setSizes([720,380])
        QVBoxLayout(self).addWidget(sp)
        self._actualizar_overlays()
        if existentes: self._extraer_previews()

    def _iniciar(self, campo):
        self._capturando = campo; self._visor.set_modo_captura(True)

    def _on_area(self, x0,y0,x1,y1):
        c = self._capturando
        if not c: return
        f = self._filas[c]
        for k,v in (('x0',x0),('y0',y0),('x1',x1),('y1',y1)):
            f['spins'][k].setValue(v)
        self._visor.set_modo_captura(False); self._capturando = None
        raw = extraer_por_plantilla(self._ruta_pdf,
              [{'campo':c,'pagina':0,'x0':x0,'y0':y0,'x1':x1,'y1':y1}])
        f['preview'].setText(raw.get(c,'') or '(vacío)')
        f['preview'].setStyleSheet('color:#2E7D32;font-size:10px;')
        self._actualizar_overlays()

    def _extraer_previews(self):
        raw = extraer_por_plantilla(self._ruta_pdf, self._construir())
        for c,t in raw.items():
            if c in self._filas:
                self._filas[c]['preview'].setText(t or '(vacío)')
                self._filas[c]['preview'].setStyleSheet('color:#2E7D32;font-size:10px;')

    def _actualizar_overlays(self):
        ov = []
        for c,f in self._filas.items():
            s = f['spins']
            x0,y0,x1,y1 = s['x0'].value(),s['y0'].value(),s['x1'].value(),s['y1'].value()
            if x1>x0 and y1>y0:
                ov.append({'x0':x0,'y0':y0,'x1':x1,'y1':y1,
                           'color':COLORES_CAMPO.get(c,'#888'),
                           'label':dict(CAMPOS_PLANTILLA).get(c,c)})
        self._visor.set_overlays(ov)

    def _construir(self):
        out = []
        for c,f in self._filas.items():
            s=f['spins']; x0,y0,x1,y1=s['x0'].value(),s['y0'].value(),s['x1'].value(),s['y1'].value()
            if x1>x0 and y1>y0:
                out.append({'campo':c,'pagina':0,'x0':x0,'y0':y0,'x1':x1,'y1':y1})
        return out

    def _guardar(self):
        p = self._construir()
        if not p: QMessageBox.warning(self,'Sin datos','Define al menos un campo.'); return
        proveedores.guardar_plantilla(self._prov['nif'], p); self.accept()


# ── DialogNuevoProveedor (nuevo y edición) ────────────────────────────────────

class DialogNuevoProveedor(QDialog):
    def __init__(self, datos: dict = None, parent=None):
        super().__init__(parent)
        self._editando = datos is not None
        self.setWindowTitle('Editar proveedor' if self._editando else 'Nuevo proveedor')
        self.setMinimumWidth(400)
        self.nif_creado: Optional[str] = None

        form = QFormLayout(self)
        self._nif      = QLineEdit(); self._nif.setMaxLength(9)
        self._nombre   = QLineEdit()
        self._cuenta   = QLineEdit(); self._cuenta.setMaxLength(8); self._cuenta.setPlaceholderText('4000xxxx')
        self._contra   = QLineEdit(); self._contra.setMaxLength(8); self._contra.setPlaceholderText('6000xxxx')
        self._banco    = QLineEdit(); self._banco.setMaxLength(8);  self._banco.setPlaceholderText('5720xxxx')
        self._dias     = QSpinBox();  self._dias.setRange(0,365); self._dias.setValue(30)
        self._dia_fijo = QSpinBox();  self._dia_fijo.setRange(0,31)
        self._iva      = QDoubleSpinBox(); self._iva.setRange(0,21); self._iva.setValue(10.0)
        self._email    = QLineEdit()

        if datos:
            self._nif.setText(datos.get('nif',''))
            self._nif.setReadOnly(True)
            self._nif.setStyleSheet('background:#333;')
            self._nombre.setText(datos.get('nombre',''))
            self._cuenta.setText(datos.get('cuenta',''))
            self._contra.setText(datos.get('contrapartida',''))
            self._banco.setText(datos.get('cuenta_pago','') or '')
            self._dias.setValue(datos.get('dias_pago',30))
            self._dia_fijo.setValue(datos.get('dia_fijo',0))
            self._iva.setValue(datos.get('iva_habitual',10.0))
            self._email.setText(datos.get('email','') or '')

        self._claves = QLineEdit()
        self._claves.setPlaceholderText('ej: TOMO 7831, B-23422  (separadas por coma)')

        if datos:
            self._claves.setText(datos.get('palabras_clave') or '')

        form.addRow('NIF (9 chars):', self._nif)
        form.addRow('Nombre:', self._nombre)
        form.addRow('Cuenta proveedor:', self._cuenta)
        form.addRow('Contrapartida gasto:', self._contra)
        form.addRow('Cuenta banco:', self._banco)
        form.addRow('Días de pago:', self._dias)
        form.addRow('Día fijo mes (0=sin ajuste):', self._dia_fijo)
        form.addRow('IVA habitual (%):', self._iva)
        form.addRow('Email:', self._email)
        form.addRow('Palabras clave identificación:', self._claves)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._guardar); btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _guardar(self):
        nif    = self._nif.text().strip().upper()
        nombre = self._nombre.text().strip().upper()
        if len(nif) != 9 or not nombre:
            QMessageBox.warning(self,'Datos incompletos','NIF (9 chars) y Nombre son obligatorios.')
            return
        datos = {
            'nif': nif, 'nombre': nombre,
            'cuenta': self._cuenta.text().strip(),
            'contrapartida': self._contra.text().strip(),
            'cuenta_pago': self._banco.text().strip(),
            'dias_pago': self._dias.value(),
            'dia_fijo': self._dia_fijo.value(),
            'iva_habitual': self._iva.value(),
            'email': self._email.text().strip(),
            'palabras_clave': self._claves.text().strip(),
        }
        try:
            if self._editando:
                proveedores.actualizar_proveedor(nif, datos)
            else:
                proveedores.insertar_proveedor(datos)
            self.nif_creado = nif
            self.accept()
        except Exception as e:
            QMessageBox.critical(self,'Error',str(e))


# ── DialogGestionProveedores ──────────────────────────────────────────────────

class DialogGestionProveedores(QDialog):
    def __init__(self, ruta_pdf: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Gestión de proveedores')
        self.setMinimumSize(820, 500)
        self._ruta_pdf = ruta_pdf   # para abrir plantilla con contexto

        layout = QVBoxLayout(self)

        # Barra de botones
        barra = QHBoxLayout()
        btn_nuevo    = QPushButton('+ Nuevo')
        btn_editar   = QPushButton('✏ Editar')
        btn_eliminar = QPushButton('🗑 Eliminar')
        btn_plantilla = QPushButton('📋 Plantilla')
        btn_cerrar   = QPushButton('Cerrar')
        btn_nuevo.clicked.connect(self._nuevo)
        btn_editar.clicked.connect(self._editar)
        btn_eliminar.clicked.connect(self._eliminar)
        btn_plantilla.clicked.connect(self._plantilla)
        btn_cerrar.clicked.connect(self.accept)
        for b in (btn_nuevo, btn_editar, btn_eliminar, btn_plantilla):
            barra.addWidget(b)
        barra.addStretch(); barra.addWidget(btn_cerrar)
        layout.addLayout(barra)

        # Tabla
        self._tabla = QTableWidget()
        self._tabla.setColumnCount(7)
        self._tabla.setHorizontalHeaderLabels(
            ['NIF','Nombre','Cuenta prov.','Contrapartida','Banco','Días','Plantilla']
        )
        self._tabla.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla.doubleClicked.connect(self._editar)
        layout.addWidget(self._tabla)
        self._recargar()

    def _recargar(self):
        self._tabla.setRowCount(0)
        for p in proveedores.listar_proveedores():
            tiene = '✓' if proveedores.tiene_plantilla(p['nif']) else '―'
            f = self._tabla.rowCount(); self._tabla.insertRow(f)
            for col, val in enumerate([p['nif'], p['nombre'], p['cuenta'],
                                        p.get('contrapartida',''), p.get('cuenta_pago','') or '',
                                        str(p['dias_pago']), tiene]):
                item = QTableWidgetItem(val)
                item.setData(_USERDATA, p['nif'])
                self._tabla.setItem(f, col, item)

    def _nif_seleccionado(self) -> Optional[str]:
        fila = self._tabla.currentRow()
        if fila < 0: return None
        return self._tabla.item(fila, 0).text()

    def _nuevo(self):
        dlg = DialogNuevoProveedor(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._recargar()

    def _editar(self):
        nif = self._nif_seleccionado()
        if not nif: return
        datos = proveedores.buscar_por_nif(nif)
        if not datos: return
        dlg = DialogNuevoProveedor(datos=datos, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._recargar()

    def _eliminar(self):
        nif = self._nif_seleccionado()
        if not nif: return
        prov = proveedores.buscar_por_nif(nif)
        resp = QMessageBox.question(
            self, 'Eliminar proveedor',
            f'¿Eliminar <b>{prov["nombre"]}</b>?<br>'
            '<small>(Borrado lógico, no se perderán los datos históricos)</small>',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if resp == QMessageBox.StandardButton.Yes:
            proveedores.eliminar_proveedor(nif)
            self._recargar()

    def _plantilla(self):
        nif = self._nif_seleccionado()
        if not nif: return
        prov = proveedores.buscar_por_nif(nif)
        if not self._ruta_pdf:
            ruta, _ = QFileDialog.getOpenFileName(
                self, f'PDF de ejemplo para {prov["nombre"]}', '', 'PDF (*.pdf *.PDF)'
            )
            if not ruta: return
        else:
            ruta = self._ruta_pdf
        dlg = DialogPlantilla(ruta, prov, self)
        dlg.exec()
        self._recargar()


# ── DialogSugerirClaves ───────────────────────────────────────────────────────

class DialogSugerirClaves(QDialog):
    """
    Aparece tras identificar manualmente un proveedor.
    Sugiere textos del PDF que podrían usarse como palabras clave para
    reconocerlo automáticamente en el futuro.
    """

    def __init__(self, ruta_pdf: str, prov: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Guardar claves — {prov["nombre"]}')
        self.setMinimumWidth(540)
        self._nif   = prov['nif']
        self._checks: list  = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f'<b>{prov["nombre"]}</b> ha sido identificado manualmente.<br>'
            'Marca los textos que aparecen en TODAS sus facturas para que<br>'
            'FactScan lo reconozca automáticamente la próxima vez:'
        ))

        # Cargar sugerencias
        sugerencias = sugerir_palabras_clave(ruta_pdf)

        # Añadir claves ya guardadas (preseleccionadas)
        ya_guardadas = set(
            c.strip().upper()
            for c in (prov.get('palabras_clave') or '').split(',')
            if c.strip()
        )

        if not sugerencias:
            layout.addWidget(QLabel('<i>No se encontraron fragmentos candidatos en este PDF.</i>'))
        else:
            layout.addWidget(QLabel(f'<small>Se encontraron {len(sugerencias)} candidatos:</small>'))
            from PyQt6.QtWidgets import QCheckBox
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            inner = QWidget(); inner_l = QVBoxLayout(inner)
            for sug in sugerencias:
                cb = QCheckBox(sug)
                cb.setChecked(sug in ya_guardadas or sug.upper() in ya_guardadas)
                inner_l.addWidget(cb)
                self._checks.append(cb)
            inner_l.addStretch()
            scroll.setWidget(inner)
            scroll.setMaximumHeight(260)
            layout.addWidget(scroll)

        # Campo de texto libre para añadir claves manualmente
        layout.addWidget(QLabel('<small>También puedes escribir claves manualmente (separadas por coma):</small>'))
        self._manual = QLineEdit()
        self._manual.setPlaceholderText('ej: TOMO 7831, B-23422, 937310418')
        # Pre-rellenar con las ya guardadas que no estén entre las sugerencias
        extras = [c for c in ya_guardadas if c not in [s.upper() for s in sugerencias]]
        if extras:
            self._manual.setText(', '.join(extras))
        layout.addWidget(self._manual)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                QDialogButtonBox.StandardButton.Discard)
        btns.button(QDialogButtonBox.StandardButton.Discard).setText('Omitir')
        btns.accepted.connect(self._guardar)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _guardar(self):
        seleccionadas = [cb.text() for cb in self._checks if cb.isChecked()]
        manuales = [c.strip() for c in self._manual.text().split(',') if c.strip()]
        todas = list(dict.fromkeys(seleccionadas + manuales))  # sin duplicados
        proveedores.guardar_palabras_clave(self._nif, todas)
        self.accept()


# ── DialogSeleccionProveedor ──────────────────────────────────────────────────

class DialogSeleccionProveedor(QDialog):
    def __init__(self, texto_pdf, fichero='', parent=None):
        super().__init__(parent)
        self.setWindowTitle('Identificar proveedor')
        self.setMinimumSize(660, 420)
        self.proveedor_elegido: Optional[dict] = None
        layout = QVBoxLayout(self)
        if fichero:
            layout.addWidget(QLabel(f'<b>{Path(fichero).name}</b>'))
        layout.addWidget(QLabel(
            'No se encontró el NIF en la BD. Selecciona el proveedor:'))
        fila = QHBoxLayout()
        self._buscar = QLineEdit(); self._buscar.setPlaceholderText('Filtrar...')
        self._buscar.textChanged.connect(self._filtrar)
        fila.addWidget(QLabel('Buscar:')); fila.addWidget(self._buscar)
        layout.addLayout(fila)
        self._tabla = QTableWidget()
        self._tabla.setColumnCount(4)
        self._tabla.setHorizontalHeaderLabels(['NIF','Nombre','Cuenta','Días'])
        self._tabla.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla.doubleClicked.connect(self._aceptar)
        layout.addWidget(self._tabla)
        btns = QHBoxLayout()
        btn_nuevo  = QPushButton('+ Nuevo'); btn_nuevo.clicked.connect(self._nuevo)
        btn_ok     = QPushButton('Seleccionar'); btn_ok.clicked.connect(self._aceptar)
        btn_cancel = QPushButton('Cancelar');  btn_cancel.clicked.connect(self.reject)
        btns.addWidget(btn_nuevo); btns.addStretch()
        btns.addWidget(btn_ok); btns.addWidget(btn_cancel)
        layout.addLayout(btns)
        candidatos = proveedores.buscar_candidatos(texto_pdf)
        nifs_ya = {p['nif'] for p in candidatos}
        self._todos = candidatos + [p for p in proveedores.listar_proveedores()
                                    if p['nif'] not in nifs_ya]
        self._rellenar(self._todos)

    def _rellenar(self, lista):
        self._tabla.setRowCount(len(lista))
        for i,p in enumerate(lista):
            for col,val in enumerate([p['nif'],p['nombre'],p['cuenta'],str(p['dias_pago'])]):
                self._tabla.setItem(i, col, QTableWidgetItem(val))
        if lista: self._tabla.selectRow(0)

    def _filtrar(self, txt):
        t = txt.upper()
        self._rellenar([p for p in self._todos
                        if t in p['nombre'].upper() or t in p['nif']])

    def _aceptar(self):
        fila = self._tabla.currentRow()
        if fila < 0: return
        self.proveedor_elegido = proveedores.buscar_por_nif(self._tabla.item(fila,0).text())
        self.accept()

    def _nuevo(self):
        dlg = DialogNuevoProveedor(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.proveedor_elegido = proveedores.buscar_por_nif(dlg.nif_creado)
            self.accept()


# ── PanelDetalle ──────────────────────────────────────────────────────────────

class PanelDetalle(QGroupBox):
    def __init__(self):
        super().__init__('Detalle de factura')
        form = QFormLayout(self); form.setSpacing(3)
        self._campos: dict[str,QLineEdit] = {}
        for clave, etiq in [('num_factura','Nº Factura'),('fecha','Fecha'),
                             ('proveedor','Proveedor'),('nif','N.I.F.'),('cuenta','Cuenta')]:
            le = QLineEdit(); form.addRow(f'{etiq}:', le); self._campos[clave] = le
        for i in ('1','2','3'):
            color = COLORES_CAMPO[f'base_{i}']
            fila = QHBoxLayout()
            for c,w in (('base',68),('tipo_iva',50),('cuota_iva',68)):
                le = QLineEdit(); le.setFixedWidth(w); self._campos[f'{c}_{i}'] = le
                fila.addWidget(le)
            form.addRow(QLabel(f'<span style="color:{color}">■</span> IVA {i}:'), fila)
        for clave, etiq in [('total','Total'),('vencimiento','Vencimiento')]:
            le = QLineEdit(); form.addRow(f'{etiq}:', le); self._campos[clave] = le

    def rellenar(self, factura, prov):
        from datetime import date as date_t
        fecha = factura.get('fecha')
        ven   = (calcular_vencimiento(fecha, prov['dias_pago'], prov['dia_fijo'])
                 if isinstance(fecha, date_t) else '')
        self._campos['num_factura'].setText(str(factura.get('num_factura') or ''))
        self._campos['fecha'].setText(str(fecha) if fecha else '')
        self._campos['proveedor'].setText(prov.get('nombre',''))
        self._campos['nif'].setText(prov.get('nif',''))
        self._campos['cuenta'].setText(prov.get('cuenta',''))
        self._campos['total'].setText(f"{factura.get('total',0):.2f} €")
        self._campos['vencimiento'].setText(str(ven))
        iva_ops = factura.get('iva_ops',[])
        if not iva_ops and factura.get('base',0):
            iva_ops = [{'base':factura['base'],'tipo_iva':factura['tipo_iva'],
                        'cuota_iva':factura['cuota_iva']}]
        for idx in range(3):
            i = str(idx+1)
            if idx < len(iva_ops):
                op = iva_ops[idx]
                self._campos[f'base_{i}'].setText(f"{op.get('base',0):.2f}")
                self._campos[f'tipo_iva_{i}'].setText(f"{op.get('tipo_iva',0):.1f}%")
                self._campos[f'cuota_iva_{i}'].setText(f"{op.get('cuota_iva',0):.2f}")
            else:
                for c in ('base','tipo_iva','cuota_iva'):
                    self._campos[f'{c}_{i}'].clear()

    def limpiar(self):
        for le in self._campos.values(): le.clear()

    def valores_editados(self) -> dict:
        import re
        from datetime import datetime
        def to_f(txt):
            t = re.sub(r'[^\d,.]','',txt).replace(',','.')
            try: return float(t)
            except: return 0.0
        fecha = None
        for fmt in ('%Y-%m-%d','%d/%m/%Y','%d/%m/%y'):
            try: fecha = datetime.strptime(self._campos['fecha'].text().strip(),fmt).date(); break
            except: pass
        iva_ops = []
        for i in ('1','2','3'):
            base = to_f(self._campos[f'base_{i}'].text())
            if base > 0:
                iva_ops.append({'base':base,
                                'tipo_iva': to_f(self._campos[f'tipo_iva_{i}'].text()),
                                'cuota_iva':to_f(self._campos[f'cuota_iva_{i}'].text()),
                                'recargo':0.0,'retencion':0.0})
        return {
            'num_factura': self._campos['num_factura'].text().strip(),
            'fecha': fecha, 'iva_ops': iva_ops,
            'base':     iva_ops[0]['base']      if iva_ops else 0.0,
            'tipo_iva': iva_ops[0]['tipo_iva']  if iva_ops else 0.0,
            'cuota_iva':iva_ops[0]['cuota_iva'] if iva_ops else 0.0,
            'total': to_f(self._campos['total'].text()),
            'recargo':0.0,'retencion':0.0,
        }


# ── Helpers de procesado ─────────────────────────────────────────────────────

def _aplicar_fallback_si_necesario(ruta: str, factura: dict) -> dict:
    """
    Si la plantilla no extrajo importes válidos (total=0 o base=0),
    activa el motor de deducción numérica sobre el texto completo del PDF.
    Combina los resultados: mantiene los campos de la plantilla que sí
    funcionaron y completa con el fallback los que están a cero.
    """
    total  = factura.get('total', 0.0)
    base   = factura.get('base', 0.0)
    iva_ok = bool(factura.get('iva_ops'))

    if total > 0 and (base > 0 or iva_ok):
        return factura          # plantilla suficiente, no hacer nada

    fb = extraer_fallback_numerico(ruta)

    # Importes
    if fb.get('total', 0) > 0:
        factura['total']   = fb['total']
        factura['iva_ops'] = fb['iva_ops']
        factura['base']    = fb.get('base', 0.0)
        factura['tipo_iva']  = fb.get('tipo_iva', 0.0)
        factura['cuota_iva'] = fb.get('cuota_iva', 0.0)

    # Número de factura
    if not factura.get('num_factura') and fb.get('num_factura'):
        factura['num_factura'] = fb['num_factura']

    # Fecha
    if not factura.get('fecha') and fb.get('fecha'):
        factura['fecha'] = fb['fecha']

    return factura


# ── Ventana principal ─────────────────────────────────────────────────────────

class VentanaPrincipal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('FactScan — Procesador de Facturas A3Con')
        self.resize(1400, 820)
        # Estado interno
        self._factura:  Optional[dict] = None
        self._prov:     Optional[dict] = None
        self._ruta_pdf: Optional[str]  = None
        # Batch: lista de resultados procesados {ruta_pdf, factura, prov, ndoc, fecha_ven}
        self._batch: list[dict] = []
        self._construir_ui()
        proveedores.inicializar_db()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _construir_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6,4,6,4); root.setSpacing(4)

        # Barra de herramientas
        barra = QHBoxLayout()

        self._btn_importar   = QPushButton('📂 Importar PDFs')
        self._btn_proc_todo  = QPushButton('▶ Procesar todo')
        self._btn_guardar    = QPushButton('💾 Guardar DAT')
        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.VLine)
        self._btn_plantilla  = QPushButton('📋 Plantilla')
        self._btn_ia         = QPushButton('🤖 IA (1 factura)')
        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        self._btn_proveedores = QPushButton('👥 Proveedores')

        self._lbl_estado = QLabel('Sin ficheros')
        self._lbl_estado.setStyleSheet('color:#888;margin-left:8px;')
        self._progress = QProgressBar(); self._progress.setFixedWidth(120)
        self._progress.setVisible(False)

        self._btn_proc_todo.setEnabled(False)
        self._btn_guardar.setEnabled(False)
        self._btn_plantilla.setEnabled(False)
        self._btn_ia.setEnabled(False)

        self._btn_importar.clicked.connect(self._importar_pdfs)
        self._btn_proc_todo.clicked.connect(self._procesar_todo)
        self._btn_guardar.clicked.connect(self._guardar_dat_batch)
        self._btn_plantilla.clicked.connect(self._editar_plantilla)
        self._btn_ia.clicked.connect(self._procesar_ia_uno)
        self._btn_proveedores.clicked.connect(self._gestionar_proveedores)

        for w in (self._btn_importar, self._btn_proc_todo, self._btn_guardar,
                  sep1, self._btn_plantilla, self._btn_ia,
                  sep2, self._btn_proveedores):
            if isinstance(w, QFrame):
                barra.addWidget(w)
            else:
                w.setMinimumHeight(30); barra.addWidget(w)
        barra.addWidget(self._lbl_estado)
        barra.addStretch()
        barra.addWidget(self._progress)
        root.addLayout(barra)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # Splitter principal
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(splitter, 1)

        # Panel izquierdo
        izq = QWidget()
        izq.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        izq_l = QVBoxLayout(izq); izq_l.setContentsMargins(0,0,4,0); izq_l.setSpacing(4)

        grp = QGroupBox('Facturas')
        gl  = QVBoxLayout(grp)
        self._tabla = QTableWidget()
        self._tabla.setColumnCount(6)
        self._tabla.setHorizontalHeaderLabels(['','Fichero','Proveedor','Nº Factura','Total','Vencimiento'])
        self._tabla.setColumnWidth(COL_ESTADO, 24)
        self._tabla.horizontalHeader().setSectionResizeMode(COL_FICHERO, QHeaderView.ResizeMode.Stretch)
        self._tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla.currentCellChanged.connect(self._on_fila_cambiada)
        gl.addWidget(self._tabla)
        izq_l.addWidget(grp)

        self._detalle = PanelDetalle()
        self._detalle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        izq_l.addWidget(self._detalle)

        # Botón generar DAT para la factura seleccionada (individual)
        self._btn_gen_uno = QPushButton('💾 Generar DAT (esta factura)')
        self._btn_gen_uno.setEnabled(False)
        self._btn_gen_uno.clicked.connect(self._generar_dat_uno)
        izq_l.addWidget(self._btn_gen_uno)

        splitter.addWidget(izq)

        # Panel derecho: visor PDF
        self._visor = VisorPDF()
        splitter.addWidget(self._visor)
        splitter.setSizes([420, 980])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _estado(self, msg, color='#888'):
        self._lbl_estado.setText(msg)
        self._lbl_estado.setStyleSheet(f'color:{color};margin-left:8px;')
        QApplication.processEvents()

    def _ruta_de_fila(self, fila: int) -> Optional[str]:
        item = self._tabla.item(fila, COL_ESTADO)
        return item.data(_USERDATA) if item else None

    def _set_estado_fila(self, fila, emoji, ruta=None):
        item = QTableWidgetItem(emoji)
        if ruta: item.setData(_USERDATA, ruta)
        self._tabla.setItem(fila, COL_ESTADO, item)
        QApplication.processEvents()

    def _siguiente_ndoc(self) -> int:
        try:
            base = siguiente_ndoc(RUTA_A3CON)
        except Exception:
            base = 1
        if self._batch:
            base = max(base, max(r['ndoc'] for r in self._batch) + 1)
        return base

    # ── Acciones principales ──────────────────────────────────────────────────

    def _importar_pdfs(self):
        rutas, _ = QFileDialog.getOpenFileNames(
            self, 'Seleccionar facturas PDF', '', 'PDF (*.pdf *.PDF)'
        )
        if not rutas: return
        for ruta in rutas:
            f = self._tabla.rowCount()
            self._tabla.insertRow(f)
            item_est = QTableWidgetItem('🟡')
            item_est.setData(_USERDATA, ruta)
            self._tabla.setItem(f, COL_ESTADO,  item_est)
            self._tabla.setItem(f, COL_FICHERO,  QTableWidgetItem(Path(ruta).name))
            for col in (COL_PROV, COL_NUMFAC, COL_TOTAL, COL_VEN):
                self._tabla.setItem(f, col, QTableWidgetItem(''))
        self._btn_proc_todo.setEnabled(True)
        self._estado(f'{self._tabla.rowCount()} facturas en lista', '#1565C0')
        # Mostrar el primer PDF recién importado
        self._tabla.selectRow(self._tabla.rowCount() - len(rutas))

    def _procesar_todo(self):
        pendientes = [i for i in range(self._tabla.rowCount())
                      if (self._tabla.item(i, COL_ESTADO) or QTableWidgetItem()).text() == '🟡']
        if not pendientes:
            QMessageBox.information(self, 'Sin pendientes',
                                    'No hay facturas pendientes de procesar.'); return

        self._progress.setVisible(True)
        self._progress.setMaximum(len(pendientes))
        self._progress.setValue(0)
        self._btn_proc_todo.setEnabled(False)

        for n, fila in enumerate(pendientes):
            self._tabla.selectRow(fila)
            self._procesar_fila(fila)
            self._progress.setValue(n + 1)
            QApplication.processEvents()

        self._progress.setVisible(False)
        self._btn_proc_todo.setEnabled(True)
        ok  = sum(1 for r in range(self._tabla.rowCount())
                  if (self._tabla.item(r, COL_ESTADO) or QTableWidgetItem()).text() == '🟢')
        err = sum(1 for r in range(self._tabla.rowCount())
                  if (self._tabla.item(r, COL_ESTADO) or QTableWidgetItem()).text() == '🔴')
        self._estado(f'✔ {ok} procesadas  ✗ {err} errores', '#2E7D32' if not err else '#E65100')
        if self._batch:
            self._btn_guardar.setEnabled(True)

    def _procesar_fila(self, fila: int):
        ruta = self._ruta_de_fila(fila)
        if not ruta: return
        self._set_estado_fila(fila, '🔵', ruta)

        try:
            nif  = identificar_proveedor_en_pdf(ruta)
            prov = proveedores.buscar_por_nif(nif) if nif else None

            # Si el proveedor no está en la BD → preguntar (solo si no conocido)
            if not prov:
                t_pdf = texto_completo(ruta)
                dlg   = DialogSeleccionProveedor(t_pdf, ruta, self)
                if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.proveedor_elegido:
                    self._set_estado_fila(fila, '⏭️', ruta)
                    return
                prov = dlg.proveedor_elegido
                # Ofrecer guardar palabras clave para reconocerlo automáticamente
                dlg_claves = DialogSugerirClaves(ruta, prov, self)
                dlg_claves.exec()
                # Recargar proveedor con las claves recién guardadas
                prov = proveedores.buscar_por_nif(prov['nif'])

            # Si no hay plantilla → ofrecer definirla
            if not proveedores.tiene_plantilla(prov['nif']):
                resp = QMessageBox.question(
                    self, f'{prov["nombre"]} — sin plantilla',
                    f'<b>{prov["nombre"]}</b> no tiene plantilla de coordenadas.<br>'
                    '¿Definirla ahora?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp == QMessageBox.StandardButton.Yes:
                    dlg = DialogPlantilla(ruta, prov, self)
                    if dlg.exec() != QDialog.DialogCode.Accepted:
                        self._set_estado_fila(fila, '⏭️', ruta); return
                else:
                    self._set_estado_fila(fila, '⏭️', ruta); return

            # Extraer datos por plantilla
            plantilla = proveedores.obtener_plantilla(prov['nif'])
            raw       = extraer_por_plantilla(ruta, plantilla)
            factura   = parsear_campos_plantilla(raw)

            # Fallback numérico si la plantilla no extrajo importes válidos
            factura = _aplicar_fallback_si_necesario(ruta, factura)

            ndoc      = self._siguiente_ndoc()
            fecha     = factura.get('fecha')
            from datetime import date as date_t
            fecha_ven = (calcular_vencimiento(fecha, prov['dias_pago'], prov['dia_fijo'])
                         if isinstance(fecha, date_t) else None)

            self._batch.append({
                'ruta_pdf': ruta, 'factura': factura,
                'prov': prov, 'ndoc': ndoc, 'fecha_ven': fecha_ven,
            })

            self._tabla.setItem(fila, COL_PROV,   QTableWidgetItem(prov['nombre']))
            self._tabla.setItem(fila, COL_NUMFAC,  QTableWidgetItem(str(factura.get('num_factura',''))))
            self._tabla.setItem(fila, COL_TOTAL,   QTableWidgetItem(f"{factura.get('total',0):.2f} €"))
            self._tabla.setItem(fila, COL_VEN,     QTableWidgetItem(str(fecha_ven or '')))
            self._set_estado_fila(fila, '🟢', ruta)

            # Actualizar detalle si es la fila seleccionada
            if self._tabla.currentRow() == fila:
                self._factura = factura; self._prov = prov; self._ruta_pdf = ruta
                self._detalle.rellenar(factura, prov)
                self._btn_gen_uno.setEnabled(True)

        except Exception as e:
            self._tabla.setItem(fila, COL_PROV, QTableWidgetItem(f'Error: {e}'))
            self._set_estado_fila(fila, '🔴', ruta)

    def _guardar_dat_batch(self):
        if not self._batch:
            QMessageBox.information(self,'Sin datos','Procesa las facturas primero.'); return
        carpeta = QFileDialog.getExistingDirectory(self, 'Carpeta de destino para SUENLACE.DAT')
        if not carpeta: return
        contenido = ''
        errores = []
        for r in self._batch:
            try:
                contenido += generar_suenlace(r['factura'], r['prov'],
                                              r['ndoc'], r['fecha_ven'])
            except Exception as e:
                errores.append(f"{r['prov']['nombre']}: {e}")
        ruta_dat = Path(carpeta) / 'SUENLACE.DAT'
        guardar_dat(contenido, str(ruta_dat))
        for r in self._batch:
            try:
                copiar_pdf(r['ruta_pdf'], r['prov']['nif'], r['ndoc'], carpeta)
            except Exception:
                pass
        msg = (f'SUENLACE.DAT con {len(self._batch)} facturas guardado en:\n{carpeta}')
        if errores:
            msg += f'\n\nErrores:\n' + '\n'.join(errores)
        QMessageBox.information(self, 'Listo', msg)

    def _on_fila_cambiada(self, fila, *_):
        ruta = self._ruta_de_fila(fila)
        if not ruta: return
        self._ruta_pdf = ruta
        # Mostrar PDF
        self._visor.cargar(ruta)
        # Buscar datos procesados
        resultado = next((r for r in self._batch if r['ruta_pdf'] == ruta), None)
        if resultado:
            self._factura = resultado['factura']
            self._prov    = resultado['prov']
            self._detalle.rellenar(resultado['factura'], resultado['prov'])
            self._btn_gen_uno.setEnabled(True)
            self._btn_ia.setEnabled(True)
            self._btn_plantilla.setEnabled(True)
            self._mostrar_overlays()
        else:
            self._factura = self._prov = None
            self._detalle.limpiar()
            self._btn_gen_uno.setEnabled(False)
            self._btn_ia.setEnabled(True)
            self._btn_plantilla.setEnabled(False)

    def _editar_plantilla(self):
        if not self._ruta_pdf:
            QMessageBox.information(self,'Sin PDF','Selecciona una factura primero.'); return
        if not self._prov:
            # Intentar identificar primero
            nif  = identificar_proveedor_en_pdf(self._ruta_pdf)
            prov = proveedores.buscar_por_nif(nif) if nif else None
            if not prov:
                texto = extraer_texto_pdf(self._ruta_pdf)
                dlg   = DialogSeleccionProveedor(texto, self._ruta_pdf, self)
                if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.proveedor_elegido:
                    return
                prov = dlg.proveedor_elegido
            self._prov = prov
        dlg = DialogPlantilla(self._ruta_pdf, self._prov, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Re-extraer con plantilla nueva
            fila = self._tabla.currentRow()
            if fila >= 0:
                self._set_estado_fila(fila, '🟡', self._ruta_pdf)
                self._procesar_fila(fila)
            self._btn_plantilla.setEnabled(True)

    def _procesar_ia_uno(self):
        if not self._ruta_pdf: return
        self._estado('Analizando con Claude...', '#E65100')
        try:
            texto   = extraer_texto_pdf(self._ruta_pdf)
            factura = parsear_factura(texto)
        except Exception as e:
            self._estado('Error IA', '#C62828')
            QMessageBox.critical(self,'Error IA', str(e)); return
        nif  = (factura.get('nif') or '').strip()
        prov = proveedores.buscar_por_nif(nif) if nif else None
        if not prov:
            dlg = DialogSeleccionProveedor(texto, self._ruta_pdf, self)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.proveedor_elegido:
                self._estado('Cancelado'); return
            prov = dlg.proveedor_elegido
        self._factura = factura; self._prov = prov
        self._detalle.rellenar(factura, prov)
        self._btn_gen_uno.setEnabled(True)
        self._btn_plantilla.setEnabled(True)
        self._estado(f'✔ IA: {prov["nombre"]}', '#2E7D32')

    def _generar_dat_uno(self):
        factura = self._detalle.valores_editados()
        prov    = self._prov
        if not prov: return
        from datetime import date as date_t
        if not isinstance(factura.get('fecha'), date_t):
            QMessageBox.warning(self,'Fecha inválida','Revisa el campo Fecha.'); return
        fecha_ven = calcular_vencimiento(factura['fecha'], prov['dias_pago'], prov['dia_fijo'])
        ndoc      = self._siguiente_ndoc()
        contenido = generar_suenlace(factura, prov, ndoc, fecha_ven)
        ruta_dat, _ = QFileDialog.getSaveFileName(
            self, 'Guardar SUENLACE.DAT', 'SUENLACE.DAT', 'DAT (*.DAT)')
        if not ruta_dat: return
        guardar_dat(contenido, ruta_dat)
        try: copiar_pdf(self._ruta_pdf, prov['nif'], ndoc, RUTA_A3CON)
        except Exception: pass
        self._estado(f'✔ DAT guardado — NºDoc {ndoc:06d}', '#2E7D32')
        QMessageBox.information(self,'DAT generado',
            f'NºDoc: {ndoc:06d}\nVencimiento: {fecha_ven}\n'
            f'Líneas IVA: {len(factura.get("iva_ops") or [1])}\n'
            f'PDF: R{prov["nif"]}{ndoc:06d}.PDF')

    def _gestionar_proveedores(self):
        dlg = DialogGestionProveedores(ruta_pdf=self._ruta_pdf, parent=self)
        dlg.exec()

    def _mostrar_overlays(self):
        if not self._prov: return
        self._visor.set_overlays([
            {**r, 'color': COLORES_CAMPO.get(r['campo'],'#888'),
             'label': dict(CAMPOS_PLANTILLA).get(r['campo'], r['campo'])}
            for r in proveedores.obtener_plantilla(self._prov['nif'])
        ])


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    ventana = VentanaPrincipal()
    ventana.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
