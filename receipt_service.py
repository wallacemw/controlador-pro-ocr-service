from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import importlib.util
import io
import json
import os
import re
import secrets
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Image = None


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        return


PROJECT_ROOT = Path(__file__).resolve().parent
_load_env_file(PROJECT_ROOT / ".env.local")
_load_env_file(PROJECT_ROOT / ".env")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


HOST = os.getenv("RECEIPT_SERVICE_HOST", "127.0.0.1")
PORT = int(os.getenv("RECEIPT_SERVICE_PORT", "8787"))
RUNNING_ON_VERCEL = bool(os.getenv("VERCEL"))
OCR_SERVICE_UPSTREAM_URL = os.getenv("OCR_SERVICE_UPSTREAM_URL", "").strip().rstrip("/")
OCR_SERVICE_UPSTREAM_TIMEOUT_MS = int(os.getenv("OCR_SERVICE_UPSTREAM_TIMEOUT_MS", "25000") or "25000")
OCR_SERVICE_SHARED_SECRET = os.getenv("OCR_SERVICE_SHARED_SECRET", "").strip()
OCR_SERVICE_ENFORCE_SHARED_SECRET = _env_flag("OCR_SERVICE_ENFORCE_SHARED_SECRET", False)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_RECEIPT_MODEL", "gpt-4o-mini")
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OCR_LANG = os.getenv("RECEIPT_OCR_LANG", "pt")
PADDLE_OCR_ENABLE_WARMUP = _env_flag("PADDLE_OCR_ENABLE_WARMUP", True)
PADDLE_OCR_USE_DOC_ORIENTATION_CLASSIFY = _env_flag("PADDLE_OCR_USE_DOC_ORIENTATION_CLASSIFY", False)
PADDLE_OCR_USE_DOC_UNWARPING = _env_flag("PADDLE_OCR_USE_DOC_UNWARPING", False)
PADDLE_OCR_USE_TEXTLINE_ORIENTATION = _env_flag("PADDLE_OCR_USE_TEXTLINE_ORIENTATION", False)
PADDLE_OCR_TEXT_DETECTION_MODEL_NAME = (os.getenv("PADDLE_OCR_TEXT_DETECTION_MODEL_NAME", "PP-OCRv5_mobile_det") or "").strip()
PADDLE_OCR_TEXT_RECOGNITION_MODEL_NAME = (os.getenv("PADDLE_OCR_TEXT_RECOGNITION_MODEL_NAME", "") or "").strip()
PADDLE_OCR_STARTUP_GRACE_MS = int(os.getenv("PADDLE_OCR_STARTUP_GRACE_MS", "8000") or "8000")
SUPABASE_PROJECT_URL = os.getenv("SUPABASE_PROJECT_URL", "https://zphgusvzgbznljqpozab.supabase.co").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_0GdmO02259hS8KydGNHCsw_JF1t5vG6").strip()
ABLY_APP_NAMESPACE = os.getenv("ABLY_APP_NAMESPACE", "controlador-gastos-pro")
ABLY_TOKEN_TTL_MS = int(os.getenv("ABLY_TOKEN_TTL_MS", "3600000") or "3600000")
ABLY_PREFERRED_KEY_NAME = os.getenv("ABLY_PREFERRED_KEY_NAME", "7Y8Xrw.ReiTmw").strip()
ABLY_API_KEYS = [item.strip() for item in (os.getenv("ABLY_API_KEYS", "") or "").split(",") if item.strip()]


_PADDLE_OCR = None
_PADDLE_OCR_CLASS = None
_PADDLE_IMPORT_ATTEMPTED = False
_PADDLE_IMPORT_ERROR = ""
_PADDLE_OCR_LOCK = threading.Lock()
_PADDLE_WARMUP_EVENT = threading.Event()
_PADDLE_WARMUP_THREAD = None
_PADDLE_WARMUP_STATE = "idle"
_PADDLE_WARMUP_ERROR = ""
_PADDLE_WARMUP_DURATION_MS = 0
_PADDLE_WARMUP_STARTED_AT = 0.0
_PADDLE_WARMUP_FINISHED_AT = 0.0


def _send_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("X-Receipt-Service-Source", "codex-2.0")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header("Access-Control-Max-Age", "86400")


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    _send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(data)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _response_payload(ok: bool, service: str, *, error: str = "", mode: str = "", details: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": bool(ok),
        "service": service,
        "timestamp": _iso_now(),
    }
    if error:
        payload["error"] = str(error)
    if mode:
        payload["mode"] = str(mode)
    if details is not None:
        payload["details"] = details
    payload.update(extra)
    return payload


def build_error_payload(service: str, error: str, *, mode: str = "", details: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    return _response_payload(False, service, error=error, mode=mode, details=details, **extra)


def _read_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    if not raw:
      return {}
    return json.loads(raw.decode("utf-8"))


def _build_upstream_url(path: str) -> str:
    return f"{OCR_SERVICE_UPSTREAM_URL}{path if path.startswith('/') else '/' + path}"


def _build_upstream_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "controlador-pro/ocr-proxy",
    }
    if OCR_SERVICE_SHARED_SECRET:
        headers["X-OCR-Service-Key"] = OCR_SERVICE_SHARED_SECRET
    if extra:
        headers.update({k: v for k, v in extra.items() if v is not None})
    return headers


def _call_upstream_json(path: str, *, method: str = "GET", payload: Optional[Dict[str, Any]] = None, timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    if not OCR_SERVICE_UPSTREAM_URL:
        raise RuntimeError("OCR upstream nao configurado.")
    body = None
    headers = _build_upstream_headers()
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _build_upstream_url(path),
        data=body,
        headers=headers,
        method=method.upper(),
    )
    effective_timeout_ms = timeout_ms if timeout_ms is not None else OCR_SERVICE_UPSTREAM_TIMEOUT_MS
    timeout = max(3, effective_timeout_ms / 1000)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = ""
        try:
            raw = err.read().decode("utf-8")
        except Exception:
            raw = ""
        try:
            body_json = json.loads(raw) if raw else {}
        except Exception:
            body_json = {}
        message = body_json.get("details", {}).get("message") or body_json.get("error") or f"HTTP {err.code}"
        error = RuntimeError(str(message))
        setattr(error, "status_code", err.code)
        setattr(error, "error_code", body_json.get("error") or "upstream_error")
        raise error
    except Exception as err:
        error = RuntimeError(str(err) or "Falha ao contactar o OCR upstream.")
        setattr(error, "status_code", getattr(err, "status_code", 0))
        setattr(error, "error_code", getattr(err, "error_code", "upstream_error"))
        raise error


def _parse_query(path: str) -> tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlparse(path)
    return parsed.path, urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)


def _clean_ably_segment(value: Any, fallback: str = "") -> str:
    clean = re.sub(r"[^A-Za-z0-9:_=-]", "-", str(value or "")).strip("-:")
    return clean or fallback


def _canonicalise_ably_capability(capability: Dict[str, List[str]]) -> str:
    clean: Dict[str, List[str]] = {}
    for channel, ops in (capability or {}).items():
        channel_name = _clean_ably_segment(channel)
        if not channel_name:
            continue
        clean[channel_name] = sorted({str(op).strip() for op in (ops or []) if str(op).strip()})
    return json.dumps(dict(sorted(clean.items())), separators=(",", ":"), ensure_ascii=False)


def _split_ably_key(raw_key: str) -> Optional[tuple[str, str]]:
    key_name, sep, key_secret = str(raw_key or "").partition(":")
    if not sep or not key_name or not key_secret:
        return None
    if "." not in key_name:
        return None
    return key_name.strip(), key_secret.strip()


def _active_ably_key() -> Optional[tuple[str, str]]:
    preferred_match: Optional[tuple[str, str]] = None
    for raw_key in ABLY_API_KEYS:
        pair = _split_ably_key(raw_key)
        if not pair:
            continue
        if ABLY_PREFERRED_KEY_NAME and pair[0] == ABLY_PREFERRED_KEY_NAME:
            preferred_match = pair
            break
        if preferred_match is None:
            preferred_match = pair
    if preferred_match:
        return preferred_match
    return None


def _build_ably_channel_name(user_id: str) -> str:
    return f"{ABLY_APP_NAMESPACE}:sync:user:{_clean_ably_segment(user_id, 'anon')}"


def _verify_supabase_access_token(access_token: str) -> Dict[str, Any]:
    token = str(access_token or "").strip()
    if not token:
        return {
            "ok": False,
            "error": "supabase_auth_required",
            "details": {"message": "Bearer token ausente ou vazio."},
        }
    if not SUPABASE_PROJECT_URL or not SUPABASE_PUBLISHABLE_KEY:
        return {
            "ok": False,
            "error": "upstream_error",
            "details": {"message": "Supabase nao configurado no servico remoto."},
        }
    req = urllib.request.Request(
        f"{SUPABASE_PROJECT_URL}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body_message = ""
        try:
            raw = err.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
            body_message = str(body.get("msg") or body.get("message") or body.get("error_description") or body.get("error") or "").strip()
        except Exception:
            body_message = ""
        error = "session_invalid" if err.code in {401, 403} else "upstream_error"
        return {
            "ok": False,
            "error": error,
            "details": {
                "status": err.code,
                "message": body_message or f"Supabase respondeu HTTP {err.code} ao validar a sessao.",
            },
        }
    except Exception as err:
        return {
            "ok": False,
            "error": "upstream_error",
            "details": {"message": f"Falha ao validar a sessao Supabase: {err}"},
        }
    if not payload.get("id"):
        return {
            "ok": False,
            "error": "session_invalid",
            "details": {"message": "A sessao Supabase nao devolveu um utilizador valido."},
        }
    return {"ok": True, "user": payload}


def _build_ably_token_request(client_id: str, channel_name: str) -> Dict[str, Any]:
    pair = _active_ably_key()
    if not pair:
        raise RuntimeError("Nenhuma chave Ably válida está configurada no serviço.")
    key_name, key_secret = pair
    nonce = secrets.token_hex(16)
    timestamp = int(time.time() * 1000)
    capability = _canonicalise_ably_capability({
        channel_name: ["history", "presence", "publish", "subscribe"]
    })
    sign_text = "\n".join([
        key_name,
        str(ABLY_TOKEN_TTL_MS),
        capability,
        client_id,
        str(timestamp),
        nonce,
        "",
    ])
    mac = base64.b64encode(
        hmac.new(key_secret.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return {
        "keyName": key_name,
        "ttl": ABLY_TOKEN_TTL_MS,
        "capability": capability,
        "clientId": client_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "mac": mac,
    }


def _num(value: Any) -> float:
    raw = re.sub(r"[^\d,.-]", "", str(value or "")).strip()
    if not raw:
        return 0.0
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except Exception:
        return 0.0


def _round3(value: Any) -> float:
    try:
        return round(float(value), 3)
    except Exception:
        return 0.0


def _validate_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        return False
    try:
        year, month, day = [int(x) for x in value.split("-")]
        if month < 1 or month > 12 or day < 1 or day > 31:
            return False
        import datetime as _dt

        _dt.date(year, month, day)
        return True
    except Exception:
        return False


def _parse_date(text: str) -> str:
    candidates: List[tuple[str, int, int]] = []
    for match in re.finditer(r"\b(20\d{2})[/-](\d{2})[/-](\d{2})\b", text):
        iso = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if _validate_date(iso):
            candidates.append((iso, match.start(), 2))
    for match in re.finditer(r"\b(\d{2})[/-](\d{2})[/-](20\d{2}|\d{2})\b", text):
        year = match.group(3)
        if len(year) == 2:
            year = f"20{year}"
        iso = f"{year}-{match.group(2)}-{match.group(1)}"
        if _validate_date(iso):
            candidates.append((iso, match.start(), 1))
    candidates.sort(key=lambda item: (item[1], -item[2]))
    return candidates[0][0] if candidates else ""


def _find_tax_id(text: str) -> Optional[str]:
    cnpj = re.search(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", text)
    if cnpj:
        return cnpj.group(0)
    nif = re.search(r"\b\d{9}\b", text)
    return nif.group(0) if nif else None


def _norm_text(value: str) -> str:
    import unicodedata

    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s/%.-]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _is_discount_line(line: str) -> bool:
    return bool(re.search(r"\b(desconto|descontos|poupanc|promoc|cupom\s*desc|voucher|econom)\b", line, re.I))


def _is_fee_line(line: str) -> bool:
    return bool(re.search(r"\b(taxa\s*serv|servico\s*10%|servi[cç]o)\b", line, re.I))


def _is_tax_line(line: str) -> bool:
    return bool(re.search(r"\b(iva|icms|iss|ipi|pis|cofins|imposto)\b", line, re.I)) and not _is_fee_line(line)


def _is_payment_line(line: str) -> bool:
    return bool(re.search(r"\b(cart[aã]o|multibanco|pix|dinheiro|numer[aá]rio|cr[eé]dito|d[eé]bito|mbway|transfer[eê]ncia|pagamento|troco)\b", line, re.I))


def _is_summary_line(line: str) -> bool:
    return bool(re.search(r"\b(sub\s*total|subtotal|total(?:\s+a\s+pagar|\s+final|\s+geral|\s+liquido|\s+cupom|\s+da\s+compra)?|valor\s+total|valor\s+a\s+pagar|desconto|troco|pagamento|iva|icms|iss|pis|cofins|taxa\s*serv)\b", line, re.I))


def _is_header_line(line: str) -> bool:
    return bool(re.search(r"\b(cnpj|cpf|nif|nf-e|nfc-e|inscr|ie:|sat|tel:|fone|www\.|http|operador|caixa)\b", line, re.I))


def _line_amount(line: str) -> float:
    matches = list(re.finditer(r"(?:R?\$?\s*)?(\d{1,6}(?:[.,]\d{3})*[.,]\d{2})\b", line or ""))
    if not matches:
        return 0.0
    return _num(matches[-1].group(1))


def _sum_amounts(entries: List[Dict[str, Any]]) -> float:
    return round(sum(_num(item.get("amount", 0)) for item in (entries or [])), 2)


def _adjusted_total(
    items_sum: float,
    discounts: List[Dict[str, Any]],
    fees: List[Dict[str, Any]],
    taxes: Optional[List[Dict[str, Any]]] = None,
    subtotal: float = 0.0,
    grand_total: float = 0.0,
) -> float:
    discount_sum = _sum_amounts(discounts)
    fee_sum = _sum_amounts(fees)
    tax_sum = _sum_amounts(taxes or [])
    item_base = round(items_sum or 0.0, 2)
    subtotal_base = round(subtotal or 0.0, 2)
    target_total = round(grand_total or 0.0, 2)
    bases = [item_base]
    if subtotal_base > 0 and abs(subtotal_base - item_base) > 0.009:
        bases.append(subtotal_base)
    candidates: List[float] = []
    for base in bases:
        candidates.append(round(base - discount_sum + fee_sum, 2))
        if tax_sum > 0:
            candidates.append(round(base - discount_sum + fee_sum + tax_sum, 2))
    if not candidates:
        return item_base
    best = candidates[0]
    if target_total > 0:
        best = min(candidates, key=lambda candidate: abs(candidate - target_total))
    elif subtotal_base > 0:
        best = min(candidates, key=lambda candidate: abs(candidate - subtotal_base))
    return round(best, 2)


def _looks_like_item_line(line: str) -> bool:
    if not line or len(line) < 3:
        return False
    if not re.search(r"\d{1,6}(?:[.,]\d{3})*[.,]\d{2}\s*$", line):
        return False
    if _is_summary_line(line) or _is_discount_line(line) or _is_tax_line(line) or _is_fee_line(line) or _is_payment_line(line):
        return False
    if re.match(r"^\d{2}[/-]\d{2}[/-]\d{2,4}", line):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", line))


def _merge_lines(lines: List[str]) -> List[str]:
    merged: List[str] = []
    i = 0
    while i < len(lines):
        current = (lines[i] or "").strip()
        if not current:
            i += 1
            continue
        next_line = (lines[i + 1] or "").strip() if i + 1 < len(lines) else ""
        current_has_amount = bool(re.search(r"\d{1,6}(?:[.,]\d{3})*[.,]\d{2}\s*$", current))
        next_has_amount = bool(re.search(r"\d{1,6}(?:[.,]\d{3})*[.,]\d{2}\s*$", next_line))
        if re.search(r"[A-Za-zÀ-ÿ]", current) and not current_has_amount and next_line and next_has_amount and not _is_summary_line(next_line):
            merged.append(re.sub(r"\s+", " ", f"{current} {next_line}").strip())
            i += 2
            continue
        merged.append(current)
        i += 1
    return merged


def _guess_category(name: str) -> str:
    n = _norm_text(name)
    if re.search(r"uber|taxi|onibus|metro|combustivel|gasolina|etanol|diesel|pedagio|carro|oleo motor|filtro oleo|lubrificante", n):
        return "Transporte"
    if re.search(r"leite|pao|arroz|feijao|carne|frango|peixe|ovos|queijo|iogurte|azeite|cafe|agua|fruta|legume|verdura|banana|hortifruti|superm", n):
        return "Supermercado"
    if re.search(r"restaurante|almoco|jantar|prato|refeicao|pizza|burger|sushi|cafe", n):
        return "Restaurante"
    if re.search(r"farmacia|medic|remedio|vitamina|pomada|curativo|drogaria", n):
        return "Saúde"
    return "Outros"


def _parse_item_line(line: str) -> Optional[Dict[str, Any]]:
    if not _looks_like_item_line(line):
        return None
    amounts = [_num(match.group(1)) for match in re.finditer(r"(?:R?\$?\s*)?(\d{1,6}(?:[.,]\d{3})*[.,]\d{2})\b", line)]
    if not amounts:
        return None
    line_total = amounts[-1]
    if line_total <= 0:
        return None
    left = re.sub(r"(\d{1,6}(?:[.,]\d{3})*[.,]\d{2})\s*$", "", line).strip()
    qty = 1.0
    unit_price = line_total
    qty_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|un(?:id)?\.?|lt?|l|cx|pc|g)?\s*[xX×*]\s*(\d{1,6}(?:[.,]\d{3})*[.,]\d{2})", left, re.I)
    if qty_match:
        qty = _round3(_num(qty_match.group(1)))
        unit_price = _num(qty_match.group(3))
        left = left.replace(qty_match.group(0), " ").strip()
    elif len(amounts) >= 2 and re.search(r"(kg|un|lt| l\b|cx|pc|g\b|[xX×*])", left):
        maybe_unit = amounts[-2]
        if maybe_unit > 0 and maybe_unit != line_total:
            unit_price = maybe_unit
            qty = _round3(line_total / max(unit_price, 0.0001))
    left = re.sub(r"^\d{7,}\s*", "", left)
    left = re.sub(r"^\d+\s+", "", left)
    left = re.sub(r"\s+[A-Z#%*T]{1,2}$", "", left, flags=re.I)
    left = re.sub(r"\s{2,}", " ", left).strip()
    if len(left) < 2:
        return None
    return {
        "description": left,
        "qty": qty if qty > 0 else 1,
        "unitPrice": unit_price if unit_price > 0 else line_total,
        "lineTotal": line_total,
        "category": _guess_category(left),
        "confidence": 0.9 if qty_match else 0.72,
        "bbox": None,
    }


def parse_receipt_text(raw: str, source: str = "service_ocr", pages_processed: int = 1) -> Dict[str, Any]:
    raw = str(raw or "")
    if not raw.strip():
        return {
            "store": "",
            "date": "",
            "taxId": None,
            "paymentMethod": None,
            "items": [],
            "subtotal": 0,
            "discounts": [],
            "fees": [],
            "taxes": [],
            "grandTotal": 0,
            "total": 0,
            "confidence": 0.0,
            "needsReview": True,
            "source": source,
            "diagnostics": {
                "mismatch": False,
                "missingTotals": True,
                "headerDetected": False,
                "pagesProcessed": pages_processed,
                "itemSectionMissing": True,
                "layoutAmbiguous": True,
            },
        }

    lines = _merge_lines([line.strip() for line in re.split(r"\r?\n", raw) if line.strip()])
    store_candidates = [
        line for line in lines[:12]
        if 3 < len(line) < 80
        and not re.match(r"^\d", line)
        and not _is_header_line(line)
        and not _is_summary_line(line)
        and not re.match(r"^[-=*_.]{2,}$", line)
        and not re.match(r"^\d{2}[/-]\d{2}[/-]\d{2,4}", line)
    ]
    store = store_candidates[0] if store_candidates else ""
    date = _parse_date(raw)
    tax_id = _find_tax_id(raw)

    totals = [line for line in lines if _is_summary_line(line)]
    grand_total = 0.0
    subtotal = 0.0
    for line in totals:
        amount = _line_amount(line)
        upper = line.upper()
        if amount > 0 and re.search(r"TOTAL A PAGAR|VALOR A PAGAR|TOTAL GERAL|TOTAL LIQUIDO|VALOR TOTAL|TOTAL FINAL|TOTAL\b", upper):
            grand_total = max(grand_total, amount)
        if amount > 0 and re.search(r"SUBTOTAL|SUB TOTAL|VALOR DOS PRODUTOS|VALOR DOS ITENS", upper):
            subtotal = max(subtotal, amount)

    discounts: List[Dict[str, Any]] = []
    fees: List[Dict[str, Any]] = []
    taxes: List[Dict[str, Any]] = []
    payment_method = None
    for line in lines:
        amount = _line_amount(line)
        if _is_discount_line(line) and amount > 0:
            discounts.append({"description": line[:60], "amount": abs(amount)})
            continue
        if _is_fee_line(line) and amount > 0:
            fees.append({"description": line[:60], "amount": abs(amount)})
            continue
        if _is_tax_line(line) and amount > 0:
            rate_match = re.search(r"(\d{1,3})\s*[%°]", line)
            taxes.append({
                "label": line[:40],
                "rate": int(rate_match.group(1)) if rate_match else 0,
                "taxable": 0,
                "amount": abs(amount),
            })
            continue
        if payment_method is None and _is_payment_line(line):
            payment_method = line[:50]

    items: List[Dict[str, Any]] = []
    entered_items = False
    entered_summary = False
    for line in lines:
      if _is_summary_line(line):
        if entered_items:
            entered_summary = True
        continue
      if entered_summary:
        continue
      parsed = _parse_item_line(line)
      if parsed:
        entered_items = True
        items.append(parsed)

    items_sum = round(sum(item["lineTotal"] for item in items), 2)
    effective_subtotal = subtotal if subtotal > 0 else items_sum
    accounted_total = _adjusted_total(items_sum, discounts, fees, taxes, subtotal=subtotal, grand_total=grand_total)
    mismatch = grand_total > 0 and abs(accounted_total - grand_total) / max(grand_total, 0.01) > 0.05
    needs_review = not items or mismatch or not store
    confidence = min(1.0, max(0.25,
        (min(0.4, len(items) * 0.08) if items else 0.0) +
        (0.12 if store else 0.0) +
        (0.05 if tax_id else 0.0) +
        (0.16 if grand_total > 0 else 0.0) +
        (0.10 if effective_subtotal > 0 else 0.0) +
        (0.07 if (discounts or fees or taxes) else 0.0) +
        (0.10 if not needs_review else 0.0)
    ))
    return {
        "store": store,
        "date": date,
        "taxId": tax_id,
        "paymentMethod": payment_method,
        "items": items,
        "subtotal": effective_subtotal,
        "discounts": discounts,
        "fees": fees,
        "taxes": taxes,
        "grandTotal": grand_total,
        "total": grand_total,
        "confidence": round(confidence, 2),
        "needsReview": needs_review,
        "source": source,
        "diagnostics": {
            "mismatch": mismatch,
            "missingTotals": not (grand_total > 0),
            "headerDetected": bool(store or tax_id),
            "pagesProcessed": pages_processed,
            "itemSectionMissing": len(items) == 0,
            "layoutAmbiguous": len(items) == 0 or len(lines) < 4,
            "accountedTotal": accounted_total,
        },
    }


def _score_extraction(result: Dict[str, Any]) -> float:
    items = result.get("items") or []
    generic_only = len(items) == 1 and items[0].get("description", "").lower() == "item do recibo"
    score = 0.0
    if result.get("store"):
        score += 0.15
    if _validate_date(result.get("date", "")):
        score += 0.1
    if result.get("taxId"):
        score += 0.05
    if items:
        score += 0.05 if generic_only else min(0.3, len(items) * 0.08)
    if result.get("subtotal", 0) > 0:
        score += 0.1
    if result.get("grandTotal", 0) > 0:
        score += 0.15
    if result.get("taxes"):
        score += 0.05
    if result.get("fees"):
        score += 0.05
    if not result.get("needsReview"):
        score += 0.1
    if not result.get("diagnostics", {}).get("mismatch"):
        score += 0.1
    confidence = float(result.get("confidence") or 0.0)
    return round(max(score, confidence), 2)


def _needs_llm(result: Optional[Dict[str, Any]]) -> bool:
    if not result:
        return True
    items = result.get("items") or []
    if not items:
        return True
    if result.get("needsReview"):
        return True
    if result.get("diagnostics", {}).get("mismatch"):
        return True
    return _score_extraction(result) < 0.72


def _decode_image(b64: str):
    if not b64:
        return None
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    raw = base64.b64decode(b64)
    return raw


def _paddle_ocr_available() -> bool:
    if Image is None:
        return False
    return importlib.util.find_spec("paddleocr") is not None


def _build_paddle_ocr_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "lang": OCR_LANG,
        "use_doc_orientation_classify": PADDLE_OCR_USE_DOC_ORIENTATION_CLASSIFY,
        "use_doc_unwarping": PADDLE_OCR_USE_DOC_UNWARPING,
        "use_textline_orientation": PADDLE_OCR_USE_TEXTLINE_ORIENTATION,
    }
    if PADDLE_OCR_TEXT_DETECTION_MODEL_NAME:
        kwargs["text_detection_model_name"] = PADDLE_OCR_TEXT_DETECTION_MODEL_NAME
    if PADDLE_OCR_TEXT_RECOGNITION_MODEL_NAME:
        kwargs["text_recognition_model_name"] = PADDLE_OCR_TEXT_RECOGNITION_MODEL_NAME
    return kwargs


def _paddle_warmup_details() -> Dict[str, Any]:
    details = {
        "enabled": bool(PADDLE_OCR_ENABLE_WARMUP),
        "state": _PADDLE_WARMUP_STATE,
        "durationMs": int(_PADDLE_WARMUP_DURATION_MS or 0),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_PADDLE_WARMUP_STARTED_AT)) if _PADDLE_WARMUP_STARTED_AT else "",
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_PADDLE_WARMUP_FINISHED_AT)) if _PADDLE_WARMUP_FINISHED_AT else "",
        "error": _PADDLE_WARMUP_ERROR,
        "config": {
            "lang": OCR_LANG,
            "use_doc_orientation_classify": PADDLE_OCR_USE_DOC_ORIENTATION_CLASSIFY,
            "use_doc_unwarping": PADDLE_OCR_USE_DOC_UNWARPING,
            "use_textline_orientation": PADDLE_OCR_USE_TEXTLINE_ORIENTATION,
            "text_detection_model_name": PADDLE_OCR_TEXT_DETECTION_MODEL_NAME or "",
            "text_recognition_model_name": PADDLE_OCR_TEXT_RECOGNITION_MODEL_NAME or "",
        },
    }
    return details


def _mark_paddle_warmup_state(state: str, *, error: str = "", started_at: Optional[float] = None, finished_at: Optional[float] = None) -> None:
    global _PADDLE_WARMUP_STATE, _PADDLE_WARMUP_ERROR, _PADDLE_WARMUP_DURATION_MS, _PADDLE_WARMUP_STARTED_AT, _PADDLE_WARMUP_FINISHED_AT
    _PADDLE_WARMUP_STATE = state
    if started_at is not None:
        _PADDLE_WARMUP_STARTED_AT = started_at
    if finished_at is not None:
        _PADDLE_WARMUP_FINISHED_AT = finished_at
        if _PADDLE_WARMUP_STARTED_AT:
            _PADDLE_WARMUP_DURATION_MS = int(max(0.0, finished_at - _PADDLE_WARMUP_STARTED_AT) * 1000)
    _PADDLE_WARMUP_ERROR = str(error or "").strip()
    if state in {"ready", "error"}:
        _PADDLE_WARMUP_EVENT.set()


def _load_paddle_ocr_class():
    global _PADDLE_OCR_CLASS, _PADDLE_IMPORT_ATTEMPTED, _PADDLE_IMPORT_ERROR
    if _PADDLE_IMPORT_ATTEMPTED:
      return _PADDLE_OCR_CLASS
    _PADDLE_IMPORT_ATTEMPTED = True
    try:
        module = importlib.import_module("paddleocr")
        _PADDLE_OCR_CLASS = getattr(module, "PaddleOCR", None)
        _PADDLE_IMPORT_ERROR = "" if _PADDLE_OCR_CLASS is not None else "Classe PaddleOCR não encontrada."
    except Exception as err:  # pragma: no cover - optional dependency
        _PADDLE_OCR_CLASS = None
        _PADDLE_IMPORT_ERROR = str(err)
    return _PADDLE_OCR_CLASS


def _build_paddle_warmup_image():
    if Image is None:
        return None
    image = Image.new("RGB", (720, 240), "white")
    try:
        from PIL import ImageDraw  # type: ignore

        draw = ImageDraw.Draw(image)
        draw.text((24, 28), "MERCADO TESTE", fill="black")
        draw.text((24, 90), "ARROZ 2 x 3,50 7,00", fill="black")
        draw.text((24, 140), "TOTAL 7,00", fill="black")
    except Exception:
        return image
    return image


def _warm_paddle_ocr(ocr: Any) -> None:
    warm_image = _build_paddle_warmup_image()
    if warm_image is None:
        return
    try:
        ocr.ocr(warm_image)
    except Exception as err:
        raise RuntimeError(f"Falha ao aquecer PaddleOCR: {err}") from err


def _get_paddle_ocr():
    global _PADDLE_OCR, _PADDLE_IMPORT_ERROR
    if not _paddle_ocr_available():
        return None
    paddle_ocr_class = _load_paddle_ocr_class()
    if paddle_ocr_class is None:
        return None
    with _PADDLE_OCR_LOCK:
        if _PADDLE_WARMUP_STATE == "error":
            _PADDLE_OCR = None
        if _PADDLE_OCR is None:
            _PADDLE_WARMUP_EVENT.clear()
            started_at = time.time()
            _mark_paddle_warmup_state("warming", started_at=started_at)
            try:
                _PADDLE_OCR = paddle_ocr_class(**_build_paddle_ocr_kwargs())
                _warm_paddle_ocr(_PADDLE_OCR)
                _mark_paddle_warmup_state("ready", started_at=started_at, finished_at=time.time())
            except Exception as err:
                _PADDLE_OCR = None
                _PADDLE_IMPORT_ERROR = str(err)
                _mark_paddle_warmup_state("error", error=str(err), started_at=started_at, finished_at=time.time())
                raise
        elif _PADDLE_WARMUP_STATE != "ready":
            started_at = _PADDLE_WARMUP_STARTED_AT or time.time()
            _mark_paddle_warmup_state("warming", started_at=started_at)
            try:
                _warm_paddle_ocr(_PADDLE_OCR)
                _mark_paddle_warmup_state("ready", started_at=started_at, finished_at=time.time())
            except Exception as err:
                _PADDLE_IMPORT_ERROR = str(err)
                _mark_paddle_warmup_state("error", error=str(err), started_at=started_at, finished_at=time.time())
                raise
    return _PADDLE_OCR


def _start_paddle_warmup() -> None:
    global _PADDLE_WARMUP_THREAD
    if not PADDLE_OCR_ENABLE_WARMUP or not _paddle_ocr_available():
        return
    if _PADDLE_WARMUP_STATE == "ready":
        return
    if _PADDLE_WARMUP_THREAD and _PADDLE_WARMUP_THREAD.is_alive():
        return

    def _runner() -> None:
        try:
            _get_paddle_ocr()
        except Exception as err:
            print(f"[receipt_service] paddle warmup failed: {err}", file=sys.stderr)

    _PADDLE_WARMUP_THREAD = threading.Thread(target=_runner, name="paddleocr-warmup", daemon=True)
    _PADDLE_WARMUP_THREAD.start()


def ocr_with_paddle(image_b64: str) -> tuple[str, list]:
    raw = _decode_image(image_b64)
    if raw is None:
        return "", []
    if Image is None:
        raise RuntimeError("PIL não disponível para OCR.")
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    ocr = _get_paddle_ocr()
    if ocr is None:
        raise RuntimeError(_PADDLE_IMPORT_ERROR or "PaddleOCR não está disponível neste ambiente.")
    result = ocr.ocr(image)
    lines: List[str] = []
    blocks: List[Any] = []
    for page in result or []:
        for block in page or []:
            text = block[1][0] if len(block) > 1 and block[1] else ""
            score = block[1][1] if len(block) > 1 and block[1] else None
            if text:
                lines.append(text)
                blocks.append({"text": text, "confidence": score, "bbox": block[0]})
    return "\n".join(lines), blocks


@dataclass
class OpenAIReceiptRequest:
    local_text: str
    image_b64: str
    currency: str
    categories: List[str]


def llm_extract_receipt(payload: OpenAIReceiptRequest) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurada no serviço.")
    schema = {
        "name": "receipt_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "store": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"]},
                "taxId": {"type": ["string", "null"]},
                "paymentMethod": {"type": ["string", "null"]},
                "subtotal": {"type": "number"},
                "grandTotal": {"type": "number"},
                "discounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "amount": {"type": "number"}
                        },
                        "required": ["description", "amount"],
                        "additionalProperties": False
                    }
                },
                "fees": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "amount": {"type": "number"}
                        },
                        "required": ["description", "amount"],
                        "additionalProperties": False
                    }
                },
                "taxes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "rate": {"type": "number"},
                            "taxable": {"type": "number"},
                            "amount": {"type": "number"}
                        },
                        "required": ["label", "rate", "taxable", "amount"],
                        "additionalProperties": False
                    }
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "qty": {"type": "number"},
                            "unitPrice": {"type": "number"},
                            "lineTotal": {"type": "number"},
                            "category": {"type": "string"},
                            "confidence": {"type": "number"}
                        },
                        "required": ["description", "qty", "unitPrice", "lineTotal", "category", "confidence"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["store", "date", "taxId", "paymentMethod", "subtotal", "grandTotal", "discounts", "fees", "taxes", "items"],
            "additionalProperties": False
        }
    }

    instruction = (
        "Extraia dados fiscais de um recibo. "
        "Separe obrigatoriamente subtotal, descontos, taxas de serviço, impostos e total final. "
        "Não promova SUBTOTAL/TOTAL/IVA/ICMS/TAXA a item. "
        f"Moeda de contexto: {payload.currency}. "
        f"Categorias disponíveis: {', '.join(payload.categories) if payload.categories else 'Outros'}."
    )
    user_text = (
        "Use primeiro o OCR textual abaixo, e só use a imagem para resolver ambiguidade visual. "
        "Retorne somente dados fiéis ao documento.\n\n"
        f"OCR bruto:\n{payload.local_text or '[vazio]'}"
    )
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0.05,
        "max_completion_tokens": 1800,
        "response_format": {
            "type": "json_schema",
            "json_schema": schema
        },
        "messages": [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{payload.image_b64}", "detail": "auto"}}
                ]
            }
        ]
    }
    req = urllib.request.Request(
        OPENAI_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI HTTP {err.code}: {body[:220]}")
    except Exception as err:
        raise RuntimeError(f"OpenAI indisponível: {err}")

    raw_content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        parsed = json.loads(raw_content)
    except Exception as err:
        raise RuntimeError(f"Structured output inválido: {err}")
    result = parse_receipt_text(payload.local_text or "", source="service_llm")
    result.update({
        "store": (parsed.get("store") or result.get("store") or "").strip(),
        "date": parsed.get("date") or result.get("date"),
        "taxId": parsed.get("taxId") or result.get("taxId"),
        "paymentMethod": parsed.get("paymentMethod") or result.get("paymentMethod"),
        "subtotal": round(parsed.get("subtotal") or result.get("subtotal") or 0, 2),
        "grandTotal": round(parsed.get("grandTotal") or result.get("grandTotal") or 0, 2),
        "total": round(parsed.get("grandTotal") or result.get("grandTotal") or 0, 2),
        "discounts": parsed.get("discounts") or result.get("discounts") or [],
        "fees": parsed.get("fees") or result.get("fees") or [],
        "taxes": parsed.get("taxes") or result.get("taxes") or [],
        "items": parsed.get("items") or result.get("items") or [],
        "needsReview": False,
        "source": "service_llm",
    })
    result["confidence"] = max(0.92, _score_extraction(result))
    result["diagnostics"] = {
        **result.get("diagnostics", {}),
        "layoutAmbiguous": False,
        "itemSectionMissing": len(result.get("items") or []) == 0,
    }
    return result


def build_backend_flags() -> Dict[str, Any]:
    return {
        "parser": True,
        "paddleocr": _paddle_ocr_available(),
        "llm_fallback": bool(OPENAI_API_KEY),
        "openai": bool(OPENAI_API_KEY),
        "paddle_ready": _PADDLE_WARMUP_STATE == "ready",
    }


def _build_receipt_mode(backends: Dict[str, Any]) -> str:
    if backends.get("paddleocr") and backends.get("llm_fallback"):
        return "paddleocr_llm"
    if backends.get("paddleocr"):
        return "paddleocr"
    return "parser_service"


def _build_receipt_runtime_message(backends: Dict[str, Any]) -> str:
    if backends.get("paddleocr") and not backends.get("paddle_ready"):
        return "PaddleOCR disponivel e em aquecimento; as primeiras leituras podem degradar ate o warm-up terminar."
    if backends.get("paddleocr") and backends.get("llm_fallback"):
        return "OCR estruturado pronto com PaddleOCR e fallback LLM."
    if backends.get("paddleocr"):
        return "OCR estruturado pronto com PaddleOCR."
    if RUNNING_ON_VERCEL:
        return "Producao sem PaddleOCR nesta infraestrutura; a API opera em parser service."
    return "Servico OCR pronto em modo parser service; active PaddleOCR no ambiente local para OCR robusto."


def _upstream_proxy_enabled() -> bool:
    return bool(OCR_SERVICE_UPSTREAM_URL)


def _classify_ably_build_error(err: Exception) -> str:
    message = str(err or "").lower()
    if "capability" in message or "channel denied" in message:
        return "capability_error"
    if "chave" in message or "ably" in message or "configur" in message:
        return "ably_not_configured"
    return "upstream_error"


def build_health_payload() -> Dict[str, Any]:
    active_key = _active_ably_key()
    backends = build_backend_flags()
    mode = _build_receipt_mode(backends)
    details = {
        "severity": "ok" if backends.get("paddleocr") and backends.get("paddle_ready") else "warn",
        "message": _build_receipt_runtime_message(backends),
        "deployment": "hosted" if RUNNING_ON_VERCEL else "local",
        "paddle": _paddle_warmup_details(),
    }
    service_up = True
    if _upstream_proxy_enabled():
        try:
            upstream = _call_upstream_json("/health", method="GET", timeout_ms=min(OCR_SERVICE_UPSTREAM_TIMEOUT_MS, 4500))
            upstream_backends = upstream.get("backends") or {}
            if upstream_backends:
                backends = {
                    "parser": upstream_backends.get("parser") is not False,
                    "paddleocr": bool(upstream_backends.get("paddleocr")),
                    "llm_fallback": bool(upstream_backends.get("llm_fallback") or upstream_backends.get("openai")),
                    "openai": bool(upstream_backends.get("openai")),
                }
                mode = str(upstream.get("mode") or _build_receipt_mode(backends))
            details = {
                "severity": upstream.get("details", {}).get("severity") or ("ok" if backends.get("paddleocr") else "warn"),
                "message": upstream.get("details", {}).get("message") or "OCR dedicado activo via upstream.",
                "deployment": "hosted" if RUNNING_ON_VERCEL else "local",
                "paddle": upstream.get("details", {}).get("paddle") or {},
                "proxy": {
                    "enabled": True,
                    "upstream": OCR_SERVICE_UPSTREAM_URL,
                    "active": True,
                    "upstream_timestamp": upstream.get("timestamp") or "",
                },
            }
            service_up = upstream.get("service_up", True) is not False
        except Exception as err:
            backends = {
                "parser": True,
                "paddleocr": False,
                "llm_fallback": False,
                "openai": False,
            }
            mode = "parser_service"
            details = {
                "severity": "warn",
                "message": f"OCR dedicado indisponivel; fallback parser service hospedado activo. Motivo: {err}",
                "deployment": "hosted" if RUNNING_ON_VERCEL else "local",
                "proxy": {
                    "enabled": True,
                    "upstream": OCR_SERVICE_UPSTREAM_URL,
                    "active": False,
                    "upstream_error": str(err),
                },
            }
            service_up = True
    return _response_payload(
        True,
        "receipt_service.health",
        mode=mode,
        details=details,
        service_up=service_up,
        backends=backends,
        ably={
            "configured": bool(active_key),
            "token_auth": bool(active_key),
            "requires_supabase_auth": True,
            "namespace": ABLY_APP_NAMESPACE,
            "key_pool_size": len(ABLY_API_KEYS),
            "active_key_name": active_key[0] if active_key else "",
            "ttl_ms": ABLY_TOKEN_TTL_MS,
        },
        python=sys.version.split()[0],
        recommended_python="3.11",
    )


def build_ably_token_response(payload: Dict[str, Any], auth_header: str) -> tuple[int, Dict[str, Any]]:
    access_token = auth_header.split(" ", 1)[1].strip() if str(auth_header or "").lower().startswith("bearer ") else ""
    auth_result = _verify_supabase_access_token(access_token)
    if not auth_result.get("ok"):
        error = str(auth_result.get("error") or "session_invalid")
        status = 401 if error in {"supabase_auth_required", "session_invalid"} else 503
        return status, build_error_payload(
            "receipt_service.ably_token",
            error,
            mode="token_auth",
            details=auth_result.get("details") or {"message": "Nao foi possivel validar a sessao Supabase."},
        )

    supabase_user = auth_result.get("user") or {}
    user_id = _clean_ably_segment(supabase_user.get("id"), "anon")
    device_id = _clean_ably_segment(payload.get("deviceId"), "device")
    requested_client_id = _clean_ably_segment(payload.get("clientId"))
    requested_channel = _clean_ably_segment(payload.get("channel"))
    expected_channel = _build_ably_channel_name(user_id)
    channel_name = requested_channel if requested_channel == expected_channel else expected_channel
    client_id = requested_client_id or f"supabase:{user_id}:{device_id or 'device'}"

    try:
        token_request = _build_ably_token_request(client_id, channel_name)
    except Exception as err:
        error = _classify_ably_build_error(err)
        return 503, build_error_payload(
            "receipt_service.ably_token",
            error,
            mode="token_auth",
            details={
                "message": str(err) or "Falha ao emitir token curto do Ably.",
                "channel": channel_name,
            },
        )
    return 200, _response_payload(
        True,
        "receipt_service.ably_token",
        mode="token_auth",
        details={
            "message": "Token curto do Ably emitido com sucesso.",
            "channel": channel_name,
            "clientId": client_id,
        },
        tokenRequest=token_request,
    )


def build_receipt_parse_response(payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    strategy = str(payload.get("strategy") or "auto").strip().lower()
    local_text = str(payload.get("localText") or "")
    image_b64 = str(payload.get("imageBase64HQ") or payload.get("imageBase64") or "")
    currency = str(payload.get("currency") or "EUR")
    categories = [str(item) for item in (payload.get("categories") or []) if str(item).strip()]
    pages_processed = int(payload.get("pagesProcessed") or 1)
    backends = build_backend_flags()
    mode = _build_receipt_mode(backends)

    try:
        local_result = parse_receipt_text(local_text, source="service_ocr", pages_processed=pages_processed) if local_text else None
        best_result = local_result
        upstream_error = ""

        if _upstream_proxy_enabled() and image_b64:
            try:
                upstream_payload = dict(payload)
                upstream_payload["strategy"] = strategy or "auto"
                upstream = _call_upstream_json("/api/receipt/parse", method="POST", payload=upstream_payload, timeout_ms=OCR_SERVICE_UPSTREAM_TIMEOUT_MS)
                upstream_backends = upstream.get("backends") or {}
                if upstream_backends:
                    backends = {
                        "parser": upstream_backends.get("parser") is not False,
                        "paddleocr": bool(upstream_backends.get("paddleocr")),
                        "llm_fallback": bool(upstream_backends.get("llm_fallback") or upstream_backends.get("openai")),
                        "openai": bool(upstream_backends.get("openai")),
                    }
                    mode = str(upstream.get("mode") or _build_receipt_mode(backends))
                upstream_result = upstream.get("result")
                if upstream.get("ok") and upstream_result:
                    normalised = normalise_extraction(upstream_result)
                    if best_result is None or _score_extraction(normalised) >= _score_extraction(best_result):
                        best_result = normalised
                        best_result.setdefault("diagnostics", {})
                        best_result["diagnostics"]["proxiedUpstream"] = True
                elif upstream.get("details", {}).get("message"):
                    upstream_error = str(upstream.get("details", {}).get("message") or "").strip()
            except Exception as err:
                upstream_error = str(err or "").strip()

        if _paddle_ocr_available() and image_b64 and (strategy in {"ocr", "auto"} or best_result is None or _needs_llm(best_result)):
            ocr_text, blocks = ocr_with_paddle(image_b64)
            if ocr_text.strip():
                ocr_result = parse_receipt_text(ocr_text, source="service_ocr", pages_processed=pages_processed)
                ocr_result["diagnostics"]["ocrBlocks"] = len(blocks)
                if best_result is None or _score_extraction(ocr_result) >= _score_extraction(best_result) + 0.06:
                    best_result = ocr_result
                    local_text = ocr_text

        if strategy in {"llm", "auto"} and image_b64 and OPENAI_API_KEY and _needs_llm(best_result):
            llm_result = llm_extract_receipt(OpenAIReceiptRequest(
                local_text=local_text,
                image_b64=image_b64,
                currency=currency,
                categories=categories,
            ))
            if best_result is None or _score_extraction(llm_result) >= _score_extraction(best_result):
                best_result = llm_result

        if best_result is None:
            raise RuntimeError("Nenhum backend conseguiu estruturar o recibo.")

        return 200, _response_payload(
            True,
            "receipt_service.receipt_parse",
            mode=mode,
            details={
                "message": (
                    f"OCR dedicado indisponivel nesta tentativa; resultado devolvido pelo fallback local/parser. Motivo: {upstream_error}"
                    if upstream_error
                    else (_build_receipt_runtime_message(backends) if not _upstream_proxy_enabled() else "OCR dedicado activo via upstream.")
                ),
                "deployment": "hosted" if RUNNING_ON_VERCEL else "local",
                "proxy": {
                    "enabled": _upstream_proxy_enabled(),
                    "upstream": OCR_SERVICE_UPSTREAM_URL if _upstream_proxy_enabled() else "",
                    "upstream_error": upstream_error,
                },
            },
            result=best_result,
            score=_score_extraction(best_result),
            strategy=strategy,
            backends=backends,
        )
    except Exception as err:
        return 503, build_error_payload(
            "receipt_service.receipt_parse",
            "receipt_parse_failed",
            mode=mode,
            details={
                "message": str(err) or "A leitura do recibo falhou nesta tentativa.",
                "deployment": "hosted" if RUNNING_ON_VERCEL else "local",
            },
            backends=backends,
        )


def _proxy_secret_error_payload() -> Dict[str, Any]:
    return build_error_payload(
        "receipt_service.ocr_proxy",
        "ocr_service_auth_required",
        details={"message": "Este OCR dedicado exige a chave curta de proxy antes de responder."},
    )


def _proxy_secret_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not OCR_SERVICE_ENFORCE_SHARED_SECRET:
        return True
    provided = str(handler.headers.get("X-OCR-Service-Key", "") or "").strip()
    return bool(OCR_SERVICE_SHARED_SECRET) and secrets.compare_digest(provided, OCR_SERVICE_SHARED_SECRET)


class ReceiptServiceHandler(BaseHTTPRequestHandler):
    server_version = "ReceiptService/1.0"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        _send_cors_headers(self)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path, _query = _parse_query(self.path)
        if path != "/health":
            _json_response(self, 404, build_error_payload("receipt_service.health", "not_found", details={"message": "Endpoint nao encontrado."}))
            return
        _json_response(self, 200, build_health_payload())

    def do_POST(self) -> None:  # noqa: N802
        path, _query = _parse_query(self.path)
        if path == "/api/ably/token":
            try:
                payload = _read_json(self)
            except Exception as err:
                _json_response(self, 400, build_error_payload("receipt_service.ably_token", "json_invalid", mode="token_auth", details={"message": f"JSON invalido: {err}"}))
                return
            status, response = build_ably_token_response(payload, self.headers.get("Authorization", ""))
            _json_response(self, status, response)
            return

        if path != "/api/receipt/parse":
            _json_response(self, 404, build_error_payload("receipt_service.receipt_parse", "not_found", details={"message": "Endpoint nao encontrado."}))
            return
        if not _proxy_secret_authorized(self):
            _json_response(self, 401, _proxy_secret_error_payload())
            return
        try:
            payload = _read_json(self)
        except Exception as err:
            _json_response(self, 400, build_error_payload("receipt_service.receipt_parse", "json_invalid", details={"message": f"JSON invalido: {err}"}))
            return
        status, response = build_receipt_parse_response(payload)
        _json_response(self, status, response)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ReceiptServiceHandler)
    _start_paddle_warmup()
    if PADDLE_OCR_ENABLE_WARMUP and PADDLE_OCR_STARTUP_GRACE_MS > 0 and _paddle_ocr_available():
        _PADDLE_WARMUP_EVENT.wait(timeout=max(0, PADDLE_OCR_STARTUP_GRACE_MS) / 1000)
    print(f"[receipt_service] listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[receipt_service] stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
