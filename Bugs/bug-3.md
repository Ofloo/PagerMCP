# bug-3.md — pagerbericht verdwijnt uit de queue zonder aan de sessie geleverd te worden

- **Pager-versie bij melden** (`https://pager.ofloo.io/version`): `{"version": "0.3.5", "build": "25"}`
- **Pager-versie bij afronden** (`https://pager.ofloo.io/version`): `{"version": "0.3.6", "build": "27", "hostname": "8265f0018e3d"}`
- **Component**: `pager_mcp/server.py` (`/wait`, `/notify`) + `pager_mcp/storage.py` + `plugins/pager.js` + `pager_mcp/client.py`
- **Ernst**: hoog / reproduceerbaar
- **Status**: **RESOLVED in `v0.3.6`** (commit `91afa20`) — uitgerold en live geverifieerd op `0.3.6 build 27`

## Versiecheck (verplicht, altijd actief uitvoeren)

```bash
curl -s https://pager.ofloo.io/version
```

- Bij melden (Privaxy-agent, host `a24bf2a0a264`): `{"version": "0.3.5", "build": "25"}`
- Twee builds kunnen dezelfde semantische versie hebben; het buildnummer onderscheidt de
  exacte push. Noteer altijd **versie én buildnummer**.

## Symptoom

Een build-melding (`pager.sh --run ./build.sh`, job-id `build-latest`, project Privaxy,
2026-10-04 ~14:43) kwam **niet** aan in de OpenCode-sessie van de melder. Later bleek het
bericht **niet meer in de wachtrij** te staan (`GET /mailboxes/<uuid>/messages` → leeg), dus
het was geconsumeerd maar nooit aan de sessie getoond. De eigen mailbox:
`cec00bba-3a08-49da-b3c8-8be28ba66d1b`.

`~/.local/share/opencode/log/opencode.log` toonde rond 15:03–15:05 herhaald:
`level=WARN message="Error: PagerMCP returned HTTP 504"` (plugin long-poll), elke ~50–120 s.

## Oorzaak

Twee elkaar versterkende problemen:

1. **Verlies bij overdracht (server).** `notify()` popte de geblokkeerde waiters en deed
   `store.remove(page.id)` zodra een waiter was gewekt. Als die waiter intussen weg was
   (proxy-504, netwerk, gekilde client), werd het bericht uit de queue gehaald terwijl
   niemand het kon lezen → permanent verloren. `wait()` had ook geen `finally` die de
   future uit `waiters` haalde, dus een afgebroken request liet een wees-waiter achter.
2. **Proxy-504-lus (client).** De server wachtte **oneindig** (`WAIT_TIMEOUT_SECONDS` niet
   gezet → `0` → `None`), maar de reverse proxy kapt een idle verbinding na **120 s** af met
   een `504`. Elke poll eindigde dus in een 504, en juist in dat venster kon probleem 1
   toeslaan.

Bewijs: een `curl --max-time 2` op `/wait`, gevolgd door een `notify`, liet de queue leeg
achter (bericht weg) op 0.3.5. De proxy-504 trad op bij exact 120,07 s.

## Bewijs

- `/version` bij melden: `{"version": "0.3.5", "build": "25", "hostname": "a24bf2a0a264"}`.
- Privaxy-mailbox leeg na de gemiste build-melding: `{"messages": []}`.
- `opencode.log`: herhaald `Error: PagerMCP returned HTTP 504` (plugin long-poll).
- Reproductie op 0.3.5: dode waiter + `notify` → `{"messages": []}` (verloren).
- Proxy-probe: `wait returned http=504 after 120.071000s`.

## Voorgestelde fix (geïmplementeerd in v0.3.6)

- **Server — at-least-once i.p.v. at-most-once.** `/wait` en `/notify` leveren een bericht
  maar **verwijderen het niet**; het verdwijnt pas na `POST /mailboxes/{uuid}/ack`
  (`{"id": "..."}`, of zonder body = oudste). Een dode/afgebroken waiter kan een bericht dus
  niet meer inslikken: de volgende `wait` levert het opnieuw. (`server.py`)
- **Server — waiter-opruiming.** `wait()` haalt de future in een `finally` uit `waiters`,
  ook bij timeout of afbreken; `WAIT_TIMEOUT_SECONDS` begrenst de wachttijd.
- **Server — `?timeout_seconds=`.** Elke wait kan zelf een bovengrens meegeven. De stack zet
  `WAIT_TIMEOUT_SECONDS=90` (< proxy-idle 120 s) zodat clients een schone `408` krijgen
  i.p.v. een `504`.
- **Client (plugin).** Long-poll met `timeout_seconds=55`; `408` = normale idle-timeout,
  direct opnieuw pollen; pas **acken na** een geslaagde `session.prompt` — faalt de levering,
  dan blijft de page in de queue en wordt ze opnieuw geprobeerd (`page kept queued`).
  Daarnaast: sessie-routering beperkt tot de **eigen projectmap** en niet-child-sessies
  (`parentID`), en `ensureToken()` maakt `.pager_session` zelf aan als de MCP-client dat nog
  niet deed.
- **Client (docker MCP).** `wait_for_page()` wacht in blokken van `PAGER_MCP_WAIT_SLICE_SECONDS`
  (default 55) en ackt daarna.

## Test

- **Python** (v0.3.6, 26 tests): o.a. `test_wait_then_ack_removes_page`,
  `test_wait_without_ack_returns_same_page`,
  `test_disconnected_waiter_does_not_consume_page`,
  `test_wait_timeout_seconds_returns_408`, `test_plugin_acknowledges_and_scopes_sessions`,
  `test_client_waits_in_bounded_slices_and_acks`.
- **Real-server smoke** (aiohttp in Docker): idle wait → `408` in 2 s; `notify` → `wait` →
  `ack` → queue leeg; dode waiter (`curl --max-time 1`) + `notify` → bericht **blijft** in de
  queue; ongeldige `timeout_seconds` → `400`.
- **Productie**: nog te verifiëren na uitrol van v0.3.6 (versie + buildnummer).

## Afronding

Uitgerold en live geverifieerd op **`0.3.6` build `27`** (`hostname 8265f0018e3d`), commit `91afa20`.

- **`GET /version`**: `{"version": "0.3.6", "build": "27", "hostname": "8265f0018e3d"}`.
- **Ack-flow live**: `notify` → `wait?timeout_seconds=10` gaf de page terug; `GET /messages`
  toonde 1 bericht vóór `ack`; `POST /ack` → `{"acked": true}`; `GET /messages` daarna leeg.
- **Dode waiter live**: `curl --max-time 1` op `/wait`, daarna `notify` → het bericht bleef in de
  queue staan (`{"messages": [ ... "live-dead-waiter" ]}`) i.p.v. te verdwijnen. Precies het
  omgekeerde van de oude 0.3.5-reproductie.
- **Privaxy-melder**: geïnformeerd op zijn mailbox `cec00bba-…`; hij hertest na de uitrol.

Aanvullend (niet server-side te verifiëren, wel in de plugin): at-least-once kan bij een
verloren ack een duplicaat geven; de plugin dedupliceert op `id` (`deliveredIds`) en ackt het
duplicaat opnieuw zonder de sessie nogmaals te prompten (commit `91cf112`).

Let op: de README-renderingfix (`053e1b6`) zit in `v0.3.7`; productie draait op dit moment
`0.3.6` en toont de README dus nog met het oude gebrek tot `v0.3.7` is uitgerold.

