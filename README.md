# Monitor errori di prezzo (multi-store, solo notifica)

Un unico processo Python che sorveglia piu' negozi, impara il prezzo "normale"
di ogni prodotto e ti manda un **alert Telegram** quando un prezzo crolla in modo
anomalo. Tu poi compri **a mano**: niente auto-checkout, niente evasione anti-bot.

**Zero dipendenze.** Gira con la sola standard library di Python 3.11+
(qui: 3.13). Nessun `pip install`.

---

## Avvio in 3 mosse

1. **Bot Telegram**
   - Scrivi a [@BotFather](https://t.me/BotFather) -> `/newbot` -> copia il token.
   - Manda un messaggio al tuo bot, poi apri
     `https://api.telegram.org/bot<TOKEN>/getUpdates` e leggi il tuo `chat id`.

2. **Configura le credenziali**
   - Copia `.env.example` in `.env` e incolla `TG_TOKEN` e `TG_CHAT`.
   - Se lasci `.env` vuoto, il monitor gira in **dry-run** (stampa a schermo).

3. **Aggiungi gli store e prova**
   - In `sources.toml` metti `enabled = true` sugli store che vuoi (vedi sotto).
   - Prova un giro singolo:
     ```bash
     python price_alert.py --once
     ```
   - Il loop continuo locale (`python price_alert.py`, senza `--once`) resta
     utile per test, ma per girare **H24 anche a PC spento** vedi la sezione
     sotto: e' il modo pensato per l'uso normale.

---

## Deploy H24 su GitHub Actions (gratis, senza VPS, anche a PC spento)

Il modo per farlo girare sempre non e' piu' `avvio_automatico.vbs` (dipende
dal PC acceso) ma **GitHub Actions**: un cron gratuito ospitato da GitHub, in
`.github/workflows/monitor.yml`. Ogni 5 minuti fa un giro (`--once`) e si
riaddormenta — niente processo sempre acceso da pagare, niente VPS.

**Perche' repo pubblico:** i repository pubblici hanno minuti Actions
illimitati gratis; quelli privati solo 2000 min/mese, che con ~30 negozi e un
giro ogni 5 minuti si esaurirebbero in pochi giorni. Nel repo pubblico non
finisce **nessun segreto**: `TG_TOKEN`/`TG_CHAT` vivono solo nei GitHub
Secrets, mai nel codice (`.env` resta escluso da `.gitignore`).

**Dove vive lo stato:** `state.json` (baseline + cooldown) non sta sul branch
`main` — starebbe li' a far crescere la storia ad ogni giro. Vive da solo sul
branch `state`, che il workflow ricrea da zero (force-push) ad ogni run:
resta sempre **un solo commit**, non cresce mai.

### Setup (una volta sola)

1. **Crea il repo su GitHub** (pubblico) e pusha questo codice sul branch
   `main`. `.env`, `state.json` e `monitor.log` restano fuori (gia' in
   `.gitignore`): non servono su GitHub, li' lo stato parte vuoto e si
   ricostruisce da solo nei primi giri (fase di warm-up, vedi "Limiti
   onesti").
2. **Aggiungi i secrets** (Settings del repo su github.com -> *Secrets and
   variables* -> *Actions* -> *New repository secret*, oppure `gh secret set
   TG_TOKEN` / `gh secret set TG_CHAT` da terminale):
   - `TG_TOKEN` — il token del bot Telegram
   - `TG_CHAT` — il tuo chat id
   (Gli stessi valori del tuo `.env` locale.)
3. **Primo avvio:** tab *Actions* del repo -> workflow "Monitor errori di
   prezzo" -> *Run workflow* (trigger manuale, non serve aspettare il cron).
   Controlla che il run finisca verde. Da li' in poi parte da solo ogni 5
   minuti, senza bisogno del PC acceso.

**Se il numero di negozi cresce** e un giro rischia di superare gli 8 minuti
(il `timeout-minutes` del job), alza il cron a `*/10 * * * *` nel workflow
invece di far girare i negozi in parallelo: in parallelo si rischiano i
429/403 e scritture concorrenti sullo stesso `state.json` — il motore e'
pensato per essere sequenziale, di proposito.

`avvia.bat` / `avvio_automatico.vbs` restano nel repo ma sono ora opzionali:
utili solo per un loop locale di test, non piu' necessari per il
funzionamento continuo.

---

## Aggiungere uno store

Tre tipi supportati, registrati in `ADAPTERS` — il motore (`evaluate`, latch,
anti-poison) e' identico per tutti, cambia solo come si legge il prezzo:

### `shopify`
Qualsiasi store su piattaforma Shopify (moltissimi e-commerce italiani)
tramite l'endpoint pubblico `/products.json` — nessun WAF da aggirare.
**Test rapido:** apri `https://IL-DOMINIO/products.json`, deve rispondere
JSON con `"products": [...]`.

```toml
[[store]]
name    = "Nome negozio"
type    = "shopify"
domain  = "negozio.it"
enabled = true
```

### `woocommerce`
Store WordPress/WooCommerce con la Store API pubblica attiva (di default
nelle versioni recenti). **Test rapido:**
`https://IL-DOMINIO/wp-json/wc/store/v1/products?per_page=1` deve rispondere
un array JSON con `"prices": {...}`. Il prezzo li' e' in unita' minori
(centesimi) scalate da `currency_minor_unit`: il motore lo gestisce da solo.

```toml
[[store]]
name    = "Nome negozio"
type    = "woocommerce"
domain  = "negozio.it"
enabled = true
```

### `jsonld`
Adattatore generico per store che NON espongono un'API JSON pubblica
(PrestaShop, Magento, altri): legge `sitemap.xml`, segue gli eventuali
sotto-sitemap di prodotto, e su ogni pagina prodotto estrae il markup
schema.org `Product` (`<script type="application/ld+json">`) che la maggior
parte degli e-commerce pubblica per la SEO — il prezzo li' e' un numero
semplice, non testo da riformattare. Piu' lento (1 richiesta per prodotto):
`max_products` limita quanti ne legge per giro (default 60).

```toml
[[store]]
name         = "Nome negozio"
type         = "jsonld"
domain       = "negozio.it"
enabled      = true
max_products = 100          # opzionale
sitemap_url  = "https://negozio.it/sitemap_index.xml"   # opzionale, se non e' /sitemap.xml
```

**Limite onesto:** su cataloghi enormi (migliaia di URL nella sitemap
prodotto) il monitor legge sempre lo stesso primo blocco di `max_products`
URL ad ogni giro, non ruota sul resto del catalogo — copertura parziale ma
stabile, non casuale.

Nessuno di questi tre ha bisogno di un servizio di scraping esterno o a
pagamento: sono tutti endpoint pubblici del negozio stesso, letti con la sola
libreria standard di Python. Per uno store che non rientra in nessuno dei tre
(WAF aggressivo, nessuna sitemap, nessuna API), serve un adattatore dedicato:
passami l'URL e lo scrivo.

---

## Come decide

**La regola, in due numeri:** un alert scatta se il calo e' **>= 50 EUR E >= 40%**
rispetto al prezzo normale imparato. L'assoluto tiene fuori le briciole; la
percentuale tiene fuori i prezzi alti che scendono di poco (un articolo da
1.500 EUR a 1.450 e' -3%, rumore, non scatta). Vale per **qualunque categoria**:
e' il filtro a lavorare. I due numeri sono `min_abs_saving` e `anomaly_drop` in
`sources.toml` — troppi alert? alza `anomaly_drop` a 0.50; troppo pochi? 0.30.

Il valore di un monitor cosi' sta nel **non spammare** e nel **non perdere il
colpo vero**. Il motore garantisce tre invarianti, verificabili con
`python price_alert.py --selftest`:

1. **Anti-poison** — la baseline (media mobile del prezzo) si aggiorna *solo*
   con prezzi non-anomali. Un prezzo-errore non entra mai nella media, altrimenti
   la trascinerebbe giu' e spegnerebbe gli alert futuri su quello stesso prodotto.
2. **Niente doppioni** — stesso `sku` allo stesso prezzo non ti risveglia due
   volte entro `cooldown_hours`.
3. **Fail-loud per store** — se una fonte va in errore (endpoint chiuso, JSON
   rotto), le altre proseguono e l'errore viene stampato, non silenziato.
   Un **HTTP 429/403** (rate-limit) non e' un guasto: lo store va in **backoff**
   (default 15 min, o il `Retry-After` indicato) e nei giri successivi viene
   saltato senza toccare la rete, poi riprende da solo. Tra una pagina e l'altra
   dello stesso store c'e' una pausa educata di 0,6 s.

Ogni alert porta un flag **rischio annullamento**:

- **MEDIO** (`-40%..-80%`): fascia dove il valore e' piu' spesso incassabile.
- **ALTO** (`>= -80%`): errore macroscopico -> giuridicamente "riconoscibile",
  quindi il venditore lo annulla spesso prima di spedire. Da tentare, ma senza
  immobilizzarci aspettative.

---

## Limiti onesti

- **Cold-start (mitigato, non eliminato).** In warm-up il riferimento e' il
  **picco** dei prezzi visti (`trust_after` letture): siccome gli errori sono
  cali, il picco stima il prezzo normale. Cosi' un errore che *compare* mentre
  il monitor osserva scatta subito, e dopo una prima lettura gia' sbagliata la
  baseline si corregge da sola appena arriva un prezzo normale. Resta cieco solo
  il caso limite: prezzo gia' errato dal primissimo avvistamento **e mai**
  corretto -> lo impara come normale. Fix definitivo solo con storico esterno
  (es. Keepa) o un listino noto: non incluso di proposito, per restare zero-dep.
- **Soglie da tarare.** I default in `sources.toml` sono conservativi. Dopo
  qualche giorno di dati aggiusta `anomaly_drop` e `min_abs_saving`: troppo
  bassi = rumore, troppo alti = ti sfugge l'affare.
- **Educazione di rete.** Usa ETag/If-Modified-Since e polling ogni 90s di
  default. Gli errori vengono spesso corretti in pochi minuti, quindi troppo
  lento te li fa perdere: su lista piccola e fidata puoi scendere a 60s. Ma se
  nei log compaiono 429, rialza: e' il segnale che stai bussando troppo.
- **Copertura ora estesa, ma non universale.** Oltre a `shopify` (endpoint
  `/products.json`) ci sono `woocommerce` (Store API) e `jsonld` (sitemap +
  markup schema.org Product) — coprono anche PrestaShop/Magento/WordPress
  senza bisogno di scraping HTML "a mano" o servizi a pagamento. Restano
  fuori i siti con WAF aggressivo, senza sitemap pubblica o senza markup
  schema.org: per quelli servirebbe rientrare nella guerra anti-bot, che
  continuiamo a evitare di proposito. Meglio pochi store puliti e affidabili
  che tanti fragili — ogni negozio in `sources.toml` e' stato verificato a
  mano prima di essere aggiunto.
- **`jsonld`: copertura parziale sui cataloghi enormi.** Con migliaia di
  prodotti in sitemap, il monitor legge sempre lo stesso blocco iniziale
  (`max_products`, default 60) ad ogni giro — non ruota su tutto il
  catalogo. E' un limite di velocita'/educazione di rete (1 richiesta HTTP
  per prodotto), non un bug: alzare `max_products` allunga il giro.

---

## Comandi

```bash
python price_alert.py --selftest   # verifica le invarianti (niente rete)
python price_alert.py --list       # elenca gli store configurati
python price_alert.py --once       # un giro solo e termina (per cron/test)
python price_alert.py              # loop continuo
```

## File

| file            | cosa e'                                             |
|-----------------|-----------------------------------------------------|
| `price_alert.py`| motore (da auditare una volta, poi non si tocca)    |
| `sources.toml`  | lista store + soglie (quello che modifichi tu)      |
| `.env`          | token Telegram per uso locale (NON condividere, NON su GitHub) |
| `state.json`    | baseline + cooldown, generato in automatico (locale: file qui; su GitHub Actions: branch `state`) |
| `.github/workflows/monitor.yml` | cron GitHub Actions per il deploy H24 (vedi sopra) |
