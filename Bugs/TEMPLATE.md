# bug-N.md — <korte titel>

Kopieer dit bestand naar `bug-<volgende nummer>.md` en vul elk veld in.
Een rapport zonder actief gecontroleerde versie is niet compleet.

- **Pager-versie bij melden** (`<url>/version`): `{"version": "0.x.y", "build": "N"}`
  (lokaal `<host:poort>/version`: idem of afwijkend)
- **Pager-versie bij afronden** (`<url>/version`): `{"version": "0.x.y", "build": "N"}`
- **Component**: <server | client | plugin | pager.sh | build> — bestand/functie
- **Ernst**: <hoog | midden | laag> / <reproduceerbaar | flaky>
- **Status**: **OPEN** — <samenvatting>

## Versiecheck (verplicht, altijd actief uitvoeren)

Draai dit tegen de server waar de bug optreedt en plak de letterlijke output hierboven:

```bash
curl -s https://pager.ofloo.io/version
```

- Noem altijd **versie én buildnummer**. Twee builds kunnen dezelfde semantische versie
  hebben; het buildnummer onderscheidt de exacte push.
- Draait de client/plugin lokaal, noteer ook de clientkant: het image of de digest waar de
  container op draait (`docker inspect <container> --format '{{.Image}}'`).
- Controleer bij het afronden opnieuw: is de bug op de **huidige** productieversie nog
  aanwezig? Zo niet, meld dat expliciet en noem de versie/build waarin het gefixt is.

## Symptoom

<wat je zag, met de letterlijke melding/output>

## Oorzaak

<de bron in de code, met bestand:regel>

## Bewijs

<logregels, id's, curl-output, tijden. Voeg ruwe output toe, geen samenvatting.>

## Voorgestelde fix

<concreet, met code of endpoint>

## Test

<stappen waarmee iemand anders het kan reproduceren of verifiëren>
