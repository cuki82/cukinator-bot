#!/bin/bash
# Corre todos los tests. Uso: bash tests/run_all.sh
set -e
cd "$(dirname "$0")/.."
echo "=== intent_router ==="
python3 tests/test_intent_router.py
echo
echo "=== credentials ==="
python3 tests/test_credentials.py
echo
echo "=== astro engine smoke ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from modules.swiss_engine import calc_carta_completa, calc_transitos, calc_retorno_solar
natal = calc_carta_completa('11/07/1982', '23:30', 'Buenos Aires, Argentina')
assert natal['planetas']['Sol']['signo'].startswith('Cancer'), 'Sol debe estar en Cáncer'
assert natal['planetas']['Quiron'].get('signo'), 'Quirón debe calcular'
trans = calc_transitos(natal)
assert trans['aspectos'], 'Debe haber al menos 1 aspecto activo'
sr = calc_retorno_solar(natal)
assert sr['planetas']['Sol']['signo'].startswith('Cancer'), 'SR Sol debe estar en Cáncer'
print('OK astro smoke')
"
echo
echo "=== db backend ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from services.db import pg_available, ping
print('pg available:', pg_available())
print('pg ping:', ping())
"
echo
echo "=== tenants resolver ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from services.tenants import resolve_tenant, list_tenants
print('tenants:', [t['slug'] for t in list_tenants()])
print('resolve(owner):', resolve_tenant(8626420783))
"
echo "=== TODOS OK ==="
