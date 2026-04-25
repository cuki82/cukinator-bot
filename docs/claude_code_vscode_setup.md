# Claude Code dentro de Visual Studio Code — configuración

Guía corta para dejar Claude Code funcionando dentro de VS Code. Asumo que ya:
- Tenés VS Code instalado (https://code.visualstudio.com, versión ≥ 1.85).
- Tenés tu API key de Anthropic (`sk-ant-api03-...`) copiada.
- Tenés Node.js 18+ instalado (requisito del CLI que la extensión usa bajo el capot).

---

## 1. Instalar la extensión

### 1.1 Desde el marketplace (vía UI)

1. Abrí VS Code.
2. Panel izquierdo → ícono de **Extensions** (o `Ctrl+Shift+X` / `Cmd+Shift+X`).
3. Buscá: `Claude Code`.
4. Publisher: **Anthropic**. ID exacto: `anthropic.claude-code`.
5. Click **Install**.

### 1.2 Desde la terminal (alternativo)

```bash
code --install-extension anthropic.claude-code
```

Si usás **code-server** (VS Code en browser):

```bash
code-server --install-extension anthropic.claude-code
```

### 1.3 Verificar instalación

- Barra lateral izquierda: debería aparecer un ícono nuevo del logo de Claude.
- `Ctrl+Shift+P` / `Cmd+Shift+P` → escribí `Claude Code:` → deberías ver varios comandos (`Claude Code: Open Chat`, `Claude Code: Resume Last Session`, etc.).

Si no aparece: reloadeá la ventana con `Ctrl+Shift+P` → **Developer: Reload Window**.

---

## 2. Cargar la API key

La extensión reutiliza la misma config que el CLI `claude`. Cuando la corrés por primera vez, te abre un tab de autenticación.

### 2.1 Primera vez — wizard de auth

1. Click en el ícono de Claude en la barra lateral.
2. Aparece un panel "Sign in to Claude Code".
3. Elegí **"I have an API key"** (no "Sign in with Claude.ai" si no usás OAuth).
4. Pegá tu `sk-ant-api03-...` y Enter.
5. Debería confirmar "Authenticated" y abrir el chat.

### 2.2 Si ya estaba configurada por CLI

Si antes corriste `claude` en terminal y te logueaste ahí, la extensión hereda esa sesión automáticamente. No hay que hacer nada.

### 2.3 Alternativa: variable de entorno

En lugar del wizard, podés setear `ANTHROPIC_API_KEY` en tu entorno:

**Windows PowerShell** (persistente):

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-api03-XXXXXXX", "User")
```

Cerrá y reabrí VS Code para que la tome.

**macOS / Linux** (agregá a `~/.zshrc` o `~/.bashrc`):

```bash
export ANTHROPIC_API_KEY="sk-ant-api03-XXXXXXX"
```

Después `source ~/.zshrc` y reabrir VS Code.

### 2.4 Verificar dónde quedó guardada

El CLI y la extensión guardan la config en:

- **Windows**: `C:\Users\<tu-user>\.claude\config.json`
- **macOS/Linux**: `~/.claude/config.json`

Podés abrirla con `code ~/.claude/config.json` para confirmar.

---

## 3. Abrir el panel de chat

Hay tres formas:

1. **Ícono lateral** (Activity Bar) → click en el logo de Claude → se abre el panel de chat.
2. **Command Palette**: `Ctrl+Shift+P` / `Cmd+Shift+P` → `Claude Code: Open Chat`.
3. **Atajo directo**: `Ctrl+Esc` (Windows/Linux) o `Cmd+Esc` (macOS) — abre/cierra el panel.

El panel tiene:
- Caja de texto abajo
- Historial de mensajes arriba
- Botones de sesión (resume / new / switch)
- Selector de modelo (Opus / Sonnet / Haiku)
- Toggle de "planning" (si querés modo plan antes de ejecutar)

---

## 4. Primera sesión — qué hacer

1. Abrí una carpeta con un proyecto real (`File → Open Folder`). Claude Code trabaja en el contexto del workspace abierto.
2. En el panel de chat escribí algo concreto, por ejemplo:

   > "Dame un resumen de la arquitectura del repo. No edites nada, solo explicame."

3. Claude va a usar tools (Read, Grep, Bash) automáticamente — vas a ver cada llamada en vivo con sus resultados desplegables.
4. Para que edite código, pedile explícito: *"Agregá un endpoint /health al servidor"*. Claude muestra el diff antes de aplicar; vos aceptás o rechazás.

---

## 5. Settings útiles para tunear

Abrí Settings (`Ctrl+,` / `Cmd+,`) → buscá `claude-code`. Las más relevantes:

| Setting | Qué hace | Recomendado |
|-|-|-|
| `claude-code.autoResumeLastSession` | Al abrir VS Code, retoma la última sesión en vez de empezar nueva | `true` |
| `claude-code.defaultSessionBehavior` | `new` / `resume` — qué hace al abrir el panel por primera vez | `resume` |
| `claude-code.openOnStartup` | Abre el panel de chat automáticamente al abrir el workspace | `true` si querés arranque directo |
| `claude-code.model` | Modelo default — `opus` / `sonnet` / `haiku` | `sonnet` para el día a día, `opus` para tareas complejas |
| `claude-code.dangerouslySkipPermissions` | Skippea el prompt de confirmación para cada tool use | `false` (solo activar si estás en sandbox) |
| `claude-code.telemetry.enabled` | Manda telemetry a Anthropic | `false` si preferís privacidad |

Estos settings pueden vivir en tu `settings.json` global o en `.vscode/settings.json` del workspace (recomendado para configs per-proyecto).

Ejemplo de `.vscode/settings.json`:

```json
{
  "claude-code.autoResumeLastSession": true,
  "claude-code.defaultSessionBehavior": "resume",
  "claude-code.openOnStartup": true,
  "claude-code.model": "sonnet"
}
```

---

## 6. Slash commands básicos

Dentro de la caja del chat, tipeá `/` y aparece un menú. Los más usados:

| Comando | Uso |
|-|-|
| `/clear` | Limpia el historial de la sesión actual (no borra archivos) |
| `/compact` | Comprime contexto viejo para liberar tokens sin perder info clave |
| `/help` | Lista todos los comandos disponibles |
| `/cost` | Muestra cuántos tokens/USD consumió esta sesión |
| `/model` | Cambia el modelo (opus / sonnet / haiku) sin salir de la sesión |
| `/resume` | Lista sesiones previas y permite retomar una |
| `/init` | Crea un `CLAUDE.md` en el repo con contexto persistente (lo lee en cada sesión nueva) |
| `/login` / `/logout` | Gestionar la cuenta autenticada |

---

## 7. Memoria persistente — `CLAUDE.md`

Claude Code lee automáticamente el archivo `CLAUDE.md` de la raíz del proyecto al empezar cada sesión. Es la forma oficial de darle contexto estable (convenciones de código, stack, rutas críticas, etc.).

Creá uno con `/init` adentro del chat, o a mano:

```markdown
# Proyecto X

## Stack
- Python 3.11, FastAPI, SQLite
- Node 20, Next.js 14

## Convenciones
- Tabs de 4 espacios en Python
- Comentarios en español rioplatense
- Tests en `tests/` con pytest

## Rutas importantes
- `core/server.py` = entry point
- `services/` = lógica de negocio
- NO tocar `legacy/` sin consultar
```

---

## 8. Sesiones y continuidad

Cada workspace tiene su historial propio de sesiones guardado en:

- **Windows**: `C:\Users\<user>\.claude\projects\<hash-del-workspace>\`
- **macOS/Linux**: `~/.claude/projects/<hash-del-workspace>/`

Cada sesión es un archivo `.jsonl`. Si abrís el workspace en otra máquina, la sesión vieja **no viaja** automáticamente — son archivos locales. Para sync entre máquinas podés copiar esa carpeta manualmente o con rsync.

Para retomar una sesión vieja:
- Comando: `Claude Code: Resume Last Session`
- O desde el panel: botón "Resume" arriba a la derecha.

---

## 9. Atajos de teclado (cheat sheet)

| Atajo | Acción |
|-|-|
| `Ctrl+Esc` / `Cmd+Esc` | Toggle panel de chat |
| `Ctrl+Shift+P` → `Claude Code: ...` | Cualquier comando de la extensión |
| `Ctrl+Enter` en el chat | Enviar mensaje (sin salto de línea) |
| `Shift+Enter` en el chat | Salto de línea dentro del mensaje |
| `/` al principio de la caja | Abre menú de slash commands |

Podés remaparlos en `File → Preferences → Keyboard Shortcuts` buscando `claude-code`.

---

## 10. Troubleshooting

| Síntoma | Causa probable | Fix |
|-|-|-|
| El panel se abre pero queda en blanco | La extensión no detectó Node.js | Verificá `node --version` ≥ 18 y reloadeá VS Code |
| "Authentication failed" con API key válida | Key con formato incorrecto o con espacios | Recopialla de Anthropic Console, evitá pegarla con newlines |
| Tools no ejecutan (`Read`/`Bash` tiran permission denied) | Falta aceptar el prompt de permisos | Marcá "Always allow in this workspace" la primera vez |
| Respuesta se corta al medio | Hit context window del modelo | `/compact` o `/clear` y empezá sesión nueva |
| "Rate limit exceeded" | Demasiadas requests paralelas | Esperá 30s, o bajá a `haiku`/`sonnet` |
| La sesión no se retoma al reabrir el workspace | `autoResumeLastSession` en false | Settings → `claude-code.autoResumeLastSession: true` |
| Extensión aparece desactivada tras update | VS Code no migró el extension state | Reinstalá: desactivar, `Developer: Reload Window`, reactivar |
| Quiero borrar todo el historial | | Borrá la carpeta `~/.claude/projects/<hash>` del workspace |

---

## 11. Checklist final

- [ ] Extensión `anthropic.claude-code` instalada y visible en la barra lateral
- [ ] Ícono de Claude abre el panel de chat sin pedir auth
- [ ] Un prompt de prueba ("decime qué hay en este repo") devuelve respuesta usando tools
- [ ] `~/.claude/config.json` existe con la key guardada
- [ ] `CLAUDE.md` creado en la raíz del proyecto (recomendado)
- [ ] Settings de auto-resume activados

Si todo eso da OK, ya estás listo. De acá en adelante, Claude Code vive dentro del editor.

---

## 12. Bonus — convivencia con el CLI

El CLI `claude` (terminal) y la extensión **comparten** la misma config, sesiones y autenticación. Podés:

- Arrancar una sesión en terminal con `claude`, seguirla en VS Code después (`Resume Last Session`).
- Editar el código en VS Code con la extensión abierta, y ejecutar `claude --resume` desde otra terminal para ver el mismo historial.

Funciona bien si no abrís dos sesiones simultáneas del mismo workspace — en ese caso puede haber conflictos al escribir el `.jsonl` de sesión.

Fin.
