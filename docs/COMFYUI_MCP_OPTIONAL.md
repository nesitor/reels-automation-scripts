# ComfyUI MCP (opcional)

La pipeline **no necesita un MCP de ComfyUI**: usa la API HTTP nativa de
ComfyUI (`http://localhost:8188/prompt`, `/queue`, `/history/{id}`) desde
`lib/comfyui_client.py`. Esto es deliberado: las generaciones de LTX duran
45 min/clip (~12 h totales) y el daemon `stage5_poll_videos.py` tiene que
poder sobrevivir al cierre de Claude Code y a desconexiones de red. Los
MCPs son síncronos y dependen del proceso del agente, así que **no son la
herramienta correcta para esa parte**.

Dicho esto, un MCP de ComfyUI puede ser útil **al margen** del pipeline:

- Lanzar workflows ad-hoc desde Claude Code mientras desarrollas.
- Inspeccionar la cola sin abrir el navegador.
- Probar prompts sueltos sin pasar por toda la pipeline.

---

## Opción recomendada: `lalanikarim/comfy-mcp-server`

Es el más mantenido y simple. Repo: https://github.com/lalanikarim/comfy-mcp-server

### Instalación

```bash
# Instálalo con uv o pipx (recomendado)
uvx --from comfy-mcp-server comfy-mcp-server --help
# o
pipx install comfy-mcp-server
```

### Configuración en Claude Code

Añade a tu `~/.claude/settings.json` (o al `settings.json` del proyecto):

```json
{
  "mcpServers": {
    "comfyui": {
      "command": "uvx",
      "args": ["--from", "comfy-mcp-server", "comfy-mcp-server"],
      "env": {
        "COMFY_HOST": "127.0.0.1",
        "COMFY_PORT": "8188",
        "COMFY_WORKFLOWS_DIR": "/Users/andresdiazmolins/code/ai/aspectados/automation/workflows"
      }
    }
  }
}
```

Reinicia Claude Code y deberían aparecer tools tipo `mcp__comfyui__submit_workflow`,
`mcp__comfyui__queue_status`, `mcp__comfyui__get_history`.

### Cuándo usarlo (y cuándo no)

| Usa el MCP para… | Usa la HTTP API (`lib/comfyui_client.py`) para… |
|---|---|
| Lanzar un workflow puntual desde una conversación | Encolar los 16 clips del pipeline |
| Mirar qué tiene ComfyUI en cola | Polling continuo durante 12 h |
| Probar un prompt antes de meterlo al guion | Cualquier cosa que tenga que sobrevivir a cerrar Claude Code |
| Inspección rápida | Reintentos automáticos por SQLite |

---

## Alternativas

Si por algún motivo el MCP de arriba no te encaja:

- **HTTP directa** (lo que hace el pipeline): usar `curl` o `httpie` contra
  `http://127.0.0.1:8188`. La doc no oficial pero útil:
  https://github.com/comfyanonymous/ComfyUI/blob/master/server.py
- **`comfy-cli`** (oficial-ish): `pip install comfy-cli`, da un CLI para
  gestionar ComfyUI. Útil pero no es un MCP.

---

## Si decides reemplazar la HTTP por MCP en el pipeline

No lo recomiendo (por las razones de arriba), pero si insistes:

1. Sustituye `lib/comfyui_client.py` por un cliente que hable con el MCP
   por stdio (más complicado: tienes que mantener el proceso vivo).
2. **No** uses MCP en `stage5_poll_videos.py`: ese daemon corre fuera de
   Claude Code; déjalo con la API HTTP.
3. Mantén el contrato de `QueueStatus`, `submit()`, `status()` y
   `collect_video_path()` para no tocar el resto del pipeline.
