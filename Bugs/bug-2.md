# bug-2.md — stale backlog-levering (oude/superseded meldingen komen later als "nieuw")

- **Pager-versie** (`https://pager.ofloo.io/version`): `{"version": "0.3.1", "build": "18"}`
  (lokaal `http://10.13.17.60:6721/version`: idem)
- **Component**: `pager_mcp/storage.py` (TTL/backlog) + `server.py` `/wait`
- **Ernst**: midden / verwarrend
- **Status**: **OPEN** — semantiek ontbreekt een age-guard

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
kan melden.
