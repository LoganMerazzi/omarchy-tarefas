#!/usr/bin/env python3
"""Tarefas: lista de pendências que insiste até você fazer.

Armazena em ~/.local/share/tarefas/tarefas.json. Usado pelo widget da barra
(logan.tarefas) e também direto pelo terminal:

  tarefas.py add "Pagar boleto amanhã 10h !"
  tarefas.py ls
  tarefas.py done <id>
  tarefas.py snooze <id> 60
  tarefas.py tick            # decide e envia as cobranças (chamado pelo widget)
"""

import datetime as dt
import fcntl
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
SELF = os.path.abspath(__file__)

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


def parse(text, now=None):
    """'Pagar boleto amanhã 10h !' -> (title, due_epoch|None, priority)."""
    now = now or dt.datetime.now()
    words = text.split()
    keep = []
    priority = 2
    date = None
    tod = None
    rel = None
    i = 0
    while i < len(words):
        w = words[i]
        f = fold(w).strip(",.;")
        nxt = fold(words[i + 1]).strip(",.;") if i + 1 < len(words) else ""

        if re.fullmatch(r"!{1,3}|!alta|!a|!urgente", f):
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
    return title, (int(due.timestamp()) if due else None), priority


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
    if not sent and now - (db.get("lastGeneralNag") or 0) >= interval:
        candidates = [t for t in awake if not (t.get("due") and t["due"] <= now)]
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
            if not p["pending"]:
                print("Nada pendente. 🎉")
            for t in p["pending"]:
                flag = "!" if t.get("priority") == 1 else ("·" if t.get("priority") == 2 else " ")
                extra = []
                if t["dueText"]:
                    extra.append(("ATRASADA " if t["overdue"] else "") + t["dueText"])
                if t["snoozed"]:
                    extra.append("adiada")
                print("%s %s  %s%s" % (t["id"][:6], flag, t["title"], ("   [" + ", ".join(extra) + "]") if extra else ""))
            return 0

        if cmd == "parse":
            title, due, pr = parse(" ".join(args))
            out({"title": title, "due": due, "dueText": human_due(due), "priority": pr})
            return 0

        if cmd == "add":
            text = " ".join(args).strip()
            title, due, pr = parse(text)
            if not title:
                print("Tarefa vazia", file=sys.stderr)
                return 2
            if sum(1 for t in db["tasks"] if not t.get("done")) >= MAX_TASKS:
                print("Muitas tarefas pendentes", file=sys.stderr)
                return 2
            t = {"id": uuid.uuid4().hex[:12], "title": title, "priority": pr, "due": due,
                 "created": now, "done": None, "snoozeUntil": 0, "snoozes": 0, "lastNag": 0}
            db["tasks"].append(t)
            changed = True
            out(t)

        elif cmd in ("done", "undo", "delete", "rm", "snooze", "snooze-from-notif", "priority", "due", "rename"):
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
                    _, due, _ = parse("x " + text)
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
            changed = True
            out({"ok": True})

        elif cmd == "tick":
            opts = {
                "interval": opt_int(args, "--interval", 60, 5, 24 * 60),
                "overdue_interval": opt_int(args, "--overdue-interval", 30, 5, 24 * 60),
                "max_snoozes": opt_int(args, "--max-snoozes", 3, 0, 99),
                "snooze_min": opt_int(args, "--snooze", 60, 5, 24 * 60),
                "quiet_start": opt_int(args, "--quiet-start", 22, 0, 23),
                "quiet_end": opt_int(args, "--quiet-end", 8, 0, 23),
            }
            sent = tick(db, now, opts)
            changed = bool(sent) or db.get("maxSnoozes") != opts["max_snoozes"]
            db["maxSnoozes"] = opts["max_snoozes"]
            out({"sent": sent})

        else:
            print("Comando desconhecido: %s" % cmd, file=sys.stderr)
            return 2

        if changed:
            save(db)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
