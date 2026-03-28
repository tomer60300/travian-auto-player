# Travian Legends — Reverse Engineering Overview

**Target:** `https://ts1.x1.europe.travian.com`
**Game Version (gpack):** 389
**CDN:** `https://cdn.legends.travian.com/gpack/389/`
**Date:** 2026-03-19

## Architecture Summary

Travian Legends is a server-rendered PHP application with a React/jQuery hybrid frontend.

```
┌─────────────────────────────────────────────────┐
│                   Browser Client                 │
├─────────────┬──────────────┬────────────────────┤
│  jQuery 3.5 │  React (JSX) │  PixiJS (map)      │
│  D3.js      │  GSAP/Tween  │  Chart.js          │
├─────────────┴──────────────┴────────────────────┤
│          Travian.api() / Travian.graphQL()       │
│             (jQuery.ajax + fetch)                │
├─────────────────────────────────────────────────┤
│              HTTPS / JSON                        │
├─────────────────────────────────────────────────┤
│           PHP Backend (Server-Rendered)           │
│           /api/v1/* (REST + GraphQL)             │
└─────────────────────────────────────────────────┘
```

## Key Characteristics

- **No WebSocket connections** — all communication is HTTP request/response
- **No SPA routing** — each page (`dorf1.php`, `karte.php`, etc.) is a full page load
- **Hybrid rendering** — PHP renders initial HTML + inline JSON data; React hydrates interactive components
- **Map rendering** — PixiJS canvas for the main map, with D3 for charts/pies
- **Authentication** — JWT cookie, obtained via Travian Lobby OAuth (Google, etc.)
- **API layer** — REST endpoints at `/api/v1/*` + GraphQL at `/api/v1/graphql`
- **i18n** — Translation files loaded as JSON from `/js/{locale}/*.json`
- **CSRF protection** — `X-Version` header required on all API calls

## Directory Structure of This Documentation

```
travian-api/
├── docs/
│   ├── 01-overview.md          ← You are here
│   ├── 02-authentication.md    ← JWT, cookies, session management
│   ├── 03-rest-api.md          ← All REST endpoints
│   ├── 04-graphql-api.md       ← GraphQL queries & schema
│   ├── 05-map-system.md        ← Map rendering, tile loading, coordinates
│   ├── 06-javascript-arch.md   ← JS framework, namespaces, React components
│   ├── 07-html-structure.md    ← Page layouts, DOM IDs, forms
│   ├── 08-assets-cdn.md        ← CDN structure, images, CSS, fonts
│   ├── 09-game-constants.md    ← Tribes, buildings, troops, resources
│   └── 10-page-routes.md       ← All game pages and their functions
├── scraper.py                   ← Map scraper tool
├── README.md                    ← Quick reference
├── map_*.json / .csv            ← Scraped data
└── villages_*.csv               ← Village data
```
