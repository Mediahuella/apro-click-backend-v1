"""Aísla los módulos de este servicio de los de otros servicios.

Cada servicio tiene sus propios paquetes ``utils``/``services``/``handlers``
con el mismo nombre (son Lambdas independientes, cada una con su raíz en
``sys.path``). Sin este conftest, correr ``pytest src`` hace que el ``utils`` del
primer servicio colectado tape el de los demás y los imports fallen con
``ModuleNotFoundError``; por servicio, en cambio, cada suite pasaba.

Al colectar esta carpeta se pone la raíz del servicio al frente de ``sys.path`` y
se descartan los módulos homónimos que haya dejado otro servicio, para que cada
suite importe los suyos.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent
SHARED_ROOT = SERVICE_ROOT / "shared"

#: Paquetes que existen con el mismo nombre en varios servicios.
_COLISIONAN = {"utils", "services", "handlers", "models", "database", "shopify"}

for mod in [m for m in sys.modules if m.split(".")[0] in _COLISIONAN]:
    del sys.modules[mod]

for ruta in (SHARED_ROOT, SERVICE_ROOT):
    texto = str(ruta)
    if texto in sys.path:
        sys.path.remove(texto)
    sys.path.insert(0, texto)
