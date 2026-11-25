"""Sincronização das tarefas com um servidor CalDAV (Radicale no Bastion).

Cada tarefa vira um VTODO num calendário só de tarefas, assim qualquer cliente
CalDAV (Tasks.org no Android, por exemplo) enxerga a mesma lista.

Configuração em ~/.config/tarefas/caldav.env (chmod 600):

  CALDAV_URL=https://bastion.exemplo.ts.net/
  CALDAV_USER=logan
  CALDAV_PASSWORD=...
  CALDAV_COLLECTION=/logan/tarefas/     # opcional; padrão /<usuário>/tarefas/

Só usa a biblioteca padrão.
"""

import base64
import datetime as dt
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "tarefas", "caldav.env")
TIMEOUT = 8
PRODID = "-//logan//tarefas//PT"

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}


class DavError(Exception):
    pass


# ── Configuração ───────────────────────────────────────────────────────────

def config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    cfg = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    if not cfg.get("CALDAV_URL") or not cfg.get("CALDAV_USER"):
        return None
    return cfg


# ── HTTP / WebDAV ──────────────────────────────────────────────────────────

class Dav:
    def __init__(self, cfg):
        base = cfg["CALDAV_URL"].rstrip("/") + "/"
        coll = cfg.get("CALDAV_COLLECTION") or "/%s/tarefas/" % urllib.parse.quote(cfg["CALDAV_USER"])
        self.collection = urllib.parse.urljoin(base, coll.rstrip("/") + "/")
        token = base64.b64encode(("%s:%s" % (cfg["CALDAV_USER"], cfg.get("CALDAV_PASSWORD", ""))).encode()).decode()
        self.auth = "Basic " + token
        self.ctx = ssl.create_default_context()
        # Redirecionamentos levariam a senha a outro lugar; não segue.
        self.opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=self.ctx))

    def request(self, method, url, body=None, headers=None, ok=(200, 201, 204, 207)):
        h = {"Authorization": self.auth, "User-Agent": "tarefas"}
        if body is not None:
            body = body.encode("utf-8") if isinstance(body, str) else body
        h.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                status, rh, data = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, rh, data = e.code, e.headers, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise DavError("sem conexão: %s" % getattr(e, "reason", e))
        if status == 401:
            raise DavError("usuário ou senha recusados")
        if status not in ok:
            raise DavError("%s %s → HTTP %d" % (method, urllib.parse.urlparse(url).path, status))
        return status, rh, data

    def href_url(self, href):
        return urllib.parse.urljoin(self.collection, href)

    def ensure_collection(self):
        st, _, _ = self.request("PROPFIND", self.collection, "", {"Depth": "0"}, ok=(207, 404))
        if st != 404:
            return False
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:mkcol xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            '<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
            '<d:displayname>Tarefas</d:displayname>'
            '<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>'
            '</d:prop></d:set></d:mkcol>'
        )
        self.request("MKCOL", self.collection, body, {"Content-Type": "application/xml; charset=utf-8"}, ok=(201,))
        return True

    def fetch_all(self):
        """{href (nome do arquivo): (etag, ics)} de todos os VTODO."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VTODO"/></c:comp-filter></c:filter>'
            '</c:calendar-query>'
        )
        _, _, data = self.request("REPORT", self.collection, body,
                                  {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, ok=(207,))
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise DavError("resposta inválida do servidor")
        coll_path = urllib.parse.urlparse(self.collection).path
        out = {}
        for resp in root.findall("d:response", NS):
            href = urllib.parse.unquote(resp.findtext("d:href", "", NS))
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if not name or href.rstrip("/") == coll_path.rstrip("/"):
                continue
            etag = ics = None
            for ps in resp.findall("d:propstat", NS):
                if "200" not in (ps.findtext("d:status", "", NS) or ""):
                    continue
                etag = ps.findtext("d:prop/d:getetag", None, NS) or etag
                ics = ps.findtext("d:prop/c:calendar-data", None, NS) or ics
            if ics:
                out[name] = (etag, ics)
        return out

    def put(self, href, ics, etag):
        h = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            h["If-Match"] = etag
        else:
            h["If-None-Match"] = "*"
        st, rh, _ = self.request("PUT", self.href_url(urllib.parse.quote(href)), ics, h, ok=(200, 201, 204, 412))
        if st == 412:
            return False, None
        return True, rh.get("ETag")

    def delete(self, href):
        self.request("DELETE", self.href_url(urllib.parse.quote(href)), None, None, ok=(200, 204, 404))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# ── iCalendar ──────────────────────────────────────────────────────────────

def _unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _fold(line):
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not parts else 74):
            parts.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    parts.append(cur)
    return "\r\n ".join(parts)


def _split(line):
    """'DUE;TZID=X:2026…' -> ('DUE', ';TZID=X', '2026…')"""
    q = False
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == ":" and not q:
            head, value = line[:i], line[i + 1:]
            name = head.split(";", 1)[0].upper()
            return name, head[len(name):], value
    return line.upper(), "", ""


def _esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _unesc(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _param(params, key):
    for p in params.split(";"):
        if "=" in p and p.split("=", 1)[0].upper() == key:
            return p.split("=", 1)[1].strip('"')
    return None


def _parse_time(params, value):
    value = value.strip()
    try:
        if len(value) == 8 or _param(params, "VALUE") == "DATE":
            d = dt.datetime.strptime(value[:8], "%Y%m%d")
            return int(d.replace(hour=9).timestamp())  # só data: 9h, como no parser local
        if value.endswith("Z"):
            return int(dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
        d = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
        tz = _param(params, "TZID")
        if tz:
            try:
                from zoneinfo import ZoneInfo
                return int(d.replace(tzinfo=ZoneInfo(tz)).timestamp())
            except Exception:
                pass
        return int(d.timestamp())
    except ValueError:
        return None


def _utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _vtodo_bounds(lines):
    start = end = None
    depth = 0
    for i, l in enumerate(lines):
        u = l.upper()
        if u == "BEGIN:VTODO" and start is None:
            start = i
        elif start is not None and end is None:
            if u.startswith("BEGIN:"):
                depth += 1
            elif u.startswith("END:"):
                if depth == 0 and u == "END:VTODO":
                    end = i
                    break
                depth -= 1
    return start, end


# iCal PRIORITY 1–9 (1 = mais alta, 0 = indefinida) ↔ local 1/2/3
def _prio_in(v):
    try:
        p = int(v)
    except ValueError:
        return 2
    if 1 <= p <= 4:
        return 1
    if 6 <= p <= 9:
        return 3
    return 2


PRIO_OUT = {1: "1", 2: "5", 3: "9"}

# Propriedades que esta ponta controla; o resto (de outros clientes) é preservado.
OWNED = {"SUMMARY", "DUE", "PRIORITY", "STATUS", "COMPLETED", "PERCENT-COMPLETE", "DESCRIPTION", "URL",
         "LAST-MODIFIED", "DTSTAMP", "X-TAREFAS-SNOOZES", "X-TAREFAS-SNOOZE-UNTIL", "UID", "CATEGORIES"}


def _split_list(value):
    """CATEGORIES:a,b\\,c → ['a', 'b,c']"""
    out, cur, i = [], "", 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            cur += value[i:i + 2]
            i += 2
            continue
        if value[i] == ",":
            out.append(cur)
            cur = ""
        else:
            cur += value[i]
        i += 1
    out.append(cur)
    return [_unesc(x).strip() for x in out if x.strip()]


def from_ics(ics):
    """VTODO -> dict com os campos que a lista local usa (ou None)."""
    lines = _unfold(ics)
    s, e = _vtodo_bounds(lines)
    if s is None or e is None:
        return None
    t = {"title": "", "due": None, "priority": 2, "done": None, "notes": "", "url": "",
         "uid": None, "lastModified": 0, "tags": []}
    depth = 0
    status = None
    for l in lines[s + 1:e]:
        name, params, value = _split(l)
        if name == "BEGIN":
            depth += 1
            continue
        if name == "END":
            depth -= 1
            continue
        if depth:
            continue
        if name == "SUMMARY":
            t["title"] = _unesc(value)
        elif name == "UID":
            t["uid"] = value
        elif name == "DUE":
            t["due"] = _parse_time(params, value)
        elif name == "PRIORITY":
            t["priority"] = _prio_in(value)
        elif name == "STATUS":
            status = value.upper()
        elif name == "COMPLETED":
            t["done"] = _parse_time(params, value)
        elif name == "DESCRIPTION":
            t["notes"] = _unesc(value)
        elif name == "URL":
            t["url"] = value
        elif name == "CATEGORIES":
            for c in _split_list(value):
                c = c.lower()[:30]
                if c not in t["tags"] and len(t["tags"]) < 8:
                    t["tags"].append(c)
        elif name == "LAST-MODIFIED":
            t["lastModified"] = _parse_time(params, value) or 0
        elif name == "X-TAREFAS-SNOOZES":
            try:
                t["snoozes"] = max(0, int(value))
            except ValueError:
                pass
        elif name == "X-TAREFAS-SNOOZE-UNTIL":
            try:
                t["snoozeUntil"] = max(0, int(value))
            except ValueError:
                pass
    if status == "COMPLETED":
        t["done"] = t["done"] or t["lastModified"] or int(dt.datetime.now().timestamp())
    elif status in ("CANCELLED",):
        t["done"] = t["done"] or t["lastModified"] or int(dt.datetime.now().timestamp())
    else:
        t["done"] = None
    return t


def to_ics(task, now, base=None):
    """Gera o VCALENDAR da tarefa, preservando o que outros clientes puseram em `base`."""
    own = []
    own.append("UID:%s" % task["uid"])
    own.append("DTSTAMP:%s" % _utc(now))
    own.append("LAST-MODIFIED:%s" % _utc(task.get("modified") or now))
    own.append("SUMMARY:%s" % _esc(task.get("title") or ""))
    own.append("PRIORITY:%s" % PRIO_OUT.get(task.get("priority", 2), "5"))
    if task.get("due"):
        own.append("DUE:%s" % _utc(task["due"]))
    if task.get("done"):
        own += ["STATUS:COMPLETED", "COMPLETED:%s" % _utc(task["done"]), "PERCENT-COMPLETE:100"]
    else:
        own.append("STATUS:NEEDS-ACTION")
    if task.get("notes"):
        own.append("DESCRIPTION:%s" % _esc(task["notes"]))
    if task.get("url"):
        own.append("URL:%s" % task["url"])
    if task.get("tags"):
        own.append("CATEGORIES:%s" % ",".join(_esc(x) for x in task["tags"]))
    if task.get("snoozes"):
        own.append("X-TAREFAS-SNOOZES:%d" % task["snoozes"])
    if task.get("snoozeUntil"):
        own.append("X-TAREFAS-SNOOZE-UNTIL:%d" % task["snoozeUntil"])

    lines = _unfold(base) if base else []
    s, e = _vtodo_bounds(lines)
    if s is None or e is None:
        alarm = []
        if task.get("due") and not task.get("done"):
            # Lembrete na hora do prazo também no celular.
            alarm = ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:%s" % _esc(task.get("title") or ""),
                     "TRIGGER;RELATED=END:PT0S", "END:VALARM"]
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:%s" % PRODID,
                 "BEGIN:VTODO"] + own + alarm + ["END:VTODO", "END:VCALENDAR"]
    else:
        kept, depth = [], 0
        for l in lines[s + 1:e]:
            name, _, _ = _split(l)
            if name == "BEGIN":
                depth += 1
            elif name == "END":
                depth -= 1
            elif depth == 0 and name in OWNED:
                continue
            kept.append(l)
        lines = lines[:s + 1] + own + kept + lines[e:]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
