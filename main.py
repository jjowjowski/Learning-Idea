import os, json, time, random, threading, uuid, datetime, base64, hashlib, hmac, struct, zlib
from functools import wraps
import requests, pytz
from flask import Flask, request, jsonify, session, Response, redirect
from pywebpush import webpush, WebPushException
from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization

KEY = os.getenv("ANTHROPIC_KEY")
APP_PASSWORD = os.getenv("APP_PASSWORD")
VAPID_EMAIL = os.getenv("VAPID_EMAIL", "learning-app@example.com")
MODEL = os.getenv("MODEL", "claude-haiku-5-5")
MODEL_CHOICES = [
    ("", "Auto (study material uses the study model, everything else Haiku)"),
    ("claude-haiku-5-5", "Haiku: fastest, cheapest"),
    ("claude-sonnet-5-5", "Sonnet: stronger, several times the cost"),
    ("claude-opus-5-5", "Opus: strongest, much higher cost"),
]
NZ = pytz.timezone("Pacific/Auckland")

TOPICS = [
    "Econometrics: OLS assumptions, instrumental variables, panel data, time series",
    "Microeconomics and game theory",
    "Macroeconomics and monetary policy",
    "Asset pricing and portfolio theory",
    "Corporate finance and valuation",
    "Derivatives and risk management",
    "Probability and statistics",
    "Linear algebra and calculus for economists",
    "Economic history",
    "World history: causes, turning points and historiography",
    "NZ history and politics",
    "Political theory",
    "Epistemology and philosophy of science",
    "Ethics and moral philosophy",
    "Formal logic and reasoning",
    "Physics and cosmology",
    "Biology and evolution",
    "Chemistry and materials",
    "Computer science and algorithms",
    "Psychology and behavioural economics",
    "Law and institutions",
    "General trivia with a surprising explanation",
]
MIN_MINUTES = float(os.getenv("MIN_MINUTES", 30))
MAX_MINUTES = float(os.getenv("MAX_MINUTES", 90))
QUIET_START = int(os.getenv("QUIET_START", 19))   # no notifications from 7pm NZ time...
QUIET_END = int(os.getenv("QUIET_END", 9))        # ...until 9am
REVIEW_CHANCE = 0.6
REVIEW_BACKLOG_LIMIT = 5
INTERVALS_DAYS = [1, 3, 7, 16, 35, 90, 180]
MAX_QUEUE = 3

DATA_FILE = os.path.join(os.getenv("DATA_DIR", "."), "learning_data.json")
lock = threading.Lock()

GRAPH_SPEC = """MATH AND GRAPH FORMATTING:
- Write all mathematics in LaTeX: inline as $...$ and display equations as $$...$$ on their own lines. Never write equations in plain text. Write money as USD 5 or NZD 5, never with a dollar sign.
- To show a graph, include a fenced block exactly like this (valid JSON, at most 2 graphs per reply, only when a diagram genuinely helps):
```graph
{"title":"Supply and demand","xlabel":"Quantity","ylabel":"Price","xmin":0,"xmax":10,"ymin":0,"ymax":10,"curves":[{"label":"Demand","expr":"10 - x"},{"label":"Supply","expr":"2 + 0.5*x"}],"points":[{"label":"Equilibrium","x":5.33,"y":4.67}]}
```
- expr is a formula in x using math.js syntax: ^ for powers, exp(x), log(x), sqrt(x). For data instead of formulas use "series":[{"label":"...","data":[[x,y],[x,y]]}]. ymin and ymax are optional. Compute any marked points (such as intersections) exactly, and only plot things you can state correctly."""

CARD_TOOL = {
    "name": "make_card",
    "description": "Create one learning card.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["quiz", "fact"]},
            "title": {"type": "string", "description": "Short title, max 8 words"},
            "content": {"type": "string", "description": "For a fact: a 150-250 word mini-lesson that builds the idea step by step, with the reasoning and a worked example or key formula where relevant (maths in LaTeX). For a quiz: a scenario-based question that tests understanding, not memory."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Exactly 4 options for a quiz, empty for a fact"},
            "correct_index": {"type": "integer", "description": "0-3 for a quiz, -1 for a fact"},
            "explanation": {"type": "string", "description": "For a quiz: why the right answer is right and why each wrong option fails, 4-6 sentences. Empty for a fact."},
            "recall_question": {"type": "string", "description": "For a fact: one question that makes the learner explain or apply the idea, without giving the answer away. Empty for a quiz."},
        },
        "required": ["kind", "title", "content", "options", "correct_index", "explanation", "recall_question"],
    },
}

TUTOR_SYSTEM = ("You are a rigorous university tutor for a postgraduate studying economics and finance, who wants to understand "
                "topics in real depth across many fields. Teach at university level: explain the mechanism or theory, give a worked "
                "example, flag common misconceptions, and connect ideas to related concepts. Be precise, and say plainly if you are "
                "unsure or if scholars disagree. Keep each reply to roughly 350-550 words: for a big topic, teach one step at a time "
                "and say what the next step would be instead of covering everything at once. Use short paragraphs, **bold** for key "
                "terms, and simple '-' bullet lists when helpful, with no headings. After a substantial explanation, end with one "
                "question that checks whether the learner really understood. If they answer, give honest feedback on what is right "
                "and what is missing. Always finish your last sentence.\n\n" + GRAPH_SPEC)

def now_nz():
    return datetime.datetime.now(NZ)

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("history", [])
    d.setdefault("stats", {"right": 0, "wrong": 0})
    d.setdefault("topic_stats", {})
    d.setdefault("cards", {})
    d.setdefault("deck", {})
    d.setdefault("queue", [])
    d.setdefault("days", {})
    d.setdefault("paused_until", None)
    d.setdefault("focus", None)
    d.setdefault("subs", [])
    d.setdefault("vapid_pem", None)
    d.setdefault("model", None)
    return d

def save_data():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print(f"Save error: {e}")

data = load_data()

def get_vapid():
    with lock:
        if not data.get("vapid_pem"):
            v = Vapid01()
            v.generate_keys()
            data["vapid_pem"] = v.private_pem().decode()
            save_data()
        pem = data["vapid_pem"]
    v = Vapid01.from_pem(pem.encode())
    pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return v, base64.urlsafe_b64encode(pub).rstrip(b"=").decode()

def push_all(title, body):
    v, _ = get_vapid()
    sent, dead = 0, []
    for sub in list(data["subs"]):
        try:
            webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body}),
                    vapid_private_key=v, vapid_claims={"sub": f"mailto:{VAPID_EMAIL}"}, ttl=3600)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            print(f"Push failed ({code}): {str(e)[:150]}")
            if code in (404, 410):
                dead.append(sub)
        except Exception as e:
            print(f"Push error: {e}")
    if dead:
        with lock:
            data["subs"] = [s for s in data["subs"] if s not in dead]
            save_data()
    return sent

def call_claude(messages, system=None, tools=None, tool_choice=None, max_tokens=2000, model=None):
    body = {"model": data.get("model") or model or MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"API error {r.status_code}: {r.text[:200]}")
    return r.json()

def generate_new_card():
    with lock:
        topic = data.get("focus") or random.choice(TOPICS)
        recent = "; ".join(data["history"][-40:]) or "none yet"
    kind = random.choice(["quiz", "quiz", "fact"])
    prompt = f"""Create one {kind} learning card on the topic: {topic}.
The learner is a university postgraduate in economics and finance who wants real, university-level understanding of a wide range of subjects, not trivia.
Teach the underlying mechanism, theory or argument, not just a fact. Include the reasoning, and a worked example or key formula where it helps. Pick a specific, substantive idea within the topic.
For a quiz, write a scenario or application question that needs reasoning to answer, with exactly 4 plausible options (the wrong ones should reflect common misconceptions) and the correct position at random.
For a fact, also write recall_question: a question that makes the learner explain or apply the idea, without revealing the answer. A fact lesson may include one graph block if a diagram genuinely helps.
Accuracy matters most. Only include material you are confident is correct and well established.
Do not repeat or closely overlap these recently shown cards: {recent}

{GRAPH_SPEC}"""
    resp = call_claude([{"role": "user", "content": prompt}], tools=[CARD_TOOL],
                       tool_choice={"type": "tool", "name": "make_card"}, max_tokens=2500)
    card = None
    for b in resp.get("content", []):
        if b.get("type") == "tool_use":
            card = b["input"]
    if not card:
        raise RuntimeError("No card returned")
    if card.get("kind") == "quiz":
        opts, ci = card.get("options", []), card.get("correct_index", -1)
        if len(opts) != 4 or not (0 <= ci < 4):
            card.update({"kind": "fact", "options": [], "correct_index": -1})
    card["topic"] = topic
    return card

def due_entries():
    n = now_nz()
    out = []
    for did, e in data["deck"].items():
        if e.get("mastered") or not e.get("due"):
            continue
        if datetime.datetime.fromisoformat(e["due"]) <= n:
            out.append((e["due"], did, e))
    out.sort(key=lambda x: x[0])
    return [(did, e) for _, did, e in out]

def apply_result(entry, result, first):
    n = now_nz()
    if first:
        entry["due"] = (n + (datetime.timedelta(hours=6) if result == "bad"
                             else datetime.timedelta(days=INTERVALS_DAYS[0]))).isoformat()
        return
    stage = entry.get("stage", 0)
    if result == "good":
        stage += 1
    elif result == "bad":
        stage = 0
        entry["lapses"] = entry.get("lapses", 0) + 1
    entry["stage"] = stage
    if stage >= len(INTERVALS_DAYS):
        entry["mastered"] = True
        entry["due"] = None
    else:
        entry["due"] = (n + datetime.timedelta(days=INTERVALS_DAYS[stage])).isoformat()

def log_study():
    k = now_nz().date().isoformat()
    data["days"][k] = data["days"].get(k, 0) + 1

def streak():
    d = now_nz().date()
    if data["days"].get(d.isoformat(), 0) == 0:
        d -= datetime.timedelta(days=1)
    s = 0
    while data["days"].get(d.isoformat(), 0) > 0:
        s += 1
        d -= datetime.timedelta(days=1)
    return s

def finish(inst, result):
    entry = data["deck"].get(inst.get("deck_id"))
    if entry:
        apply_result(entry, result, first=not inst.get("review"))

def dequeue(cid):
    if cid in data["queue"]:
        data["queue"].remove(cid)

def prune_cards():
    keep = set(data["queue"])
    extra = [c for c in data["cards"] if c not in keep]
    for c in extra[:-60]:
        data["cards"].pop(c, None)

def make_instance(prefer_review=False):
    with lock:
        queued = {data["cards"][c].get("deck_id") for c in data["queue"] if c in data["cards"]}
        due = [(d, e) for d, e in due_entries() if d not in queued]
    use_review = bool(due) and (prefer_review or len(due) >= REVIEW_BACKLOG_LIMIT or random.random() < REVIEW_CHANCE)
    if use_review:
        did, entry = due[0]
        inst = dict(entry["card"])
        if inst["kind"] == "quiz":
            orig, correct = list(inst["options"]), inst["correct_index"]
            order = list(range(4))
            random.shuffle(order)
            inst["options"] = [orig[i] for i in order]
            inst["correct_index"] = order.index(correct)
        inst["review"] = True
        inst["deck_id"] = did
    else:
        card = generate_new_card()
        did = uuid.uuid4().hex[:8]
        with lock:
            data["deck"][did] = {"card": dict(card), "stage": 0, "lapses": 0, "mastered": False,
                                 "due": (now_nz() + datetime.timedelta(days=INTERVALS_DAYS[0])).isoformat()}
            data["history"].append(card["title"])
            data["history"] = data["history"][-100:]
        inst = dict(card)
        inst["review"] = False
        inst["deck_id"] = did
    cid = uuid.uuid4().hex[:10]
    with lock:
        data["cards"][cid] = inst
        data["queue"].append(cid)
        prune_cards()
        save_data()
    return cid, inst

def public_card(cid, inst):
    out = {"cid": cid, "kind": inst["kind"], "topic": inst["topic"], "title": inst["title"],
           "review": bool(inst.get("review"))}
    if inst["kind"] == "quiz":
        out["text"] = inst["content"]
        out["options"] = inst["options"]
    elif inst.get("review"):
        out["text"] = inst.get("recall_question") or f"What do you remember about: {inst['title']}?"
        out["answer"] = inst["content"]
    else:
        out["text"] = inst["content"]
    return out

def paused_now():
    p = data.get("paused_until")
    return bool(p) and datetime.datetime.fromisoformat(p) > now_nz()

def build_state():
    with lock:
        s = data["stats"]
        today = now_nz().date()
        days = [data["days"].get((today - datetime.timedelta(days=i)).isoformat(), 0) for i in range(27, -1, -1)]
        paused = None
        p = data.get("paused_until")
        if p and datetime.datetime.fromisoformat(p) > now_nz():
            paused = datetime.datetime.fromisoformat(p).strftime("%a %I:%M%p").lstrip("0")
        return {
            "right": s["right"], "wrong": s["wrong"],
            "deck": len(data["deck"]),
            "mastered": sum(1 for e in data["deck"].values() if e.get("mastered")),
            "due": len(due_entries()),
            "streak": streak(),
            "topic_stats": data["topic_stats"],
            "days": days,
            "paused": paused,
            "focus": data.get("focus"),
            "topics": TOPICS,
            "devices": len(data["subs"]),
            "queue": len(data["queue"]),
            "model": data.get("model") or "",
            "models": MODEL_CHOICES,
        }

def scheduler():
    next_at = time.time() + 60
    while True:
        time.sleep(15)
        if time.time() < next_at:
            continue
        next_at = time.time() + random.uniform(MIN_MINUTES * 60, MAX_MINUTES * 60)
        try:
            n = now_nz()
            if paused_now() or n.hour >= QUIET_START or n.hour < QUIET_END or not data["subs"]:
                continue
            with lock:
                qlen = len(data["queue"])
            if qlen >= MAX_QUEUE:
                push_all("📚 Cards waiting", f"{qlen} cards are waiting for you")
                continue
            cid, inst = make_instance()
            body = "Time for a review" if inst.get("review") else "New card: " + inst["topic"].split(":")[0]
            push_all("📚 Learning card ready", body)
        except Exception as e:
            print(f"Scheduler error: {e}")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or hashlib.sha256(((APP_PASSWORD or "x") + "learning").encode()).hexdigest()
app.permanent_session_lifetime = datetime.timedelta(days=365)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

def need_auth(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("ok"):
            return jsonify(error="Not signed in"), 401
        return f(*a, **k)
    return wrapper

@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/") or request.path.startswith("/study/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

def make_png(size):
    bg, fg = (79, 140, 255), (255, 255, 255)
    def inside(x, y):
        fx, fy = x / size, y / size
        return (0.36 <= fx < 0.48 and 0.28 <= fy < 0.72) or (0.36 <= fx < 0.66 and 0.60 <= fy < 0.72)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(fg if inside(x, y) else bg)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))

ICONS = {}

@app.get("/icon-<int:size>.png")
def icon(size):
    if size not in (192, 512):
        return Response(status=404)
    if size not in ICONS:
        ICONS[size] = make_png(size)
    return Response(ICONS[size], mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/manifest.json")
def manifest():
    return jsonify({"name": "Learning", "short_name": "Learning", "start_url": "/", "display": "standalone",
                    "background_color": "#14151b", "theme_color": "#14151b",
                    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]})

SW_JS = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {});
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data.json(); } catch (_) {}
  e.waitUntil(self.registration.showNotification(d.title || 'Learning card ready', {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-192.png', tag: 'learning-card', renotify: true
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) { if ('focus' in c) return c.focus(); }
    return clients.openWindow('/');
  }));
});
"""

@app.get("/sw.js")
def sw():
    return Response(SW_JS, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

@app.post("/login")
def login():
    pw = request.form.get("password", "")
    if APP_PASSWORD and hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
        session.permanent = True
        session["ok"] = True
        return redirect("/")
    time.sleep(1.5)
    return redirect("/?e=1")

@app.get("/")
def index():
    if not session.get("ok"):
        err = "Wrong password." if request.args.get("e") else ""
        return Response(LOGIN_HTML.replace("__ERR__", err), mimetype="text/html")
    return Response(APP_HTML, mimetype="text/html")

@app.get("/api/state")
@need_auth
def api_state():
    return jsonify(build_state())

@app.get("/api/next")
@need_auth
def api_next():
    with lock:
        data["queue"] = [c for c in data["queue"] if c in data["cards"]]
        cid = data["queue"][0] if data["queue"] else None
    if cid is None:
        try:
            cid, _ = make_instance(prefer_review=True)
        except Exception as e:
            return jsonify(error=f"Couldn't make a card: {e}"), 500
    with lock:
        inst = data["cards"][cid]
        waiting = max(len(data["queue"]) - 1, 0)
    return jsonify(card=public_card(cid, inst), waiting=waiting)

@app.post("/api/answer")
@need_auth
def api_answer():
    b = request.get_json(silent=True) or {}
    cid, i = b.get("cid"), b.get("index")
    inst = data["cards"].get(cid)
    if not inst or inst["kind"] != "quiz" or not isinstance(i, int) or not 0 <= i < 4:
        return jsonify(error="That card is no longer available."), 400
    if inst.get("answered"):
        return jsonify(error="Already answered."), 400
    ok = (i == inst["correct_index"])
    with lock:
        inst["answered"] = True
        ts = data["topic_stats"].setdefault(inst["topic"], [0, 0])
        data["stats"]["right" if ok else "wrong"] += 1
        ts[0 if ok else 1] += 1
        log_study()
        finish(inst, "good" if ok else "bad")
        dequeue(cid)
        save_data()
    return jsonify(correct=ok, correct_index=inst["correct_index"], explanation=inst.get("explanation", ""))

@app.post("/api/rate")
@need_auth
def api_rate():
    b = request.get_json(silent=True) or {}
    cid, result = b.get("cid"), b.get("result")
    inst = data["cards"].get(cid)
    if not inst or result not in ("good", "fuzzy", "bad") or inst.get("rated"):
        return jsonify(error="Can't rate that card."), 400
    with lock:
        inst["rated"] = True
        log_study()
        finish(inst, result)
        dequeue(cid)
        save_data()
    return jsonify(ok=True)

@app.post("/api/seen")
@need_auth
def api_seen():
    cid = (request.get_json(silent=True) or {}).get("cid")
    with lock:
        if cid in data["cards"] and not data["cards"][cid].get("seen"):
            data["cards"][cid]["seen"] = True
            log_study()
        dequeue(cid)
        save_data()
    return jsonify(ok=True)

@app.post("/api/skip")
@need_auth
def api_skip():
    cid = (request.get_json(silent=True) or {}).get("cid")
    with lock:
        dequeue(cid)
        save_data()
    return jsonify(ok=True)

@app.post("/api/chat")
@need_auth
def api_chat():
    b = request.get_json(silent=True) or {}
    msgs = []
    for m in (b.get("messages") or [])[-14:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])[:8000]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs:
        return jsonify(error="Empty message."), 400
    system = TUTOR_SYSTEM
    inst = data["cards"].get(b.get("cid"))
    if inst:
        system += f"\n\nThe learner is looking at this learning card.\nTitle: {inst['title']}\nContent: {inst['content']}"
        if inst["kind"] == "quiz":
            system += (f"\nOptions: {inst['options']}\nCorrect answer: {inst['options'][inst['correct_index']]}"
                       f"\nExplanation: {inst.get('explanation', '')}"
                       "\nIf the learner has not answered the quiz yet, do not reveal the answer unless they ask.")
    parts = []
    cur = list(msgs)
    try:
        for _ in range(4):
            resp = call_claude(cur, system=system, max_tokens=2500)
            txt = "".join(x.get("text", "") for x in resp.get("content", []) if x.get("type") == "text")
            parts.append(txt)
            if resp.get("stop_reason") != "max_tokens":
                break
            cur = cur + [{"role": "assistant", "content": txt},
                         {"role": "user", "content": "Continue exactly where you left off. Do not repeat anything."}]
        else:
            parts.append("\n\n[Very long reply, stopped here. Ask me to carry on.]")
    except Exception as e:
        if not parts:
            return jsonify(error=f"Couldn't reach Claude: {e}"), 500
    reply = "".join(parts).strip()
    return jsonify(reply=reply or "I couldn't come up with an answer, try rephrasing.")

@app.post("/api/settings")
@need_auth
def api_settings():
    b = request.get_json(silent=True) or {}
    a = b.get("action")
    with lock:
        if a == "snooze":
            data["paused_until"] = (now_nz() + datetime.timedelta(hours=1)).isoformat()
        elif a == "pause":
            t = now_nz() + datetime.timedelta(days=1)
            data["paused_until"] = t.replace(hour=QUIET_END, minute=0, second=0, microsecond=0).isoformat()
        elif a == "resume":
            data["paused_until"] = None
        elif a == "focus":
            v = str(b.get("value") or "").strip()[:80]
            data["focus"] = v or None
        elif a == "model":
            v = str(b.get("value") or "")
            data["model"] = v if v in {k for k, _ in MODEL_CHOICES if k} else None
        save_data()
    return jsonify(build_state())

@app.get("/api/vapid")
@need_auth
def api_vapid():
    return jsonify(key=get_vapid()[1])

@app.post("/api/subscribe")
@need_auth
def api_subscribe():
    sub = request.get_json(silent=True) or {}
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        return jsonify(error="Bad subscription."), 400
    clean = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
    with lock:
        data["subs"] = [s for s in data["subs"] if s["endpoint"] != clean["endpoint"]] + [clean]
        data["subs"] = data["subs"][-5:]
        save_data()
    return jsonify(ok=True)

@app.post("/api/test_push")
@need_auth
def api_test_push():
    n = push_all("📚 Test notification", "Notifications are working.")
    return jsonify(sent=n)

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Learning</title>
<style>body{margin:0;background:#14151b;color:#e8e8ee;font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#1e1f26;padding:28px;border-radius:14px;width:min(320px,88vw)}
input,button{width:100%;box-sizing:border-box;padding:14px;margin-top:12px;border-radius:10px;border:0;font-size:16px}
input{background:#2b2d38;color:#e8e8ee}button{background:#4f8cff;color:#fff;font-weight:600}
.e{color:#e0706a;margin-top:10px;min-height:1em}</style></head>
<body><form method="post" action="/login"><h2 style="margin:0">📚 Learning</h2>
<input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">Sign in</button><div class="e">__ERR__</div></form></body></html>"""

APP_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#14151b"><link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png"><title>Learning</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mathjs@13.2.0/lib/browser/math.js"></script>
<style>
:root{--bg:#14151b;--card:#1e1f26;--card2:#2b2d38;--fg:#e8e8ee;--muted:#9aa0b4;--accent:#4f8cff;--good:#2e9e5b;--bad:#c0453f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,sans-serif;padding-bottom:76px}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;font-weight:700;font-size:18px}
#streak{font-size:14px;color:var(--muted);font-weight:500}
main{max-width:680px;margin:0 auto;padding:0 14px}
.panel{background:var(--card);border-radius:16px;padding:18px;margin-bottom:14px}
.tag{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.04em}
h2{margin:6px 0 10px;font-size:21px}.q{font-size:16px;line-height:1.6;margin:0 0 14px}
.muted{color:var(--muted);font-size:14px;line-height:1.5}
.opt,.btn{display:block;width:100%;text-align:left;border:0;border-radius:12px;padding:14px;margin-bottom:8px;font-size:15px;color:var(--fg);background:var(--card2);cursor:pointer;min-height:48px}
.btn{text-align:center;font-weight:600}.primary{background:var(--accent);color:#fff}.ghost{background:transparent;border:1px solid #3a3d4d}
.opt.good{background:var(--good);color:#fff}.opt.bad{background:var(--bad);color:#fff}.opt:disabled{opacity:1}
.row{display:flex;gap:8px}.row .btn{margin-bottom:0}.fb{margin:6px 0 12px;line-height:1.6}
.goodtxt{color:#5fd493}.badtxt{color:#ee8a84}
#log{max-height:60vh;overflow-y:auto;margin-bottom:10px}
.msg{padding:10px 12px;border-radius:12px;margin-bottom:8px;line-height:1.6;font-size:15px}
.msg.user{background:var(--accent);margin-left:20%;white-space:pre-wrap}.msg.bot{background:var(--card2);margin-right:4%}
.rich p{margin:0 0 10px}.rich p:last-child{margin-bottom:0}.rich ul,.rich ol{margin:0 0 10px;padding-left:22px}.opt p{margin:0}
.gr{height:300px;margin:12px 0;background:#181920;border-radius:10px;padding:8px}
.katex-display{overflow-x:auto;overflow-y:hidden;padding:4px 0;margin:.6em 0}
#chatrow{display:flex;gap:8px}#chatin{flex:1;background:var(--card2);color:var(--fg);border:0;border-radius:12px;padding:13px;font-size:16px}
#chatsend{width:84px;margin:0}
nav{position:fixed;bottom:0;left:0;right:0;background:var(--card);display:flex;border-top:1px solid #2b2d38}
nav button{flex:1;background:none;border:0;color:var(--muted);padding:14px 0 16px;font-size:14px}
nav button.on{color:var(--accent);font-weight:700}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.tile{background:var(--card);border-radius:14px;padding:14px}.tile b{display:block;font-size:26px}
.heat{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.heat div{aspect-ratio:1;border-radius:5px;background:var(--card2)}
.l1{background:#2c4a80!important}.l2{background:#3b6bc4!important}.l3{background:#4f8cff!important}
select{width:100%;background:var(--card2);color:var(--fg);border:0;border-radius:12px;padding:13px;font-size:15px;margin-bottom:8px}
.trow{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #2b2d38;font-size:14px;gap:10px}
.sbtn{border:1px solid #3a3d4d;background:transparent;color:var(--muted);border-radius:999px;padding:6px 12px;font-size:13px;font-weight:600;cursor:pointer;margin-right:8px}.sbtn.on{background:var(--accent);border-color:var(--accent);color:#fff}
</style></head><body>
<header><span>📚 Learning</span><span><button id="studybtn" class="sbtn" hidden></button> <span id="streak"></span></span></header>
<main>
<section id="v-study">
  <div class="panel" id="card"></div>
  <div class="panel" id="chat" hidden>
    <div class="tag">TUTOR</div><div id="log"></div>
    <div id="chatrow"><input id="chatin" placeholder="Ask a follow-up, or answer the tutor's question…" autocomplete="off"><button class="btn primary" id="chatsend">Send</button></div>
  </div>
</section>
<section id="v-stats" hidden></section>
<section id="v-set" hidden></section>
</main>
<nav><button id="n-study" class="on">Study</button><button id="n-stats">Stats</button><button id="n-set">Settings</button><button onclick="location.href='/study'">Materials</button></nav>
<script>
const $=s=>document.querySelector(s);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
function btn(x,f,c){const b=el('button',c||'btn',x);b.onclick=f;return b}
async function api(p,b){
  const r=await fetch(p,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
  if(r.status===401){location.reload();return{}}
  let j={};try{j=await r.json()}catch(e){}
  if(!r.ok&&!j.error)j.error='Request failed ('+r.status+')';return j}

/* ---------- rich text: maths (KaTeX), light markdown, graphs (Chart.js + math.js) ---------- */
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function md(s){
  let h=esc(s).replace(/^#{1,4}\s+(.+)$/gm,'<strong>$1</strong>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  const li=/^\s*([-*•]|\d+[.)])\s+/;
  return h.split(/\n{2,}/).map(b=>{
    const lines=b.split('\n');
    if(lines.length&&lines.every(l=>li.test(l))){
      const ord=/^\s*\d/.test(lines[0]);
      return (ord?'<ol>':'<ul>')+lines.map(l=>'<li>'+l.replace(li,'')+'</li>').join('')+(ord?'</ol>':'</ul>')}
    return '<p>'+lines.join('<br>')+'</p>'}).join('')}
const PAL=['#4f8cff','#f5a742','#5fd493','#ee6a84','#b48cff','#4fd1d9'];
function drawGraph(node,src){
  let g;try{g=JSON.parse(src)}catch(e){node.textContent='(graph could not be read)';return}
  if(!window.Chart||!window.math){node.textContent='(graph libraries did not load)';return}
  try{
    const x0=Number(g.xmin??0),x1=Number(g.xmax??10),sets=[];let i=0;
    for(const c of (g.curves||[])){
      let f;try{f=math.compile(String(c.expr))}catch(e){continue}
      const pts=[];
      for(let k=0;k<=160;k++){const x=x0+(x1-x0)*k/160;let y;try{y=f.evaluate({x})}catch(e){continue}
        if(typeof y==='number'&&isFinite(y))pts.push({x,y})}
      sets.push({label:c.label||String(c.expr),data:pts,showLine:true,pointRadius:0,borderWidth:2.5,borderColor:PAL[i%6],backgroundColor:PAL[i%6]});i++}
    for(const s of (g.series||[])){
      sets.push({label:s.label||'',data:(s.data||[]).map(p=>({x:p[0],y:p[1]})),showLine:true,pointRadius:3,borderWidth:2,borderColor:PAL[i%6],backgroundColor:PAL[i%6]});i++}
    for(const p of (g.points||[])){
      sets.push({label:p.label||'',data:[{x:p.x,y:p.y}],showLine:false,pointRadius:6,borderColor:'#ffffff',backgroundColor:PAL[i%6]});i++}
    const canvas=document.createElement('canvas');node.replaceChildren(canvas);
    const ax=(t)=>({title:{display:!!t,text:t||'',color:'#9aa0b4'},grid:{color:'#2f3240'},ticks:{color:'#9aa0b4'}});
    new Chart(canvas,{type:'scatter',data:{datasets:sets},options:{responsive:true,maintainAspectRatio:false,animation:false,
      scales:{x:Object.assign({type:'linear',min:x0,max:x1},ax(g.xlabel)),y:Object.assign({type:'linear',min:g.ymin,max:g.ymax},ax(g.ylabel))},
      plugins:{legend:{labels:{color:'#e8e8ee'}},title:{display:!!g.title,text:g.title||'',color:'#e8e8ee'}}}});
  }catch(e){node.textContent='(graph could not be drawn)'}}
function setRich(node,text){
  const st=[];const keep=(t,v)=>{st.push([t,v]);return '\u0000'+(st.length-1)+'\u0000'};
  let s=String(text||'');
  s=s.replace(/```graph\s*([\s\S]*?)```/g,(m,j)=>keep('g',j));
  s=s.replace(/\$\$([\s\S]+?)\$\$/g,(m,x)=>keep('d',x));
  s=s.replace(/\\\[([\s\S]+?)\\\]/g,(m,x)=>keep('d',x));
  s=s.replace(/\\\(([\s\S]+?)\\\)/g,(m,x)=>keep('i',x));
  s=s.replace(/\$(?=\S)([^$\n]*?\S)\$/g,(m,x)=>keep('i',x));
  let h=md(s);
  h=h.replace(/\u0000(\d+)\u0000/g,(m,i)=>{const p=st[Number(i)];const e=encodeURIComponent(p[1].trim());
    return p[0]==='g'?'<div class="gr" data-s="'+e+'"></div>':'<span class="mk" data-d="'+(p[0]==='d'?1:0)+'" data-t="'+e+'"></span>'});
  node.innerHTML=h;
  node.querySelectorAll('.mk').forEach(n=>{const t=decodeURIComponent(n.dataset.t);
    if(window.katex){try{katex.render(t,n,{displayMode:n.dataset.d==='1',throwOnError:false})}catch(e){n.textContent=t}}else n.textContent=t});
  node.querySelectorAll('.gr').forEach(n=>drawGraph(n,decodeURIComponent(n.dataset.s)))}
function rich(cls,t){const e=el('div','rich '+cls);setRich(e,t);return e}

let current=null,chatMsgs=[],tab='study';
function showTab(t){tab=t;for(const n of['study','stats','set']){$('#v-'+n).hidden=n!==t;$('#n-'+n).className=n===t?'on':''}
  if(t==='stats')loadStats();if(t==='set')loadSettings();}
$('#n-study').onclick=()=>showTab('study');$('#n-stats').onclick=()=>showTab('stats');$('#n-set').onclick=()=>showTab('set');

async function loadNext(){
  const box=$('#card');box.replaceChildren(el('div','muted','Getting your card…'));
  $('#chat').hidden=true;chatMsgs=[];$('#log').replaceChildren();
  const j=await api('/api/next');
  if(j.error){box.replaceChildren(el('div','muted',j.error),btn('Try again',loadNext,'btn primary'));return}
  current=j.card;renderCard(j.card,j.waiting);refreshStreak();}

function renderCard(c,waiting){
  const box=$('#card');box.replaceChildren();
  const tag=c.review?'REVIEW':(c.kind==='quiz'?'QUIZ':'LESSON');
  box.append(el('div','tag',c.topic.split(':')[0]+' · '+tag),el('h2','',c.title),rich('q',c.text));
  const area=el('div'),fb=el('div','fb rich'),actions=el('div','row');box.append(area,fb,actions);
  if(waiting>0)box.append(el('div','muted',waiting+' more card'+(waiting>1?'s':'')+' waiting'));
  const more=btn('Tell me more',openChat,'btn ghost'),next=btn('Next card',loadNext,'btn primary');
  const skip=btn('Skip',async()=>{await api('/api/skip',{cid:c.cid});loadNext()},'btn ghost');
  if(c.kind==='quiz'){
    const opts=c.options.map((o,i)=>{const b=el('button','opt rich');setRich(b,'ABCD'[i]+')  '+o);
      b.onclick=async()=>{opts.forEach(x=>x.disabled=true);
        const j=await api('/api/answer',{cid:c.cid,index:i});
        if(j.error){fb.textContent=j.error;return}
        opts[j.correct_index].classList.add('good');if(!j.correct)opts[i].classList.add('bad');
        setRich(fb,(j.correct?'**Correct.** ':'**Not quite.** ')+j.explanation);fb.className='fb rich '+(j.correct?'goodtxt':'badtxt');
        actions.replaceChildren(more,next)};return b});
    area.append(...opts);actions.append(skip);
  }else if(c.review){
    actions.append(btn('Reveal answer',()=>{
      setRich(fb,c.answer);
      const rate=r=>async()=>{await api('/api/rate',{cid:c.cid,result:r});
        area.replaceChildren(el('div','muted',r==='good'?'Nice, it moves further out.':r==='fuzzy'?'It will come back at the same spacing.':'It will come back tomorrow.'));
        actions.replaceChildren(more,next)};
      const row=el('div','row');
      row.append(btn('Knew it',rate('good'),'btn primary'),btn('Fuzzy',rate('fuzzy'),'btn'),btn('Forgot',rate('bad'),'btn'));
      area.replaceChildren(row);actions.replaceChildren();
    },'btn primary'),skip);
  }else{
    actions.append(btn('Got it',async()=>{await api('/api/seen',{cid:c.cid});actions.replaceChildren(more,next)},'btn primary'),skip);
  }}

function addMsg(role,text){const m=el('div','msg '+role+(role==='bot'?' rich':''));
  if(role==='bot')setRich(m,text);else m.textContent=text;
  $('#log').append(m);$('#log').scrollTop=1e9;return m}
function openChat(){$('#chat').hidden=false;
  if(!chatMsgs.length)ask('Teach me this at university level: the underlying mechanism or theory, a worked example, common misconceptions, and how it connects to related ideas. Then ask me one question to check I understood.',true);
  $('#chat').scrollIntoView({behavior:'smooth'})}
async function ask(text,hidden){
  chatMsgs.push({role:'user',content:text});if(!hidden)addMsg('user',text);
  const t=addMsg('bot','Thinking…');
  const j=await api('/api/chat',{cid:current&&current.cid,messages:chatMsgs});
  if(j.error){t.textContent='Error: '+j.error;chatMsgs.pop()}else{setRich(t,j.reply);chatMsgs.push({role:'assistant',content:j.reply})}
  $('#log').scrollTop=1e9}
function sendChat(){const v=$('#chatin').value.trim();if(v){$('#chatin').value='';ask(v,false)}}
$('#chatsend').onclick=sendChat;$('#chatin').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat()});

async function refreshStreak(){const s=await api('/api/state');if(s.streak!==undefined)$('#streak').textContent=s.streak>0?'🔥 '+s.streak+' day streak':''}

async function loadStats(){
  const s=await api('/api/state'),v=$('#v-stats');v.replaceChildren();
  const total=s.right+s.wrong,pct=total?Math.round(100*s.right/total)+'%':'n/a';
  const tiles=el('div','tiles');
  for(const[n,l]of[[s.due,'Reviews due'],[s.deck,'Cards in deck'],[s.mastered,'Mastered'],[s.streak,'Day streak'],[pct,'Quiz score'],[s.right+'/'+total,'Right / answered']]){
    const t=el('div','tile');t.append(el('b','',String(n)),el('span','muted',l));tiles.append(t)}
  v.append(tiles);
  const hp=el('div','panel');hp.append(el('div','tag','LAST 28 DAYS'),el('div','','\u00a0'));
  const heat=el('div','heat');for(const d of s.days){const x=el('div',d>=6?'l3':d>=3?'l2':d>=1?'l1':'');x.title=d+' cards';heat.append(x)}
  hp.append(heat);v.append(hp);
  const tp=el('div','panel');tp.append(el('div','tag','QUIZ SCORE BY TOPIC'));
  const keys=Object.keys(s.topic_stats).sort();
  if(!keys.length)tp.append(el('div','muted','Answer a few quizzes and your topics will show up here.'));
  for(const k of keys){const[r,w]=s.topic_stats[k];const row=el('div','trow');row.append(el('span','',k.split(':')[0]),el('span','',r+'/'+(r+w)));tp.append(row)}
  v.append(tp)}

function b64(s){const p='='.repeat((4-s.length%4)%4),r=atob((s+p).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from([...r].map(c=>c.charCodeAt(0)))}
async function enablePush(note){
  if(!('serviceWorker' in navigator)||!('PushManager' in window)){note.textContent='This browser does not support push notifications.';return}
  const perm=await Notification.requestPermission();
  if(perm!=='granted'){note.textContent='Notifications were blocked. Allow them in your browser or Android site settings, then try again.';return}
  const reg=await navigator.serviceWorker.ready;
  const old=await reg.pushManager.getSubscription();if(old)await old.unsubscribe();
  const k=await api('/api/vapid');
  const sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64(k.key)});
  const j=await api('/api/subscribe',sub.toJSON());
  note.textContent=j.error?j.error:'Notifications enabled on this device.';loadSettings(true)}

async function loadSettings(keep){
  const s=await api('/api/state'),v=$('#v-set');const prev=keep&&$('#pnote')?$('#pnote').textContent:'';v.replaceChildren();
  const p1=el('div','panel');p1.append(el('div','tag','NOTIFICATIONS'));
  p1.append(el('p','muted',s.devices+' device'+(s.devices===1?'':'s')+' subscribed. Cards arrive at random times during your active hours.'));
  const note=el('p','muted',prev);note.id='pnote';
  p1.append(btn('Enable notifications on this device',()=>enablePush(note),'btn primary'),el('div'));
  p1.append(btn('Send a test notification',async()=>{const j=await api('/api/test_push',{});note.textContent=j.sent?'Sent. Check your notifications.':'Nothing sent. Enable notifications first.'},'btn ghost'),note);
  v.append(p1);
  const p2=el('div','panel');p2.append(el('div','tag','CONTROL'));
  p2.append(el('p','muted',s.paused?'Notifications paused until '+s.paused+'.':'Notifications are on.'));
  const row=el('div','row');
  const act=a=>async()=>{await api('/api/settings',{action:a});loadSettings()};
  row.append(btn('Snooze 1h',act('snooze'),'btn'),btn('Pause till tomorrow',act('pause'),'btn'),btn('Resume',act('resume'),'btn'));
  p2.append(row);v.append(p2);
  const p3=el('div','panel');p3.append(el('div','tag','FOCUS TOPIC (NEW CARDS)'));
  const sel=el('select');sel.append(new Option('Mix of everything',''));
  for(const t of s.topics)sel.append(new Option(t.split(':')[0],t));
  if(s.focus&&!s.topics.includes(s.focus))sel.append(new Option(s.focus,s.focus));
  sel.value=s.focus||'';sel.onchange=async()=>{await api('/api/settings',{action:'focus',value:sel.value})};
  p3.append(sel,el('p','muted','Reviews of cards you have already seen still come back whatever the focus.'));v.append(p3);
  const p4=el('div','panel');p4.append(el('div','tag','INSTALL'),el('p','muted','In Chrome, open the menu and choose Install app (or Add to Home screen). It then opens like a normal app with its own icon.'));
  const p5=el('div','panel');p5.append(el('div','tag','MODEL'));
  const ms=el('select');
  for(const [id,label] of s.models)ms.append(new Option(label,id));
  ms.value=s.model||'';
  ms.onchange=async()=>{await api('/api/settings',{action:'model',value:ms.value})};
  p5.append(ms,el('p','muted','Applies from the next card or reply. Stronger models are more accurate on hard material but cost more per call.'));v.append(p5);
  v.append(p4)}

async function loadMode(){
  const j=await api('/study/api/mode'),b=$('#studybtn');
  if(j.on===undefined){b.hidden=true;return}
  b.hidden=false;b.textContent=j.on?'Study mode: ON':'Study mode: off';b.className='sbtn'+(j.on?' on':'');b.title=j.count+' file(s) selected'}
$('#studybtn').onclick=async()=>{
  const j=await api('/study/api/mode');if(j.on===undefined)return;
  const k=await api('/study/api/mode',{on:!j.on});
  if(k.error){alert(k.error);return}
  loadMode()};
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');
loadNext();loadMode();
</script></body></html>"""

if not (KEY and APP_PASSWORD):
    print("Missing ANTHROPIC_KEY or APP_PASSWORD")
    raise RuntimeError("Set ANTHROPIC_KEY and APP_PASSWORD")

import study
study.init(app)

threading.Thread(target=scheduler, daemon=True).start()
print("Learning web app started")
