# Web Scraping Event Application

This repository contains two independent applications:

- [`legacy_app/`](legacy_app/) is the existing city-events aggregator. It is
  preserved unmodified and has its own setup instructions.
- [`new_app/`](new_app/) is the replacement FastAPI application under active
  development. It currently provides database foundations, local
  authentication, RBAC, public registration, and account management.
  Extraction and scheduling remain deferred.

See [`docs/migration-notes.md`](docs/migration-notes.md) for repository history
and [`new_app/README.md`](new_app/README.md) for current setup and verification.
