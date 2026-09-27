# WallaBot — Implementación del agente

Este documento explica, con detalle de implementación, cómo funciona el agente
autónomo de WallaBot: su arquitectura, las herramientas que usa, el bucle de
razonamiento, el modelo del "tablero" en vivo, la persistencia, y cómo
ejecutarlo. Para una vista de más alto nivel (qué componentes existen y por qué
se diseñaron así) consulta [ARCHITECTURE.md](ARCHITECTURE.md).

## Visión general

WallaBot convierte una petición en lenguaje natural (*"el mejor PC gaming que
pueda conseguir por 1000€"*) en recomendaciones de compra concretas de segunda
mano en Wallapop. Funciona como un cazachollos paciente: averigua qué
necesitas, busca, compara precios, inspecciona los anuncios prometedores, y
monta una o varias "opciones de compra" en un tablero en vivo — actualizándolo
a medida que encuentra mejores ofertas.

Hay tres capas:

```
  Navegador (chat + tablero UI)      static/index.html
        │  websocket
        ▼
  Servidor web                        server.py   (FastAPI + WebSocket)
        │  run_turn(session, texto, emit)
        ▼
  Agente (bucle de razonamiento)      agent.py    (bucle de uso de herramientas del SDK de Anthropic)
        │  llama a herramientas
        ▼
  Capa de acceso al sitio             access.py   (API de Wallapop vía httpx / Playwright)
```

- **`access.py`** — capa de datos determinista y barata. Solo obtiene datos de
  Wallapop; no interviene ningún LLM. Sus dos funciones públicas se describen
  más abajo.
- **`agent.py`** — el cerebro. Dirige a Claude en un bucle de uso de
  herramientas, mantiene el estado de la sesión, y emite eventos a través de un
  callback.
- **`server.py`** — capa web fina. Un websocket por pestaña del navegador;
  reenvía los mensajes del usuario al agente y retransmite sus eventos a la
  página.
- **`static/index.html`** — el visualizador de chat + tablero (HTML/CSS/JS sin
  frameworks).

## Selección de modelo

Un desplegable junto a "Enviar" permite elegir el modelo por mensaje —
**Haiku**, **Sonnet** (por defecto) u **Opus** (`MODELS` en `agent.py`). La
elección se envía con cada mensaje y determina ese turno; el modelo usado en
cada mensaje queda registrado en la lista `models` de la sesión (alineada con
`messages`) como parte del registro. Los títulos de sesión siempre se generan
con una llamada barata a **Haiku** (`generate_title`), independientemente del
modelo seleccionado para la conversación.

## Por qué el SDK de Anthropic a secas (y no el Agent SDK de Claude)

El diseño original mencionaba el Agent SDK de Claude. En la práctica, el Agent
SDK de Python necesita tener instalado el binario de la CLI `claude`, algo que
esta máquina no tiene (y tampoco hay Node). El SDK de `anthropic` a secas:

- no necesita nada más que la clave de la API (ya está en `.env`),
- permite controlar el bucle a mano, así que se puede retransmitir cada token y
  cada llamada a herramienta directamente al websocket, y
- es exactamente el bucle de uso de herramientas que describía el diseño
  original.

Mismo comportamiento autónomo, con menos piezas moviéndose. El modelo por
defecto es **`claude-sonnet-5`**.

## Las herramientas que Claude puede llamar

Definidas en `agent.py` (`TOOLS`) y ejecutadas en `_dispatch_tool`. Cada
herramienta devuelve un resultado a Claude *y* además emite un evento hacia la
interfaz.

| Herramienta | Qué hace |
| --- | --- |
| `set_requirements(requirements, budget, summary)` | Registra la lista de requisitos derivada más el presupuesto. Se muestra en la tarjeta de Requisitos. Se llama una vez, al principio. |
| `search_wallapop(query, max_price, min_price, next_page)` | Busca en Wallapop. Devuelve ~40 resúmenes recortados + un token `next_page`. Pasa ese token de vuelta para "cargar más". Los anuncios muertos se filtran antes de llegar aquí (ver abajo). |
| `get_ad_details(item_id)` | Anuncio completo: flag `available`, descripción, todas las imágenes, reputación del vendedor (valoración, ventas, reseñas), última actualización. Se usa para verificar especificaciones y la confianza del vendedor. |
| `set_location(place)` | Registra la provincia/comunidad/ciudad del usuario (geocodificada vía `geo.py`) para centrar las búsquedas cerca de él y filtrar por anuncios alcanzables. Se llama cuando el usuario indica su ubicación por chat. |
| `update_board(options)` | Reemplaza el tablero entero con la mejor recomendación (o recomendaciones) actual. Los productos se agrupan por categoría; se pueden mostrar varias opciones de compra a la vez. |
| `web_search(query)` | Búsqueda web **del lado del servidor** de Anthropic. El agente la usa para comprobar especificaciones, compatibilidad, benchmarks, y qué modelos son los más recientes — *no* para buscar anuncios. Limitada a 5 usos por turno. |

`search_wallapop` y `get_ad_details` son envoltorios finos sobre `access.py`.
Como esas llamadas de red son síncronas, se ejecutan en un hilo aparte
(`asyncio.to_thread`) para no bloquear nunca el bucle de eventos ni el
websocket.

**Filtrado de anuncios muertos.** El índice de búsqueda de Wallapop puede
devolver anuncios cuyo vendedor ha borrado su cuenta; los datos del anuncio
siguen cargando, pero la página web pública da un 404 (un enlace muerto).
`search_wallapop` detecta estos casos —un vendedor borrado hace que
`/users/{id}` devuelva 404— y los descarta antes de que el agente los vea
siquiera (comprobación concurrente, con caché por id de vendedor).
`get_ad_details` también lleva un flag `available` como red de seguridad, y al
agente se le indica que nunca recomiende un anuncio donde `available` sea
falso.

**Filtrado de anuncios reservados.** Un anuncio marcado como "Reservado" no se
puede comprar, así que `search_wallapop` los descarta directamente — el agente
nunca los ve como candidatos, en vez de confiar en que se dé cuenta y los
descarte por su cuenta. `get_ad_details` también lleva un flag `reserved` como
red de seguridad (un anuncio puede reservarse entre la búsqueda y esta
llamada), y se le indica al agente que lo descarte de inmediato si es así.

**Filtrado por alcanzabilidad (ubicación).** Un anuncio no sirve de nada si
está lejos y solo admite recogida en persona. En cuanto se conoce la ubicación
del usuario —por el campo "Provincia" o por una llamada a `set_location`,
geocodificada a coordenadas vía `geo.py`— `search_wallapop` centra la búsqueda
ahí y solo conserva los anuncios **alcanzables**: los que el vendedor envía
(`user_allows_shipping`) O que están a menos de `REACH_KM` (150 km) del
usuario. Cada resultado lleva `shippable` y `distance_km`. La ubicación del
usuario se inyecta en el prompt del sistema en cada turno; si no se conoce, se
le indica al agente que la pregunte antes de fiarse de los resultados. La
provincia se guarda tanto por navegador como por sesión.

El campo "Provincia" tiene dos ayudas, ambas de mejor esfuerzo y siempre
editables:
- **Autocompletado** — `GET /api/provinces` (respaldado por
  `geo.list_provinces()`, las 50 provincias oficiales + Ceuta + Melilla)
  rellena un `<datalist>` nativo del navegador.
- **Sugerencia automática por IP** — al cargar la página, si el campo está
  vacío, el navegador consulta una API pública gratuita de geolocalización por
  IP (`ipapi.co`) y, si resuelve a una comunidad autónoma española, rellena una
  sugerencia inicial (traducida vía `geo.place_for_region_code`, código ISO
  3166-2:ES → nombre de lugar — la geolocalización por IP solo acierta a nivel
  de comunidad, no de provincia exacta). Nunca sobrescribe un valor ya
  existente (guardado, de una sesión reanudada, o ya escrito a mano), y falla
  en silencio si el servicio no está disponible.

`web_search` es diferente: es la herramienta integrada de Anthropic
(`web_search_20250305`), que se ejecuta en el lado de Anthropic, así que no hay
código propio que ejecutar para ella. Sus resultados llegan dentro del mensaje
del asistente como bloques `server_tool_use` / `web_search_tool_result`.
Cuando Anthropic pausa un turno de búsqueda largo, devuelve
`stop_reason == "pause_turn"`; el bucle simplemente vuelve a llamar para dejar
que continúe. El agente solo recurre a la búsqueda web cuando realmente no
está seguro — así que la mayoría de las ejecuciones no la usan en absoluto.

### El modelo del tablero

El tablero es deliberadamente **declarativo**: en cada llamada a
`update_board`, Claude envía el tablero *completo* y este reemplaza al
anterior. Esto es lo que hace natural el comportamiento de "añadir/quitar
productos según encuentra mejores opciones" — Claude simplemente envía el
mejor estado nuevo y la interfaz se vuelve a pintar.

```jsonc
{
  "options": [
    {
      "title": "Piezas por separado",
      "note": "Mejor rendimiento para el presupuesto",
      "recommended": true,
      "products": [
        { "category": "CPU", "item_id": "…", "title": "Ryzen 5 5600", "price": 75,
          "url": "…", "thumbnail": "…", "note": "AM4, buen vendedor 4.9★" },
        { "category": "GPU", "…": "…" }
      ]
    },
    { "title": "PC completo", "products": [ { "category": "Full PC", "…": "…" } ] }
  ]
}
```

Tener varias opciones permite al agente presentar alternativas reales: todas
las piezas por separado, un único PC ya montado, o un combo CPU+placa base más
el resto por separado. `thumbnail` y `url` se rellenan automáticamente a
partir de los anuncios que el agente ya consultó, así Claude no tiene que
(ni puede, de forma fiable) proporcionar URLs de imagen por su cuenta.
`recommended: true` en una única opción activa la cinta "★ Recomendado" en la
interfaz, en vez de que el agente lo escriba en el título.

## El bucle de razonamiento (`run_turn`)

Un "turno" es todo lo que hace el agente para responder a un mensaje del
usuario.

1. Añade el mensaje del usuario al historial de la conversación.
2. **Retransmite** en streaming una respuesta de Claude
   (`client.messages.stream`). Los tokens de texto se emiten en vivo como
   eventos `text_delta`.
3. Guarda el turno completo del asistente (texto + cualquier bloque de uso de
   herramienta) en el historial.
4. Si Claude pidió herramientas (`stop_reason == "tool_use"`), ejecuta cada una
   vía `_dispatch_tool`, recoge los resultados, los añade como un mensaje
   `tool_result`, y vuelve al paso 2.
5. Si Claude simplemente respondió (sin herramientas), el turno termina.

El bucle está acotado a `MAX_TOOL_CALLS` (40) por turno. Al alcanzar el
límite, se le pide al agente que dé una recomendación final **sin** usar más
herramientas (`force_finish`), para que una ejecución nunca pueda quedarse en
bucle indefinidamente ni consumir tokens sin control.

### Manejo de fallos — el cuadro de texto nunca se queda bloqueado

`run_turn` es un envoltorio fino de `try/except/finally` alrededor del bucle
real (`_run_turn`). Si algo falla a mitad de turno —un error de red pasajero,
un error de la API de Anthropic (límite de peticiones, sobrecarga momentánea,
...), un bloque de contenido inesperado— se captura, se muestra al usuario
como una burbuja de chat `error` diferenciada (⚠️, en rojo, nunca marcada como
la recomendación "final"), y se registra en el log. El bloque `finally`
**siempre** emite `{"type": "status", "state": "idle"}`, pase lo que pase.
Esto importa porque ese evento es el que reactiva el cuadro de texto (botón
Enviar + input) en la interfaz — sin este envoltorio, una excepción sin
capturar aquí se propagaría fuera del manejador del websocket en `server.py`,
matando la conexión y dejando el botón de enviar bloqueado para siempre hasta
recargar la página.

El comportamiento (preguntar si algo es ambiguo, buscar como lo haría una
persona, verificar especificaciones y vendedor, respetar el presupuesto,
mostrar varias opciones) lo dirige `SYSTEM_PROMPT` en `agent.py` — es el
primer sitio a mirar si quieres cambiar cómo actúa el agente.

### Preguntas de aclaración

La interfaz es un chat, así que aclarar dudas no necesita ningún mecanismo
especial: si la petición es genuinamente ambigua, el agente simplemente
responde con una pregunta y termina el turno. Lo siguiente que escribas
continúa la misma sesión (el historial se conserva).

### Gestión del presupuesto

El presupuesto viene del campo opcional de la interfaz (se añade al mensaje) o
del propio texto — el agente extrae cualquier límite indicado ("máximo 300€",
"menos de 1000€", un límite combinado para varias piezas) al campo `budget`.
Si no se da presupuesto, las instrucciones del agente son encontrar la opción
**más barata** que cumpla todos los requisitos.

El presupuesto se trata como **aproximado**: una opción que se pase un poco
(~5-10%) se puede mostrar igualmente si es un chollo, en vez de descartarla por
unos pocos euros. Cada tarjeta de opción muestra una insignia verde/roja
comparando su total con el presupuesto, para que el usuario vea exactamente
cómo queda cada una.

## Eventos (servidor → navegador)

El agente solo habla con el navegador a través de `emit(evento)`. Tipos de
evento:

| `type` | Payload | Efecto en la interfaz |
| --- | --- | --- |
| `session` | `id`, `title` | Guarda el id de sesión (URL + localStorage). |
| `status` | `state` (`thinking`/`idle`) | Punto de estado en la cabecera; `idle` reactiva el cuadro de texto (ver Manejo de fallos arriba). |
| `text_delta` | `text` | Añade texto a la burbuja de chat actual. |
| `message_done` | `model` | Cierra la burbuja actual; también lleva la etiqueta del modelo para marcarla con un chip. |
| `log` | `text` | Añade una línea al panel de Actividad. |
| `requirements` | `requirements`, `budget`, `summary` | Pinta la tarjeta de Requisitos. |
| `board` | `board` | Vuelve a pintar las tarjetas de opciones. |
| `restore` | `chat` (con `model` por turno) | Al reanudar, repinta la conversación anterior con los chips de modelo. |
| `error` | `text` | Pinta una burbuja ⚠️ diferenciada para un turno que falló (nunca se marca como "final"). |

Del cliente al servidor: `{ "text": "...", "budget": 1000 | null, "model": "haiku"|"sonnet"|"opus", "location": "..." }`
para enviar un mensaje, o `{ "type": "stop" }` para cancelar el turno en curso.

El turno se ejecuta como una tarea en segundo plano dentro del manejador del
websocket, para que el bucle de recepción pueda seguir leyendo; un mensaje
`stop` cancela esa tarea, y el manejador informa de la cancelación y reactiva
el cuadro de texto.

### Detalles de la interfaz

- **Chip de modelo** — cada mensaje del asistente muestra qué modelo lo
  generó (a partir de `message_done.model`, o de `chat_turns` al reanudar).
- El panel de **Actividad** se pliega automáticamente al terminar un turno,
  mostrando un resumen de una línea (`🔎 N búsquedas · 🔬 N anuncios · 🌐 N web`);
  su cabecera lo despliega/pliega a mano.
- Las **tarjetas del tablero** muestran una cinta `★ Recomendado` en la opción
  con `recommended: true`, una insignia de confianza del vendedor
  (`⭐ valoración · N ventas`) y una etiqueta `Reservado`. Los datos de
  vendedor/reservado se enriquecen en el servidor, dentro de `update_board`, a
  partir de lo que el agente ya consultó.

## Persistencia

Cada sesión se guarda en `sessions/<id>.json` (se reescribe tras cada paso).
Recoge la **trayectoria completa** de la ejecución — suficiente para
reproducirla, depurarla, o más adelante usarla como datos de entrenamiento:

- `default_model`, `system_prompt` — cómo estaba configurado el agente.
- `messages` — la **conversación completa**: cada mensaje del usuario, cada
  mensaje del asistente (texto + bloques de uso de herramienta), y cada
  resultado de herramienta. Es la trayectoria completa de entradas y salidas.
  Los objetos de bloque de contenido del SDK se serializan vía
  `_json_default` (que llama a su `model_dump` de pydantic).
- `models` — el id del modelo usado en cada mensaje (mismo orden/longitud que
  `messages`).
- `requirements`, `budget` — el plan.
- `board` y `board_history` — el tablero final más cada instantánea anterior.
- `accessed_items` — cada anuncio que el agente vio (resúmenes de búsqueda +
  detalles completos), indexado por id de anuncio.
- `logs` — un registro de pasos con marca de tiempo.

Como `messages` es la conversación exacta de Anthropic (llamadas a
herramientas y sus resultados incluidos), cada archivo de sesión se puede usar
directamente como ejemplo de entrenamiento de uso de herramientas.

Junto a cada sesión se escribe un `<id>.meta.json` diminuto (id, título,
fecha de actualización) para que la lista del historial cargue sin tener que
analizar los archivos grandes. Las sesiones antiguas sin archivo de metadatos
se rellenan la primera vez que se listan.

## Sesiones y reanudación

Cada pestaña del navegador está ligada a un id de sesión, guardado en la URL
(`?session=<id>`) y en `localStorage`, así que recargar la página continúa
donde lo dejaste. La interfaz tiene una barra lateral de historial plegable:

- `GET /api/sessions` → la lista (más reciente primero) que se muestra en la
  barra lateral.
- El websocket acepta `/ws?session=<id>`: el servidor llama a
  `load_session`, reconstruye el objeto `Session` en memoria, y emite
  `restore` (la conversación anterior) más los `requirements` y `board`
  guardados, para que la página quede exactamente como la dejaste.
- "＋ Nueva" abre un websocket sin el parámetro `session` → una sesión nueva.

Reanudar una sesión reenvía la conversación guardada al modelo, así que las
preguntas de seguimiento mantienen el contexto completo. `_sanitize_messages`
reconstruye el historial en bloques válidos para la API (conserva
texto/uso de herramienta/resultado de herramienta, descarta bloques de
razonamiento interno y de búsqueda web del servidor) y recorta el final hasta
una respuesta completa del asistente, para poder añadir un nuevo mensaje del
usuario sin problemas.

## Cómo ejecutarlo

Requisitos previos: un entorno virtual con las dependencias instaladas, tu
`ANTHROPIC_API_KEY` en `.env`, y opcionalmente una sesión de Wallapop guardada
vía `python login.py` (los endpoints públicos de búsqueda/detalle funcionan de
forma anónima).

**Aplicación web (chat + visualizador):**

```bash
.venv/bin/python -m uvicorn server:app --reload
# abre http://localhost:8000
```

**Línea de comandos (sin navegador, imprime los eventos):**

```bash
.venv/bin/python agent.py "Un PC gaming por menos de 600€"
```

## Archivos

| Archivo | Función |
| --- | --- |
| `access.py` | Capa de datos de Wallapop: `search_wallapop`, `get_ad_details`. |
| `geo.py` | Geocodificación offline de provincias/comunidades españolas + cálculo de distancia, para el filtrado por alcanzabilidad. |
| `agent.py` | El agente: herramientas, estado de sesión, el bucle de uso de herramientas, y el arnés de línea de comandos. |
| `server.py` | La app de FastAPI: sirve la interfaz y ejecuta el agente sobre un websocket. |
| `static/index.html` | La interfaz web de chat + tablero. |
| `login.py` | Login manual único en Wallapop → `storage_state.json`. |
| `sessions/` | Registros de ejecuciones guardadas (excluido de git). |

## Ajustes

- **Cómo se comporta el agente** → `SYSTEM_PROMPT` en `agent.py`.
- **Coste / duración de una ejecución** → `MAX_TOOL_CALLS`, `MAX_TOKENS` en
  `agent.py`.
- **Cuántos datos recibe Claude de cada búsqueda** → la lista `trimmed` en
  `_dispatch_tool` (actualmente los 30 primeros anuncios, descripciones
  recortadas a 250 caracteres).
- **Ubicación por defecto de la búsqueda** → `DEFAULT_LATITUDE` /
  `DEFAULT_LONGITUDE` en `access.py` (Madrid, por defecto).
