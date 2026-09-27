"""WallaBot autonomous shopping agent.

This is the reasoning/iteration layer. It drives Claude (via the Anthropic SDK's
tool-use loop) as an autonomous second-hand shopper on Wallapop: it turns a
natural-language request into a list of requirements, searches like a real
bargain hunter, inspects promising listings, and builds up one or more
"build options" on a live board that the web UI renders.

Design notes:
  - We use the plain Anthropic SDK (not the Agent SDK) so the whole loop runs in
    this process with no external CLI, and so we can stream every token and every
    tool call straight to the web UI.
  - The access layer (access.py) does the actual Wallapop fetching. Its functions
    are wrapped here as Claude tools.
  - The agent talks to the outside world through an async ``emit`` callback. The
    web server passes one that forwards events over a websocket; the CLI test
    passes one that just prints.
"""

import asyncio
import json
import os
import time
import uuid

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

import access
import geo

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Models the user can pick from in the UI dropdown (key -> API id).
MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}
MODEL = MODELS["sonnet"]  # default agent model
# Short display label per API id, e.g. "claude-sonnet-5" -> "Sonnet".
MODEL_LABELS = {api_id: key.capitalize() for key, api_id in MODELS.items()}
# Cheap, fast model used only to generate short session titles.
TITLE_MODEL = MODELS["haiku"]
MAX_TOKENS = 8000
# Safety cap on tool calls per user message, so a run can't loop forever / burn
# unbounded tokens. One "turn" = everything the agent does to answer one message.
MAX_TOOL_CALLS = 40

# Anthropic's server-side web search tool. Runs on Anthropic's side (no code to
# implement); the agent uses it to check specs, compatibility, benchmarks and
# current-generation models — NOT to find listings. Capped to control cost.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")

client = AsyncAnthropic()


def _json_default(obj):
    """JSON encoder fallback for Anthropic SDK objects (pydantic models).

    Lets us serialise the raw message history — which contains SDK content-block
    objects (text, tool_use, web-search results, etc.) — into plain JSON.

    Args:
        obj: An object json.dump can't handle natively.

    Returns:
        A JSON-serialisable representation of the object.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return str(obj)


async def generate_title(text):
    """Generate a short 4-5 word session title from the user's request (Haiku).

    Args:
        text (str): The user's first message.

    Returns:
        str: A concise title. Falls back to a truncated request on any error.
    """
    fallback = text.split("\n")[0][:60]
    try:
        resp = await client.messages.create(
            model=TITLE_MODEL,
            max_tokens=30,
            messages=[{
                "role": "user",
                "content": (
                    "Genera un título muy corto (máximo 4-5 palabras) que resuma "
                    "esta petición de compra, en su mismo idioma. Responde SOLO con "
                    f"el título, sin comillas ni punto final.\n\nPetición: {text}"
                ),
            }],
        )
        title = "".join(b.text for b in resp.content if b.type == "text").strip().strip('"')
        return title[:60] or fallback
    except Exception:
        return fallback


def _btype(block):
    """Return a content block's type, whether it's an SDK object or a dict."""
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _api_messages(messages):
    """Return the message history with `thinking` blocks removed, for the API.

    Thinking blocks carry a model-specific signature; stripping them before each
    request keeps the conversation replayable even when the user switches models
    between messages. The stored history (session.messages) keeps them for the
    training record.

    Args:
        messages (list): The in-memory message history.

    Returns:
        list: A copy safe to send as the `messages` argument.
    """
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [b for b in content if _btype(b) != "thinking"]
        out.append({"role": m["role"], "content": content})
    return out


SYSTEM_PROMPT = """\
You are WallaBot, an autonomous second-hand shopping assistant for Wallapop (a \
Spanish marketplace). Prices are in euros. You behave like a savvy, patient \
bargain hunter who really wants to find the user the best deal.

## Your workflow

1. UNDERSTAND. Read the user's request. If something essential to the \
recommendation is genuinely unclear (e.g. the intended use, a must-have feature, \
or a wildly open budget), ask a SHORT clarifying question and stop — do not call \
tools until the user answers. Otherwise, don't nag: make sensible assumptions and \
proceed.

2. SET REQUIREMENTS. Call `set_requirements` once with a concrete checklist \
derived from the request (use case, budget, must-haves, constraints). ALWAYS put \
any spending limit the user mentions into the `budget` field as a number in euros \
— no matter how it's phrased ("máximo 300€", "por 300€", "menos de 1000€", "un \
coste de 1000€", or a single combined limit covering several parts). The budget \
is the target TOTAL for one build option. Set budget to null ONLY if the user \
truly gives no figure at all; in that case aim for the CHEAPEST option that meets \
every requirement.

3. SEARCH LIKE A HUMAN. First, briefly brainstorm the FULL candidate space before \
searching — don't anchor on the first platform/brand that comes to mind. \
Especially for "best / most powerful / fastest possible" requests, list options \
across BOTH brands and MULTIPLE product generations that fit the constraints (e.g. \
for a powerful DDR4 CPU: AMD AM4 Ryzen 5000 AND Intel 12th/13th/14th gen on DDR4 \
boards), then search for the strongest candidates from each. Use natural Spanish \
keywords (e.g. "placa base am4", "i7 13700k ddr4", "pc gaming rtx 3060"). Compare \
prices. If results are poor or too expensive, refine: try other keywords, load \
more pages via `next_page`, split a combined need into separate searches, or hunt \
for especially cheap listings. Don't settle for the first thing you see.

4. VERIFY. For promising listings, call `get_ad_details` to confirm specs and \
compatibility from the full description, and to check the seller (prefer higher \
rating and more sales). search_wallapop already excludes reserved listings, but \
if `get_ad_details` ever returns `reserved: true` or `available: false` (reserved \
or dead in the moment between search and this check), NEVER recommend it — drop \
it and, if it was already on the board, replace it. Use your own hardware \
knowledge for \
compatibility (CPU socket like AM4/AM5/LGA1700, DDR4 vs DDR5, PSU wattage, form \
factor, etc.). When you're unsure about a spec, a compatibility detail, which \
generation/model is newest, or which of two products performs better for the \
money, use `web_search` to check (benchmarks, compatibility notes, current market \
prices). In particular, whenever the request hinges on a superlative ("the most \
powerful/best/fastest") or on ranking options across different brands or \
generations, DO a web_search to ground your ranking in real benchmarks/reviews \
before deciding — don't rely on memory alone. Do NOT use web_search to find \
listings — that's what search_wallapop is for.

5. BUILD THE BOARD. Call `update_board` to show the user your current best \
recommendation. Group products by category (CPU, Motherboard, GPU, RAM, Storage, \
PSU, Case, ...). When it makes sense, show MULTIPLE options side by side, e.g.:
   - all parts bought separately,
   - a single prebuilt/full PC listing,
   - a CPU+motherboard combo listing plus the remaining parts separately.
Send the FULL board every time — it replaces the previous one. As you find better \
or cheaper listings, update the board again (dropping the old pick, adding the new \
one). It is normal to update the board several times as you iterate.
Keep option titles and notes short and descriptive — do NOT write prices or totals \
in them, and do NOT write "(recomendado)" in the title: instead set \
`recommended: true` on exactly ONE option (your top pick) and the UI highlights it. \
The UI computes and shows each option's total and whether it fits the budget, so a \
price in the title only risks disagreeing with the real sum. It's fine for an \
option to be slightly over budget if it's a great deal; the UI flags how each one \
compares.

6. TREAT THE BUDGET AS APPROXIMATE. Aim for the budget, but it's a guideline, not \
a hard wall: an option that lands a little over (roughly up to ~5-10%) is fine to \
show if it's a great deal — don't discard an excellent pick over a few euros. The \
UI shows how each option compares. With no budget, minimise total cost while \
meeting all requirements.

7. FINISH. When you're satisfied, write a short final summary in chat: what you \
recommend, the totals, and any tradeoffs.

## Style
- Narrate briefly what you're doing as you go (a sentence before a batch of \
searches), so the user can follow your reasoning in the chat.
- Reply in the same language the user writes in — this is nearly always Spanish. \
ALL text you show the user must be in that language, with no exceptions: this \
includes web_search findings, benchmark figures, and quotes from English sources. \
NEVER paste raw English sentences from a search result into your reply (e.g. \
quoting "X outperforms Y by 28% based on our aggregate benchmark results" \
verbatim) — translate or paraphrase every fact into Spanish before writing it.
- Be efficient: searches cost time and money, so don't run redundant ones.
"""


# --- Tool schemas (Anthropic tool-use format) --------------------------------

TOOLS = [
    {
        "name": "set_requirements",
        "description": (
            "Record the structured checklist of requirements derived from the "
            "user's request, plus the budget. Call once, early, after you "
            "understand the goal. Shown to the user in the UI."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete requirements, one per string.",
                },
                "budget": {
                    "type": ["number", "null"],
                    "description": "Total budget in euros, or null if none given.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-sentence restatement of the goal.",
                },
            },
            "required": ["requirements", "summary"],
        },
    },
    {
        "name": "set_location",
        "description": (
            "Record where the user is (province, region or city in Spain), so "
            "searches are centered near them and filtered to listings they can "
            "actually get. Call this when the user tells you their location in "
            "chat. Returns whether the place was recognised."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "e.g. 'Bizkaia', 'País Vasco', 'Bilbao'."},
            },
            "required": ["place"],
        },
    },
    {
        "name": "search_wallapop",
        "description": (
            "Search Wallapop listings. Returns up to ~40 result summaries plus a "
            "next_page token. Results are already filtered to listings the user "
            "can get (shippable or near them) once their location is known, and "
            "each item includes `shippable` and `distance_km`. To load MORE "
            "results of the same search (the site's 'load more'), call again with "
            "that next_page token. Use natural Spanish keywords."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords."},
                "max_price": {"type": ["number", "null"]},
                "min_price": {"type": ["number", "null"]},
                "next_page": {
                    "type": ["string", "null"],
                    "description": "Pagination token from a previous search result.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_ad_details",
        "description": (
            "Fetch full details of one listing by its item id (from search "
            "results): full description, all image URLs, seller reputation "
            "(rating, number of sales, reviews) and last update. Use to verify "
            "specs/compatibility and seller trust before recommending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "update_board",
        "description": (
            "Replace the visual board shown to the user with your current best "
            "recommendation(s). Send the FULL board each time; it replaces the "
            "previous one. Group products by category and show one or more build "
            "options."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "description": "One or more build options to show.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "e.g. 'Parts bought separately' or 'Prebuilt PC'.",
                            },
                            "note": {
                                "type": "string",
                                "description": "Short rationale / tradeoff for this option.",
                            },
                            "recommended": {
                                "type": "boolean",
                                "description": "Set true on exactly ONE option — your top pick — to highlight it.",
                            },
                            "products": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "category": {
                                            "type": "string",
                                            "description": "e.g. CPU, Motherboard, GPU, RAM, PSU, Case, Full PC, CPU+Motherboard.",
                                        },
                                        "item_id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "price": {"type": "number"},
                                        "url": {"type": "string"},
                                        "thumbnail": {"type": ["string", "null"]},
                                        "note": {
                                            "type": "string",
                                            "description": "Why this listing (specs, seller rating, price).",
                                        },
                                    },
                                    "required": ["category", "title", "price"],
                                },
                            },
                        },
                        "required": ["title", "products"],
                    },
                }
            },
            "required": ["options"],
        },
    },
]


# --- Session state -----------------------------------------------------------

class Session:
    """Holds all state for one shopping conversation.

    Attributes:
        id (str): Unique session id (also the persistence filename).
        messages (list): The Anthropic message history (kept in memory only).
        requirements (list[str]): The derived requirement checklist.
        budget (float | None): Total budget in euros, or None.
        board (dict): The current board ({"options": [...]}).
        board_history (list[dict]): Every board snapshot, in order.
        accessed_items (dict): item_id -> the fullest data we've seen for it.
        logs (list[dict]): Timestamped step log.
        title (str | None): Short label (first user message) for the history list.
    """

    def __init__(self, session_id):
        self.id = session_id
        self.messages = []
        self.requirements = []
        self.budget = None
        self.board = {"options": []}
        self.board_history = []
        self.accessed_items = {}
        self.logs = []
        self.title = None
        self.models = []  # model id responsible for each entry in self.messages
        # User location (for reachability filtering): resolved coords + label.
        self.user_lat = None
        self.user_lon = None
        self.user_place = None

    def add_message(self, message, model):
        """Append a message and record which model was responsible for it.

        Args:
            message (dict): The message to add to the history.
            model (str): The model id in use for this turn.
        """
        self.messages.append(message)
        self.models.append(model)

    def set_location(self, place):
        """Resolve and store the user's location for reachability filtering.

        Args:
            place (str): A province/region/city name.

        Returns:
            bool: True if the place was recognised and stored, else False.
        """
        coords = geo.geocode(place)
        if not coords:
            return False
        self.user_lat, self.user_lon = coords
        self.user_place = place
        return True

    def log(self, text):
        """Append a timestamped entry to the step log.

        Args:
            text (str): Human-readable description of the step.
        """
        self.logs.append({"time": time.strftime("%H:%M:%S"), "text": text})

    def save(self):
        """Persist the full trajectory of the session to disk.

        Saves everything needed to understand or later train on the run: the
        complete conversation (every input and output, including tool calls and
        their results), the system prompt and model, plus the derived plan, board,
        accessed items and step log. The raw SDK objects in the message history are
        serialised via _json_default.
        """
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        updated = time.time()
        record = {
            "id": self.id,
            "title": self.title,
            "updated": updated,
            "user_place": self.user_place,
            "default_model": MODEL,
            "system_prompt": SYSTEM_PROMPT,
            # The complete conversation: user messages, assistant messages (text +
            # tool_use), and tool results — the full input/output trajectory.
            "messages": self.messages,
            # Per-message model (same length/order as messages).
            "models": self.models,
            "requirements": self.requirements,
            "budget": self.budget,
            "board": self.board,
            "board_history": self.board_history,
            "accessed_items": self.accessed_items,
            "logs": self.logs,
        }
        path = os.path.join(SESSIONS_DIR, f"{self.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=_json_default)
        # Also write a tiny meta file so the history list loads fast without
        # parsing the (potentially large) full session files.
        meta = {"id": self.id, "title": self.title, "updated": updated}
        with open(os.path.join(SESSIONS_DIR, f"{self.id}.meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)


def new_session():
    """Create a fresh Session with a unique id.

    Returns:
        Session: A new, empty session.
    """
    return Session(uuid.uuid4().hex[:12])


def _sanitize_messages(raw, models=None):
    """Rebuild a saved message history into a form safe to replay to the API.

    Keeps only the block types the Messages API accepts as input (text, tool_use,
    tool_result) and rebuilds them as minimal dicts. Thinking and server-side
    web-search blocks are dropped. Finally, trailing messages are trimmed so the
    history ends on a completed assistant reply (no dangling tool_use), which is
    required before appending a new user message. The per-message model list is
    kept aligned throughout.

    Args:
        raw (list): The saved "messages" list from a session file.
        models (list | None): The saved per-message model list (same length).

    Returns:
        tuple[list, list]: (clean messages, aligned model list).
    """
    models = models or []
    out, out_models = [], []
    for i, m in enumerate(raw):
        mdl = models[i] if i < len(models) else None
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            out_models.append(mdl)
            continue
        blocks = []
        for b in content or []:
            t = b.get("type")
            if t == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif t == "tool_use":
                blocks.append({"type": "tool_use", "id": b.get("id"),
                               "name": b.get("name"), "input": b.get("input", {})})
            elif t == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": b.get("tool_use_id"),
                               "content": b.get("content", "")})
        if blocks:
            out.append({"role": role, "content": blocks})
            out_models.append(mdl)

    # Trim the tail down to a clean assistant reply so we can resume cleanly.
    while out:
        last = out[-1]
        has_tool_use = isinstance(last["content"], list) and any(
            b.get("type") == "tool_use" for b in last["content"]
        )
        if last["role"] == "user" or has_tool_use:
            out.pop()
            out_models.pop()
            continue
        break
    return out, out_models


def chat_turns(messages, models=None):
    """Extract the visible chat (user + assistant text) from a message history.

    Args:
        messages (list): A (sanitized) message history.
        models (list | None): Per-message model ids (same length as messages),
            used to label assistant turns with the model that produced them.

    Returns:
        list[dict]: [{"role", "text", "model"?}, ...] for the UI to re-render when
            resuming a session.
    """
    models = models or []
    turns = []
    for i, m in enumerate(messages):
        if m["role"] == "user" and isinstance(m["content"], str):
            # Drop the "(My budget is €X.)" line the server appends, for display.
            text = m["content"].split("\n\n(My budget is")[0]
            turns.append({"role": "user", "text": text})
        elif m["role"] == "assistant" and isinstance(m["content"], list):
            text = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
            if text.strip():
                mdl = models[i] if i < len(models) else None
                turns.append({"role": "assistant", "text": text,
                              "model": MODEL_LABELS.get(mdl, mdl)})
    return turns


def load_session(session_id):
    """Load a saved session from disk so its conversation can be continued.

    Args:
        session_id (str): The session id.

    Returns:
        Session | None: The restored session, or None if it doesn't exist.
    """
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    s = Session(session_id)
    s.title = data.get("title")
    s.requirements = data.get("requirements", [])
    s.budget = data.get("budget")
    s.board = data.get("board", {"options": []})
    s.board_history = data.get("board_history", [])
    s.accessed_items = data.get("accessed_items", {})
    s.logs = data.get("logs", [])
    if data.get("user_place"):
        s.set_location(data["user_place"])
    s.messages, s.models = _sanitize_messages(data.get("messages", []), data.get("models"))
    return s


def _meta_from_full(session_id):
    """Build (and cache) a meta record for a session that has no meta file yet.

    Used to backfill sessions created before meta files existed, so they still
    show up in the history list. Derives the title from the stored title or the
    first user message, and the timestamp from the file's mtime.

    Args:
        session_id (str): The session id.

    Returns:
        dict | None: {"id", "title", "updated"} or None if unreadable.
    """
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    title = data.get("title")
    if not title:
        for entry in data.get("logs", []):
            if entry.get("text", "").startswith("User:"):
                title = entry["text"][5:].strip().split("\n")[0][:80]
                break
    meta = {"id": session_id, "title": title or session_id,
            "updated": data.get("updated") or os.path.getmtime(path)}
    with open(os.path.join(SESSIONS_DIR, f"{session_id}.meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return meta


def list_sessions():
    """List saved sessions for the history sidebar, newest first.

    Returns:
        list[dict]: [{"id", "title", "updated"}], sorted by updated descending.
    """
    if not os.path.isdir(SESSIONS_DIR):
        return []
    names = os.listdir(SESSIONS_DIR)
    items = {}
    for name in names:
        if name.endswith(".meta.json"):
            try:
                with open(os.path.join(SESSIONS_DIR, name), "r", encoding="utf-8") as f:
                    m = json.load(f)
                items[m["id"]] = m
            except Exception:
                continue
    # Backfill any full session file that has no meta yet (older sessions).
    for name in names:
        if name.endswith(".json") and not name.endswith(".meta.json"):
            sid = name[:-5]
            if sid not in items:
                meta = _meta_from_full(sid)
                if meta:
                    items[sid] = meta
    return sorted(items.values(), key=lambda m: m.get("updated") or 0, reverse=True)


# --- Tool dispatch -----------------------------------------------------------

async def _dispatch_tool(name, args, session, emit):
    """Execute one tool call, apply side effects, and return a result for Claude.

    The Wallapop network calls are synchronous, so they run in a worker thread
    (asyncio.to_thread) to avoid blocking the event loop / websocket.

    Args:
        name (str): Tool name.
        args (dict): Tool input as provided by Claude.
        session (Session): The current session (mutated in place).
        emit (callable): Async callback to push UI events.

    Returns:
        dict: The tool result to hand back to Claude.
    """
    if name == "set_requirements":
        session.requirements = args.get("requirements", [])
        session.budget = args.get("budget")
        await emit({
            "type": "requirements",
            "requirements": session.requirements,
            "budget": session.budget,
            "summary": args.get("summary", ""),
        })
        session.log(f"Requirements set ({len(session.requirements)}), budget={session.budget}")
        return {"ok": True}

    if name == "set_location":
        place = args.get("place", "")
        ok = session.set_location(place)
        if ok:
            await emit({"type": "log", "text": f"📍 Ubicación: {place}"})
            session.log(f"Location set: {place} ({session.user_lat},{session.user_lon})")
            return {"ok": True, "place": place}
        return {"ok": False, "error": f"No reconozco la ubicación '{place}'. Pide una provincia o ciudad española."}

    if name == "search_wallapop":
        query = args["query"]
        max_price = args.get("max_price")
        note = f' ≤€{max_price}' if max_price else ""
        await emit({"type": "log", "text": f'🔎 Buscando "{query}"{note}'})
        page = await asyncio.to_thread(
            access.search_wallapop, query, None, max_price,
            args.get("min_price"), args.get("next_page"),
            access.DEFAULT_LATITUDE, access.DEFAULT_LONGITUDE, True,
            session.user_lat, session.user_lon,
        )
        items = page["items"]
        for it in items:  # remember everything we've seen
            session.accessed_items[it["id"]] = it
        session.log(f'Search "{query}" -> {len(items)} results')
        await emit({"type": "log", "text": f"   → {len(items)} resultados"})
        # Trim what we hand back to Claude to keep token cost sane; it can always
        # call get_ad_details for the full picture.
        trimmed = [
            {
                "id": it["id"],
                "title": it["title"],
                "price": it["price"],
                "reserved": it["reserved"],
                "shippable": it["shippable"],
                "distance_km": it.get("distance_km"),
                "city": it["location"]["city"],
                "url": it["url"],
                "description": (it["description"] or "")[:250],
            }
            for it in items[:30]
        ]
        return {"count": len(items), "items": trimmed, "next_page": page["next_page"]}

    if name == "get_ad_details":
        item_id = args["item_id"]
        await emit({"type": "log", "text": f"🔬 Analizando anuncio {item_id}"})
        details = await asyncio.to_thread(access.get_ad_details, item_id)
        session.accessed_items[details["id"]] = details
        session.log(f'Inspected {item_id}: {details.get("title")}')
        # Trim the (sometimes huge) description before returning to Claude.
        trimmed = dict(details)
        trimmed["description"] = (details.get("description") or "")[:1200]
        return trimmed

    if name == "update_board":
        options = args.get("options", [])
        # Enrich each product from the data we already fetched for it, so the
        # model doesn't have to re-supply things it can't see: image, url, and the
        # seller reputation / reserved flag for the richer product cards.
        for opt in options:
            for product in opt.get("products", []):
                known = session.accessed_items.get(product.get("item_id"), {})
                if not product.get("thumbnail"):
                    product["thumbnail"] = known.get("thumbnail") or (
                        known.get("images") or [None]
                    )[0]
                if not product.get("url"):
                    product["url"] = known.get("url", "")
                if "seller" not in product and known.get("seller"):
                    product["seller"] = known["seller"]
                if "reserved" not in product and known.get("reserved") is not None:
                    product["reserved"] = known["reserved"]
        session.board = {"options": options}
        session.board_history.append(session.board)
        await emit({"type": "board", "board": session.board})
        n_products = sum(len(o.get("products", [])) for o in session.board["options"])
        session.log(f"Board updated: {len(session.board['options'])} option(s), {n_products} product(s)")
        await emit({
            "type": "log",
            "text": f"🧩 Tablero actualizado ({len(session.board['options'])} opción(es), {n_products} producto(s))",
        })
        return {"ok": True}

    return {"error": f"unknown tool {name}"}


# --- Main agent loop ---------------------------------------------------------

async def run_turn(session, user_text, emit, model=MODEL):
    """Run the agent until it finishes answering one user message.

    Streams the assistant's text and every tool call through ``emit``, executes
    tools, and repeats until the model stops requesting tools (or the tool-call
    cap is hit).

    Any exception during the turn (network blip, a transient API error, a rate
    limit, ...) is caught so it can never leave the UI stuck: an error message is
    shown to the user and the "idle" status is always emitted, which is what
    re-enables the composer. Without this, an uncaught exception here would crash
    the websocket handler in server.py, permanently disabling Send until reload.

    Args:
        session (Session): The conversation session (mutated in place).
        user_text (str): The user's message.
        emit (callable): Async callback to push UI events (see event types in
            the module docstring / server).
        model (str): The model id to drive this turn (from the UI dropdown).
    """
    try:
        await _run_turn(session, user_text, emit, model)
    except Exception as exc:
        session.log(f"Turn failed: {exc}")
        await emit({"type": "log", "text": f"⚠️ El turno falló con un error: {exc}"})
        await emit({
            "type": "error",
            "text": f"Se produjo un error y tuve que detenerme: {exc}. "
                    "Puedes intentarlo de nuevo o reformular la petición.",
        })
    finally:
        # Always fires, success or failure, so the composer re-enables.
        await emit({"type": "status", "state": "idle"})
        session.save()


async def _run_turn(session, user_text, emit, model):
    """Do the actual work of run_turn; see run_turn for the safety wrapper."""
    session.add_message({"role": "user", "content": user_text}, model)
    session.log(f"User: {user_text}")
    if not session.title:  # first message: ask Haiku for a short title
        raw = user_text.split("\n\n(My budget is")[0]
        session.title = await generate_title(raw)
    calls = 0
    force_finish = False  # once true, we stop offering tools and wrap up

    # Tell the model what we know about the user's location this turn.
    if session.user_place:
        location_note = (
            f"\n\nUSER LOCATION: {session.user_place}. Searches are centered there "
            f"and already filtered to listings the user can get — shippable, or "
            f"within {access.REACH_KM} km (each result has `shippable` and "
            f"`distance_km`). When you recommend a nearby pickup-only listing, say "
            f"it's pickup in that city; otherwise the seller ships."
        )
    else:
        location_note = (
            "\n\nUSER LOCATION: unknown. Before relying on search results, ask the "
            "user which province/city in Spain they're in and call `set_location` "
            "with it — otherwise you may recommend far-away, pickup-only listings "
            "that are useless to them. They can also set it in the 'Provincia' field."
        )
    system = SYSTEM_PROMPT + location_note

    while True:
        await emit({"type": "status", "state": "thinking"})

        # Stream one assistant response: text tokens go to the UI live. When
        # forcing a finish, we omit the tools so the model can only reply.
        stream_kwargs = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": _api_messages(session.messages),
        }
        if not force_finish:
            stream_kwargs["tools"] = TOOLS + [WEB_SEARCH_TOOL]

        async with client.messages.stream(**stream_kwargs) as stream:
            async for chunk in stream.text_stream:
                await emit({"type": "text_delta", "text": chunk})
            final = await stream.get_final_message()

        # Keep the full assistant turn (text + tool_use blocks) in history.
        session.add_message({"role": "assistant", "content": final.content}, model)
        await emit({"type": "message_done", "model": MODEL_LABELS.get(model, model)})

        # Log any web searches the model ran (executed server-side by Anthropic).
        for block in final.content:
            if block.type == "server_tool_use" and block.name == "web_search":
                query = (block.input or {}).get("query", "")
                await emit({"type": "log", "text": f'🌐 Búsqueda web "{query}"'})
                session.log(f'Web search "{query}"')

        if force_finish:
            break

        # Collect the model's calls to OUR tools (web search is already resolved
        # server-side and needs no result from us).
        my_tool_uses = [b for b in final.content if b.type == "tool_use"]

        if not my_tool_uses:
            # "pause_turn" means Anthropic paused a long (web-search) turn — loop
            # again to let it continue. Anything else means the agent has replied.
            if final.stop_reason == "pause_turn":
                continue
            break

        # Execute every tool the model asked for, then feed results back.
        tool_results = []
        for block in my_tool_uses:
            calls += 1
            # A tool failure (dead listing, network error, ...) must not crash the
            # whole run — hand the error back so the agent can try something else.
            try:
                result = await _dispatch_tool(block.name, block.input, session, emit)
            except Exception as exc:
                result = {"error": f"{block.name} failed: {exc}"}
                await emit({"type": "log", "text": f"⚠️ {block.name} falló: {exc}"})
                session.log(f"{block.name} failed: {exc}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        session.add_message({"role": "user", "content": tool_results}, model)
        session.save()

        if calls >= MAX_TOOL_CALLS:
            await emit({
                "type": "log",
                "text": f"⚠️ Alcanzado el límite de {MAX_TOOL_CALLS} llamadas a herramientas en este turno.",
            })
            session.add_message({
                "role": "user",
                "content": "You've hit the tool-call limit. Give your best "
                           "recommendation now using what you've found, without "
                           "calling more tools.",
            }, model)
            force_finish = True


# --- CLI test harness --------------------------------------------------------

async def _cli(prompt):
    """Run a single turn from the command line, printing events to the terminal.

    Args:
        prompt (str): The user request to run.
    """
    session = new_session()

    async def emit(ev):
        kind = ev["type"]
        if kind == "text_delta":
            print(ev["text"], end="", flush=True)
        elif kind == "message_done":
            print()
        elif kind == "log":
            print(f"  {ev['text']}")
        elif kind == "board":
            for opt in ev["board"]["options"]:
                total = sum(p.get("price", 0) for p in opt["products"])
                print(f"  📋 {opt['title']} — €{total:.0f}")
        elif kind == "requirements":
            print(f"  ✅ Requirements: {ev['requirements']} (budget={ev['budget']})")

    await run_turn(session, prompt, emit)
    print(f"\n[session saved to sessions/{session.id}.json]")


if __name__ == "__main__":
    import sys

    request = sys.argv[1] if len(sys.argv) > 1 else "Un PC gaming por menos de 600€"
    asyncio.run(_cli(request))
