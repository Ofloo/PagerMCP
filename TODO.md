# TODO

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
- **Clock-skew / uurgrenzen:** de app dekt uurwissels met een ±2 minuten venster
  (hash-kandidaten van het vorige en volgende uur ook proberen, of de server
  stuurt het gebruikte uur expliciet mee). Worst-case: de app kijkt voor een
  bericht dat er niet is — één loze fetch, onschuldig want at-least-once.
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
