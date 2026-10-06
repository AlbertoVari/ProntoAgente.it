# Smoke test manuale PostgreSQL del catalogo prompt

## Prerequisiti e consenso

- Python 3.12+, uv e dipendenze bloccate: `uv sync --extra dev --extra postgres --locked`.
- Server PostgreSQL reale raggiungibile e database **nuovo, vuoto, usa e getta**,
  dedicato esclusivamente a questa esecuzione. Nessuna applicazione o worker connesso.
  Non usare production, staging condiviso o un database con dati da conservare.
- Ruolo proprietario del database con permessi per tabelle, indici, funzioni PL/pgSQL
  e trigger nello schema public. Non occorre un superuser.
- Fornire `POSTGRES_SMOKE_DATABASE_URL` tramite secret manager oppure input nascosto.
  Il driver obbligatorio è `postgresql+psycopg`; SQLite viene rifiutato.
  Non passare URL sulla command line, non usare shell tracing (`set -x`).
- Il flag `--confirm-disposable-database` è la conferma esplicita che il database
  selezionato è sacrificabile e che sono autorizzate migrazioni e inserimenti.
  Senza flag non viene aperta alcuna connessione. CI e APP_ENV=production sono rifiutati.

## Esecuzione

Dalla radice del repository, in bash:

```bash
uv sync --extra dev --extra postgres --locked
read -r -s -p 'URL PostgreSQL del database usa e getta: ' POSTGRES_SMOKE_DATABASE_URL
echo
export POSTGRES_SMOKE_DATABASE_URL
export APP_ENV=development
uv run python scripts/smoke_postgres.py --confirm-disposable-database
smoke_status=$?
unset POSTGRES_SMOKE_DATABASE_URL
echo "Exit status: $smoke_status"
```

Lo script è separato da pytest e dalla CI: nessun workflow viene aggiunto.
La variabile DATABASE_URL dell'applicazione non viene usata come fallback.
La preflight verifica PostgreSQL, schema public e assenza di relazioni utente;
il search_path è fissato a public. Il database resta dedicato per l'intera esecuzione.

## Criteri di esito

| Fase | Condizione PASS |
| --- | --- |
| preflight | Connessione reale, driver PostgreSQL e database vuoto |
| upgrade | `alembic upgrade head` termina con exit code 0 sul database selezionato |
| check | `alembic check` termina con exit code 0, senza drift rilevato |
| guards | SQL diretto rifiutato con SQLSTATE P0001: UPDATE di contenuto, stato, hash e tool della versione pubblicata; DELETE della versione; UPDATE di prompt_id, prompt_hash, prompt_snapshot e tool_name della preparazione. Le righe risultano identiche dopo rollback; una modifica dello stato della preparazione è consentita |
| race | Due sessioni indipendenti raggiungono una barriera prima del conditional UPDATE della stessa versione draft con lock_version=1; esattamente una pubblicazione e un ConflictError. Stato finale published, lock_version=2 e hash presente |

La race riproduce `tests/test_prompt_catalog.py::test_publish_race`, usando il servizio
reale con transazioni PostgreSQL concorrenti. I record sono sintetici; non vengono
chiamati provider, mailbox o ERP. Le guardie sono verificate tramite SQL diretto,
senza protezioni ORM e senza `Base.metadata.create_all`: i trigger devono provenire
dalle migrazioni.

Successo: tutte le cinque fasi PASS e `PASS postgres_smoke`, exit code 0.
Errore: `FAIL <fase>`, exit code 1; consenso/configurazione mancanti, CI o production:
exit code 2. Timeout delle query/lock, barriera o subprocess sono FAIL, mai PASS
o skip. Il limite per fase è 120 secondi, per Alembic 90, query 15 e lock 10.
L'output è una lista chiusa di etichette: niente URL, password, SQL, record,
traceback o output grezzo Alembic. Il dettaglio dell'errore viene deliberatamente
scartato; per indagare usare strumenti locali e log del server con accesso controllato.

## Evidenza e pulizia

Registrare commit testato, versione server PostgreSQL, data UTC, exit code e sole
righe PASS/FAIL. Un PASS è evidenza per questo smoke e questo server, non una
certificazione di production né una verifica live di OpenAI/M365.

Migrazioni e fixture committano: anche dopo FAIL il database può contenere schema
e dati parziali. Lo script non esegue downgrade, DROP o cleanup automatico perché
le versioni pubblicate sono intenzionalmente indelebili. Terminata l'esecuzione,
eliminare il database usa e getta con gli strumenti amministrativi dopo avere
verificato il target. Ogni retry richiede un nuovo database vuoto.
