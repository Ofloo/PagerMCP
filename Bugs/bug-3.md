# bug-3.md — pagerbericht verdwijnt uit de queue zonder aan de sessie geleverd te worden

- **Pager-versie bij melden** (`https://pager.ofloo.io/version`): `{"version": "0.3.5", "build": "25"}`
- **Pager-versie bij afronden** (`https://pager.ofloo.io/version`): zie afronding onderaan (v0.3.6)
- **Component**: `pager_mcp/server.py` (`/wait`, `/notify`) + `pager_mcp/storage.py` + `plugins/pager.js` + `pager_mcp/client.py`
- **Ernst**: hoog / reproduceerbaar
- **Status**: **OPEN** — gemeld door de Privaxy-agent; fix in v0.3.6 (nog niet uitgerold)

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

Nog uit te rollen. Zodra `https://pager.ofloo.io/version` op v0.3.6 (buildnummer noteeren)
staat: opnieuw een dode-waiter-scenario plus een echte `pager.sh --run`-melding testen en hier
de productieversie/build vastleggen.
