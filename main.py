"""
FactScan — Procesador de Facturas A3Con
Workflow: importar PDFs → procesar todo (auto si NIF conocido) → guardar DAT
"""
import json
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
    # Campos de posición fija → coordenadas de plantilla (prioridad)
    ('num_factura', 'Nº Factura'),
    ('fecha',       'Fecha'),
    # Campos de importe → deducción numérica automática (última página)
    # Las coordenadas aquí son solo respaldo si la deducción falla
    ('base_1',      'Base 1  ★auto'),
    ('tipo_iva_1',  '% IVA 1  ★auto'),
    ('cuota_iva_1', 'Cuota 1  ★auto'),
    ('base_2',      'Base 2  ★auto'),
    ('tipo_iva_2',  '% IVA 2  ★auto'),
    ('cuota_iva_2', 'Cuota 2  ★auto'),
    ('base_3',      'Base 3  ★auto'),
    ('tipo_iva_3',  '% IVA 3  ★auto'),
    ('cuota_iva_3', 'Cuota 3  ★auto'),
    ('total',       'Total  ★auto'),
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

# ── Configuración persistente (última ruta, etc.) ─────────────────────────────
_CFG_PATH = Path(__file__).parent / '.factscan_config.json'

def _leer_cfg() -> dict:
    try:
        return json.loads(_CFG_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}

def _guardar_cfg(clave: str, valor) -> None:
    cfg = _leer_cfg()
    cfg[clave] = valor
    try:
        _CFG_PATH.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    except Exception:
        pass

# ── Caché LRU de documentos fitz (evita reabrir el mismo PDF) ────────────────

class _DocCache:
    """Mantiene hasta `maxsize` documentos fitz abiertos en memoria."""
    def __init__(self, maxsize: int = 20):
        self._cache: dict[str, fitz.Document] = {}
        self._order: list[str] = []
        self._maxsize = maxsize

    def get(self, ruta: str) -> fitz.Document:
        if ruta in self._cache:
            self._order.remove(ruta)
            self._order.append(ruta)
            return self._cache[ruta]
        doc = fitz.open(ruta)
        self._cache[ruta] = doc
        self._order.append(ruta)
        while len(self._order) > self._maxsize:
            viejo = self._order.pop(0)
            try:
                self._cache.pop(viejo).close()
            except Exception:
                pass
        return doc

_DOC_CACHE = _DocCache()


# ── _PaginaPDF ────────────────────────────────────────────────────────────────

class _PaginaPDF(QWidget):
    hovering      = pyqtSignal(float, float)
    capturado     = pyqtSignal(float, float, float, float)
    overlayMovido = pyqtSignal(str, float, float, float, float)  # campo,x0,y0,x1,y1

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self._pixmap:     Optional[QPixmap] = None
        self._zoom:       float = 1.0
        self._capturando: bool  = False
        self._draggable:  bool  = False       # activar en DialogPlantilla
        self._inicio:     Optional[QPoint] = None
        self._actual:     Optional[QPoint] = None
        self._overlays:   list[dict] = []
        # Estado del arrastre de overlay
        self._drag_ov:    Optional[dict]   = None
        self._drag_off:   Optional[QPoint] = None  # offset click dentro del rect
        # Estado del resize de overlay
        self._resize_ov:    Optional[dict] = None
        self._resize_flags: int            = 0

    def actualizar(self, pixmap, zoom):
        self._pixmap = pixmap; self._zoom = zoom
        self.setFixedSize(pixmap.size())
        self._inicio = self._actual = None; self.update()

    def set_captura(self, activo):
        self._capturando = activo; self._inicio = self._actual = None
        self.setCursor(Qt.CursorShape.CrossCursor if activo else Qt.CursorShape.ArrowCursor)
        self.update()

    # Flags de borde para resize (combinables con |)
    _L, _R, _T, _B = 1, 2, 4, 8
    _THRESH = 8   # píxeles pantalla de tolerancia para detectar borde

    # Cursores por combinación de flags
    _CURSORES = {
        1:  Qt.CursorShape.SizeHorCursor,    # izquierda
        2:  Qt.CursorShape.SizeHorCursor,    # derecha
        4:  Qt.CursorShape.SizeVerCursor,    # arriba
        8:  Qt.CursorShape.SizeVerCursor,    # abajo
        5:  Qt.CursorShape.SizeFDiagCursor,  # TL
        10: Qt.CursorShape.SizeFDiagCursor,  # BR
        6:  Qt.CursorShape.SizeBDiagCursor,  # TR
        9:  Qt.CursorShape.SizeBDiagCursor,  # BL
    }

    def set_draggable(self, activo: bool):
        self._draggable = activo

    def set_overlays(self, overlays):
        self._overlays = overlays; self.update()

    def _detectar(self, pos: QPoint):
        """
        Devuelve (overlay, flags) para la posición dada.
        flags != 0  → resize (indica qué bordes)
        flags == 0  → drag interior
        (None, 0)   → sin overlay
        """
        z = self._zoom
        T = self._THRESH
        for ov in reversed(self._overlays):
            x0 = int(ov['x0'] * z); y0 = int(ov['y0'] * z)
            x1 = int(ov['x1'] * z); y1 = int(ov['y1'] * z)
            expanded = QRect(x0 - T, y0 - T, x1 - x0 + 2*T, y1 - y0 + 2*T)
            if not expanded.contains(pos):
                continue
            f = 0
            if abs(pos.x() - x0) <= T: f |= self._L
            if abs(pos.x() - x1) <= T: f |= self._R
            if abs(pos.y() - y0) <= T: f |= self._T
            if abs(pos.y() - y1) <= T: f |= self._B
            if f:
                return ov, f          # borde → resize
            if QRect(x0, y0, x1-x0, y1-y0).contains(pos):
                return ov, 0          # interior → drag
        return None, 0

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
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._capturando:
            self._inicio = e.pos(); self._actual = e.pos()
            return
        if self._draggable:
            ov, flags = self._detectar(e.pos())
            if ov is None:
                return
            if flags:                           # borde → resize
                self._resize_ov    = ov
                self._resize_flags = flags
                self.setCursor(self._CURSORES.get(flags, Qt.CursorShape.SizeAllCursor))
            else:                               # interior → drag
                z = self._zoom
                self._drag_ov  = ov
                self._drag_off = e.pos() - QPoint(int(ov['x0']*z), int(ov['y0']*z))
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        self.hovering.emit(e.pos().x()/self._zoom, e.pos().y()/self._zoom)

        # ── Captura de nueva zona ────────────────────────────────────────────
        if self._capturando and self._inicio:
            self._actual = e.pos(); self.update(); return

        # ── Resize de borde ──────────────────────────────────────────────────
        if self._resize_ov is not None:
            ov = self._resize_ov
            z  = self._zoom
            f  = self._resize_flags
            px = e.pos().x() / z
            py = e.pos().y() / z
            mn = 10 / z                         # tamaño mínimo en puntos PDF
            if f & self._L: ov['x0'] = min(px, ov['x1'] - mn)
            if f & self._R: ov['x1'] = max(px, ov['x0'] + mn)
            if f & self._T: ov['y0'] = min(py, ov['y1'] - mn)
            if f & self._B: ov['y1'] = max(py, ov['y0'] + mn)
            self.update(); return

        # ── Drag de posición ─────────────────────────────────────────────────
        if self._drag_ov and self._drag_off:
            z  = self._zoom
            ov = self._drag_ov
            w  = (ov['x1'] - ov['x0'])
            h  = (ov['y1'] - ov['y0'])
            nx0 = (e.pos().x() - self._drag_off.x()) / z
            ny0 = (e.pos().y() - self._drag_off.y()) / z
            ov['x0'] = nx0;     ov['y0'] = ny0
            ov['x1'] = nx0 + w; ov['y1'] = ny0 + h
            self.update(); return

        # ── Cursor hover (sin operación activa) ──────────────────────────────
        if self._draggable:
            ov, flags = self._detectar(e.pos())
            if ov is not None:
                if flags:
                    self.setCursor(self._CURSORES.get(flags, Qt.CursorShape.SizeAllCursor))
                else:
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
            elif not self._capturando:
                self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return

        # ── Fin captura ──────────────────────────────────────────────────────
        if self._capturando and self._inicio:
            rect = QRect(self._inicio, e.pos()).normalized()
            if rect.width() > 4 and rect.height() > 4:
                z = self._zoom
                self.capturado.emit(rect.left()/z, rect.top()/z,
                                    rect.right()/z, rect.bottom()/z)
            self._inicio = self._actual = None; self.update(); return

        # ── Fin resize ───────────────────────────────────────────────────────
        if self._resize_ov is not None:
            ov = self._resize_ov
            self.overlayMovido.emit(
                ov.get('campo', ''), ov['x0'], ov['y0'], ov['x1'], ov['y1'])
            self._resize_ov = None; self._resize_flags = 0
            self.setCursor(Qt.CursorShape.ArrowCursor); return

        # ── Fin drag ─────────────────────────────────────────────────────────
        if self._drag_ov:
            ov = self._drag_ov
            self.overlayMovido.emit(
                ov.get('campo', ''), ov['x0'], ov['y0'], ov['x1'], ov['y1'])
            self._drag_ov = self._drag_off = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)


# ── VisorPDF ──────────────────────────────────────────────────────────────────

class VisorPDF(QWidget):
    capturado     = pyqtSignal(float, float, float, float)
    overlayMovido = pyqtSignal(str, float, float, float, float)

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._doc:    Optional[fitz.Document] = None
        self._ruta:   str   = ''     # PDF actualmente cargado
        self._pagina: int   = 0
        self._zoom:   float = 1.0
        self._fitted: bool  = False  # True tras el primer fit_page exitoso

        barra = QHBoxLayout(); barra.setContentsMargins(4,2,4,2)
        # Zoom
        self._btn_m  = QPushButton('−'); self._btn_m.setFixedSize(28,26)
        self._lbl_z  = QLabel('―', alignment=Qt.AlignmentFlag.AlignCenter)
        self._lbl_z.setFixedWidth(46)
        self._btn_p  = QPushButton('+'); self._btn_p.setFixedSize(28,26)
        self._btn_f  = QPushButton('Encajar'); self._btn_f.setFixedHeight(26)
        # Páginas
        self._btn_prev = QPushButton('◀'); self._btn_prev.setFixedSize(26,26)
        self._btn_next = QPushButton('▶'); self._btn_next.setFixedSize(26,26)
        self._lbl_pag  = QLabel('―', alignment=Qt.AlignmentFlag.AlignCenter)
        self._lbl_pag.setFixedWidth(42)
        self._lbl_pag.setStyleSheet('font-size:10px;')
        # Coordenadas
        self._lbl_xy = QLabel('', alignment=Qt.AlignmentFlag.AlignRight)
        self._lbl_xy.setStyleSheet('color:#888;font-size:10px;')

        self._btn_m.clicked.connect(lambda: self._zoom_rel(0.8))
        self._btn_p.clicked.connect(lambda: self._zoom_rel(1.25))
        self._btn_f.clicked.connect(self.fit_page)
        self._btn_prev.clicked.connect(lambda: self._ir_pagina(self._pagina - 1))
        self._btn_next.clicked.connect(lambda: self._ir_pagina(self._pagina + 1))

        sep_v = QFrame(); sep_v.setFrameShape(QFrame.Shape.VLine)
        for w in (self._btn_m, self._lbl_z, self._btn_p, self._btn_f,
                  sep_v, self._btn_prev, self._lbl_pag, self._btn_next):
            barra.addWidget(w)
        barra.addStretch(); barra.addWidget(self._lbl_xy)

        self._pw = _PaginaPDF()
        self._pw.hovering.connect(lambda x,y: self._lbl_xy.setText(f'x={x:.0f} y={y:.0f}'))
        self._pw.capturado.connect(self.capturado)
        self._pw.overlayMovido.connect(self.overlayMovido)

        self._sc = QScrollArea()
        self._sc.setWidget(self._pw); self._sc.setWidgetResizable(False)
        self._sc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._sc.setStyleSheet('background:#505050;')

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        lay.addLayout(barra); lay.addWidget(sep); lay.addWidget(self._sc, 1)

    def _ir_pagina(self, n: int):
        if not self._doc:
            return
        n = max(0, min(n, len(self._doc) - 1))
        if n == self._pagina:
            return
        self._pagina = n
        self._renderizar()
        self._sc.verticalScrollBar().setValue(0)   # volver arriba al cambiar página

    def pagina_actual(self) -> int:
        return self._pagina

    def cargar(self, ruta: str):
        if ruta == self._ruta:
            return                       # mismo PDF, no tocar nada

        # Guardar posición relativa del scroll ANTES de cambiar
        frac = self._scroll_fracs() if self._fitted else None

        self._ruta   = ruta
        self._doc    = _DOC_CACHE.get(ruta)
        self._pagina = 0

        if self._fitted:
            # Renderizar al mismo zoom → el PDF aparece instantáneamente
            self._renderizar()
            # Restaurar posición de scroll tras el repintado
            if frac:
                QTimer.singleShot(15, lambda fx=frac[0], fy=frac[1]:
                                  self._restore_scroll(fx, fy))
        else:
            # Primera carga: encajar la página
            QTimer.singleShot(60, self.fit_page)

    def fit_page(self):
        if not self._doc:
            return
        pag = self._doc[self._pagina]
        vp  = self._sc.viewport()
        w   = max(vp.width()  - 6, 100)
        h   = max(vp.height() - 6, 100)
        self._fitted = True              # a partir de aquí conservamos zoom/scroll
        self._set_zoom(min(w / pag.rect.width, h / pag.rect.height))

    def _zoom_rel(self, f):
        self._fitted = True              # zoom manual → conservar desde ahora
        self._set_zoom(self._zoom * f)

    # ── Helpers de scroll ─────────────────────────────────────────────────────

    def _scroll_fracs(self) -> tuple[float, float]:
        """Devuelve la posición actual del scroll como fracciones [0..1]."""
        h = self._sc.horizontalScrollBar()
        v = self._sc.verticalScrollBar()
        fx = h.value() / h.maximum() if h.maximum() > 0 else 0.0
        fy = v.value() / v.maximum() if v.maximum() > 0 else 0.0
        return fx, fy

    def _restore_scroll(self, fx: float, fy: float):
        """Restaura el scroll a las fracciones guardadas."""
        h = self._sc.horizontalScrollBar()
        v = self._sc.verticalScrollBar()
        h.setValue(int(fx * h.maximum()))
        v.setValue(int(fy * v.maximum()))

    def _set_zoom(self, z):
        self._zoom = max(0.15, min(5.0, z)); self._renderizar()

    def _renderizar(self):
        if not self._doc: return
        total_pags = len(self._doc)
        self._lbl_pag.setText(f'p.{self._pagina+1}/{total_pags}')
        self._btn_prev.setEnabled(self._pagina > 0)
        self._btn_next.setEnabled(self._pagina < total_pags - 1)
        pag = self._doc[self._pagina]
        mat = fitz.Matrix(self._zoom, self._zoom)
        pix = pag.get_pixmap(matrix=mat, alpha=False)
        img = QImage(pix.samples, pix.width, pix.height,
                     pix.stride, QImage.Format.Format_RGB888)
        self._pw.actualizar(QPixmap.fromImage(img), self._zoom)
        self._lbl_z.setText(f'{int(self._zoom*100)}%')

    def set_modo_captura(self, a):        self._pw.set_captura(a)
    def set_overlays(self, ov):           self._pw.set_overlays(ov)
    def habilitar_arrastre(self, activo): self._pw.set_draggable(activo)

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
        self._visor.overlayMovido.connect(self._on_overlay_movido)
        self._visor.habilitar_arrastre(True)
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
            lp_pag = QLabel('p.1'); lp_pag.setFixedWidth(26)
            lp_pag.setStyleSheet('color:#888;font-size:9px;')
            lp = QLabel('―'); lp.setStyleSheet('color:#666;font-size:10px;')
            fb.addWidget(btn); fb.addWidget(lp_pag); fb.addWidget(lp, 1)
            gl.addLayout(fb)
            pag_guardada = existentes[campo]['pagina'] if campo in existentes else 0
            lp_pag.setText(f'p.{pag_guardada + 1}')
            self._filas[campo] = {
                'spins': spins, 'preview': lp,
                'pag_lbl': lp_pag, 'pagina_num': pag_guardada,
            }
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

    def _on_area(self, x0, y0, x1, y1):
        c = self._capturando
        if not c: return
        f   = self._filas[c]
        pag = self._visor.pagina_actual()   # página donde se capturó
        for k, v in (('x0', x0), ('y0', y0), ('x1', x1), ('y1', y1)):
            f['spins'][k].setValue(v)
        f['pagina_num'] = pag
        f['pag_lbl'].setText(f'p.{pag + 1}')
        self._visor.set_modo_captura(False); self._capturando = None
        raw = extraer_por_plantilla(self._ruta_pdf,
              [{'campo': c, 'pagina': pag, 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1}])
        f['preview'].setText(raw.get(c, '') or '(vacío)')
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
        for c, f in self._filas.items():
            s = f['spins']
            x0, y0, x1, y1 = s['x0'].value(), s['y0'].value(), s['x1'].value(), s['y1'].value()
            if x1 > x0 and y1 > y0:
                ov.append({
                    'campo': c,
                    'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                    'color': COLORES_CAMPO.get(c, '#888'),
                    'label': dict(CAMPOS_PLANTILLA).get(c, c),
                })
        self._visor.set_overlays(ov)

    def _on_overlay_movido(self, campo: str, x0: float, y0: float,
                           x1: float, y1: float):
        """Actualiza spinboxes y preview cuando el usuario arrastra un overlay."""
        if campo not in self._filas:
            return
        f = self._filas[campo]
        for nombre, val in (('x0', x0), ('y0', y0), ('x1', x1), ('y1', y1)):
            f['spins'][nombre].setValue(round(val, 1))
        pag = f.get('pagina_num', 0)
        # Re-extraer el texto de la nueva posición
        raw = extraer_por_plantilla(self._ruta_pdf, [{
            'campo': campo, 'pagina': pag,
            'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
        }])
        texto = raw.get(campo, '') or '(vacío)'
        f['preview'].setText(texto)
        f['preview'].setStyleSheet('color:#2E7D32; font-size:10px;')
        # Redibujar overlays desde los spinboxes (valores limpios)
        self._actualizar_overlays()

    def _construir(self):
        out = []
        for c, f in self._filas.items():
            s = f['spins']
            x0, y0, x1, y1 = s['x0'].value(), s['y0'].value(), s['x1'].value(), s['y1'].value()
            if x1 > x0 and y1 > y0:
                out.append({
                    'campo': c,
                    'pagina': f.get('pagina_num', 0),   # página capturada
                    'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                })
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
        self._campos['fecha'].setText(fmt_fecha(fecha))
        self._campos['proveedor'].setText(prov.get('nombre',''))
        self._campos['nif'].setText(prov.get('nif',''))
        self._campos['cuenta'].setText(prov.get('cuenta',''))
        self._campos['total'].setText(f"{factura.get('total',0):.2f} €")
        self._campos['vencimiento'].setText(fmt_fecha(ven))
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
        for fmt in ('%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y'):
            try: fecha = datetime.strptime(self._campos['fecha'].text().strip(), fmt).date(); break
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


# ── Formato de fechas ────────────────────────────────────────────────────────

def fmt_fecha(d) -> str:
    """Convierte date/datetime/str a DD-MM-YYYY para mostrar en la UI."""
    from datetime import date as _date, datetime as _dt
    if isinstance(d, (_date, _dt)):
        return d.strftime('%d-%m-%Y')
    return ''


# ── Helpers de procesado ─────────────────────────────────────────────────────

def _n_paginas(ruta: str) -> int:
    """Devuelve el número de páginas del PDF (1 si no se puede determinar)."""
    try:
        doc = _DOC_CACHE.get(ruta)
        return len(doc)
    except Exception:
        return 1


def _aplicar_fallback_si_necesario(ruta: str, factura: dict) -> dict:
    """
    Estrategia de extracción en dos niveles:

    IMPORTES (base, IVA, total):
      → Siempre por deducción numérica (busca en la última página del PDF).
        Es robusta para facturas de longitud variable (1 o 2 páginas del mismo
        proveedor). Solo usa las coordenadas de plantilla como último recurso
        si la deducción numérica devuelve cero.

    Nº FACTURA y FECHA:
      → La plantilla tiene prioridad (posición fija en la factura).
        El fallback los rellena si la plantilla no los extrajo.
    """
    fb = extraer_fallback_numerico(ruta)
    fb_total = fb.get('total', 0.0)

    if fb_total > 0:
        # Deducción numérica exitosa → usar siempre para importes
        factura['total']     = fb_total
        factura['iva_ops']   = fb.get('iva_ops', [])
        factura['base']      = fb.get('base', 0.0)
        factura['tipo_iva']  = fb.get('tipo_iva', 0.0)
        factura['cuota_iva'] = fb.get('cuota_iva', 0.0)
    # Si fb_total == 0, se conservan los importes de la plantilla (último recurso)

    # Nº Factura: plantilla primero; fallback si la plantilla no lo encontró
    if not factura.get('num_factura') and fb.get('num_factura'):
        factura['num_factura'] = fb['num_factura']

    # Fecha: plantilla primero; fallback si vacía
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
        # Debounce para navegación entre facturas
        self._nav_timer = QTimer()
        self._nav_timer.setSingleShot(True)
        self._nav_timer.timeout.connect(self._nav_cargar)
        self._nav_fila: int = -1
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
        self._btn_proc_uno   = QPushButton('▶ Procesar esta')
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
        self._btn_proc_uno.setEnabled(False)
        self._btn_guardar.setEnabled(False)
        self._btn_plantilla.setEnabled(False)
        self._btn_ia.setEnabled(False)

        self._btn_importar.clicked.connect(self._importar_pdfs)
        self._btn_proc_todo.clicked.connect(self._procesar_todo)
        self._btn_proc_uno.clicked.connect(self._procesar_esta)
        self._btn_guardar.clicked.connect(self._guardar_dat_batch)
        self._btn_plantilla.clicked.connect(self._editar_plantilla)
        self._btn_ia.clicked.connect(self._procesar_ia_uno)
        self._btn_proveedores.clicked.connect(self._gestionar_proveedores)

        for w in (self._btn_importar, self._btn_proc_todo, self._btn_proc_uno,
                  self._btn_guardar, sep1, self._btn_plantilla, self._btn_ia,
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
        self._tabla.setHorizontalHeaderLabels(['','Fichero','Proveedor','Nº Factura','Total','Fecha'])
        self._tabla.setColumnWidth(COL_ESTADO, 24)
        self._tabla.horizontalHeader().setSectionResizeMode(COL_FICHERO, QHeaderView.ResizeMode.Stretch)
        self._tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla.currentCellChanged.connect(self._on_fila_seleccionada)
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
        self._visor.habilitar_arrastre(True)
        self._visor.overlayMovido.connect(self._on_overlay_movido_principal)
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
        ultima = _leer_cfg().get('ultima_ruta_importacion', '')
        rutas, _ = QFileDialog.getOpenFileNames(
            self, 'Seleccionar facturas PDF', ultima, 'PDF (*.pdf *.PDF)'
        )
        if not rutas:
            return
        # Guardar la carpeta para la próxima vez
        _guardar_cfg('ultima_ruta_importacion', str(Path(rutas[0]).parent))

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
            self._tabla.setItem(fila, COL_VEN,     QTableWidgetItem(fmt_fecha(fecha)))
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

    # ── Navegación entre facturas (con debounce) ──────────────────────────────

    def _on_fila_seleccionada(self, fila, *_):
        """Dispara el timer; si el usuario sigue pulsando teclas no carga nada."""
        if fila < 0:
            return
        self._nav_fila = fila
        self._nav_timer.start(140)      # 140 ms de debounce

    def _nav_cargar(self):
        """Ejecutado 140 ms después del último cambio de fila."""
        fila = self._nav_fila
        if fila < 0:
            return
        ruta = self._ruta_de_fila(fila)
        if not ruta:
            return
        self._ruta_pdf = ruta
        self._visor.cargar(ruta)        # usa caché → casi siempre instantáneo
        resultado = next((r for r in self._batch if r['ruta_pdf'] == ruta), None)
        if resultado:
            self._factura = resultado['factura']
            self._prov    = resultado['prov']
            self._detalle.rellenar(resultado['factura'], resultado['prov'])
            self._btn_gen_uno.setEnabled(True)
            self._btn_ia.setEnabled(True)
            self._btn_plantilla.setEnabled(True)
            self._btn_proc_uno.setText('🔄 Reprocesar')
            self._btn_proc_uno.setEnabled(True)
            self._mostrar_overlays()
        else:
            self._factura = self._prov = None
            self._detalle.limpiar()
            self._visor.set_overlays([])      # limpiar overlays del proveedor anterior
            self._btn_gen_uno.setEnabled(False)
            self._btn_ia.setEnabled(True)
            self._btn_plantilla.setEnabled(False)
            self._btn_proc_uno.setText('▶ Procesar esta')
            self._btn_proc_uno.setEnabled(True)

    def _procesar_esta(self):
        """Procesa solo la factura actualmente seleccionada en la lista."""
        fila = self._tabla.currentRow()
        if fila < 0:
            return
        # Marcar como pendiente para que _procesar_fila la acepte
        ruta = self._ruta_de_fila(fila)
        self._set_estado_fila(fila, '🟡', ruta)
        self._procesar_fila(fila)
        if self._batch:
            self._btn_guardar.setEnabled(True)

    def _on_overlay_movido_principal(self, campo: str,
                                     x0: float, y0: float,
                                     x1: float, y1: float):
        """
        El usuario arrastró un overlay en la ventana principal.
        Guarda las nuevas coordenadas en la BD y re-extrae los datos.
        """
        if not self._prov or not self._ruta_pdf:
            return

        # Persistir en la BD
        proveedores.guardar_plantilla(self._prov['nif'], [{
            'campo': campo, 'pagina': 0,
            'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
        }])

        # Re-extraer todos los campos con la plantilla actualizada
        plantilla = proveedores.obtener_plantilla(self._prov['nif'])
        raw       = extraer_por_plantilla(self._ruta_pdf, plantilla)
        factura   = parsear_campos_plantilla(raw)
        factura   = _aplicar_fallback_si_necesario(self._ruta_pdf, factura)
        self._factura = factura
        self._detalle.rellenar(factura, self._prov)

        # Actualizar la fila en la tabla
        fila = self._tabla.currentRow()
        if fila >= 0:
            fecha = factura.get('fecha')
            self._tabla.setItem(fila, COL_NUMFAC,
                                QTableWidgetItem(str(factura.get('num_factura', ''))))
            self._tabla.setItem(fila, COL_TOTAL,
                                QTableWidgetItem(f"{factura.get('total', 0):.2f} €"))
            self._tabla.setItem(fila, COL_VEN,
                                QTableWidgetItem(fmt_fecha(fecha)))
            # Actualizar también en el batch
            for r in self._batch:
                if r['ruta_pdf'] == self._ruta_pdf:
                    r['factura'] = factura
                    from datetime import date as date_t
                    if isinstance(fecha, date_t):
                        r['fecha_ven'] = calcular_vencimiento(
                            fecha, self._prov['dias_pago'], self._prov['dia_fijo'])
                    break

        # Redibujar overlays con coordenadas actualizadas
        self._mostrar_overlays()
        self._estado(f'Plantilla ajustada — {dict(CAMPOS_PLANTILLA).get(campo, campo)}',
                     '#1565C0')

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
            f'NºDoc: {ndoc:06d}\nVencimiento: {fmt_fecha(fecha_ven)}\n'
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
