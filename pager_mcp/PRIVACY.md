# Privacy

PagerMCP logt standaard niets. De server houdt geen access-, request- of
payload-logs bij. De enige data die bewaard wordt is de inhoud van je mailbox: de UUID en de
berichten in de wachtrij, in het geheugen of in `DATA_DIR` (SQLite). Berichten
verlopen standaard na 7 dagen, en een lege, ongebruikte mailbox na 30 dagen.

## Federatie (peer mesh)

Peers zien bij een lookup alleen een **roterende hash** van een mailbox-UUID
(of een publieke sleutel-fingerprint), nooit de kale UUID en nooit
bericht-inhoud. Berichten blijven op de eigenaar-instance; de client wordt met
een 302 daarheen gestuurd. Geen registratie of centrale accounts nodig.

## Optionele debug-logging

Een operator kan voor debug-doeleinden een roterende access-log aanzetten met
`PAGER_LOG_FILE`. Die logt per request: client-IP, tijdstip, methode, pad en
status. Daarin worden UUID's **gemaskeerd** (`<uuid>`), en het bestand wordt
dagelijks geroteerd met maximaal `PAGER_LOG_RETENTION_DAYS` (standaard **3**) dagen
bewaard — oudere files worden automatisch verwijderd. De bedoeling daarvan is
alleen om kort terug te kunnen debuggen; het exposure-venster blijft zo klein
mogelijk.

Dit is een open protocol zonder ingebouwde beveiliging.

- Je privacy hangt volledig af van de geheimhouding van je mailbox-UUID. Die
  UUID is tegelijk je adres en je geheim: wie hem heeft, kan je berichten sturen
  én alles lezen wat in je mailbox staat. Er is geen authenticatie anders dan
  het bezit van de UUID, en de server versleutelt niets.
- Zodra je je UUID uitdeelt, kan iedereen die hem heeft je berichten lezen.
  Deel hem dus zorgvuldig, zoals een wachtwoord. Een gedeelde UUID kun je niet
  intrekken; maak in dat geval een nieuwe mailbox aan en werk je
  `.pager_session` bij.
- Wil je dat alleen de bedoelde tegenpartij de inhoud kan lezen, versleutel dan
  de `message` zelf met GPG voordat je hem verstuurt (zie de README, "Optional
  end-to-end encryption"). De UUID blijft zichtbaar voor de server, want die is
  nodig om te routeren.

Transport naar de server hoort over HTTPS/TLS te lopen, maar de relay — en
iedereen met toegang tot de opslag — kan de berichten in principe lezen.

## Push naar een telefoon

Registreer je een push-endpoint (subscription), dan stuurt de server bij een
nieuw bericht een **lege trigger** (`{"type":"page","mailbox_hash":"...","id":...}`, zonder
bericht-inhoud) naar dat endpoint. Sinds v0.13.0 bevat de trigger (standaard) **alleen de roterende
`mailbox_hash`** = `sha256("<uuid>:<unix-periode>")` en **geen kale UUID meer**.
Het endpoint is meestal van een derde partij (Google FCM, een ntfy-server of
een UnifiedPush-distributor), die dus alleen een per-periode-wisselende hash
ziet — niet welke mailbox het is en niet wat erin staat. De app vergelijkt die
hash met zijn eigen UUID('s), met de klok-offset uit `GET /settings`
(`server_time`) en de periode uit `identity_period_seconds`. Wie de hash
uitschakelt (`MAILBOX_HASH_ENABLED=0`) krijgt de oude vorm met de kale UUID
(`/settings` → `subscriptions.identity` is dan `"uuid"`). Voor maximale privacy:
gebruik SSE, dan is er helemaal geen derde partij.
Subscriptions hebben een **lease** (standaard 30 dagen): je moet ze verlengen,
anders schakelt push automatisch uit. Vlak vóór het verlopen (standaard 3 dagen)
stuurt de server één `warning`-trigger, zodat je op tijd kunt verlengen. Zo
bewaren we geen permanente push-hook (of data) van een mailbox die je vergeten
bent. Mailboxes, berichten en subscriptions verlopen sowieso en worden
opgeruimd.
