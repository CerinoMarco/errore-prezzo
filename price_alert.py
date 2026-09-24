#!/usr/bin/env python3
"""
price_alert.py - Monitor multi-store per errori di prezzo (SOLO notifica).

Motore agnostico alle fonti + adattatori pluggable. Aggiungere uno store =
aggiungere una sezione [[store]] in sources.toml, senza toccare il motore.
Nessuna dipendenza esterna: solo la standard library di Python 3.11+.

Modalita':
  python price_alert.py             loop continuo (poll ogni settings.poll_seconds)
  python price_alert.py --once      un solo giro e termina (utile per cron / test)
  python price_alert.py --list      elenca gli store configurati
  python price_alert.py --selftest  verifica le invarianti del motore (niente rete)

Invarianti garantite (vedi README.md):
  1. ANTI-POISON: la baseline si aggiorna SOLO con prezzi non-anomali. Un prezzo
     flaggato come errore non entra mai nella media mobile, altrimenti
     trascinerebbe giu' la baseline e spegnerebbe gli alert futuri.
  2. NIENTE RIPETIZIONI (latch): avvisa 1 volta per prodotto; ri-avvisa solo se
     il prezzo scende ANCORA o dopo che e' rientrato nel normale.
  3. FAIL-LOUD PER STORE: se una fonte va in errore, le altre proseguono e
     l'errore viene stampato, non silenziato.
"""

from __future__ import annotations

import argparse
import email
import gzip
import imaplib
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.parsers.expat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "sources.toml"
STATE_PATH = BASE_DIR / "state.json"
ENV_PATH = BASE_DIR / ".env"
USER_AGENT = "price-alert/1.0 (personal price-error monitor)"
HTTP_TIMEOUT = 20
INTER_PAGE_DELAY = 0.6     # pausa educata tra pagine dello stesso store (s)
INTER_STORE_DELAY = 4      # pausa tra un negozio e l'altro: niente raffiche (s)
DEFAULT_BACKOFF = 900      # attesa base dopo un 429/403 senza Retry-After (s)
MAX_BACKOFF = 3600         # tetto della pausa crescente (s)
MAIL_POLL_SECONDS = 300    # ogni quanto controllare la casella del ponte email (s)


class NotModified(Exception):
    """La risorsa non e' cambiata (HTTP 304): niente da rielaborare."""


class RateLimited(Exception):
    """Lo store ci sta rallentando (HTTP 429/403): backoff, non e' un guasto."""

    def __init__(self, retry_after: int | None = None):
        super().__init__()
        self.retry_after = retry_after

DEFAULT_SETTINGS = {
    "poll_seconds": 300,       # ogni quanto ripassare tutte le fonti
    "anomaly_drop": 0.40,      # -40% vs baseline (percentuale minima)
    "min_abs_saving": 20.0,    # 20 EUR minimo di sconto assoluto (baseline - prezzo)
    "alto_rischio_drop": 0.80, # oltre questo drop l'errore e' spesso annullabile
    "ema_alpha": 0.30,         # peso della nuova osservazione nella media mobile
    "trust_after": 3,          # letture normali prima di fidarsi della baseline
    "heartbeat_hours": [15],   # ore locali per il ping "tutto ok" su Telegram (oltre all'avvio)
    "max_workers": 8,          # richieste parallele
}


# --------------------------------------------------------------------------- #
# Modello dati
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    sku: str
    title: str
    price: float
    url: str
    currency: str = "EUR"
    compare_at: float | None = None   # prezzo barrato (Shopify compare_at_price)


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


_PRICE_JUNK_RE = re.compile(
    r"(EUR|USD|GBP|€|\$|£|\xa0)", re.IGNORECASE)
_PRICE_KEEP_RE = re.compile(r"[^0-9,.\-]")


def parse_price(raw) -> float | None:
    """Legge un prezzo da fonti eterogenee (JSON puro o testo scrapato) senza
    confondere virgola/punto. Regole (in ordine):
      - numero gia' numerico -> passa diretto.
      - presenti sia ',' che '.': l'ultimo dei due e' il separatore decimale,
        l'altro sono le migliaia ("1.299,00" e "1,299.00" -> 1299.00).
      - solo ',' con 1-2 cifre dopo l'ultima -> decimale ("19,99" -> 19.99);
        altrimenti sono migliaia ("1,299" -> 1299).
      - solo '.' o nessun separatore: lasciato a float() (i JSON/JSON-LD usano
        gia' il punto come decimale per specifica).
    Ritorna None se il valore non e' interpretabile: il chiamante scarta la
    variante invece di propagare un prezzo sbagliato."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = _PRICE_JUNK_RE.sub("", str(raw)).strip()
    if not s:
        return None
    neg = s.lstrip().startswith("-")
    s = _PRICE_KEEP_RE.sub("", s).lstrip("-")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if s.count(",") == 1 and 1 <= len(tail) <= 2:
            s = head.replace(",", "") + "." + tail
        else:
            s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def force_utf8_console() -> None:
    """Su Windows la console usa cp1252 e va in crash sulle emoji. Forza UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def load_env() -> None:
    """Carica .env (KEY=VALUE) in os.environ, senza sovrascrivere l'ambiente."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# Stato persistente (baseline + latch alert + cache HTTP)
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"baseline": {}, "alerts": {}, "http": {}}
    try:
        d = json.loads(STATE_PATH.read_text("utf-8"))
        for k in ("baseline", "alerts", "http"):
            d.setdefault(k, {})
        return d
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ATTENZIONE] state.json illeggibile ({e}); riparto da vuoto.",
              file=sys.stderr)
        return {"baseline": {}, "alerts": {}, "http": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    tmp.replace(STATE_PATH)   # scrittura atomica: niente stato corrotto se crasha


# --------------------------------------------------------------------------- #
# HTTP: GET JSON con ETag / If-Modified-Since (polling educato)
# --------------------------------------------------------------------------- #
def _http_fetch(url: str, http_state: dict, accept: str) -> bytes:
    """GET con ETag/If-Modified-Since (polling educato). Solleva NotModified
    (304) o RateLimited (429/403); su altri 4xx/5xx solleva HTTPError e il
    chiamante logga e passa oltre."""
    entry = http_state.get(url, {})
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    if entry.get("etag"):
        req.add_header("If-None-Match", entry["etag"])
    if entry.get("modified"):
        req.add_header("If-Modified-Since", entry["modified"])
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            http_state[url] = {
                "etag": r.headers.get("ETag"),
                "modified": r.headers.get("Last-Modified"),
            }
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:                    # non modificato
            raise NotModified
        if e.code in (429, 403):             # rallentaci: backoff, non e' un guasto
            ra = e.headers.get("Retry-After")
            raise RateLimited(int(ra) if (ra and ra.isdigit()) else None)
        raise


def http_get_json(url: str, http_state: dict):
    """Come _http_fetch, ma decodifica la risposta come JSON."""
    raw = _http_fetch(url, http_state, "application/json")
    return json.loads(raw.decode("utf-8", "replace"))


def http_get_text(url: str, http_state: dict) -> str:
    """Come _http_fetch, ma ritorna testo (per HTML/XML, es. sitemap). Le
    sitemap di cataloghi grandi sono spesso pubblicate compresse (.xml.gz):
    decomprime prima di decodificare."""
    raw = _http_fetch(url, http_state, "text/html,application/xml;q=0.9,*/*;q=0.8")
    if url.lower().endswith(".gz"):
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Adattatori per tipo di fonte  (registrati in ADAPTERS)
# --------------------------------------------------------------------------- #
def fetch_shopify(store: dict, http_state: dict) -> list[Product]:
    """Qualsiasi store Shopify espone /products.json (fino a 250 item/pagina).
    Nessun WAF da aggirare: e' un endpoint pubblico e strutturato."""
    domain = (store["domain"].strip()
              .replace("https://", "").replace("http://", "").strip("/"))
    max_pages = int(store.get("max_pages", 5))
    currency = store.get("currency", "EUR")
    out: list[Product] = []
    for page in range(1, max_pages + 1):
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            data = http_get_json(url, http_state)
        except NotModified:                    # pagina invariata: salta
            continue
        items = data.get("products", [])
        if not items:
            break
        for p in items:
            handle = p.get("handle", "")
            purl = f"https://{domain}/products/{handle}"
            title = p.get("title", "")
            for v in p.get("variants", []):
                price = parse_price(v.get("price"))
                if price is None:
                    continue
                compare = parse_price(v.get("compare_at_price"))
                vtitle = v.get("title", "")
                full = title if vtitle in ("", "Default Title") else f"{title} - {vtitle}"
                out.append(Product(sku=f"{domain}:{v.get('id')}", title=full,
                                   price=price, url=purl, currency=currency,
                                   compare_at=compare))
        if len(items) < 250:
            break
        time.sleep(INTER_PAGE_DELAY)           # educazione tra pagine
    return out


def fetch_woocommerce(store: dict, http_state: dict) -> list[Product]:
    """WooCommerce Store API pubblica (attiva di default dalle release con
    WooCommerce Blocks): endpoint JSON strutturato, stesso spirito di
    Shopify. ATTENZIONE: il prezzo arriva in UNITA' MINORI (es. centesimi)
    scalate da currency_minor_unit -> va DIVISO, non e' testo da
    riformattare (e' la trappola equivalente al virgola/punto per questa
    fonte)."""
    domain = (store["domain"].strip()
              .replace("https://", "").replace("http://", "").strip("/"))
    max_pages = int(store.get("max_pages", 5))
    out: list[Product] = []
    for page in range(1, max_pages + 1):
        url = (f"https://{domain}/wp-json/wc/store/v1/products"
               f"?per_page=100&page={page}")
        try:
            items = http_get_json(url, http_state)
        except NotModified:
            continue
        if not isinstance(items, list) or not items:
            break
        for p in items:
            prices = p.get("prices") or {}
            try:
                minor = int(prices.get("currency_minor_unit", 2) or 2)
            except (TypeError, ValueError):
                minor = 2
            scale = 10 ** minor
            price = parse_price(prices.get("price"))
            if price is None:
                continue
            price /= scale
            regular = parse_price(prices.get("regular_price"))
            compare = (regular / scale) if regular else None
            if compare is not None and abs(compare - price) < 1e-9:
                compare = None
            currency = prices.get("currency_code") or store.get("currency", "EUR")
            out.append(Product(sku=f"{domain}:{p.get('id')}",
                               title=p.get("name", ""), price=price,
                               url=p.get("permalink") or f"https://{domain}/",
                               currency=currency, compare_at=compare))
        if len(items) < 100:
            break
        time.sleep(INTER_PAGE_DELAY)
    return out


_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)


def _iter_jsonld_products(html_text: str):
    """Estrae i nodi schema.org Product dai blocchi <script type=ld+json>.
    Il prezzo in JSON-LD e' per specifica un numero semplice (offers.price),
    molto meno ambiguo del testo visibile in pagina."""
    for raw in _JSONLD_RE.findall(html_text):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        expanded = []
        for n in nodes:
            if isinstance(n, dict) and isinstance(n.get("@graph"), list):
                expanded.extend(n["@graph"])
            else:
                expanded.append(n)
        for n in expanded:
            if not isinstance(n, dict):
                continue
            t = n.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() == "product" for x in types if x):
                yield n


def _product_from_jsonld(node: dict, domain: str, page_url: str) -> Product | None:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    price = parse_price(offers.get("price", offers.get("lowPrice")))
    if price is None:
        return None
    sku = node.get("sku") or node.get("productID") or node.get("mpn") or page_url
    currency = offers.get("priceCurrency", "EUR")
    return Product(sku=f"{domain}:{sku}", title=node.get("name", ""),
                   price=price, url=node.get("url") or page_url, currency=currency)


class _XXEBlocked(Exception):
    """La sitemap dichiarava un'entita' XML: input remoto non fidato, la
    rifiutiamo invece di risolverla (protezione XXE / entity-expansion)."""


def _sitemap_locs(xml_text: str) -> list[str]:
    """Estrae i valori <loc> da una sitemap XML con un parser expat indurito:
    nessuna entita' esterna o DTD viene risolta, la sitemap e' contenuto
    remoto scaricato da terzi e va trattato come non fidato."""
    locs: list[str] = []
    state = {"in_loc": False, "buf": []}

    def start(name, _attrs):
        if name.rsplit(":", 1)[-1] == "loc":
            state["in_loc"] = True
            state["buf"] = []

    def end(name):
        if name.rsplit(":", 1)[-1] == "loc":
            state["in_loc"] = False
            text = "".join(state["buf"]).strip()
            if text:
                locs.append(text)

    def chardata(data):
        if state["in_loc"]:
            state["buf"].append(data)

    def reject_entity(*_a):
        raise _XXEBlocked

    p = xml.parsers.expat.ParserCreate()
    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chardata
    p.EntityDeclHandler = reject_entity
    p.UnparsedEntityDeclHandler = reject_entity
    p.ExternalEntityRefHandler = lambda *_a: 0   # rifiuta la risoluzione -> errore expat
    try:
        p.Parse(xml_text, True)
    except (xml.parsers.expat.ExpatError, _XXEBlocked):
        return []
    return locs


def fetch_jsonld_sitemap(store: dict, http_state: dict) -> list[Product]:
    """Adattatore generico per store NON-Shopify (PrestaShop, Magento, ecc.)
    senza API JSON pubblica: legge la sitemap per trovare le pagine prodotto,
    poi estrae il markup schema.org Product (JSON-LD) che la maggior parte
    degli e-commerce pubblica per la SEO. Piu' lento (1 richiesta per
    prodotto): il numero di prodotti per giro e' limitato da max_products
    per restare educati e stare nei tempi di un giro."""
    domain = (store["domain"].strip()
              .replace("https://", "").replace("http://", "").strip("/"))
    sitemap_url = store.get("sitemap_url", f"https://{domain}/sitemap.xml")
    max_products = int(store.get("max_products", 60))

    def fetch_locs(url: str) -> list[str]:
        try:
            text = http_get_text(url, http_state)
        except NotModified:
            return []
        return _sitemap_locs(text)

    locs = fetch_locs(sitemap_url)
    if locs and all(l.lower().endswith((".xml", ".xml.gz")) for l in locs):
        # sitemap-index (anche compresso): segui solo i sotto-sitemap che
        # sembrano di prodotto.
        product_children = [l for l in locs if "product" in l.lower()]
        targets = (product_children or locs)[:5]
        product_urls: list[str] = []
        for child in targets:
            time.sleep(INTER_PAGE_DELAY)
            product_urls.extend(fetch_locs(child))
            if len(product_urls) >= max_products:
                break
    else:
        product_urls = locs

    out: list[Product] = []
    for url in product_urls[:max_products]:
        time.sleep(INTER_PAGE_DELAY)
        try:
            page = http_get_text(url, http_state)
        except NotModified:
            continue
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue                          # pagina sparita/rotta: salta
        for node in _iter_jsonld_products(page):
            p = _product_from_jsonld(node, domain, url)
            if p is not None:
                out.append(p)
                break                          # una pagina prodotto -> un Product
    return out


ADAPTERS = {
    "shopify": fetch_shopify,
    "woocommerce": fetch_woocommerce,
    "jsonld": fetch_jsonld_sitemap,
}


# --------------------------------------------------------------------------- #
# Impostazioni (globali + override per-store)
# --------------------------------------------------------------------------- #
def resolve_settings(config: dict) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(config.get("settings", {}))
    # Validazione fail-loud: meglio fermarsi ora che alertare a caso.
    if not (0 < s["anomaly_drop"] < 1):
        raise ValueError("settings.anomaly_drop deve stare tra 0 e 1")
    if not (0 < s["alto_rischio_drop"] < 1):
        raise ValueError("settings.alto_rischio_drop deve stare tra 0 e 1")
    if not (0 < s["ema_alpha"] <= 1):
        raise ValueError("settings.ema_alpha deve stare tra 0 e 1")
    if s["min_abs_saving"] < 0:
        raise ValueError("settings.min_abs_saving non puo' essere negativo")
    if s["trust_after"] < 1:
        raise ValueError("settings.trust_after deve essere >= 1")
    return s


def effective_settings(settings: dict, store: dict) -> dict:
    s = dict(settings)
    for k in ("anomaly_drop", "min_abs_saving", "alto_rischio_drop",
              "ema_alpha", "trust_after"):
        if k in store:
            s[k] = store[k]
    return s


# --------------------------------------------------------------------------- #
# Rilevamento anomalie (invarianti anti-poison + latch anti-ripetizione)
# --------------------------------------------------------------------------- #
def _make_alert(p: Product, ref: float, drop: float,
                saving: float, settings: dict) -> dict:
    risk = "ALTO" if drop >= settings["alto_rischio_drop"] else "MEDIO"
    return {
        "sku": p.sku, "title": p.title, "price": round(p.price, 2),
        "reference": round(ref, 2), "drop_pct": round(drop * 100, 1),
        "saving": round(saving, 2), "currency": p.currency,
        "compare_at": p.compare_at, "legal_risk": risk, "url": p.url,
        "at": now_iso(),
    }


def evaluate(products: list[Product], state: dict, settings: dict) -> list[dict]:
    """Confronta ogni prodotto con la sua baseline (media mobile) e ritorna
    la lista degli alert. Muta state['baseline'] e state['alerts']."""
    baseline = state["baseline"]
    alerts = state["alerts"]
    fired: list[dict] = []
    n_trust = settings["trust_after"]
    alpha = settings["ema_alpha"]

    for p in products:
        b = baseline.get(p.sku)
        if b is None:
            # Prima volta: bootstrap. 'peak' = miglior stima del prezzo normale.
            baseline[p.sku] = {"ema": p.price, "peak": p.price, "n": 1,
                               "last_seen": now_iso()}
            continue

        b.setdefault("peak", b.get("ema", p.price))   # compat con stati vecchi
        trusted = b["n"] >= n_trust
        # WARM-UP (baseline giovane): riferimento = PICCO osservato. Gli errori
        # sono cali, quindi il prezzo piu' alto visto finora stima il "normale"
        # anche se la PRIMA lettura era gia' sbagliata: appena arriva un prezzo
        # normale, il picco lo cattura e l'errore successivo scatta.
        # A REGIME: media mobile, che segue la deriva legittima dei prezzi.
        ref = b["ema"] if trusted else max(b["peak"], p.price)
        if ref <= 0:                                  # dato sporco: reinizializza
            b.update(ema=p.price, peak=p.price, n=b["n"] + 1, last_seen=now_iso())
            continue

        drop = (ref - p.price) / ref
        saving = ref - p.price

        if drop >= settings["anomaly_drop"] and saving >= settings["min_abs_saving"]:
            # Candidato errore. Anti-poison: NON tocchiamo ema/peak/n.
            # LATCH: avvisa UNA sola volta per errore. Ri-notifica solo se il
            # prezzo scende ANCORA in modo netto (>=5%). Se resta uguale (saldo
            # che dura giorni) NON ripete, nemmeno dopo notti/riavvii: si sblocca
            # solo quando il prodotto rientra nel normale (ramo else -> pop).
            prev = alerts.get(p.sku)
            if prev is None or p.price < prev["price"] * 0.95:
                alerts[p.sku] = {"price": round(p.price, 2), "at": now_iso()}
                fired.append(_make_alert(p, ref, drop, saving, settings))
            b["last_seen"] = now_iso()
        else:
            # Prezzo rientrato nel normale: sblocca il latch, aggiorna picco/media.
            alerts.pop(p.sku, None)
            b["peak"] = max(b["peak"], p.price)
            if trusted:
                b["ema"] = alpha * p.price + (1 - alpha) * b["ema"]
            else:
                b["ema"] = p.price                    # in warm-up segue l'ultimo normale
                if b["n"] + 1 >= n_trust:
                    b["ema"] = max(b["ema"], b["peak"])  # a regime parti dal robusto
            b["n"] += 1
            b["last_seen"] = now_iso()

    return fired


# --------------------------------------------------------------------------- #
# Notifica (Telegram, con fallback console in dry-run)
# --------------------------------------------------------------------------- #
def format_alert(a: dict) -> str:
    risk_emoji = "\U0001F534" if a["legal_risk"] == "ALTO" else "\U0001F7E1"
    lines = [
        f"⚡ <b>Possibile errore prezzo</b> {risk_emoji}",
        f"<b>{esc(a['title'])}</b>",
        f"Prezzo: <b>{a['price']:.2f} {a['currency']}</b>  (rif. {a['reference']:.2f})",
        f"Calo: <b>-{a['drop_pct']:.1f}%</b> · risparmio ~{a['saving']:.2f} {a['currency']}",
    ]
    if a.get("compare_at"):
        lines.append(f"Prezzo barrato store: {a['compare_at']:.2f}")
    note = (" (drop estremo: spesso annullabile pre-spedizione)"
            if a["legal_risk"] == "ALTO" else "")
    lines.append(f"Rischio annullamento: <b>{a['legal_risk']}</b>{note}")
    lines.append(f'<a href="{esc(a["url"])}">Apri prodotto</a>')
    return "\n".join(lines)


def notify(alert: dict) -> None:
    text = format_alert(alert)
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if not token or not chat:
        print("\n[ALERT - dry-run, nessun TG_TOKEN/TG_CHAT]\n"
              + strip_html(text) + "\n" + alert["url"])
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(api, data=payload,
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            r.read()
    except urllib.error.URLError as e:
        print(f"[ERRORE Telegram] {e}", file=sys.stderr)


def send_test() -> int:
    """Invia un messaggio di prova a Telegram per verificare token e chat id."""
    load_env()
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if not token or not chat:
        print("Manca TG_TOKEN o TG_CHAT nel file .env. Compilali e riprova.")
        return 2
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": "✅ Test riuscito: il monitor errori di prezzo e' collegato a Telegram.",
    }).encode()
    req = urllib.request.Request(api, data=payload, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            r.read()
        print("OK: messaggio di test inviato. Controlla Telegram sul telefono.")
        return 0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            desc = json.loads(body).get("description", body[:200])
        except json.JSONDecodeError:
            desc = body[:200]
        print(f"ERRORE Telegram ({e.code}): {desc}")
        if "Unauthorized" in desc:
            print("  -> il TG_TOKEN e' sbagliato (ricontrolla da BotFather).")
        elif "chat not found" in desc.lower():
            print("  -> il TG_CHAT e' sbagliato, o non hai ancora scritto al bot.")
        return 1
    except urllib.error.URLError as e:
        print(f"ERRORE di rete: {e}")
        return 1


# --------------------------------------------------------------------------- #
# Ponte email -> Telegram (alert Amazon di CamelCamelCamel, opzionale)
# --------------------------------------------------------------------------- #
def _tg_send(text: str) -> tuple[bool, str]:
    """Invia testo (HTML) al bot Telegram. Ritorna (ok, errore)."""
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if not token or not chat:
        return False, "no-credentials"
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(api, data=payload, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            r.read()
        return True, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            desc = json.loads(body).get("description", body[:200])
        except json.JSONDecodeError:
            desc = body[:200]
        return False, f"{e.code}: {desc}"
    except urllib.error.URLError as e:
        return False, str(e)


def _decode_hdr(raw: str) -> str:
    parts = []
    for chunk, enc in decode_header(raw or ""):
        parts.append(chunk.decode(enc or "utf-8", "replace")
                     if isinstance(chunk, bytes) else chunk)
    return "".join(parts).strip()


def _first_link(msg) -> str:
    text = ""
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() in ("text/plain", "text/html"):
            try:
                text += (p.get_payload(decode=True) or b"").decode(
                    p.get_content_charset() or "utf-8", "replace")
            except Exception:
                pass
    links = re.findall(r'https?://[^\s"\'<>]+', text)
    for link in links:
        if "amazon." in link or "camelcamelcamel" in link:
            return link
    return links[0] if links else ""


def poll_mail_forward(state: dict) -> int:
    """Legge una casella IMAP dedicata e rilancia su Telegram le email di
    CamelCamelCamel. Attivo SOLO se MAIL_USER/MAIL_PASS sono in .env."""
    user = os.environ.get("MAIL_USER", "").strip()
    pw = os.environ.get("MAIL_PASS", "").strip()
    if not user or not pw:
        return 0
    host = os.environ.get("MAIL_HOST", "imap.gmail.com").strip()
    flt = os.environ.get("MAIL_FILTER", "camelcamelcamel").strip().lower()
    last = int(state.get("mail_last_uid", 0))
    sent = 0
    try:
        M = imaplib.IMAP4_SSL(host)
        M.login(user, pw)
        M.select("INBOX")
        crit = f"UID {last + 1}:*" if last else "ALL"
        typ, data = M.uid("search", None, crit)
        uids = [int(u) for u in data[0].split()] if data and data[0] else []
        for uidn in sorted(uids):
            if uidn <= last:
                continue
            last = uidn
            typ, md = M.uid("fetch", str(uidn), "(RFC822)")
            if not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            if flt and flt not in str(msg.get("From", "")).lower():
                continue
            subj = _decode_hdr(msg.get("Subject", ""))
            link = _first_link(msg)
            text = "\U0001F6D2 <b>Amazon · CamelCamelCamel</b>\n" + esc(subj)
            if link:
                text += f'\n<a href="{esc(link)}">Apri prodotto</a>'
            ok, err = _tg_send(text)
            if ok:
                sent += 1
            else:
                print(f"[mail->tg ERRORE] {err}", file=sys.stderr)
        M.logout()
        state["mail_last_uid"] = last
        save_state(state)
    except Exception as e:
        print(f"[ponte email FALLITO] {type(e).__name__}: {e}", file=sys.stderr)
    return sent


def mail_test() -> int:
    """Verifica la connessione IMAP alla casella del ponte."""
    load_env()
    user = os.environ.get("MAIL_USER", "").strip()
    pw = os.environ.get("MAIL_PASS", "").strip()
    if not user or not pw:
        print("Manca MAIL_USER o MAIL_PASS in .env (ponte email non configurato).")
        return 2
    host = os.environ.get("MAIL_HOST", "imap.gmail.com").strip()
    try:
        M = imaplib.IMAP4_SSL(host)
        M.login(user, pw)
        M.select("INBOX")
        typ, data = M.uid("search", None, "ALL")
        n = len(data[0].split()) if data and data[0] else 0
        M.logout()
        print(f"OK: connesso a {host} come {user}. Email in INBOX: {n}.")
        return 0
    except Exception as e:
        print(f"ERRORE IMAP: {type(e).__name__}: {e}")
        print("  -> per Gmail serve una 'password per app' (non quella normale)")
        print("     e l'accesso IMAP dev'essere attivo nell'account.")
        return 1


# --------------------------------------------------------------------------- #
# Ciclo principale
# --------------------------------------------------------------------------- #
def _poll_store(store: dict, http_state: dict) -> list[Product]:
    typ = store.get("type")
    adapter = ADAPTERS.get(typ)
    if adapter is None:
        raise ValueError(f"tipo fonte sconosciuto: {typ!r} "
                         f"(disponibili: {list(ADAPTERS)})")
    return adapter(store, http_state)


def run_once(config: dict, state: dict, settings: dict) -> int:
    all_stores = [s for s in config.get("store", []) if s.get("enabled", False)]
    if not all_stores:
        print("Nessuno store attivo (enabled=true) in sources.toml.")
        return 0

    http_state = state.setdefault("http", {})
    backoff = state.setdefault("backoff", {})
    now = datetime.now(timezone.utc)
    n_alerts = 0

    # Salta gli store ancora in pausa dopo un 429/403 recente.
    stores = []
    for s in all_stores:
        name = s.get("name", s.get("domain", "?"))
        entry = backoff.get(name)
        until = entry["until"] if isinstance(entry, dict) else entry  # compat vecchio formato
        if until and parse_iso(until) > now:
            print(f"[{name}] in pausa (rate-limit) fino a {until}")
            continue
        stores.append(s)

    # SEQUENZIALE, un negozio per volta con una pausa in mezzo. Shopify limita
    # quante richieste /products.json accetta da uno stesso IP in poco tempo:
    # le raffiche in parallelo prendono 429. Meglio lento e completo.
    for i, s in enumerate(stores):
        name = s.get("name", s.get("domain", "?"))
        if i:
            time.sleep(INTER_STORE_DELAY)
        try:
            products = _poll_store(s, http_state)
        except RateLimited as e:        # non e' un guasto: rallenta (pausa crescente)
            prev = backoff.get(name)
            strikes = (prev["strikes"] if isinstance(prev, dict) else 0) + 1
            secs = e.retry_after or min(DEFAULT_BACKOFF * 2 ** (strikes - 1),
                                        MAX_BACKOFF)
            backoff[name] = {
                "until": (now + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "strikes": strikes}
            print(f"[{name}] 429/403: rallento, backoff {secs}s (x{strikes})",
                  file=sys.stderr)
            continue
        except Exception as e:          # fail-loud: uno store rotto non ferma gli altri
            print(f"[store '{name}' FALLITO] {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        backoff.pop(name, None)         # tutto ok: azzera eventuale pausa
        fired = evaluate(products, state, effective_settings(settings, s))
        for al in fired:
            notify(al)
            n_alerts += 1
        if products:
            print(f"[{name}] {len(products)} varianti · {len(fired)} alert")
        else:
            print(f"[{name}] nessuna modifica")

    save_state(state)
    return n_alerts


def selftest() -> int:
    """Verifica le invarianti del motore senza toccare la rete."""
    s = resolve_settings({"settings": {
        "anomaly_drop": 0.5, "min_abs_saving": 10,
        "ema_alpha": 0.3, "alto_rischio_drop": 0.8, "trust_after": 2}})
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  OK   " if cond else " FAIL  ") + msg)
        ok = ok and cond

    def ev(state, sku, price):
        return evaluate([Product(sku=sku, title="T", price=price, url="http://x")],
                        state, s)

    # --- parse_price: virgola/punto, valute, input sporco ---
    check(parse_price("19,99") == 19.99, "parse_price: virgola decimale")
    check(parse_price("1.299,00") == 1299.0,
          "parse_price: punto migliaia + virgola decimale (IT)")
    check(parse_price("1,299.00") == 1299.0,
          "parse_price: virgola migliaia + punto decimale (EN)")
    check(parse_price("1,299") == 1299.0,
          "parse_price: sola virgola, 3+ cifre -> migliaia")
    check(parse_price("€ 1.234,56") == 1234.56,
          "parse_price: simbolo valuta + spazio")
    check(parse_price("1.234") == 1.234,
          "parse_price: solo punto -> decimale per specifica (JSON/JSON-LD)")
    check(parse_price(19.99) == 19.99, "parse_price: gia' numerico")
    check(parse_price(None) is None, "parse_price: None -> None")
    check(parse_price("") is None, "parse_price: stringa vuota -> None")
    check(parse_price("n/a") is None, "parse_price: testo non numerico -> None")
    check(parse_price("-45,50") == -45.5, "parse_price: negativo")

    # --- Regime normale (baseline gia' consolidata) ---
    st = {"baseline": {}, "alerts": {}, "http": {}}
    check(ev(st, "a", 100.0) == [], "bootstrap: nessun alert")
    ev(st, "a", 100.0)                    # 2a lettura normale -> baseline fidata
    base = st["baseline"]["a"]["ema"]
    check(abs(base - 100.0) < 1e-9 and st["baseline"]["a"]["n"] >= 2,
          "warm-up: baseline consolidata a 100")
    fired = ev(st, "a", 30.0)             # errore
    check(len(fired) == 1, "errore: 1 alert generato")
    check(abs(st["baseline"]["a"]["ema"] - base) < 1e-9,
          "anti-poison: baseline NON modificata dal prezzo-errore")
    check(ev(st, "a", 30.0) == [], "latch: stesso prezzo NON ripete")
    check(ev(st, "a", 30.0) == [], "latch: persiste (niente ripetizione ogni mattina)")
    check(len(ev(st, "a", 25.0)) == 1, "latch: ri-avvisa se il prezzo scende ANCORA")
    ev(st, "a", 100.0)                    # rientra nel normale -> sblocca il latch
    check(len(ev(st, "a", 30.0)) == 1, "latch: sbloccato al rientro, poi ri-avvisa")

    # --- Cold-start #1: errore che COMPARE durante il warm-up ---
    st = {"baseline": {}, "alerts": {}, "http": {}}
    ev(st, "b", 99.0)                     # 1a lettura normale
    fired = ev(st, "b", 30.0)             # 2a lettura (warm-up): errore
    check(len(fired) == 1, "cold-start: errore in warm-up -> alert (vs picco)")

    # --- Cold-start #2: PRIMA lettura gia' errata, poi recupero ---
    st = {"baseline": {}, "alerts": {}, "http": {}}
    check(ev(st, "c", 30.0) == [],
          "cold-start: 1a lettura errata -> nessun alert (cieco, inevitabile)")
    ev(st, "c", 99.0)                     # arriva il normale: il picco lo cattura
    fired = ev(st, "c", 30.0)             # l'errore si ripresenta
    check(len(fired) == 1,
          "cold-start: baseline si corregge da sola, poi l'errore scatta")

    print("\nSELF-TEST: " + ("PASS ✅" if ok else "FAIL ❌"))
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Monitor multi-store per errori di prezzo (solo notifica).")
    ap.add_argument("--once", action="store_true", help="un solo giro e termina")
    ap.add_argument("--list", action="store_true", help="elenca gli store configurati")
    ap.add_argument("--selftest", action="store_true",
                    help="verifica le invarianti del motore (niente rete)")
    ap.add_argument("--test", action="store_true",
                    help="invia un messaggio di prova a Telegram e termina")
    ap.add_argument("--mailtest", action="store_true",
                    help="verifica la connessione alla casella email del ponte")
    args = ap.parse_args()

    force_utf8_console()

    if args.selftest:
        sys.exit(selftest())

    if args.test:
        sys.exit(send_test())

    if args.mailtest:
        sys.exit(mail_test())

    load_env()

    if not CONFIG_PATH.exists():
        print(f"Manca {CONFIG_PATH.name}. Configura gli store prima di partire.",
              file=sys.stderr)
        sys.exit(2)
    with open(CONFIG_PATH, "rb") as f:
        config = tomllib.load(f)

    if args.list:
        for s in config.get("store", []):
            flag = "ON " if s.get("enabled") else "off"
            print(f"[{flag}] {s.get('name', '?'):28} "
                  f"{s.get('type', '?'):10} {s.get('domain', '')}")
        return

    settings = resolve_settings(config)
    state = load_state()

    if args.once:
        n = run_once(config, state, settings)
        print(f"\nGiro singolo completato: {n} alert.")
        return

    print(f"Monitor avviato · poll ogni {settings['poll_seconds']}s "
          f"· Ctrl+C per fermare.")
    n_stores = sum(1 for s in config.get("store", []) if s.get("enabled"))
    hb_hours = settings.get("heartbeat_hours", []) or []
    # Heartbeat di AVVIO: la conferma "della mattina" quando si accende il PC.
    _tg_send(f"🟢 <b>Monitor avviato</b> — sorveglio {n_stores} negozi. Ti scrivo "
             f"solo se trovo un errore, o per il check di controllo.")
    last_mail = 0.0
    try:
        while True:
            t0 = time.time()
            run_once(config, state, settings)
            if time.time() - last_mail >= MAIL_POLL_SECONDS:
                m = poll_mail_forward(state)   # inoltra alert Amazon se il ponte e' attivo
                if m:
                    print(f"[mail] {m} alert Amazon inoltrati sul bot")
                last_mail = time.time()
            # Heartbeat PROGRAMMATO: una volta per ora-slot (es. le 15).
            now_local = datetime.now()
            slot = f"{now_local:%Y-%m-%d}-{now_local.hour}"
            if now_local.hour in hb_hours and state.get("hb_slot") != slot:
                _tg_send(f"🟢 <b>Tutto ok</b> — monitor attivo, {n_stores} negozi, "
                         f"nessun problema. ({now_local:%H:%M})")
                state["hb_slot"] = slot
                save_state(state)
            time.sleep(max(5, settings["poll_seconds"] - (time.time() - t0)))
    except KeyboardInterrupt:
        save_state(state)
        print("\nInterrotto. Stato salvato.")


if __name__ == "__main__":
    main()
