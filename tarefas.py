#!/usr/bin/env python3
"""Tarefas: lista de pendências que insiste até você fazer.

Armazena em ~/.local/share/tarefas/tarefas.json. Usado pelo widget da barra
(logan.tarefas) e também direto pelo terminal:

  tarefas.py add "Pagar boleto amanhã 10h !"
  tarefas.py ls [#tag]
  tarefas.py tag <id> +obra -casa
  tarefas.py done <id>
  tarefas.py snooze <id> 60
  tarefas.py tick            # decide e envia as cobranças (chamado pelo widget)
  tarefas.py sync            # sincroniza com o servidor CalDAV (ver sincronia.py)
  tarefas.py boletos [-n]    # cria tarefas dos boletos do Drive (-n: só mostra)
  tarefas.py abrir <id>      # abre o boleto da tarefa
  tarefas.py copiar <id>     # copia o código de barras
"""

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "tarefas")
DATA_FILE = os.path.join(DATA_DIR, "tarefas.json")
LOCK_FILE = os.path.join(DATA_DIR, ".lock")
SYNC_LOCK_FILE = os.path.join(DATA_DIR, ".sync.lock")
SELF = os.path.abspath(__file__)
SYNC_EVERY = 120          # segundos entre sincronizações automáticas
MAX_TOMBSTONES = 500

# Rodamos com `python3 -I`, que não põe a pasta do script no sys.path.
sys.path.insert(0, os.path.dirname(SELF))
import sincronia  # noqa: E402
import boletos  # noqa: E402

BOLETOS_EVERY = 30 * 60
BOLETOS_LOCK_FILE = os.path.join(DATA_DIR, ".boletos.lock")
SKIP_WORDS = ("nao pagar", "errad", "cancelad", "substituid")
RECEIPT_WINDOW_DAYS = 20

MAX_TITLE = 200
MAX_TASKS = 500
KEEP_DONE_DAYS = 7
PRIORITY_NAMES = {1: "alta", 2: "média", 3: "baixa"}

WEEKDAYS = {
    "segunda": 0, "seg": 0, "segunda-feira": 0,
    "terca": 1, "ter": 1, "terca-feira": 1,
    "quarta": 2, "qua": 2, "quarta-feira": 2,
    "quinta": 3, "qui": 3, "quinta-feira": 3,
    "sexta": 4, "sex": 4, "sexta-feira": 4,
    "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}

DEFAULT_MORNING_HOUR = 9


# ── Storage ────────────────────────────────────────────────────────────────

def empty_db():
    return {"version": 1, "tasks": [], "lastGeneralNag": 0}


def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            db = json.load(f)
    except FileNotFoundError:
        return empty_db()
    except (OSError, ValueError):
        # Arquivo corrompido: guarda uma cópia em vez de perder tudo.
        try:
            os.replace(DATA_FILE, DATA_FILE + ".corrompido.%d" % int(time.time()))
        except OSError:
            pass
        return empty_db()
    if not isinstance(db, dict) or not isinstance(db.get("tasks"), list):
        return empty_db()
    db.setdefault("lastGeneralNag", 0)
    db.setdefault("tombstones", [])
    db["tasks"] = [t for t in db["tasks"] if isinstance(t, dict) and t.get("id")][:MAX_TASKS]
    return db


def save(db):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".tarefas.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


class Locked:
    def __enter__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.fh = open(LOCK_FILE, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def prune(db, now):
    cutoff = now - KEEP_DONE_DAYS * 86400
    db["tasks"] = [t for t in db["tasks"] if not t.get("done") or t["done"] >= cutoff]


def touch(t, now):
    """Marca uma mudança que precisa ir para o servidor."""
    t["rev"] = t.get("rev", 0) + 1
    t["modified"] = now


def find(db, task_id):
    task_id = str(task_id)
    for t in db["tasks"]:
        if t["id"] == task_id or t["id"].startswith(task_id) and len(task_id) >= 4:
            return t
    return None


# ── Natural-language parsing (pt-BR) ────────────────────────────────────────

def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


TIME_RE = re.compile(r"^(?:(\d{1,2})h(\d{2})?|(\d{1,2}):(\d{2}))$")
DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")
REL_RE = re.compile(r"^(\d{1,3})(min|m|h|d)$")
TAG_RE = re.compile(r"^#([\w-]{1,30})$")
MAX_TAGS = 8


def norm_tag(tag):
    """'#Obra-Casa' → 'obra-casa' (minúsculas; acentos mantidos)."""
    return tag.lstrip("#").strip().lower()[:30]


def add_tags(tags, new):
    for t in new:
        t = norm_tag(t)
        if t and t not in tags and len(tags) < MAX_TAGS:
            tags.append(t)
    return tags


def parse(text, now=None):
    """'Pagar boleto amanhã 10h ! #casa' -> (title, due_epoch|None, priority, tags)."""
    now = now or dt.datetime.now()
    words = text.split()
    keep = []
    priority = 2
    date = None
    tod = None
    rel = None
    tags = []
    i = 0
    while i < len(words):
        w = words[i]
        f = fold(w).strip(",.;")
        nxt = fold(words[i + 1]).strip(",.;") if i + 1 < len(words) else ""

        if TAG_RE.match(w.strip(",.;")):
            add_tags(tags, [TAG_RE.match(w.strip(",.;")).group(1)])
        elif re.fullmatch(r"!{1,3}|!alta|!a|!urgente", f):
            priority = 1
        elif f in ("!media", "!m"):
            priority = 2
        elif f in ("!baixa", "!b"):
            priority = 3
        elif f == "hoje":
            date = now.date()
        elif f == "amanha":
            date = now.date() + dt.timedelta(days=1)
        elif f == "depois" and nxt == "de" and i + 2 < len(words) and fold(words[i + 2]).strip(",.;") == "amanha":
            date = now.date() + dt.timedelta(days=2)
            i += 2
        elif f in WEEKDAYS or (f in ("na", "no", "dia", "em") and (nxt in WEEKDAYS or DATE_RE.match(nxt) or REL_RE.match(nxt) or (f == "dia" and nxt.isdigit()))):
            if f in WEEKDAYS:
                ahead = (WEEKDAYS[f] - now.weekday()) % 7 or 7
                date = now.date() + dt.timedelta(days=ahead)
            elif f == "dia" and nxt.isdigit():
                day = int(nxt)
                cand = None
                for add in range(0, 3):
                    m = (now.month - 1 + add) % 12 + 1
                    y = now.year + (now.month - 1 + add) // 12
                    try:
                        c = dt.date(y, m, day)
                    except ValueError:
                        continue
                    if c >= now.date():
                        cand = c
                        break
                if cand:
                    date = cand
                    i += 1
                else:
                    keep.append(w)
            # "na/no/em" seguido de algo reconhecível: descarta a preposição
        elif DATE_RE.match(f):
            d, m, y = DATE_RE.match(f).groups()
            y = int(y) + (2000 if y and len(y) == 2 else 0) if y else now.year
            try:
                date = dt.date(y, int(m), int(d))
                if not DATE_RE.match(f).group(3) and date < now.date():
                    date = dt.date(y + 1, int(m), int(d))
            except ValueError:
                keep.append(w)
        elif REL_RE.match(f) and i > 0 and fold(words[i - 1]) == "em":
            n, unit = REL_RE.match(f).groups()
            mult = {"min": 60, "m": 60, "h": 3600, "d": 86400}[unit]
            rel = now + dt.timedelta(seconds=int(n) * mult)
        elif f in ("as", "às", "a", "ate") and TIME_RE.match(nxt):
            pass  # "às 10h": a preposição some, a hora é lida na próxima volta
        elif TIME_RE.match(f):
            g = TIME_RE.match(f).groups()
            h = int(g[0] or g[2])
            mi = int(g[1] or g[3] or 0)
            if h < 24 and mi < 60:
                tod = dt.time(h, mi)
            else:
                keep.append(w)
        else:
            keep.append(w)
        i += 1

    # Remove um "em" solto que sobrou antes de um tempo relativo, e conectivos
    # pendurados no fim ("Ligar pro banco até").
    while keep and fold(keep[-1]) in ("em", "na", "no", "as", "ate", "dia", "de"):
        keep.pop()

    title = " ".join(keep).strip()[:MAX_TITLE]
    due = None
    if rel:
        due = rel
    elif date or tod:
        if date and tod:
            due = dt.datetime.combine(date, tod)
        elif tod:
            due = dt.datetime.combine(now.date(), tod)
            if due <= now:
                due += dt.timedelta(days=1)
        else:
            due = dt.datetime.combine(date, dt.time(DEFAULT_MORNING_HOUR, 0))
            if due < now:
                due = now.replace(second=0, microsecond=0)
    return title, (int(due.timestamp()) if due else None), priority, tags


# ── Formatting ──────────────────────────────────────────────────────────────

def human_due(due, now=None):
    if not due:
        return ""
    now = now or time.time()
    d = dt.datetime.fromtimestamp(due)
    today = dt.date.today()
    hm = d.strftime("%H:%M")
    if d.date() == today:
        day = "hoje"
    elif d.date() == today + dt.timedelta(days=1):
        day = "amanhã"
    elif d.date() == today - dt.timedelta(days=1):
        day = "ontem"
    else:
        names = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
        day = "%s %s" % (names[d.weekday()], d.strftime("%d/%m"))
    return "%s %s" % (day, hm)


def human_ago(seconds):
    m = int(seconds // 60)
    if m < 60:
        return "%d min" % max(1, m)
    h = m // 60
    if h < 48:
        return "%dh" % h
    return "%d dias" % (h // 24)


def sort_key(t):
    return (t.get("priority", 2), t.get("due") or 1e12, t.get("created", 0))


def public(db, now):
    tasks = []
    for t in db["tasks"]:
        tt = dict(t)
        tt["overdue"] = bool(not t.get("done") and t.get("due") and t["due"] <= now)
        tt["snoozed"] = bool(not t.get("done") and (t.get("snoozeUntil") or 0) > now)
        tt["dueText"] = human_due(t.get("due"))
        tasks.append(tt)
    pending = sorted([t for t in tasks if not t.get("done")], key=lambda t: (not t["overdue"],) + sort_key(t))
    done = sorted([t for t in tasks if t.get("done")], key=lambda t: -t["done"])
    return {
        "now": now,
        "pending": pending,
        "done": done,
        "pendingCount": len(pending),
        "overdueCount": sum(1 for t in pending if t["overdue"]),
        "sync": db.get("sync"),
        "tags": sorted({x for t in pending for x in (t.get("tags") or [])}),
    }


# ── Notifications ───────────────────────────────────────────────────────────

def notify(task, headline, body, urgency, can_snooze, snooze_min):
    cmd = [
        "omarchy-notification-send", "--app-name", "Tarefas", "-g", "󰄲", "-u", urgency,
        "--transient", "-p",
    ]
    if task.get("notifId"):
        cmd += ["-r", str(task["notifId"])]
    cmd += [headline, body]
    # A primeira ação é o clique no card: abre a lista, nunca conclui sem querer.
    cmd += ["--action", "Abrir lista", sys.executable, "-I", SELF, "open"]
    if can_snooze:
        cmd += ["--action", "Adiar %s" % human_ago(snooze_min * 60), sys.executable, "-I", SELF, "snooze-from-notif", str(snooze_min)]
    cmd += ["--action", "Feito ✓", sys.executable, "-I", SELF, "done"]
    cmd += ["--with", task["id"]]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        nid = out.stdout.strip()
        if nid.isdigit():
            task["notifId"] = int(nid)
    except (OSError, subprocess.SubprocessError):
        pass


def in_quiet_hours(now, start, end):
    h = dt.datetime.fromtimestamp(now).hour
    if start == end:
        return False
    if start > end:  # ex.: 22 → 8
        return h >= start or h < end
    return start <= h < end


def tick(db, now, opts):
    interval = opts["interval"] * 60
    overdue_interval = opts["overdue_interval"] * 60
    max_snoozes = opts["max_snoozes"]
    snooze_min = opts["snooze_min"]
    sent = []

    if in_quiet_hours(now, opts["quiet_start"], opts["quiet_end"]):
        return sent

    pending = [t for t in db["tasks"] if not t.get("done")]
    awake = [t for t in pending if (t.get("snoozeUntil") or 0) <= now]
    remaining = len(pending)

    def fire(t, headline, body, urgency):
        can_snooze = t.get("snoozes", 0) < max_snoozes
        if not can_snooze:
            body = (body + "\n" if body else "") + "Sem mais adiamentos: faça ou apague."
        else:
            left = max_snoozes - t.get("snoozes", 0)
            body = (body + "\n" if body else "") + ("Resta 1 adiamento." if left == 1 else "Restam %d adiamentos." % left)
        notify(t, headline, body, urgency, can_snooze, snooze_min)
        t["lastNag"] = now
        sent.append(t["id"])

    # 1) Prazos que chegaram e adiamentos que acabaram.
    for t in sorted(awake, key=sort_key):
        if len(sent) >= 3:
            break
        wake = max(t.get("due") or 0, t.get("snoozeUntil") or 0)
        last = t.get("lastNag") or 0
        due = t.get("due")
        if wake and wake <= now and last < wake:
            label = "Agora" if due and now - due < 600 else ("Atrasada" if due and due <= now else "De volta")
            fire(t, "%s: %s" % (label, t["title"]), human_due(due) if due else "", "critical" if due and due <= now else "normal")
        elif due and due <= now and now - last >= overdue_interval:
            fire(t, "Atrasada há %s: %s" % (human_ago(now - due), t["title"]), "Era para %s." % human_due(due), "critical")

    # 2) Cobrança geral: a pendência mais importante, a cada `interval`.
    #    Com prazo, só entra quando o prazo está perto (`near_hours`); antes
    #    disso a tarefa fica quieta até a hora do aviso de prazo.
    if not sent and now - (db.get("lastGeneralNag") or 0) >= interval:
        near = opts["near_hours"] * 3600
        candidates = [t for t in awake if not (t.get("due") and t["due"] <= now)
                      and (not t.get("due") or t["due"] - now <= near)]
        candidates = [t for t in candidates if not t.get("due") or t["due"] - now > 3600 or t.get("priority", 2) == 1] or candidates
        if candidates:
            t = sorted(candidates, key=sort_key)[0]
            others = remaining - 1
            body = PRIORITY_NAMES.get(t.get("priority", 2), "") .capitalize() + " prioridade"
            if t.get("due"):
                body += " · prazo %s" % human_due(t["due"])
            if others > 0:
                body += "\n+%d %s" % (others, "outra pendente" if others == 1 else "outras pendentes")
            fire(t, "Pendente: %s" % t["title"], body, "normal")
            db["lastGeneralNag"] = now
    return sent


# ── Sincronização ───────────────────────────────────────────────────────────

def spawn_sync():
    spawn("sync")


def spawn(what):
    """Roda um subcomando em segundo plano: o widget e o terminal não esperam a rede."""
    try:
        subprocess.Popen([sys.executable, "-I", SELF, what], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def is_dirty(t):
    return not t.get("href") or t.get("rev", 0) != t.get("syncedRev", 0)


def apply_remote(t, r, now):
    old_due = t.get("due")
    for k in ("title", "priority", "done", "notes", "url", "uid", "tags"):
        t[k] = r.get(k)
    t["due"] = r.get("due")
    if "snoozes" in r:
        t["snoozes"] = r["snoozes"]
    if "snoozeUntil" in r:
        t["snoozeUntil"] = r["snoozeUntil"]
    if t["due"] != old_due:
        t["lastNag"] = 0 if t["due"] and t["due"] > now else now
    t["modified"] = r.get("lastModified") or now


def merge(db, remote, now):
    """Aplica o que mudou no servidor e devolve o que falta enviar.

    Conflito (mudou dos dois lados): vence a edição mais recente.
    """
    tomb = set(db.get("tombstones", []))
    known = set()
    uploads = []
    for t in list(db["tasks"]):
        href = t.get("href")
        if href:
            known.add(href)
            if href not in remote:
                if is_dirty(t):
                    t["etag"] = None            # apagada lá, mas mexida aqui: recria
                else:
                    db["tasks"].remove(t)       # apagada em outro aparelho
                    continue
            else:
                etag, ics = remote[href]
                if etag != t.get("etag"):
                    r = sincronia.from_ics(ics)
                    if r and not (is_dirty(t) and (t.get("modified") or 0) > (r["lastModified"] or 0)):
                        apply_remote(t, r, now)
                        t["etag"], t["ics"] = etag, ics
                        t["syncedRev"] = t.get("rev", 0)
                        continue
                    t["etag"] = etag            # vence a local: sobrescreve a versão atual
        if is_dirty(t):
            t.setdefault("uid", "%s@tarefas" % t["id"])
            uploads.append({"id": t["id"], "rev": t.get("rev", 0), "href": href or t.get("newHref") or "%s.ics" % t["id"],
                            "etag": t.get("etag"), "ics": sincronia.to_ics(t, now, t.get("ics"))})

    for href, (etag, ics) in remote.items():
        if href in known or href in tomb:
            continue
        r = sincronia.from_ics(ics)
        # Concluídas que não conhecemos ficam só no servidor (histórico do celular).
        if not r or r["done"] or not r["title"]:
            continue
        if sum(1 for t in db["tasks"] if not t.get("done")) >= MAX_TASKS:
            break
        t = {"id": uuid.uuid4().hex[:12], "created": now, "snoozeUntil": 0, "snoozes": 0, "lastNag": 0,
             "href": href, "rev": 0, "syncedRev": 0}
        apply_remote(t, r, now)
        t["title"] = t["title"][:MAX_TITLE]
        t["etag"], t["ics"] = etag, ics
        db["tasks"].append(t)

    deletes = [h for h in tomb if h in remote]
    db["tombstones"] = []
    return uploads, deletes


def sync(verbose=False):
    cfg = sincronia.config()
    if not cfg:
        if verbose:
            print("Sincronização desligada: falta %s" % sincronia.CONFIG_FILE)
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = open(SYNC_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # já tem uma sincronização rodando

    dav = sincronia.Dav(cfg)
    status = {"ok": True}
    stats = {"enviadas": 0, "apagadas": 0}
    try:
        for _round in range(3):
            dav.ensure_collection()
            remote = dav.fetch_all()                     # rede, sem segurar a lista
            now = int(time.time())
            with Locked():
                db = load()
                uploads, deletes = merge(db, remote, now)
                save(db)
            results = {}
            for u in uploads:                            # rede, sem segurar a lista
                results[u["id"]] = dav.put(u["href"], u["ics"], u["etag"])
            for h in deletes:
                dav.delete(h)
                stats["apagadas"] += 1
            with Locked():
                db = load()
                for u in uploads:
                    ok, etag = results[u["id"]]
                    t = next((x for x in db["tasks"] if x["id"] == u["id"]), None)
                    if not ok and t and not u["etag"]:
                        # Já existe no servidor (outra máquina criou o mesmo boleto):
                        # fica a de lá, que entra na próxima volta.
                        db["tasks"].remove(t)
                        continue
                    if not ok or not t:
                        continue                         # 412: resolve na próxima volta
                    stats["enviadas"] += 1
                    t["href"], t["etag"], t["ics"] = u["href"], etag, u["ics"]
                    if t.get("rev", 0) == u["rev"]:
                        t["syncedRev"] = u["rev"]
                pending = any(is_dirty(t) for t in db["tasks"]) or db.get("tombstones")
                save(db)
            if not pending:
                break
    except sincronia.DavError as e:
        status = {"ok": False, "error": str(e)[:160]}
    status["at"] = int(time.time())
    with Locked():
        db = load()
        db["sync"] = status
        save(db)
    if verbose:
        if status["ok"]:
            print("Sincronizado: %d enviada(s), %d apagada(s) no servidor." % (stats["enviadas"], stats["apagadas"]))
        else:
            print("Falhou: %s" % status["error"], file=sys.stderr)
    return 0 if status["ok"] else 1


# ── Boletos ─────────────────────────────────────────────────────────────────

def barcode_of(t):
    if t.get("barcodeDigits"):
        return t["barcodeDigits"]
    m = re.search(r"Código de barras:\s*([\d .\-]+)", t.get("notes") or "")
    return re.sub(r"\D", "", m.group(1)) if m else ""


def _merge_group(group):
    b = dict(group[0])
    for o in group[1:]:
        for k in ("due", "value", "barcode", "barcodeDigits", "url"):
            b[k] = b.get(k) or o.get(k)
        if b.get("pdf") != "ok" and o.get("pdf") == "ok":
            b["pdf"] = "ok"
    return b


def import_boletos(dry_run=False, verbose=False):
    if not boletos.config():
        if verbose:
            print("Boletos desligado: falta %s" % boletos.CONF_FILE)
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = open(BOLETOS_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    with Locked():
        db0 = load()
        seen = dict(db0.get("boletosVistos") or {})
        # Tarefas criadas de PDF protegido: relê o arquivo até alguma senha servir.
        for k in (db0.get("boletosProtegidos") or {}):
            seen.pop(k, None)
    today = dt.date.today()
    bills, receipts, warnings = boletos.scan(today, seen=set(seen))   # lento: sem segurar a lista

    groups = {}
    for b in bills:
        groups.setdefault(b["key"], []).append(b)

    now = int(time.time())
    created, completed, skipped = [], [], []
    with Locked():
        db = load()
        seen = db.setdefault("boletosVistos", {})
        locked_pdfs = db.setdefault("boletosProtegidos", {})
        enriched = []
        first_run = not db.get("boletosInit")
        cutoff = today if first_run else today - dt.timedelta(days=boletos.LOOKBACK_DAYS)

        def paid(match_key, due):
            return any(r["key"] == match_key and due and abs((r["date"] - due).days) <= RECEIPT_WINDOW_DAYS
                       for r in receipts)

        for key, group in groups.items():
            b = _merge_group(group)
            keys = [key] + [g["fileKey"] for g in group]
            old_ids = [locked_pdfs[g["fileKey"]] for g in group if g["fileKey"] in locked_pdfs]
            if old_ids:
                if b["pdf"] != "ok":
                    continue                     # ainda sem senha que sirva
                for g in group:
                    locked_pdfs.pop(g["fileKey"], None)
                for k in keys:
                    seen[k] = now
                t = next((x for x in db["tasks"] if x["id"] == old_ids[0]), None)
                if t and not t.get("done"):
                    t["title"], t["notes"] = (x[:MAX_TITLE] if i == 0 else x for i, x in enumerate(boletos.task_text(b)))
                    t["barcodeDigits"] = b["barcodeDigits"]
                    if b["due"]:
                        t["due"] = int(dt.datetime.combine(b["due"], dt.time(DEFAULT_MORNING_HOUR)).timestamp())
                    enriched.append(t)
                    if not dry_run:
                        touch(t, now)
                continue
            if any(k in seen for k in keys):
                for k in keys:
                    seen[k] = now
                continue
            for k in keys:
                seen[k] = now
            label = "%s/%s" % (b["month"], b["file"])
            if any(w in boletos.fold(b["file"]) for w in SKIP_WORDS):
                skipped.append((label, "marcado para não pagar"))
                continue
            if not b["due"]:
                skipped.append((label, "sem vencimento"))
                continue
            if b["due"] < cutoff:
                skipped.append((label, "vencido em %s" % b["due"].strftime("%d/%m")))
                continue
            if paid(b["matchKey"], b["due"]):
                skipped.append((label, "já tem comprovante"))
                continue
            title, notes = boletos.task_text(b)
            due = int(dt.datetime.combine(b["due"], dt.time(DEFAULT_MORNING_HOUR)).timestamp())
            tid = uuid.uuid4().hex[:12]
            # UID fixo pelo código de barras: duas máquinas lendo o mesmo boleto
            # geram a mesma tarefa no servidor.
            uid = "boleto-" + hashlib.sha1(key.encode()).hexdigest()[:20]
            t = {"id": tid, "uid": uid + "@tarefas", "newHref": uid + ".ics", "title": title[:MAX_TITLE],
                 "priority": 1, "due": due, "created": now, "done": None, "snoozeUntil": 0, "snoozes": 0,
                 "lastNag": 0, "notes": notes, "url": b["url"], "localPath": b["path"],
                 "barcodeDigits": b["barcodeDigits"], "boleto": b["matchKey"],
                 "tags": add_tags([], ["pagamento", b["label"] or b["source"]])}
            if any(x.get("uid") == t["uid"] for x in db["tasks"]):
                continue
            touch(t, now)
            created.append(t)
            if not dry_run:
                db["tasks"].append(t)
                if b["pdf"] == "protegido":
                    for g in group:
                        locked_pdfs[g["fileKey"]] = tid
                        seen.pop(g["fileKey"], None)

        for r in receipts:
            rk = "rec:%s:%s:%s" % (r["source"], r["date"], r["key"])
            if rk in seen:
                continue
            seen[rk] = now
            for t in db["tasks"]:
                if t.get("done") or not t.get("boleto") or t["boleto"] != r["key"] or not t.get("due"):
                    continue
                if abs((dt.date.fromtimestamp(t["due"]) - r["date"]).days) <= RECEIPT_WINDOW_DAYS:
                    completed.append(t)
                    if not dry_run:
                        t["done"] = now
                        touch(t, now)

        old = now - 400 * 86400
        db["boletosVistos"] = {k: v for k, v in seen.items() if v >= old}
        live = {t["id"] for t in db["tasks"] if not t.get("done")}
        db["boletosProtegidos"] = {k: v for k, v in locked_pdfs.items() if v in live}
        if not dry_run:
            db["boletosInit"] = db.get("boletosInit") or now
            save(db)

    if verbose:
        print(("[simulação] " if dry_run else "") + "%d tarefa(s) criada(s), %d concluída(s) por comprovante."
              % (len(created), len(completed)))
        for t in created:
            print("  + %s  [%s]" % (t["title"], human_due(t["due"])))
        for t in completed:
            print("  ✓ %s" % t["title"])
        for t in enriched:
            print("  ↻ %s (PDF aberto, dados completados)" % t["title"])
        if skipped:
            print("Ignorados:")
            for label, why in skipped:
                print("  - %s: %s" % (label, why))
        for w in warnings:
            print("  ! " + w)
    if (created or completed or enriched) and not dry_run and sincronia.config():
        spawn_sync()
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def opt_int(args, name, default, lo, hi):
    if name in args:
        try:
            v = int(args[args.index(name) + 1])
            return max(lo, min(hi, v))
        except (ValueError, IndexError):
            pass
    return default


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    now = int(time.time())

    if cmd == "sync":
        return sync(verbose=sys.stdout.isatty() or "-v" in args)

    if cmd == "boletos":
        return import_boletos(dry_run="-n" in args, verbose=sys.stdout.isatty() or "-v" in args)

    if cmd == "open":
        subprocess.run(["omarchy-shell", "-q", "logan.tarefas", "open"], timeout=10)
        return 0

    with Locked():
        db = load()
        prune(db, now)
        changed = False

        if cmd in ("list", "json"):
            out(public(db, now))
            return 0

        if cmd == "ls":
            p = public(db, now)
            st = p.get("sync")
            if st:
                when = dt.datetime.fromtimestamp(st.get("at", 0)).strftime("%d/%m %H:%M")
                print(("󰓦 sincronizado %s" % when) if st.get("ok") else ("󰓨 sem sincronizar (%s): %s" % (when, st.get("error", ""))))
            if not p["pending"]:
                print("Nada pendente. 🎉")
            want = [norm_tag(a) for a in args if a.startswith("#")]
            for t in p["pending"]:
                if want and not all(w in (t.get("tags") or []) for w in want):
                    continue
                flag = "!" if t.get("priority") == 1 else ("·" if t.get("priority") == 2 else " ")
                extra = []
                if t["dueText"]:
                    extra.append(("ATRASADA " if t["overdue"] else "") + t["dueText"])
                if t["snoozed"]:
                    extra.append("adiada")
                tags = "".join("  #" + x for x in (t.get("tags") or []))
                print("%s %s  %s%s%s" % (t["id"][:6], flag, t["title"], tags, ("   [" + ", ".join(extra) + "]") if extra else ""))
            return 0

        if cmd == "parse":
            title, due, pr, tags = parse(" ".join(args))
            out({"title": title, "due": due, "dueText": human_due(due), "priority": pr, "tags": tags})
            return 0

        if cmd == "add":
            text = " ".join(args).strip()
            title, due, pr, tags = parse(text)
            if not title:
                print("Tarefa vazia", file=sys.stderr)
                return 2
            if sum(1 for t in db["tasks"] if not t.get("done")) >= MAX_TASKS:
                print("Muitas tarefas pendentes", file=sys.stderr)
                return 2
            tid = uuid.uuid4().hex[:12]
            t = {"id": tid, "uid": "%s@tarefas" % tid, "title": title, "priority": pr, "due": due, "tags": tags,
                 "created": now, "done": None, "snoozeUntil": 0, "snoozes": 0, "lastNag": 0}
            touch(t, now)
            db["tasks"].append(t)
            changed = True
            out(t)

        elif cmd in ("done", "undo", "delete", "rm", "snooze", "snooze-from-notif", "priority", "due", "rename", "tag"):
            if not args:
                print("Faltou o id", file=sys.stderr)
                return 2
            # snooze-from-notif <min> <id>  (a notificação acrescenta o id no fim)
            if cmd == "snooze-from-notif":
                if len(args) < 2:
                    return 2
                args = [args[-1], args[0]]
                cmd = "snooze"
            t = find(db, args[0])
            if not t:
                print("Tarefa não encontrada", file=sys.stderr)
                return 1
            if cmd == "done":
                t["done"] = now
            elif cmd == "undo":
                t["done"] = None
                t["lastNag"] = now
            elif cmd in ("delete", "rm"):
                db["tasks"].remove(t)
                if t.get("href"):
                    db["tombstones"] = (db["tombstones"] + [t["href"]])[-MAX_TOMBSTONES:]
            elif cmd == "snooze":
                max_snoozes = opt_int(args, "--max", int(db.get("maxSnoozes", 3)), 0, 99)
                if t.get("snoozes", 0) >= max_snoozes:
                    print("Sem mais adiamentos para esta tarefa", file=sys.stderr)
                    return 3
                arg = args[1] if len(args) > 1 else "60"
                if fold(arg) == "amanha":
                    until = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time(DEFAULT_MORNING_HOUR))
                    t["snoozeUntil"] = int(until.timestamp())
                else:
                    try:
                        minutes = max(1, min(7 * 24 * 60, int(arg)))
                    except ValueError:
                        return 2
                    t["snoozeUntil"] = now + minutes * 60
                t["snoozes"] = t.get("snoozes", 0) + 1
            elif cmd == "priority":
                try:
                    t["priority"] = max(1, min(3, int(args[1])))
                except (ValueError, IndexError):
                    return 2
            elif cmd == "due":
                text = " ".join(args[1:])
                if fold(text) in ("", "nenhum", "sem", "remover"):
                    t["due"] = None
                else:
                    _, due, _, _ = parse("x " + text)
                    if not due:
                        print("Não entendi a data", file=sys.stderr)
                        return 2
                    t["due"] = due
                t["lastNag"] = 0 if t["due"] and t["due"] > now else now
            elif cmd == "rename":
                title = " ".join(args[1:]).strip()[:MAX_TITLE]
                if not title:
                    return 2
                t["title"] = title
            elif cmd == "tag":
                tags = list(t.get("tags") or [])
                for a in args[1:]:
                    if a.startswith("-"):
                        tags = [x for x in tags if x != norm_tag(a[1:])]
                    else:
                        add_tags(tags, [a.lstrip("+")])
                t["tags"] = tags
            if cmd not in ("delete", "rm"):
                touch(t, now)
            changed = True
            out({"ok": True})

        elif cmd in ("abrir", "copiar"):
            t = find(db, args[0]) if args else None
            if not t:
                print("Tarefa não encontrada", file=sys.stderr)
                return 1
            if cmd == "abrir":
                target = t.get("localPath") if t.get("localPath") and os.path.exists(t["localPath"]) else t.get("url")
                if not target:
                    print("Esta tarefa não tem boleto", file=sys.stderr)
                    return 1
                subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            else:
                code = barcode_of(t)
                if not code:
                    print("Esta tarefa não tem código de barras", file=sys.stderr)
                    return 1
                subprocess.run(["wl-copy", code], timeout=5)
                subprocess.run(["omarchy-notification-send", "--app-name", "Tarefas", "-g", "󰆏", "-t", "3000",
                                "Código copiado", t.get("title", "")], timeout=10, capture_output=True)
            out({"ok": True})
            return 0

        elif cmd == "tick":
            opts = {
                "interval": opt_int(args, "--interval", 60, 5, 24 * 60),
                "overdue_interval": opt_int(args, "--overdue-interval", 30, 5, 24 * 60),
                "max_snoozes": opt_int(args, "--max-snoozes", 3, 0, 99),
                "snooze_min": opt_int(args, "--snooze", 60, 5, 24 * 60),
                "quiet_start": opt_int(args, "--quiet-start", 22, 0, 23),
                "quiet_end": opt_int(args, "--quiet-end", 8, 0, 23),
                "near_hours": opt_int(args, "--near-hours", 24, 0, 24 * 14),
            }
            sent = tick(db, now, opts)
            if now - (db.get("lastSyncTry") or 0) >= SYNC_EVERY and sincronia.config():
                db["lastSyncTry"] = now
                changed = True
                spawn_sync()
            if now - (db.get("lastBoletosTry") or 0) >= BOLETOS_EVERY and boletos.config():
                db["lastBoletosTry"] = now
                changed = True
                spawn("boletos")
            changed = bool(sent) or db.get("maxSnoozes") != opts["max_snoozes"]
            db["maxSnoozes"] = opts["max_snoozes"]
            out({"sent": sent})

        else:
            print("Comando desconhecido: %s" % cmd, file=sys.stderr)
            return 2

        if changed:
            save(db)
    if changed and cmd not in ("tick",) and sincronia.config():
        spawn_sync()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
