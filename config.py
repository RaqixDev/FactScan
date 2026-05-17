import os
from pathlib import Path

# Cargar variables de .env si existe (fichero local, nunca en git)
_env_path = Path(__file__).parent / '.env'
if _env_path.exists():
    for _line in _env_path.read_text(encoding='utf-8').splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

EMPRESA         = '00002'
RUTA_A3CON      = os.environ.get('RUTA_A3CON',   r'C:\A3\A3CONV5\E00002\FACTURAS\2026')
RUTA_ENTRADA    = os.environ.get('RUTA_ENTRADA',  r'C:\Facturas_Entrada')
ANTHROPIC_KEY   = os.environ.get('ANTHROPIC_KEY', '')
MODELO_CLAUDE   = 'claude-sonnet-4-6'
ENCODING_DAT    = 'latin-1'
