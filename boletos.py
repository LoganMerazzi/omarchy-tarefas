"""Cria tarefas de pagamento a partir dos boletos (PDF) numa pasta do Google Drive.

Configuração em ~/.config/tarefas/boletos.conf (fora do repositório):

  [pessoal]
  pasta = /home/voce/GoogleDrive/Finanças/Boletos
  # remote = gdrive:Finanças/Boletos   (opcional; deduzido se a pasta está no ponto de montagem)
  # rotulo =                           (opcional; vira tag. Padrão: o nome da seção)

  [empresa]
  pasta = /home/voce/GoogleDrive/Finanças/Empresa/Pagamentos
  rotulo = empresa

As pastas são organizadas em AAAA/MM; olhamos o mês passado, o atual e os dois
próximos. Senhas de PDFs protegidos: ~/.config/tarefas/pdf-senhas (uma por
linha, chmod 600). As senhas nunca vão para a linha de comando.

Do boleto sai: vencimento e valor (do código de barras ou do texto), o código
para copiar e o link do Drive. Um "Comprovante" com o mesmo nome conclui a tarefa.
"""

import configparser
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse

CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "tarefas")
CONF_FILE = os.path.join(CONF_DIR, "boletos.conf")
PASSWORDS_FILE = os.path.join(CONF_DIR, "pdf-senhas")
MOUNT = os.path.expanduser("~/GoogleDrive")
RCLONE_REMOTE = "gdrive:"

LOOKBACK_DAYS = 15      # boletos vencidos há mais que isso não viram tarefa
MAX_PDF_BYTES = 15 * 1024 * 1024

MONTHS_PT = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
             "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


# ── Configuração ───────────────────────────────────────────────────────────

def config():
    if not os.path.exists(CONF_FILE):
        return []
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(CONF_FILE, encoding="utf-8")
    except configparser.Error:
        return []
    out = []
    for name in cp.sections():
        sec = cp[name]
        folder = os.path.expanduser(sec.get("pasta", "").strip())
        if not folder:
            continue
        remote = sec.get("remote", "").strip()
        if not remote and os.path.realpath(folder).startswith(os.path.realpath(MOUNT) + os.sep):
            remote = RCLONE_REMOTE + os.path.relpath(os.path.realpath(folder), os.path.realpath(MOUNT))
        out.append({"name": name, "folder": folder, "remote": remote, "label": sec.get("rotulo", "").strip()})
    return out


def passwords():
    try:
        with open(PASSWORDS_FILE, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.strip() and not l.startswith("#")][:20]
    except OSError:
        return []


# ── Listagem ───────────────────────────────────────────────────────────────

def months_around(today):
    out = []
    for delta in (-1, 0, 1, 2):
        m = today.month - 1 + delta
        out.append("%04d/%02d" % (today.year + m // 12, m % 12 + 1))
    return out


def list_month(src, month):
    """[(nome, caminho local, id do Drive ou None, tamanho)] dos PDFs do mês."""
    local_dir = os.path.join(src["folder"], month)
    files = []
    if src["remote"] and shutil.which("rclone"):
        try:
            r = subprocess.run(["rclone", "lsjson", "--files-only", "--no-modtime", "--no-mimetype",
                                src["remote"].rstrip("/") + "/" + month],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                for e in json.loads(r.stdout or "[]"):
                    if e.get("Name", "").lower().endswith(".pdf"):
                        files.append((e["Name"], os.path.join(local_dir, e["Name"]), e.get("ID"), e.get("Size", 0)))
                return files
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        for n in os.listdir(local_dir):
            p = os.path.join(local_dir, n)
            if n.lower().endswith(".pdf") and os.path.isfile(p):
                files.append((n, p, None, os.path.getsize(p)))
    except OSError:
        pass
    return files


# ── Texto do PDF ───────────────────────────────────────────────────────────

def _pdftotext(path):
    try:
        r = subprocess.run(["pdftotext", "-layout", "-l", "3", path, "-"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, False
    locked = "password" in (r.stderr or "").lower()
    return (r.stdout if r.returncode == 0 else None), locked


def pdf_text(path, pwds):
    """(texto, situação): situação é 'ok', 'protegido' (nenhuma senha serviu) ou 'erro'."""
    text, locked = _pdftotext(path)
    if text is not None and not locked:
        return text, "ok"
    if not locked:
        return "", "erro"
    run_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=run_dir, prefix="tarefas-pdf.") as tmp:
        os.chmod(tmp, 0o700)
        pw_file = os.path.join(tmp, "pw")
        out_pdf = os.path.join(tmp, "out.pdf")
        for pw in pwds:
            fd = os.open(pw_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pw + "\n")
            try:
                r = subprocess.run(["qpdf", "--password-file=" + pw_file, "--decrypt", path, out_pdf],
                                   capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                return "", "erro"
            if r.returncode in (0, 3) and os.path.exists(out_pdf):  # 3 = avisos
                text, _ = _pdftotext(out_pdf)
                return text or "", "ok"
    return "", "protegido"


# ── Código de barras ───────────────────────────────────────────────────────

def _mod10(num):
    total, w = 0, 2
    for d in reversed(num):
        p = int(d) * w
        total += p // 10 + p % 10
        w = 1 if w == 2 else 2
    return (10 - total % 10) % 10


def _mod11_arrec(num):
    total, w = 0, 2
    for d in reversed(num):
        total += int(d) * w
        w = 2 if w == 9 else w + 1
    r = total % 11
    return 0 if r in (0, 1) else 11 - r


BANK_RE = re.compile(r"(\d{5})[.\s]?(\d{5})\s+(\d{5})[.\s]?(\d{6})\s+(\d{5})[.\s]?(\d{6})\s+(\d)\s+(\d{14})")
ARREC_RE = re.compile(r"(8\d{10})[\s-]?(\d)\s+(\d{11})[\s-]?(\d)\s+(\d{11})[\s-]?(\d)\s+(\d{11})[\s-]?(\d)")


def _bank_due(factor, today):
    if factor < 1000:
        return None
    # O fator de vencimento recomeçou em 1000 no dia 22/02/2025.
    cands = [dt.date(1997, 10, 7) + dt.timedelta(days=factor),
             dt.date(2025, 2, 22) + dt.timedelta(days=factor - 1000)]
    return min(cands, key=lambda d: abs((d - today).days))


def find_barcode(text, today):
    """{'linha', 'due', 'value'} do primeiro código válido, ou None."""
    for m in BANK_RE.finditer(text):
        g = m.groups()
        f1, f2, f3 = g[0] + g[1], g[2] + g[3], g[4] + g[5]
        if _mod10(f1[:-1]) != int(f1[-1]) or _mod10(f2[:-1]) != int(f2[-1]) or _mod10(f3[:-1]) != int(f3[-1]):
            continue
        linha = "".join(g)
        value = int(g[7][4:]) / 100
        return {"linha": linha, "due": _bank_due(int(g[7][:4]), today), "value": value or None,
                "pretty": "%s.%s %s.%s %s.%s %s %s" % g}
    for m in ARREC_RE.finditer(text):
        g = m.groups()
        blocks = [(g[0], g[1]), (g[2], g[3]), (g[4], g[5]), (g[6], g[7])]
        ind = g[0][2]
        check = _mod10 if ind in "67" else _mod11_arrec
        if any(check(b) != int(dv) for b, dv in blocks):
            continue
        barcode = "".join(b for b, _ in blocks)
        value = int(barcode[4:15]) / 100 if ind in "68" else None
        return {"linha": "".join(g), "due": None, "value": value or None,
                "pretty": " ".join("%s-%s" % bd for bd in blocks)}
    return None


DUE_TEXT_RES = [
    re.compile(r"pagar\s+(?:este\s+documento\s+)?at[eé]\s*:?\s*(\d{2})/(\d{2})/(\d{4})", re.I),
    re.compile(r"(?:data\s+de\s+)?vencimento\s*:?\s*(\d{2})/(\d{2})/(\d{4})", re.I),
    re.compile(r"(?:data\s+de\s+)?vencimento\s*:?\s*(\d{1,2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\w*\.?\s+(\d{4})", re.I),
]
VALUE_RE = re.compile(r"(?:valor\s+(?:total|cobrado|do\s+documento|a\s+pagar)|total\s+a\s+pagar)[^\d\n]{0,40}?(\d{1,3}(?:\.\d{3})*,\d{2})", re.I)


def due_from_text(text):
    for rx in DUE_TEXT_RES:
        m = rx.search(text)
        if not m:
            continue
        d, mo, y = m.groups()
        mo = MONTHS_PT.get(mo.upper()[:3]) if not mo.isdigit() else int(mo)
        try:
            return dt.date(int(y), int(mo), int(d))
        except (TypeError, ValueError):
            continue
    return None


def value_from_text(text):
    m = VALUE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


# ── Nome do arquivo ────────────────────────────────────────────────────────

def clean_name(filename):
    """'PARCELAMENTO%2520…&fNameZ=DAS-082026+1234.pdf' → 'DAS-082026 1234'; '08 - Claro - Boleto.pdf' → 'Claro'."""
    n = filename
    if "fNameZ=" in n:
        n = n.split("fNameZ=", 1)[1]
    n = urllib.parse.unquote(urllib.parse.unquote(n)).replace("+", " ")
    n = re.sub(r"\.pdf$", "", n, flags=re.I)
    n = re.sub(r"\s+copy$", "", n, flags=re.I)
    n = re.sub(r"^\d{1,2}\s*-\s*", "", n)
    n = re.sub(r"\s*-\s*(boleto|comprovante)\s*$", "", n, flags=re.I)
    n = re.sub(r"[_]+", " ", n)
    n = re.sub(r"\b\d{8,}\b", " ", n)          # CNPJ, números de documento
    return re.sub(r"\s+", " ", n).strip(" -")


def kind_of(filename):
    f = fold(filename)
    if "comprovante" in f or "recibo" in f:
        return "comprovante"
    if "extrato" in f:
        return "outro"
    return "boleto"


def match_key(name):
    f = fold(name)
    f = re.sub(r"\b(pgt|pagto|pagamento|boleto|comprovante|de|do|da)\b", " ", f)
    return re.sub(r"[^a-z0-9]+", " ", f).strip()


def date_from_path(month, filename):
    m = re.match(r"(\d{1,2})\s*-", filename)
    if not m:
        return None
    try:
        y, mo = month.split("/")
        return dt.date(int(y), int(mo), int(m.group(1)))
    except ValueError:
        return None


def brl(v):
    s = "%.2f" % v
    inteiro, cent = s.split(".")
    inteiro = "{:,}".format(int(inteiro)).replace(",", ".")
    return "R$ %s,%s" % (inteiro, cent)


# ── Varredura ──────────────────────────────────────────────────────────────

def scan(today=None, seen=()):
    """Lê as pastas e devolve (boletos, comprovantes, avisos).

    `seen`: chaves já processadas antes; esses PDFs nem são abertos.
    """
    today = today or dt.date.today()
    pwds = passwords()
    bills, receipts, notes = [], [], []
    for src in config():
        for month in months_around(today):
            for name, path, file_id, size in list_month(src, month):
                kind = kind_of(name)
                if kind == "outro":
                    continue
                file_key = "file:" + (file_id or os.path.relpath(path, src["folder"]))
                title = clean_name(name)
                if kind == "comprovante":
                    receipts.append({"key": match_key(title), "date": date_from_path(month, name) or today,
                                     "source": src["name"], "file": name})
                    continue
                if file_key in seen:
                    continue
                if size and size > MAX_PDF_BYTES:
                    continue
                text, state = pdf_text(path, pwds)
                bc = find_barcode(text, today) if text else None
                due = (bc and bc["due"]) or due_from_text(text or "") or date_from_path(month, name)
                value = (bc and bc["value"]) or value_from_text(text or "")
                if state == "protegido":
                    notes.append("PDF protegido (nenhuma senha serviu): %s/%s" % (month, name))
                bills.append({
                    "fileKey": file_key,
                    "key": ("cod:" + bc["linha"]) if bc else file_key,
                    "source": src["name"], "label": src["label"], "title": title,
                    "matchKey": match_key(title), "month": month, "file": name, "path": path,
                    "url": ("https://drive.google.com/file/d/%s/view" % file_id) if file_id else "",
                    "due": due, "value": value, "barcode": bc["pretty"] if bc else "",
                    "barcodeDigits": bc["linha"] if bc else "", "pdf": state,
                })
    return bills, receipts, notes


def task_text(b):
    title = "Pagar " + b["title"]
    if b["value"]:
        title += " · " + brl(b["value"])
    lines = []
    if b["value"]:
        lines.append("Valor: " + brl(b["value"]))
    if b["due"]:
        lines.append("Vencimento: " + b["due"].strftime("%d/%m/%Y"))
    if b["barcode"]:
        lines.append("Código de barras: " + b["barcode"])
    if b["pdf"] == "protegido":
        lines.append("PDF protegido: confira valor e código no arquivo.")
    lines.append("Arquivo: %s/%s" % (b["month"], b["file"]))
    return title, "\n".join(lines)
