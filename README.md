# ProntoAgente platform

Backend FastAPI production-oriented per definire agenti e workflow versionati, creare
proposte deterministiche, approvarle e affidarne l'esecuzione a un worker outbox. Il
Milestone 2 aggiunge autenticazione API-key, isolamento tenant, RBAC, catalogo e connettori
allowlisted senza cambiare il contratto rilasciato del Milestone 1.

## Contratti disponibili

- `/v2` è l'API tenant-scoped corrente. Richiede sempre
  `Authorization: Bearer pa2_<key_id>.<secret>`.
- `/v1/erp-drafts` resta identica alla baseline Milestone 1 in sviluppo e test. In
  `APP_ENV=production` è disabilitata, salvo `ENABLE_LEGACY_V1=true`.
- `GET /v1/health` segue la stessa regola di esposizione di `/v1`.

Il database memorizza solo prefix e HMAC-SHA256 della API key; il secret è mostrato una
sola volta dal bootstrap. `API_KEY_PEPPER` deve essere impostata in produzione. I ruoli
supportati sono `owner`, `builder`, `operator`, `approver` e `auditor`; un ID appartenente
ad altro tenant è deliberatamente indistinguibile da una risorsa inesistente (`404`).

## Avvio locale

Con `uv` e il lock incluso:

```bash
uv sync --extra dev --locked
uv run alembic upgrade head
API_KEY_PEPPER='local-secret-pepper' uv run python scripts/bootstrap_m2.py \
  --tenant-slug demo --tenant-name 'Demo tenant' \
  --owner-subject founder@demo.local --owner-name Founder
uv run uvicorn prontoagente.main:app --reload
```

Conservare il valore `api_key` stampato al primo bootstrap: una seconda esecuzione è
idempotente e mostra soltanto il prefix. Per una demo end-to-end locale:

```bash
export API_KEY_PEPPER='local-secret-pepper'
export PRONTOAGENTE_API_KEY='pa2_...'
uv run python scripts/demo_m2.py
```

Le variabili sono elencate in `.env.example`. PostgreSQL è disponibile installando
`uv sync --extra postgres` e usando un URL `postgresql+psycopg://...`; l'engine abilita
`pool_pre_ping`. Il DDL è compilato nei test per PostgreSQL, ma questa release non dichiara
una verifica contro un server PostgreSQL live.

## Catalogo e lifecycle v2

Un owner o builder crea `Agent` e `Workflow`, aggiunge una versione draft e la pubblica.
Una versione pubblicata è immutabile e una nuova versione usa il numero N+1. Ogni
`WorkflowVersion` pinna una `AgentVersion` già pubblicata e una coppia connector/action
del registry statico. Tutti i workflow richiedono approvazione: `approval_required=false`
è rifiutato dall'API e da un CHECK del database.

Le route principali sono:

- `GET /v2/me`, `GET /v2/connectors`;
- `GET|POST /v2/agents`, `GET /v2/agents/{id}` e version create/edit/publish;
- equivalenti route sotto `/v2/workflows`;
- `POST /v2/workflows/{id}/runs/dry-run`, quindi GET/approve/reject/execute del run;
- `GET /v2/execution-operations/{id}` e `GET /v2/runs/{id}/audit-events`.

Le mutation del lifecycle run richiedono `Idempotency-Key` (8–128 caratteri), con
namespace per tenant e operazione. Dry-run salva snapshot di entrambe le versioni,
proposta, hash, summary, target e payload. Approve verifica l'hash persistito ma non
esegue alcun connector; execute è ammesso esclusivamente dallo stato `approved`. Le
transizioni approve/reject/execute usano compare-and-set sullo stato, quindi una race ha
un solo vincitore. Execute crea atomicamente una `ExecutionOperation` e un evento
outbox, risponde `202` e non effettua rete nella richiesta HTTP.

Il worker si avvia con:

```bash
uv run python -m prontoagente.worker --once
# oppure, senza --once, polling continuo
uv run python -m prontoagente.worker
```

Il worker acquisisce un lease e fa commit prima dell'I/O, usa `operation_id` come token
di deduplica, quindi finalizza atomicamente run, operation, outbox, audit e replay
idempotente. La finalizzazione usa `lease_owner` come fence e scarta risultati di worker
che hanno perso il lease. `proposal`/`proposal_hash`, versioni pubblicate e audit sono
protetti da guard ORM e trigger DB. Un connector reale deve inoltre deduplicare sul
sistema remoto: timeout o crash dopo l'invio possono produrre stato `unknown` e richiedono
riconciliazione, non un retry cieco.

## Connector Microsoft 365 read-only

`m365_mail_intake_v1` offre soltanto capability `READ` e action `list_messages`. È
`configured=false` finché flag e tutte le variabili runtime M365 non sono presenti. La
configurazione è legata a un solo tenant interno tramite `M365_PLATFORM_TENANT_ID`; ogni
altro tenant fallisce chiuso senza rete. Credenziali e token non entrano mai in DB, API,
audit o log. Le variabili dedicate includono `M365_TENANT_ID`, `M365_CLIENT_ID`,
`M365_CLIENT_SECRET`, `M365_MAILBOX_ID` e `M365_FOLDER_ID`.

L'app Entra deve ricevere il solo permesso applicativo necessario alla demo,
`Mail.ReadBasic.All`, con admin consent. L'avvio richiede anche l'attestazione operativa
esatta `M365_PERMISSION_ATTESTATION=Mail.ReadBasic.All`: non è introspezione del token,
perché il token Graph viene deliberatamente trattato come opaque. Il client usa
esclusivamente scope `https://graph.microsoft.com/.default`, una GET sul percorso configurato
`/users/{mailbox}/mailFolders/{folder}/messages`, `$top` limitato a 1–50 e `$select`
senza body, bodyPreview o allegati. Non esistono chiamate attachments. HTTP 429 rispetta
`Retry-After` tramite retry outbox.

## Compatibilità Milestone 1

La proposta v1, la sua serializzazione canonica e il relativo SHA-256 non sono cambiati.
La migrazione `0002_milestone2_platform` aggiunge `tenant_id=legacy-local` alle tabelle M1
preservando righe, hash, audit e idempotenza. I trigger SQLite originali rimangono
equivalenti; su PostgreSQL sono installate guard compatibili.

Il flusso legacy resta dry-run → approve/reject → simulate, con `Idempotency-Key` e
`X-Actor-Id` obbligatori sulle mutation. Il connector è soltanto il fake locale senza
rete. Un golden test blocca regressioni di schema e hash v1.

## Qualità e migrazioni

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv run alembic check
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
```

SQLite è destinato a sviluppo e test. PostgreSQL è la scelta raccomandata per deployment
concorrenti; restano fuori scope rate limiting, secret manager, retention/export audit,
telemetria centralizzata e un job automatico di riconciliazione degli esiti `unknown`.
