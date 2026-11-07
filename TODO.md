# TODO

## Hub / multi-instance roaming (later uit te werken door eigenaar)

**Status:** idee — eigenaar legt dit later uit; geen ontwerp of implementatie nog.

### Variant: P2P-mesh i.p.v. hub (nieuwste richting van de eigenaar)

**Status: GEÏMPLEMENTEERD in v0.14.x** (mesh.py; PAGER_PEERS, WHO HAS voor mailbox-hashes én fingerprints, 302-roaming, hop-limit + seen-set, statische + dynamische peer-blacklist, MESH_OPEN_QUERIES, PAGER_PUBLIC_URL; one-sided open peering).
Geen hub, maar een **peer-to-peer-netwerk**:
- **`PAGER_PEERS=host1,host2,...`** via env: je directe peers; zij doen hetzelfde.
- Bij een onbekende UUID stuurt de server **"WHO HAS `<hash>`?"** naar zijn
  peers; wie de mailbox kent antwoordt, anders wordt de query doorgestuurd naar
  hún peers (met **hop-limit/TTL** zodat iedereen iedereen kan bereiken).
- Gevonden → **302** naar die peer; niemand → lokaal nieuwe mailbox.
- Alleen een **hash** op de draad, geen kale UUID; berichten blijven op de
  eigenaar-instance.
- Open punten: **loop-preventie** (query-id + seen-set), hop-limit,
  peer-authenticatie, split-brain bij gelijktijdige aanmaak.
- **Peering is one-sided en open** (eigenaar + voorkeur): alleen de vragende
  kant configureert (`PAGER_PEERS`); de andere kant hoeft niets te doen en
  iedereen die wil peeren kan dat gewoon doen. Geen wederzijdse handshake.
  Afbakening via hashes, hop-limit, en de blacklist (statisch + dynamisch bij
  query-flooding). Optioneel gesloten: `MESH_OPEN_QUERIES=0` beantwoordt dan
  alleen geconfigureerde peers.
- (De hub-variant hieronder blijft als alternatief staan; de mesh dekt hetzelfde
  doel zonder directory-server.)

**Leidend principe: deel alleen wat gedeeld móét worden** — dat bespaart
bandbreedte én privacy. Dus: lookup = alleen een hash (geen UUID/payload);
registratie = alleen roterende hashes, opt-in; geen heartbeats/meldingen;
berichten blijven op de eigenaar-instance en worden niet gesynchroniseerd;
de hub bewaart geen berichten en de cache verloopt met de hash-periode.

Richting: de pager-server opsplitsen in een **hub**-model waarbij meerdere
instances zich kunnen **inschrijven** en clients kunnen **roamen** tussen
instances — kortom een **federated systeem zonder centrale registratie**:
elke instance is soeverein en standalone-by-default; wie wil zet alleen
`HUB_HOST` ("daar moet ik kijken") en is daarmee klaar. Consequenties voor
het huidige een-replica-model (SQLite per instance) en voor
UUID-routing/aflevergaranties volgen zodra de eigenaar het toelicht. Eerder
ontwerpdocument zei al: multi-replica hoort dan een shared store (Redis e.d.)
te gebruiken zonder het externe protocol te wijzigen.

### Beoogd lookup-mechanisme (toelichting eigenaar)
**De hub ís de party line**: een directory-dienst waarbij instances zich
**inschrijven**. De hub bewaart geen berichten, alleen de mapping
**hash(mailbox-UUID) → instance** (geen kale UUID's).
1. Komt een request voor een **onbekende mailbox-UUID** binnen, dan vraagt de
   server aan de **hub** of een instance die mailbox kent — via het
   **hash-principe** (de hub ziet dus alleen de hash).
2. **Niemand kent hem** → lokaal een **nieuwe mailbox** (huidige impliciete
   aanmaakgedrag).
3. **De hub kent een eigenaar** → **HTTP 302** naar die instance; de client
   vraagt daar opnieuw.
Open punten: **geen registratie, geen meldingen, geen heartbeats** — de
operator geeft via `HUB_HOST` alleen mee *waar* de instance moet kijken, en
dat is alles. De instance doet uitsluitend een **lookup** bij een onbekende
UUID. **Leeg = standalone** (huidige gedrag). Verder uit te werken: het
party-line-request/response-formaat, hash-versheid/periode, split-brain (twee
instances maken tegelijk dezelfde onbekende UUID aan), vertrouwen van het
302-doel, en hoe de hub zelf aan zijn eigenaar-mapping komt (buiten de
instance om; geen instance-registratie of -rapportering).

### Verfijningen (eigenaar)
- **Protocol-verfijning:** bij `mailbox_encrypted` hoort een **verplichte
  `key_id`** — de fingerprint van het certificaat waarmee versleuteld is
  (**alleen verplicht als er versleuteld wordt**; bij kale `uuid` geen
  vereiste). Daarmee weet de server (a) met welke private key hij moet
  ontsleutelen (renew-overlap met meerdere sleutels) en (b) voor welke
  instance het bericht is in het federatieve model (fingerprint = server-id).
  **Bij een onbekende fingerprint: niet meteen 400, maar `WHO HAS
  <fingerprint>?` naar de peers → 302 naar de instance die die sleutel heeft
  (en dus kan ontsleutelen).** Pas als niemand hem kent → 400/unknown. Zo
  is de lookup uniform voor mailbox-hashes én voor fingerprints.
  hoeft toe te kennen. Nodig voor P2P: de afzender moet weten **met welk
  certificaat** hij de bestemmings-UUID moet versleutelen. Lookup: hub zegt
  *welke instance-id* bij een mailbox-hash hoort (en/of diens public key);
  afzender versleutelt met die instance's cert; de eigenaar ontsleutelt met
  zijn eigen private key.
- **Server-id ≠ `hostname`**: op Docker is dat de random container-id (wijzigt
  bij elke redeploy); op Bastille/Pot wel stabiel (`pager`/`pagerpot.test`).
  Beter: **de PGP-key-fingerprint als server-id** (persistent in DATA_DIR,
  stabiel over redeploys, en direct de id van het P2P-certificaat), met een
  env-override (`PAGER_SERVER_ID`) voor een eigen naam. Bijkomend voordeel:
  de P2P-client **kent die fingerprint al** (het is de key-id van de public key
  waarmee hij versleutelt), dus er is geen aparte id-uitwisseling nodig — de
  hub zegt alleen *welke fingerprint* bij de mailbox-hash hoort.
- **Optionele registratie van vastgehouden mailboxen bij de hub**: een instance
  kan (opt-in, naast het kale query-model) zijn mailboxen als **hash** bij de
  hub melden, zodat de hub de mapping **cachet** en niet bij elke lookup hoeft
  rond te vragen.
- **Hash-rotatie (bijv. elke 24h)**: anders kan de hub mailboxen onbeperkt
  volgen. Met een roterende hash is de cache alleen binnen de periode
  linkbaar. Beide kanten berekenen dezelfde hash (`sha256(uuid:periode)`),
  met klok-offset via `/settings` `server_time`; periode instelbaar via env
  (bijv. `HUB_HASH_PERIOD_SECONDS=86400`).
- **Hub-cache TTL = de hash-periode** (default 24h, configureerbaar): een
  geregistreerde hash verloopt vanzelf mee met de rotatie; geen eeuwige
  mapping.
- **De hub leidt de servers via zijn eigen `/settings`**: hij adverteert daar
  zijn parameters (hash-periode, cache-TTL, ...), zodat een instance zich
  **zelf configureert** tegen de hub — hetzelfde principe als clients
  tegenover elke PagerMCP-server.

## P2P: bestemmings-UUID versleutelen (server-public-key via /settings)

**Status: GEÏMPLEMENTEERD in v0.13.0** (gpgkeys.py; auto-keygen in DATA_DIR/gnupg, 1 jaar geldig + renew 30 dagen voor expiry met overlap; `p2p.public_key`/`key_id`/`gpg_enabled` in /settings; `P2P_GPG_ENABLED` standaard aan; `mailbox_encrypted` verplicht op het p2p-pad; decrypt-faal = 400; legacy_public_key/legacy_key_id placeholder in /settings; P2P_GPG_NEWKEY=true roteert, legacy blijft 2x message-TTL en wordt dan gepruned).

### Probleem
Bij P2P store-and-forward relayen peers hetzelfde pakket door. Naast de
GPG-versleutelde inhoud ziet elke relay-peer nu ook de **bestemmings-mailbox-UUID**
(in de notify-call). Die UUID is het bearer-adres van de ontvanger; delen met
alle tussenpeers is onnodig en onwenselijk.

### Voorstel
- De server krijgt een **eigen sleutelpaar** (PGP/RSA). De **publieke sleutel**
  wordt via `GET /settings` uitgedeeld (publiek sleutelmateriaal is per
  definitie openbaar; `/settings` is daarvoor geschikt).
- De afzender **versleutelt de bestemmings-UUID** met die publieke sleutel en
  stuurt alleen de ciphertext mee (bijv. `mailbox_encrypted` i.p.v. `uuid`).
  Relay-peers zien dan geen UUID meer.
- De server ontsleutelt, routeert, en gebruikt de ontsleutelde UUID voor de
  p2p-dedupe-cache `(mailbox, p2p_id)` zoals nu.
- Backwards compatibel: gewone `/notify` met kale UUID blijft bestaan; het
  versleutelde pad is opt-in voor P2P.

### Gekozen richting: GPG-encryptie van de bestemmings-UUID
**Reden:** de server moet de bestemmings-UUID kunnen **ontsleutelen om te
routeren**. De hash+created-variant kan dat niet (zou recipient-claims
vereisen) en is daarom verworpen.

- Server krijgt een **eigen sleutelpaar**; de **publieke sleutel** wordt via
  `GET /settings` uitgedeeld (publiek materiaal mag openbaar).
- P2P-afzender versleutelt de bestemmings-UUID met die publieke sleutel en
  stuurt `mailbox_encrypted` (ciphertext, base64) i.p.v. `uuid`.
- Server ontsleutelt met zijn private key, routeert naar de mailbox, en de
  dedupe-cache `(mailbox, p2p_id)` werkt ongewijzigd.
- Relay-peers zien alleen ciphertext. **Op het P2P-pad is de versleutelde UUID
  verplicht** (geen kale `uuid` toegestaan bij `p2p:true`); gewone
  direct-client `/notify` behoudt kale UUID (TLS naar je eigen server volstaat).
- De publieke sleutel komt in `/settings` **onder `p2p`** (`public_key` +
  `key_id`), zodat elke peer de bestemming kan versleutelen zonder iets geheims
  te delen; `key_id` bepaalt met welke private key de server ontsleutelt.
- `p2p.gpg_enabled` (bool) geeft aan of de versleuteling aan of uit staat.
- **Filosofie: de client configureert zichzelf volledig vanuit `/settings`**
  (kanalen, identity + periode, p2p-public-key, caps, retentie, server_time).

### Open implementatievragen
- **Schakelaar:** `P2P_GPG_ENABLED` (env, **standaard aan**) — het
  versleutelde-UUID-pad is uit te zetten voor wie het niet wil.
- **Key auto-generatie:** bij opstart een sleutelpaar maken als `DATA_DIR` er
  nog geen bevat (geen handmatige setup vereist).
- **Levensduur 1 jaar + auto-renew:** de server vernieuwt de sleutel zelf
  wanneer deze dreigt te verlopen (bijv. 30 dagen ervoor). Overlap: de oude
  publieke sleutel blijft tijdelijk naast de nieuwe in `/settings` zodat
  in-flight berichten nog ontsleuteld kunnen worden; elke sleutel krijgt een
  key-id zodat de server weet met welke private key hij moet ontsleutelen.
- **gpg-binary in de image** (python:3.12-slim heeft geen gpg → `apt-get
  install gnupg`; Bastille/Pot: `pkg install gnupg`) of python-gnupg-wrapper
  (heeft alsnog de binary nodig). RSA-OAEP via `cryptography` zou lichter zijn,
  maar de keuze is **echt PGP/GPG**.
- Sleutelopslag: **persistent in `DATA_DIR`** zodat dezelfde publieke sleutel
  over restarts geldig blijft; bij in-memory mode per-boot genereren (clients
  lezen dan opnieuw `/settings`).
- Alleen de UUID versleutelen, of de hele bestemmingsheader?
- Peers die geen GPG implementeren: kale-UUID-fallback blijft documentatie.

## Batch-polling: alle mailboxen in één request (blob)

**Status:** idee — feature, geen patch-release.

### Aanleiding
Elk contact heeft een eigen mailbox-UUID. Een client met 20 contacten pollt dus
20 mailboxen (20× `GET /wait` of 20 SSE-streams). Dat schaalt lineair met de
contactenlijst en maakt per-IP rate limiting lastig: "normaal gebruik" is al
tientallen requests per minuut.

### Voorkeursvariant (index-gecorreleerd)
Request en response worden **positioneel** gecorreleerd: je POST't
`{"bearers": ["<uuid0>", "<uuid1>", ...]}` en krijgt een lijst terug in dezelfde
volgorde, bijv. `[{"index": 0, "messages": [...]}, {"index": 1, ...}]`.
De response hoeft de mailbox-UUID dan niet te herhalen (alleen de index), wat
logging/caching van antwoorden ongevoelig maakt voor de bearer-waarden.

### Voorstel
- Nieuw endpoint, bijv. `POST /mailboxes/poll`, dat in één antwoord de pending
  berichten van álle opgegeven mailboxen teruggeeft — per mailbox de oudste
  unacked page, of een lijst per mailbox (te beslissen).
- **Hoe de mailboxen meegeven (drie opties, te kiezen bij implementatie):**
  1. Body: `{"uuids": ["...", "..."]}` — simpelste.
  2. Multi-bearer: `Authorization: Bearer <uuid1>,<uuid2>,...` (komma-gescheiden
     in één header; meerdere `Authorization`-headers zijn niet HTTP-standaard).
  3. Eigen header: `X-Pager-Mailboxes: <uuid1>,<uuid2>,...` naast de gewone
     `Authorization` (backwards-compatibel met de bestaande bearer-semantiek).
- Optioneel long-poll-semantiek: blokkeer tot **één** van de mailboxen een
  bericht heeft (dezelfde `timeout_seconds`-regels als `/wait`).
- Beveiliging blijft gelijk: elke UUID is z'n eigen bearer-credential; de client
  geeft alleen zijn eigen UUID's mee. Geen nieuwe rechten, geen registratie.
- Ack blijft per mailbox/page (`POST /ack` bestaat al).
- Voordeel: 1 request i.p.v. N, dus vriendelijker voor rate limits én batterij.
- Zou ook als gecombineerde SSE-stream kunnen (`/mailboxes/events` met lijst) —
  te overwegen bij de implementatie.

### Open vragen
- Maximaal aantal UUID's per request (bijv. 128) + payload-begrenzing.
- Eén timeout voor alle, of per-mailbox `max_age`-filters?
- Volgorde: per-mailbox FIFO behouden.

## FCM: mailbox-UUID in de push-trigger-body (gewogen → roterende hash)

**Status:** gewogen afweging — geen actie nodig in de server-code nu.

### Achtergrond
De data-only trigger die de server naar een push-endpoint stuurt
(`{"type":"page","mailbox":"<uuid>","id":...}`) bevat de **mailbox-UUID**.
Bij het `fcm`-kanaal lekt die daardoor naar Google FCM; bij `ntfy` of een
generieke `http`-subscription naar die service/endpoint. Wie de UUID heeft, kan
de mailbox lezen én schrijven.

De UUID blijft in de trigger-body omdat de **pagerapp** dat veld gebruikt om te
bepalen welke mailbox hij op moet halen.

### Toekomstmigratie (vereist een pagerapp-update)
- De trigger-body stuurt dan `sha256(uuid || unix-uur)` in plaats van de UUID.
  Roteerbaar: elke uurwissel een nieuwe, pseudorandom waarde — correlatie over
  uren door breekt (binnen het uur blijft het identiek, dat is de trade-off).
  Omgrekeerd brute-forcen is onmogelijk (128-bit UUID is al random).
- **Beslissing: client handelt de uur-berekening zelf af** — de server stuurt
  géén `h`-veld mee in de trigger. De client berekent `h` met zijn eigen klok.
- **Klok-offset:** `GET /settings` gaat de huidige server-Unix-tijd tonen
  (bijv. `"server_time": <unix>`), zodat de client `offset = server_time -
  local_now` kan bepalen en `h = floor((local_now + offset)/3600)` uitrekent.
  Settings, niet status: settings is de discovery-endpoint die de client al
  raadpleegt. Toevoegen valt onder de migratie-release.
- **Clock-skew / uurgrenzen:** extra veiligheid is een ±2 minuten venster
  (kandidaat van het voorgaande uur ook proberen als de minuut < 2 is).
  Worst-case: de app kijkt voor een bericht dat er niet is — één loze fetch,
  onschuldig want at-least-once.
- **Levering mist nooit data** ongeacht de trigger: berichten blijven in de
  queue tot `POST /ack`; SSE-reconnect replay't de backlog; een gemiste of
  gecollapse'e wake-up is hoogstens een vertraagde wekking.

### Stappen
1. pagerapp: hash-matching op `mailbox_hash` (met skew-venster) i.p.v. lezen van
   `mailbox`.
2. Server: `sha256(uuid || unix-uur)` in `mailbox_hash` zetten; `mailbox` één
   release lang er naast (dual-read), daarna weglaten.
3. Docs (`/privacy`, README, RFC) bijwerken bij de migratieswitch.

### Tot dan
- Documenteer het lek als gewogen afweging in `/privacy`, README en RFC
  (todo: FCM/UUID-lek documenteren — sessie).
- Aanbeveling aan gebruikers: SSE waar mogelijk (geen derde partij, geen
  UUID-lek, direct).
