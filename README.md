# Travian Developer Platform

A comprehensive developer toolkit for **Travian Legends** — combining a full-featured CLI automation tool with reverse-engineered game documentation.

---

## What's Inside

### CLI Tool (`src/`)

A Python CLI for automating Travian Legends gameplay. Async-first, multi-village, with an auto-builder that chains upgrades from a YAML plan.

**Highlights:**
- Authentication with JWT caching and interactive setup
- Multi-village support with village switching
- YAML-based auto-builder with priorities, chaining, gold guard, and video speedup
- Farm lists with full CRUD and smart raid intelligence
- Auto-scout with map scanning, population filtering, and exclude lists
- Military operations: scouts, raids, attacks
- Video reward automation for production boosts and build speedups
- Report parsing with smart type detection

**Quick start:**
```bash
git clone https://github.com/tomer60300/travian-auto-player.git
cd travian-auto-player
pip install -e .
travian-setup
travian login
```

For the full CLI reference with all commands, flags, and examples, see [CLI-README.md](CLI-README.md).

---

### Game Documentation (`docs/`)

Comprehensive technical documentation reverse-engineered from Travian Legends — covering architecture, APIs, game mechanics, and protocol analysis.

| File | Topic |
|------|-------|
| [01-overview.md](docs/01-overview.md) | Architecture overview (PHP backend, React/jQuery frontend) |
| [02-authentication.md](docs/02-authentication.md) | JWT, cookies, session management |
| [03-rest-api.md](docs/03-rest-api.md) | REST API endpoints |
| [04-graphql-api.md](docs/04-graphql-api.md) | GraphQL queries and schema |
| [05-map-system.md](docs/05-map-system.md) | Map rendering, tile loading, coordinates |
| [06-javascript-arch.md](docs/06-javascript-arch.md) | JS framework, React components |
| [07-html-structure.md](docs/07-html-structure.md) | Page layouts, DOM IDs, forms |
| [08-assets-cdn.md](docs/08-assets-cdn.md) | CDN structure, images, CSS |
| [09-game-constants.md](docs/09-game-constants.md) | Tribes, buildings, troops, resources |
| [10-page-routes.md](docs/10-page-routes.md) | All game pages and functions |
| [11-video-reward-protocol.md](docs/11-video-reward-protocol.md) | Video ad reward system |
| [12-reports-system.md](docs/12-reports-system.md) | Scout/battle reports |
| [13-troop-sending.md](docs/13-troop-sending.md) | Military operations |
| [14-farm-list-api.md](docs/14-farm-list-api.md) | Farm list system |
| [15-gold-club-features.md](docs/15-gold-club-features.md) | Premium/gold features |
| [16-buildings-resources.md](docs/16-buildings-resources.md) | Building mechanics, resource production |
| [17-auction-house.md](docs/17-auction-house.md) | Market/trading system |
| [18-multi-village.md](docs/18-multi-village.md) | Multi-village management |
| [19-authentication-full.md](docs/19-authentication-full.md) | Detailed authentication flow |
| [20-resource-production.md](docs/20-resource-production.md) | Resource production mechanics |

Additional docs: [GITLAB.md](docs/GITLAB.md)

---

## Repository Structure

```
travian-developer-platform/     (default branch — unified view)
├── src/travian_api/            CLI source code
├── tests/                      CLI tests
├── docs/                       Travian game documentation
├── plans/                      YAML build plans
├── CLI-README.md               Full CLI reference
├── pyproject.toml
└── README.md                   This file

cli                             Active development branch for CLI
docs                            Game documentation source branch
```

## Branch Guide

| Branch | Purpose |
|--------|---------|
| `travian-developer-platform` | Default. Unified view of the full project |
| `cli` | Active CLI development — submit PRs here |
| `docs` | Travian game documentation — standalone |

## Contributing

- **CLI changes**: Branch from `cli`, open PRs targeting `cli`
- **Documentation**: Branch from `docs` for game docs, or edit `CLI-README.md` on `cli` for CLI docs
- The `travian-developer-platform` branch is periodically updated from `cli` and `docs`

## Disclaimer

Educational purposes only. Respect your server's terms of service. Use responsibly.
