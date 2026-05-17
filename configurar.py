"""
Asistente de configuración inicial de FactScan.
Ejecutar una vez al instalar en un equipo nuevo: python configurar.py
"""
from pathlib import Path

ENV = Path(__file__).parent / '.env'

if ENV.exists():
    print('.env ya existe. Bórralo si quieres reconfigurarlo.')
    raise SystemExit(0)

print('=== Configuración inicial de FactScan ===\n')
key   = input('API Key de Anthropic (sk-ant-...): ').strip()
ruta  = input(r'Ruta A3Con [C:\A3\A3CONV5\E00002\FACTURAS\2026]: ').strip()
if not ruta:
    ruta = r'C:\A3\A3CONV5\E00002\FACTURAS\2026'

ENV.write_text(
    f'ANTHROPIC_KEY={key}\n'
    f'RUTA_A3CON={ruta}\n',
    encoding='utf-8'
)
print(f'\n.env creado en {ENV}')
print('Ya puedes ejecutar la aplicación: python main.py')
