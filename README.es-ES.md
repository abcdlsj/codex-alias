

# codex-alias

`codex-alias` ejecuta múltiples cuentas/perfiles de Codex con directorios de inicio separados. Cada
perfil obtiene un `CODEX_HOME` aislado y un comando envoltorio (por ejemplo
`codex-work`) que reenvía al binario original de `codex`, para que la autenticación, la configuración y
el historial permanezcan separados.

Se distribuye como un paquete de Python con dos partes:

- una biblioteca reutilizable sin interfaz de usuario (`codex_alias`) que realiza todo el trabajo en el sistema de archivos
- una CLI con `rich` + `click` (`codexa`) construida sobre ella

## Instalación

Requiere [uv](https://docs.astral.sh/uv/).

```bash
# One-shot install onto PATH
make install

# Equivalently, via uv directly
uv tool install .

# Or work inside the project
uv sync
uv run codexa doctor
```

Otros objetivos de `make`: `make test`, `make sync`, `make uninstall`, `make clean`
(ejecute `make help` para ver la lista).

`uv tool install .` coloca `codexa` en su PATH e instala también los alias
compatibles `codexalias` y `codex-alias`. A partir de ahí:

```bash
codexa add work
codex-work
```

Durante `add`, los prompts interactivos le permiten:
1. Copiar complementos/habilidades desde el directorio de origen
2. Copiar la configuración actual (`auth.json` + `config.toml`)
3. Seleccionar los hooks del perfil raíz para compartirlos con el nuevo perfil
4. Compartir sesiones con el directorio raíz (enlace simbólico)
5. De lo contrario, migrar las sesiones al nuevo perfil

Las elecciones se guardan como tipos de sincronización ordenados. Un `codexa
sync <profile>` posterior vuelve a ejecutar, en ese orden, el migrador
correspondiente a cada tipo. Pase `--no-bootstrap` para omitir los prompts.

## Comandos

```bash
# Gestionar directorios de perfil y sus comandos envoltorio
codexa profile manage add <profile> [command-name]
codexa profile manage list [--json] [--details]
codexa profile manage path <profile>
codexa profile manage rename <profile> <new-profile> \
  [--command-name <old-wrapper>] [--new-command-name <new-wrapper>]
codexa profile manage remove <profile> [command-name] [--keep-data] [--yes]
codexa profile manage refresh-wrappers

# Los comandos de ciclo de vida de nivel superior siguen disponibles por compatibilidad
codexa add <profile> [command-name]

# Import one session from default ~/.codex into current/target home
codexa import <session-id> [target|@current]

# Repair stale provider metadata (provider defaults to HOME/config.toml)
codexa fix-session <session-id> [home|@current] [--provider <provider>]

# Copy a session for default/another profile, then resume the copy
codexa resume <session-id> [--profile default|<profile>]

# Detect the latest session in the current directory and show its final output
codexa detect

# Detect the latest session and resume it directly in its profile
codexa detect resume [codex args...]

# Interactive session migration into the current home
codexa migrate session

# Copy all sessions from one home into another
codexa migrate copy <source|@source> [target|@current]

# Copy one session from one home into another
codexa migrate one <source|@source> <session-id> [target|@current]

# Share sessions with a source home via symlink (existing profile)
codexa share-sessions <profile> [source|@source]

# Run codex once with a profile (without creating a wrapper)
codexa run <profile> [codex args...]

# Listar perfiles (alias compatible)
codexa list

# Imprimir la ruta absoluta del perfil (alias compatible)
codexa path <profile>

# Eliminar un perfil (alias compatible)
codexa remove <profile> [command-name]

# Environment and sanity checks
codexa doctor

# Select root hooks for a profile
codexa hooks

# Reapply the profile's saved migration types from the source home
codexa sync [profile] [--yes]
```

`@source` hace referencia al directorio de origen configurado; `@current` hace referencia al
`CODEX_HOME` actual (volviendo al directorio de origen cuando no está configurado). También funciona
usar un nombre de perfil sin más o una ruta absoluta en cualquier lugar donde se espere un directorio.

`codexa profile manage remove` pide confirmación antes de eliminar el directorio del perfil
(autenticación, configuración, sesiones y el resto de sus datos). Use `--yes` para omitirla o
`--keep-data` para conservar los datos y eliminar solo el envoltorio. No se permite eliminar el
directorio de origen configurado ni el `CODEX_HOME` actual.

`codexa profile manage rename` mueve el directorio del perfil sin modificar sus datos y actualiza
el envoltorio generado. Use `--command-name` y `--new-command-name` si el envoltorio existente
usa un nombre personalizado. Los comandos superiores `add`, `list`, `path`, `remove` y
`refresh-wrappers` siguen disponibles como alias compatibles.

## Variables de entorno

- `CODEXALIAS_PROFILE_ROOT`: directorio raíz de perfiles (predeterminado: `~/.codex/profiles`)
- `CODEXALIAS_BIN_DIR`: directorio de salida para los envoltorios (predeterminado: `~/.local/bin`)
- `CODEXALIAS_CODEX_CMD`: comando original de Codex (predeterminado: `codex`)
- `CODEXALIAS_CODEX_WRAPPER`: ejecutable envoltorio de Codex; tiene prioridad sobre
  `CODEXALIAS_CODEX_CMD` para `run`, `resume` y los comandos de perfil generados
- `CODEXALIAS_CODEX_ARGS`: argumentos fijos que se agregan antes en cada invocación de Codex
- `CODEXALIAS_SOURCE_HOME`: directorio de origen utilizado por `add`/`@source` (predeterminado: `$CODEX_HOME` o `~/.codex`)
- `CODEXALIAS_MANAGER_BIN_NAME`: nombre del binario del administrador utilizado por los comandos de perfil generados (predeterminado: `codexalias`, un alias compatible)

Para reutilizar un envoltorio que agregue automáticamente banderas `yolo`, ganchos o notificaciones:

```bash
export CODEXALIAS_CODEX_WRAPPER="$HOME/.superset/bin/codex"
export CODEXALIAS_CODEX_ARGS="--dangerously-bypass-approvals-and-sandbox"
codexa resume <session-id>
```

El valor debe ser un nombre o ruta de un ejecutable. Los alias y funciones de shell no son
archivos ejecutables y, por lo tanto, no se pueden usar como envoltorios de proceso.

## Uso de la biblioteca

El núcleo es importable y nunca imprime ni finaliza la ejecución; devuelve objetos de valor o
genera subclases de `CodexAliasError`, por lo que puede controlarlo desde sus propias herramientas:

```python
from codex_alias import CodexAlias, Config

mgr = CodexAlias(Config.from_env())
mgr.add_profile("work")

for profile in mgr.list_profiles():
    print(profile.name, "shared" if profile.sessions_shared else "isolated")

# Copy one session between homes
src = mgr.resolve_home_ref("@source").path
dst = mgr.resolve_home_ref("work").path
result = mgr.copy_session_by_query(src, "019d1df0-8f1e-7393-b54a-0f0b511c5a33", dst)
print(result.status)
```

## Compartir sesiones

Por defecto, cada perfil tiene sesiones aisladas. Para compartir el historial entre perfiles
(útil cuando diferentes configuraciones de proveedor acceden a las mismas conversaciones), comparta
las sesiones durante la creación (responda sí a "Share sessions with root home") o para
un perfil existente:

```bash
codexa share-sessions work
```

Esto crea un enlace simbólico de `~/.codex/profiles/work/sessions` (junto con `history.jsonl` y
las bases de datos de metadatos `state_5.sqlite` / `logs_1.sqlite`) hacia el directorio de origen,
por lo que los perfiles compartidos ven el mismo historial de conversaciones mientras mantienen
autenticación y configuración separadas. Los archivos reales existentes se respaldan en `*.backup.N` antes de ser
reemplazados por un enlace simbólico.

## Reparar una sesión

Codex persiste los metadatos del proveedor del modelo tanto dentro de cada sesión JSONL como en
el índice de hilos `state_5.sqlite`. Si un proveedor se renombra o elimina posteriormente,
`codex resume` puede fallar antes de que se inicie la TUI con el mensaje `Model provider '<name>' not
found`. Repare ambas copias persistentes con:

```bash
# Preview the repair; "custom" is inferred from ~/.codex/config.toml
codexa fix-session 019f8938-544e-7160-901c-af1ffb2657a5 --dry-run

# Apply it, but only where the stale value is exactly "aicoding"
codexa fix-session 019f8938-544e-7160-901c-af1ffb2657a5 \
  --from-provider aicoding
```

El comando valida cada registro JSONL antes de escribir, crea copias únicas
`*.backup.N` para los archivos JSONL y SQLite modificados, reemplaza el
JSONL de manera atómica y actualiza condicionalmente solo la fila del hilo SQLite coincidente. Use
`--provider` para anular el proveedor inferido desde la configuración de nivel superior `model_provider` del directorio seleccionado.

## Reanudar con otro perfil

`codexa resume <session-id>` muestra una lista numerada de Rich que contiene
`default` y cada perfil agregado. Después de seleccionar el perfil, puede
reparar el proveedor y el modelo de la copia antes de iniciar Codex. La sesión
de origen nunca se modifica. Esto también funciona cuando los perfiles
comparten el almacenamiento mediante enlaces simbólicos, ya que la sesión
clonada tiene un ID distinto.

La copia aplica además las reglas de compatibilidad registradas,
independientemente de la respuesta a la pregunta de reparación. La regla actual
para `gpt-5*` vacía el `reasoning.content` no vacío. Las reglas usan las
capacidades del modelo y de la API, no nombres de proveedores codificados.

El historial cifrado tiene un límite de portabilidad distinto. Codexa
compara una huella normalizada `wire_api + base_url` cuando conoce ambos lados.
Conserva el razonamiento cifrado entre alias del mismo backend, mantiene el
registro de razonamiento externo (y su ordinal paginado) pero limpia
`encrypted_content` ligado al backend como una conversión con pérdida, y no
adivina si falta una huella. Una compactación cifrada externa bloquea la
reparación porque puede ser la única copia del contexto anterior. Las llamadas
de herramientas incompletas o huérfanas solo generan diagnósticos. Añada futuras
reglas en
`src/codex_alias/session_mappings.py` y clasifíquelas como sin pérdida o con
pérdida.

Use `--profile cpa` para omitir el selector de perfil o `--no-launch` para crear
la copia sin iniciar Codex. El ejecutable principal instalado es `codexa`, junto
con los alias compatibles `codexalias` y `codex-alias`.

`codexa detect` busca la sesión más reciente del directorio actual en el hogar
fuente y en todos los perfiles, muestra el perfil detectado y la última
respuesta del asistente. `--cwd PATH` permite inspeccionar otro directorio.
`codexa detect resume` usa directamente el hogar del perfil detectado para
`codex resume`, sin copiar la sesión ni pedir otro perfil. Las formas antiguas
`resume detect`, `detach` y `--detach` ya no son compatibles.

## Desarrollo

```bash
uv sync
uv run pytest
```
