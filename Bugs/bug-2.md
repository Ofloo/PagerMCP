# bug-2.md — stale backlog-levering (oude/superseded meldingen komen later als "nieuw")

- **Pager-versie bij melden** (`https://pager.ofloo.io/version`): `{"version": "0.3.1", "build": "18"}`
  (lokaal `http://10.13.17.60:6721/version`: idem)
- **Pager-versie bij afronden** (`https://pager.ofloo.io/version`): `{"version": "0.3.4", "build": "24"}`
- **Component**: `pager_mcp/storage.py` (TTL/backlog) + `server.py` `/wait` + `plugins/pager.js`
- **Ernst**: midden / verwarrend
- **Status**: **RESOLVED in `v0.3.4`** (commit `72ab583`) — server- en clientdeel uitgerold, productie geverifieerd

## Symptoom
Een melding die al achterhaald was kwam **later als nieuwe levering** binnen. Concreet: een
`done`-melding van een **afgebroken** poudriere-run werd bezorgd terwijl er al een **nieuwe**
`make release` liep. De client kon niet zien dat het om een oude page ging.

## Oorzaak
Pages blijven in de mailbox staan (default `MESSAGE_TTL_DAYS=7`) en worden bij de volgende
`/wait` bezorgd, ongeacht leeftijd. Er is geen age-/`since`-filter en geen korte server-timeout.
Een gekilde producer (`pager.sh`) verwijdert reeds geaccepteerde (`HTTP 202`) notificaties niet
uit de queue.

Dit staat los van bug-1: dat is dubbel leveren van hetzelfde `id`; dit is een **andere (oudere)
page** die te laat en zonder leeftijdsignaal aankomt.

## Bewijs
- `done`-melding van de afgebroken poudriere-run arriveerde tijdens een nieuwe `make release`.
- Payload bevat wel `created_at`, maar de plugin rendert alle velden even prominent → een
  naïeve client behandelt een oude page als nieuw. Dat gebeurde hier.
- Killing van de producer annuleert geaccepteerde notificaties niet.

## Voorgestelde fix (kies één of meer)
- **Client**: negeer pages ouder dan X s (gebruik `created_at`).
- **Server**: optionele `?max_age_seconds=` / `since`-filter op `/wait`.
- **Server**: korte timeout zodat pages van een gecrashte producer snel verlopen.
- **Documentatie**: leg de backlog-semantiek expliciet vast (RFC-0001 zegt nu alleen
  "blocks until a message is available").

## Aanverwant (nog te overwegen)
Server-side timeout-melding wanneer een `JOB_ID` wel een `started`-status stuurde maar nooit
een eindstatus kreeg. Dat vangt een `kill -9`/OOM van de producer, wat geen enkele shell-trap
kan melden. (Nog niet geïmplementeerd; vereist server-side job-tracking.)

## Afronding (uitgerold in v0.3.4)

Alle voorgestelde punten zijn geadresseerd:

- **Server — leeftijd in elke levering**: `page_data()` voegt `age_seconds` toe naast `created_at`.
- **Server — optionele filter**: `GET /wait?max_age_seconds=<n>` levert alleen een page binnen
  het venster; een page ouder dan `n` wordt overgeslagen **en verbruikt** (zodat de queue niet
  blijft hangen). Ongeldige waarde → HTTP 400. Zonder parameter verandert er niets.
- **Client — zichtbaar leeftijdssignaal**: `plugins/pager.js` markeert pages ouder dan 15 min
  met een `STALE:`-regel.
- **Client — niet-destructief by default**: de plugin stuurt standaard géén filter, zodat een
  legitieme backlog-melding (consumer offline) niet stil verdwijnt; `PAGER_MAX_AGE_MS` maakt
  server-side filteren opt-in.
- **Documentatie**: RFC-0001 beschrijft de backlog-semantiek en het `max_age_seconds`-filter.

Verificatie:

- **Python-tests** (v0.3.4, 21 totaal): `test_wait_max_age_skips_stale_page`,
  `test_wait_max_age_expired_page_is_skipped`, `test_wait_max_age_rejects_bad_value`.
- **Docker E2E** op de gepubliceerde image: levering bevat `age_seconds`; `max_age_seconds=60`
  werkt; ongeldige waarde → 400.
- **Productie**: `https://pager.ofloo.io` op `0.3.4 build 24` levert `age_seconds` en geeft 400
  op een ongeldige `max_age_seconds`.
