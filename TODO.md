# TODO

## P2P: bestemmings-UUID versleutelen (server-public-key via /settings)

**Status:** ontwerp-idee — feature voor 0.13.x, niet voor de 0.12.x-patchreeks.

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
