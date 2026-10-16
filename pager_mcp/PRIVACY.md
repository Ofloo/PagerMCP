# Privacy

PagerMCP logt niets. De server houdt geen access-, request- of payload-logs bij.
De enige data die bewaard wordt is de inhoud van je mailbox: de UUID en de
berichten in de wachtrij, in het geheugen of in `DATA_DIR` (SQLite). Berichten
verlopen standaard na 7 dagen, en een lege, ongebruikte mailbox na 30 dagen.

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
