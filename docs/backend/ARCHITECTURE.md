# Backend architecture

The backend is a modular monolith. Domain packages group related behavior;
they run in one FastAPI process and share one SQLAlchemy database. A package
name alone is not a boundary: dependencies and construction paths below define
the boundary.

## Runtime layers

```text
HTTP request
    -> app.api.routes.<domain>       validation, authentication, HTTP mapping
    -> app.services.<domain>         use cases and domain policy
    -> app.repositories.<domain>     SQLAlchemy queries and persistence
    -> app.models.<domain>           ORM table mappings
```

Routes should not construct ORM queries. Services should not know HTTP status
codes or construct SQLAlchemy queries. Repositories may use SQLAlchemy and
model classes; they should expose intent-named methods and leave business
decisions to services. A service or repository receives the request/worker
session from its caller; it does not create a second session unless it owns a
background unit of work.

`app.models` is the SQLAlchemy persistence layer, not the API schema layer.
Mapped entities are grouped by bounded context under `app.models/`. The root
package imports every domain package so Alembic and startup see complete
`Base.metadata`. Flat model modules remain temporary re-export shims for old
imports; new application code uses the domain paths. API DTOs belong in
`app.schemas` or beside their route, not in the ORM model packages.

## Domain packages

| Area | Routes | Services | Models |
| --- | --- | --- | --- |
| Accounts and users | `api/routes/auth` | `services/users` | `models/users` |
| Billing | `api/routes/billing` | `services/billing` | `models/billing` |
| Market data | `api/routes/markets` | `services/market_data`, `services/venues` | `models/market_data` |
| Trading | `api/routes/trading` | `services/trading` | `models/trading` |
| Analytics | `api/routes/portfolio`, `api/routes/analysis` | `services/analytics` | `models/analytics` |
| Intelligence | `api/routes/analysis` | `services/intelligence` | `models/intelligence` |
| Notifications | `api/routes/platform/notifications.py` | `services/users` channel dispatch | `models/notifications` |
| Platform | `api/routes/platform` | `services/platform` | `models/platform` |

Cross-domain calls should use a service-level operation or a small transport-
free contract. Avoid importing route modules from services, and avoid making a
provider adapter import the service that coordinates it.

## Replaceable integrations

Each external or strategy choice needs one contract and one resolver. Callers
use the resolver or receive the contract through construction; they do not
select concrete SDK classes or duplicate provider-specific branches.

| Changeable integration | Contract / resolver | Current state |
| --- | --- | --- |
| Billing | `BillingProvider`, `get_billing_provider()` | Registry-backed and selected by `BILLING_PROVIDER`. |
| Notification delivery | `NotificationChannelAdapter`, `channel_registry` | Registry-backed; built-ins and custom channels share the send path. |
| Cross-venue market data | `VenueAdapter`, `VenueRegistry` | Registry-backed; built-in adapter registration is configured by `VENUE_ADAPTERS`, and each venue remains feature-flagged. |
| LLM backend routing | `LLMGateway` | Resolves configured providers to the analysis backend. Provider implementations remain behind the LLM services. |
| Polymarket and Polygon access | `get_polymarket_service()` and endpoint settings | A singleton factory supplies the current Polymarket implementation; API/RPC endpoints come from settings. Keep CLOB, Gamma, Data API, WebSocket, and RPC access inside market-data services. |
| Order execution | `ExecutionEngineRegistry`, `get_execution_engine()` | Paper execution is registered. Live resolution fails closed until a complete live adapter is registered; the gateway is not yet used in production order paths. |

Register built-ins at an explicit startup or domain-bootstrap boundary, not as
an accidental side effect of importing a route or a business service. Test and
local implementations can then be registered without changing callers.

## Persistence rule

SQLAlchemy query construction and execution belong in repositories. Routes map
HTTP input and output and call services; they do not construct repositories,
issue ORM queries, or own persistence operations. Services coordinate use cases
and transaction boundaries, and repositories provide intent-named data access.
Keep commits and rollbacks at the same logical point when migrating a flow.

The old flat model and repository modules remain compatibility re-exports.
New application code imports canonical domain modules. Do not add generic
repositories that accept arbitrary SQL expressions from services, since that
only relocates the query syntax and leaves the dependency unchanged.

## Composition and lifecycle

`app.main` is the composition root: it creates the FastAPI app, registers
routers, configures providers and venue adapters, and starts/stops background
services. Keep feature decisions in settings and lifecycle coordination here;
move database queries and business policy into services and repositories.
Request-scoped dependencies stay in `app.api.deps`, and database engine/session
construction stays in `app.utils.database`.
