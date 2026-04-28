# 📝 Guía de Arquitectura y Estándares de Importación

Este proyecto utiliza una estructura de **Paquete Raíz** para asegurar la compatibilidad entre el entorno de desarrollo local y el entorno de ejecución de AWS Lambda.

---

## 🏗 Estructura del Proyecto

```text
backend-v1/
├── src/
│   ├── services/
│   │   ├── credentials/              # Un servicio (sin carpeta src/ anidada)
│   │   │   ├── handlers/             # Entry points Lambda
│   │   │   │   ├── __init__.py
│   │   │   │   ├── credentials.py
│   │   │   │   └── health.py
│   │   │   ├── models/
│   │   │   ├── services/             # Lógica de negocio (paquete Python `services`)
│   │   │   ├── utils/
│   │   │   ├── shared/               # Copia de src/shared (npm run shared:sync)
│   │   │   ├── serverless.yml
│   │   │   └── requirements.txt
│   │   └── [otros-servicios]/
│   └── shared/                       # Código compartido fuente (se copia a cada servicio)
│       ├── database/
│       ├── middleware/
│       ├── models/
│       └── utils/
└── docs/
```

La **raíz del paquete Python de cada servicio** es la carpeta del servicio (`src/services/<nombre>/`). En Lambda equivale a `/var/task` en el zip de despliegue.

---

## 🚦 Reglas de Oro para Imports

### 1. **El Prefijo `src.` NO se usa en imports internos**

Dentro del **mismo servicio**, los imports entre módulos (`models`, `handlers`, `utils`, paquete `services` de negocio, etc.) no llevan prefijo `src.`:

#### ❌ Incorrecto:
```python
# En handlers/credentials.py
from src.models.credential import Credential  # ❌ NO
```

#### ✅ Correcto:
```python
# En handlers/credentials.py
from models.credential import Credential  # ✅ SÍ
```

**¿Por qué?**  
La raíz del servicio está en `sys.path` (localmente y en Lambda como `/var/task`). Así Python resuelve `from models.credential` sin prefijo.

---

### 2. **Configuración del Handler en AWS Lambda**

El punto de entrada configurado en `serverless.yml` debe reflejar la jerarquía completa:

```yaml
functions:
  myFunction:
    handler: handlers/credentials.lambda_handler
    # NO usar: src/handlers/... (no hay src/ dentro del servicio)
    # NO usar: src.handlers.credentials.lambda_handler
```

---

### 3. **Configuración de `sys.path` al inicio del archivo**

Cada archivo que necesite importar módulos del mismo servicio debe configurar `sys.path` **ANTES** de cualquier import local:

#### Patrón Estándar (usar en todos los archivos):

```python
# ============================================================================
# CONFIGURAR PATHS PRIMERO (antes de cualquier import local)
# ============================================================================
import sys
import os

# Calcular path a la raíz del servicio (carpeta del servicio)
current_file = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file)
service_root = os.path.dirname(current_dir)  # p. ej. desde models/ o handlers/ sube al servicio

# Agregar a sys.path si no está
if service_root not in sys.path:
    sys.path.insert(0, service_root)

# Fallback para Lambda (raíz del zip = raíz del servicio)
lambda_root = '/var/task'
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

# ============================================================================
# AHORA SÍ IMPORTAR MÓDULOS LOCALES
# ============================================================================
from models.credential import Credential
from utils.responses import success_response
```

---

### 4. **Manejo de la carpeta `shared/`**

La carpeta `shared/` se copia a cada servicio con `npm run shared:sync` (`scripts/sync_shared.py`). Para acceder a ella:

```python
import sys
from pathlib import Path

# Buscar shared/ en Lambda o localmente
possible_shared_paths = [
    Path("/var/task/shared"),  # Lambda
    Path(__file__).resolve().parent.parent / "shared",  # raíz del servicio / shared (desde handlers/ o models/)
]

for shared_path in possible_shared_paths:
    if shared_path.exists() and (shared_path / "database").exists():
        if str(shared_path) not in sys.path:
            sys.path.insert(0, str(shared_path))
        break

# Ahora importar desde shared
from database.dynamodb import get_table
```

---

## 🛠 Plantillas Estándar

### Para un nuevo **Modelo** (`models/nombre.py`)

```python
"""Modelo de [Nombre] para [propósito]"""
import os
import sys
import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from aws_lambda_powertools import Logger

# ============================================================================
# CONFIGURAR PATH A SHARED (si necesita DynamoDB)
# ============================================================================
from pathlib import Path

possible_shared_paths = [
    Path("/var/task/shared"),
    Path(__file__).resolve().parent.parent / "shared",
]

for shared_path in possible_shared_paths:
    if shared_path.exists() and (shared_path / "database" / "dynamodb.py").exists():
        if str(shared_path) not in sys.path:
            sys.path.insert(0, str(shared_path))
        break

from database.dynamodb import get_table

logger = Logger()


class MiModelo:
    """Modelo de [Nombre]"""
    
    TABLE_NAME = "mi-tabla"
    
    def __init__(self, id: str, **kwargs):
        self.id = id
        # ... resto de campos
    
    @classmethod
    def get_table(cls):
        """Obtiene la tabla DynamoDB"""
        stage = os.getenv("STAGE", "dev")
        full_table_name = f"mh-backoffice-{stage}-{cls.TABLE_NAME}"
        return get_table(cls.TABLE_NAME, full_table_name=full_table_name)
    
    # ... resto de métodos
```

---

### Para un nuevo **Servicio de negocio** (`services/nombre_service.py`)

```python
"""Servicio de lógica de negocio para [nombre]"""
# ============================================================================
# CONFIGURAR PATHS PRIMERO
# ============================================================================
import sys
import os
from typing import Optional, Dict, Any
from aws_lambda_powertools import Logger

# Raíz del servicio (desde services/nombre_service.py: subir un nivel desde el paquete services/)
current_file = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file)
service_root = os.path.dirname(current_dir)

if service_root not in sys.path:
    sys.path.insert(0, service_root)

lambda_root = '/var/task'
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

# ============================================================================
# IMPORTS DESPUÉS DE CONFIGURAR PATHS
# ============================================================================
from models.mi_modelo import MiModelo

logger = Logger()


class MiServicio:
    """Servicio para gestión de [nombre]"""
    
    def metodo_ejemplo(self) -> Dict[str, Any]:
        """Descripción del método"""
        try:
            # Lógica aquí
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Error: {e}")
            raise
```

---

### Para un nuevo **Handler** (`handlers/nombre.py`)

```python
"""Handler para [nombre]"""
import sys
import os
from pathlib import Path
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.utilities.typing import LambdaContext

# ============================================================================
# CONFIGURAR PATHS PRIMERO
# ============================================================================
# Raíz del servicio (handlers/archivo.py -> subir dos niveles)
service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = '/var/task'
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]

for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

# ============================================================================
# IMPORTS DESPUÉS DE CONFIGURAR PATHS
# ============================================================================
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    NotFoundError,
    InternalServerError
)

from services.mi_servicio import MiServicio
from utils.responses import success_response, error_response

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
mi_servicio = MiServicio()


@app.get("/api/v2/recurso")
@tracer.capture_method
def listar_recursos():
    """Lista recursos"""
    try:
        result = mi_servicio.metodo_ejemplo()
        return success_response(data=result, message="Recursos obtenidos")
    except Exception as e:
        logger.error("Error", extra={"error": str(e)})
        raise InternalServerError("Error interno del servidor")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
```

---

## Serverless por servicio

Configuración compartida en `src/` del monorepo (referencias `../../...` desde `src/services/<servicio>/`):

| Archivo | Contenido |
|---------|-----------|
| `serverless.provider.yml` | `providerBase`: runtime Python 3.13, capas (dependencias + Powertools), HTTP API, `environment` |
| `serverless.package.yml` | `package` e patterns del zip de despliegue |
| `serverless.pythonRequirements.yml` | `custom.pythonRequirements` para la layer de pip |

Las **`functions`** (incluida **health** y el resto) se declaran **solo** en el `serverless.yml` de cada servicio; no hay módulo YAML común para funciones.

```yaml
custom:
  serviceSlug: auth   # opcional: útil para rutas con ${self:custom.serviceSlug}

functions:
  health:
    handler: handlers/health.lambda_handler
    events:
      - httpApi:
          path: /api/v2/health-${self:custom.serviceSlug}
          method: get
  login:
    handler: handlers/login.lambda_handler
    events:
      - httpApi:
          path: /api/v2/auth/login
          method: post
```

En Serverless Framework v4 **no** hace falta declarar el plugin `serverless-python-requirements` si ya existe `custom.pythonRequirements`.

---

## ✅ Checklist para Nuevos Servicios

Para generar la estructura base (`handlers/`, `models/`, `services/`, `utils/`, `serverless.yml`, health, `requirements.txt` y sincronizar `shared/`):

```bash
npm run create:service -- <slug>
# ejemplo: npm run create:service -- orders
```

Opciones: `--name mi-servicio-custom`, `--dry-run`, `--no-sync`.

Antes de hacer deploy, verifica:

- [ ] Todos los archivos tienen `# ============================================================================`
- [ ] Configuración de `sys.path` está ANTES de imports locales
- [ ] Los imports usan rutas relativas sin prefijo `src.`
- [ ] Existe `__init__.py` en cada paquete del servicio (`handlers/`, `models/`, `services/`, `utils/`)
- [ ] `serverless.yml` usa `handler: handlers/nombre.lambda_handler` (sin `src/` dentro del servicio)
- [ ] `npm run shared:sync` (o `python scripts/sync_shared.py`) copia `src/shared/` a cada servicio
- [ ] Los patterns de `package` en `serverless.yml` incluyen `shared/**/*.py`

---

## 🐛 Troubleshooting

### Error: `ModuleNotFoundError: No module named 'models.credential'`

**Causa**: `sys.path` no está configurado antes del import.

**Solución**:
1. Verifica que el bloque de configuración de paths esté ANTES de cualquier import local
2. Agrega logging temporal para verificar el `sys.path`:
   ```python
   import sys
   print(f"sys.path: {sys.path}")
   ```
3. Verifica que exista el archivo `__init__.py` en todas las carpetas

---

### Error: `Handler 'handlers/nombre.lambda_handler' not found`

**Causa**: La ruta del handler en `serverless.yml` no coincide con la estructura de archivos.

**Solución**:
```yaml
# Correcto
handler: handlers/credentials.lambda_handler

# Incorrecto
handler: src/handlers/credentials.lambda_handler
handler: src.handlers.credentials.lambda_handler
```

---

### Error: `No module named 'database.dynamodb'`

**Causa**: La carpeta `shared/` no se copió correctamente.

**Solución**:
1. Ejecuta la sincronización (funciona en Windows, macOS y Linux):
   ```bash
   npm run shared:sync
   ```
   También puedes usar: `python scripts/sync_shared.py`
3. Verifica que `shared/` esté en la carpeta del servicio antes del deploy

---

## 📚 Referencias

- [AWS Lambda Python Packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)
- [Python sys.path documentation](https://docs.python.org/3/library/sys.html#sys.path)
- [Python Package Structure](https://docs.python.org/3/tutorial/modules.html#packages)

---

## 🔄 Última Actualización

**Fecha**: 2026-01-29  
**Versión**: 1.0  
**Autor**: Backend Team

---

**¿Preguntas o mejoras a esta guía?**  
Contacta al equipo de backend o abre un issue en el repositorio.