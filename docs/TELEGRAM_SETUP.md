# Configurar el bot de Telegram para notificaciones

La pipeline manda pings a un bot de Telegram en cada transición importante
(clip terminado, clip fallado, run completo, preview listo). Configurar el bot
te lleva **~3 minutos** y es gratuito.

---

## 1. Crear el bot

1. En Telegram, busca **@BotFather** y abre el chat.
2. Manda `/newbot`.
3. BotFather te pide un nombre legible (puede tener espacios): por ejemplo
   `Aspectados Reels Notifier`.
4. Luego te pide un **username** que tiene que acabar en `bot`: por ejemplo
   `aspectados_reels_bot`. Tiene que ser único en todo Telegram, prueba
   variantes si te lo rechaza.
5. BotFather te devuelve un mensaje con el **token**, algo así:
   ```
   123456789:AAH8Xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   Cópialo y pégalo en `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:AAH8Xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

> ⚠️ El token es como una contraseña. Nunca lo commitees ni lo compartas.
> Si se te escapa, vuelve a hablar con BotFather y manda `/revoke` para
> generar uno nuevo.

---

## 2. Obtener tu `chat_id` (a dónde te escribirá el bot)

El bot necesita saber **a qué chat** mandarte los mensajes. Lo más sencillo es
que sea tu propio chat privado con el bot:

1. Busca el username del bot que creaste (`@aspectados_reels_bot`) en Telegram.
2. Pulsa **Start** (o manda cualquier mensaje, p. ej. "hola").
3. Abre en tu navegador esta URL, sustituyendo `<TOKEN>` por tu token:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Verás un JSON. Busca `"chat":{"id":...}`. Ese número es tu `chat_id`.
   Si es un usuario es un entero positivo (p. ej. `7654321`). Si fuera un
   grupo sería negativo.
5. Cópialo a `.env`:
   ```env
   TELEGRAM_CHAT_ID=7654321
   ```

> Si `getUpdates` devuelve `{"ok":true,"result":[]}`, es que aún no mandaste
> ningún mensaje al bot. Mándale uno y refresca la URL.

---

## 3. Probar la notificación

Con `.env` ya relleno:

```bash
cd automation
source venv/bin/activate
python -c "from lib import telegram; telegram.send(('test','manual',), 'Ping desde el pipeline ✅')"
```

Deberías recibir el mensaje en tu chat al instante. Si no llega:

- Revisa que `TELEGRAM_ENABLED=1`.
- Comprueba el token con:
  ```bash
  curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe
  ```
- Asegúrate de que **iniciaste el chat** con el bot (paso 2.2). Si no
  empezaste tú, Telegram no le deja escribir.

---

## 4. Qué te va a enviar

| Evento | Mensaje |
|---|---|
| Stage 2 (imágenes) terminado | `🖼️ Stage 2 complete — run X. images ✅ N ❌ M` |
| Stage 4 (encolado) terminado | `🎬 Stage 4 complete — run X. queued ✅ N ❌ M` |
| Clip empieza en ComfyUI | `⏳ Clip 03 started — run X` |
| Clip terminado | `✅ Clip 03 done — run X` |
| Clip fallado | `❌ Clip 03 FAILED — run X` + extracto del error |
| Run completo | `🏁 Run X complete · ✅ 16 ❌ 0 / 16` |
| Preview montado | `🎞️ Preview ready — run X. /path/to/preview.mp4` |

Cada mensaje se desduplica por `event_key` en la tabla `notifications` de
SQLite, así que si relanzas un stage no te bombardea otra vez con los mismos
pings que ya recibiste.

---

## 5. Silenciar temporalmente

Si quieres correr la pipeline en silencio (p. ej. tests):

```env
TELEGRAM_ENABLED=0
```

Los stages siguen escribiendo en `events` y `notifications` pero saltan el
`POST` a Telegram.
