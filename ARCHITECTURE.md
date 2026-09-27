# WallaBot — Arquitectura

> Este documento describe el diseño general de WallaBot: qué componentes existen,
> cómo se relacionan, y por qué se tomó cada decisión de diseño importante. Para
> el detalle de implementación línea a línea (herramientas exactas, formato de los
> eventos, prompt del agente, persistencia...) consulta [AGENT.md](AGENT.md).
>
> *Nota histórica: este documento describía originalmente un plan (Playwright como
> capa principal, un Agent SDK de Claude, una herramienta `submit_candidates`...).
> Durante la implementación varias de esas decisiones cambiaron por razones
> prácticas — están explicadas más abajo. Este documento refleja la arquitectura
> **real** del proyecto, no el plan inicial.*

## Objetivo

Un bot que recibe una petición en lenguaje natural (p. ej. *"CPU + placa base,
DDR4, por menos de 300€"* o *"un coche de gasolina fiable para ir a trabajar,
8000€"*), busca en Wallapop, razona sobre los resultados de varias búsquedas, y
devuelve candidatos que cumplen los requisitos — con la reputación del vendedor y
la ubicación ya verificadas.

## Componentes

La aplicación tiene tres capas, cada una con una responsabilidad clara:

```
  Navegador (chat + tablero UI)       static/index.html
        │  websocket
        ▼
  Servidor web                        server.py   (FastAPI + WebSocket)
        │  run_turn(session, texto, emit)
        ▼
  Agente (capa de razonamiento)       agent.py    (bucle de uso de herramientas, SDK de Anthropic)
        │  llama a herramientas
        ▼
  Capa de acceso al sitio             access.py + geo.py   (API de Wallapop vía httpx / Playwright)
```

### 1. Capa de acceso a Wallapop (`access.py`, `geo.py`)

Esta capa es **determinista y barata**: solo obtiene datos de Wallapop, sin
ningún coste de LLM. Expone funciones Python normales (no control de navegador
en crudo) para que la capa de razonamiento no tenga que preocuparse de cómo se
obtienen los datos, solo de qué hacer con ellos.

- **Dos modos de acceso, en orden de preferencia:**
  1. **Llamadas directas a la API interna de Wallapop** (vía `httpx`) — rápido,
     barato, sin renderizar ninguna página. Es una API no documentada, así que
     puede cambiar sin avisar; por eso existe el modo 2.
  2. **Playwright como respaldo** — si la llamada directa falla (bloqueo,
     cambio de la API, error de red), se repite la misma petición desde dentro
     de un navegador real, reutilizando la sesión guardada si existe.
- **Filtrado proactivo antes de que el agente vea nada:** en lugar de confiar en
  que el modelo note y descarte un anuncio problemático, la propia capa de
  acceso elimina de los resultados de búsqueda:
  - anuncios **huérfanos** (el vendedor ha borrado su cuenta — el enlace da 404),
  - anuncios **reservados** (no se pueden comprar),
  - anuncios **inalcanzables** (ni el vendedor hace envíos, ni están cerca de la
    ubicación del usuario).
- **Geocodificación offline** (`geo.py`): traduce "Bizkaia", "País Vasco" o
  "Bilbao" a coordenadas aproximadas, sin depender de ningún servicio externo,
  para poder centrar la búsqueda y calcular distancias.

### 2. Agente de razonamiento (`agent.py`)

Es el cerebro del proyecto: usa el bucle de uso de herramientas de Claude (vía
el SDK de Anthropic) como orquestador. Claude decide qué herramienta llamar a
continuación, observa el resultado, y decide si buscar de nuevo, refinar la
búsqueda, verificar un anuncio, o terminar con una recomendación.

- **Herramientas registradas** (ver [AGENT.md](AGENT.md) para el detalle
  completo de cada una): fijar los requisitos y el presupuesto, buscar en
  Wallapop, inspeccionar un anuncio en detalle, fijar la ubicación del usuario,
  actualizar el tablero de recomendaciones, y buscar en la web cuando haga
  falta verificar algo (comparativas, generación más reciente de un producto,
  compatibilidad).
- Claude asume los juicios que la propia búsqueda de Wallapop no puede hacer:
  entender qué significa "compatible con DDR4", repartir un presupuesto
  conjunto entre varias piezas, no repetir anuncios ya vistos, decidir cuándo
  conviene relanzar una búsqueda con otros términos, y verificar la reputación
  del vendedor antes de recomendar.
- El bucle está **acotado** (máximo de llamadas a herramientas por turno) para
  controlar el coste y el tiempo de ejecución, y **blindado** contra errores: un
  fallo de red o de la API nunca deja la conversación bloqueada.

### 3. Servidor web y visualizador (`server.py`, `static/index.html`)

Una capa fina que conecta el bucle del agente con el navegador:

- Un WebSocket por pestaña del navegador; cada mensaje del usuario dispara un
  turno del agente, y cada paso del agente (texto, log de actividad, tablero
  actualizado...) se retransmite al instante a la interfaz.
- La interfaz es un **chat** con un **tablero en vivo** al lado: a medida que el
  agente encuentra mejores opciones, el tablero se reemplaza — así se ve
  cómo "descarta" una opción y "añade" otra mejor, en tiempo real.
- Persistencia de sesiones: cada conversación se guarda en disco (incluyendo el
  historial completo de mensajes y herramientas usadas) y se puede reanudar
  desde un historial lateral.

## Flujo de una petición

1. El usuario escribe su petición (y, opcionalmente, presupuesto y provincia).
2. El agente entiende la petición y fija los requisitos; si algo esencial no
   está claro, pregunta antes de buscar.
3. Planifica una búsqueda inicial y llama a la herramienta de búsqueda.
4. Revisa los resultados: si son pocos, muy caros, o ambiguos, refina la
   búsqueda (otras palabras clave, separar en varias búsquedas, cargar más
   resultados...).
5. Para los anuncios más prometedores, consulta el detalle completo para
   confirmar especificaciones, compatibilidad, y reputación del vendedor.
6. Cuando lo necesita (por ejemplo, para comparar rendimiento entre dos
   modelos, o confirmar cuál es la generación más reciente de un producto),
   contrasta con una búsqueda web en lugar de fiarse solo de su memoria.
7. Actualiza el tablero con las mejores opciones encontradas hasta el momento —
   puede repetir este ciclo varias veces, mejorando el tablero conforme
   encuentra mejores anuncios.
8. Cuando está satisfecho, da un resumen final: qué recomienda, el total, y las
   ventajas/inconvenientes de cada alternativa.
9. El usuario puede seguir la conversación (pedir ajustes, otra ubicación,
   otro presupuesto...) sin perder el contexto.

## Por qué este diseño

- **La capa de acceso es determinista y barata.** Solo obtiene datos; no hay
  coste de LLM por cada página o cada anuncio consultado. Esto también permite
  filtrar de forma fiable (anuncios muertos, reservados, inalcanzables) con
  código normal, en vez de depender de que el modelo lo note.
- **Todo el razonamiento vive en un solo sitio.** El bucle de uso de
  herramientas de Claude concentra la interpretación de la petición, el
  refinamiento de búsquedas, y el juicio de compatibilidad — es más fácil
  ajustar el comportamiento del agente editando un único prompt que repartir
  esa lógica entre varios módulos.
- **El tablero es declarativo.** El agente envía el estado completo del tablero
  cada vez, y la interfaz simplemente lo vuelve a pintar. Esto hace que "añadir
  y quitar productos según encuentra mejores opciones" sea trivial de
  implementar y de entender.
- **Los fallos nunca bloquean la conversación.** Un error transitorio de red o
  de la API de Anthropic se captura, se muestra al usuario, y el chat queda
  listo para seguir — nunca se queda el botón de enviar bloqueado.
- **Acotar las iteraciones mantiene el coste y el tiempo predecibles.**
