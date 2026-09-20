# bug-1.md — dubbele levering met hetzelfde `id`

- **Pager-versie** (`https://pager.ofloo.io/version`): `{"version": "0.3.1", "build": "18"}`
  (lokaal `http://10.13.17.60:6721/version`: idem)
- **Component**: `pager_mcp/server.py` `notify()`/`wait()` + `pager_mcp/storage.py`
- **Ernst**: hoog / reproduceerbaar
- **Status**: **OPEN** — oorzaak bewezen, fix voorgesteld

## Symptoom
Dezelfde melding wordt **twee keer met een identiek `id`** bezorgd bij de client
(`plugins/pager.js`, long-poll op `GET /mailboxes/{token}/wait`). Twee meldingen met
hetzelfde `id` horen niet te bestaan.

## Oorzaak
`notify()` enqueuet de page **en** levert hem aan een wachtende `/wait`, maar haalt hem
niet uit de store. De volgende `wait()` doet `store.pop()` en vindt dezelfde page nog
steeds in de queue → tweede levering.

Timeline:
```
t0  client:  GET /wait        -> store.pop() leeg -> waiter F geregistreerd
t1  producer: POST /notify    -> enqueue(page)
                                 waiter F.set_result(page)   <-- levering #1
t2  client:  GET /wait        -> store.pop() geeft page      <-- levering #2 (zelfde id)
```
Omdat de plugin direct opnieuw `/wait` doet is de tweede levering vrijwel gegarandeerd.
Met N wachtende waiters op één token: N+1 leveringen.

## Bewijs
| Datum | `id` | JOB_ID | Leveringen |
|---|---|---|---|
| 2026-09-20 | `e17492f3-37b8-4de5-90ad-0cbb722da72a` | samba-retry | 2×, identieke payload (`exit_code: 0`) |
| 2026-09-20 | `a9a68ff2-d31a-4ab3-80ba-b2dcbbf33979` | pager-update-test | 2× |
| 2026-09-19 | `ee45e30a-b54d-4fd3-a790-07a71f0b0ac0` | release-build | 2× (~1 u ertussen) |
| 2026-09-19 | `640b726b-f251-42e1-9a77-007edf70fc8f` | live-sample-test | 2× |

**Ghost-check** (uitgesloten dat het een tweede producer was): op 2026-09-20 13:02
draaide exact één `pager.sh` (PID 66556) + één `run_release.sh` (67001) + één
`make release` (67009). Geen tweede proces. Server-queue was tijdens eerdere duplicaten
leeg (`GET /mailboxes/.../messages -> {"messages": []}`), consistent met defect 1.

## Voorgestelde fix
Voeg delete-by-id toe aan `storage.py` en verwijder de page in `notify()` zodra hij aan
een live waiter is afgeleverd (niet `pop()`, dat breekt FIFO voor andere pages):

```python
# storage.py
def remove(self, page_id: str) -> None:
    with self.lock:
        if self.db:
            self.db.execute("DELETE FROM pages WHERE id=?", (page_id,))
            self.db.commit()
        else:
            for token, (last_seen, pages) in self.mailboxes.items():
                self.mailboxes[token] = (last_seen, [p for p in pages if p.id != page_id])
```
```python
# server.py notify()
delivered = False
for waiter in waiters.pop(token, []):
    if not waiter.done():
        waiter.set_result(page_data(page))
        delivered = True
if delivered:
    store.remove(page.id)
```

## Test
1. `POST /mailboxes` → token; start een geblokkeerde `GET /wait`.
2. `POST /notify`; await de wait → levering #1, onthoud `id`.
3. Tweede `GET /wait` met korte `WAIT_TIMEOUT_SECONDS` → mag **niet** hetzelfde `id`
   teruggeven (verwacht HTTP 408/leeg).
4. `GET /mailboxes/{token}/messages` → de geleverde page is weg.
