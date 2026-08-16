# GraphQL API

**Endpoint:** `POST /api/v1/graphql`
**Introspection:** DISABLED (queries with `__schema` or `__type` are blocked)

## Usage

```javascript
// Game's internal wrapper:
Travian.graphQL({query: "...", variables: {...}}, successFn, errorFn)

// Which calls:
Travian.api("graphql", {data: {query: "...", variables: {...}}})
```

```python
# External usage:
requests.post(BASE + "/api/v1/graphql",
    json={"query": "{ownPlayer{name}}", "variables": {}},
    cookies={"JWT": token},
    headers={"Content-Type": "application/json", "X-Version": "389"})
```

---

## Known Queries

### Player Data

#### Own Player (Full)
```graphql
{
  ownPlayer {
    name
    tribeId
    isSitter
    goldFeatures {
      travianPlus { isActive }
      goldClub
    }
    village {
      id
      tribeId
      name
      sortIndex
      population
      loyalty
      x
      y
    }
    culturalPointsOverview {
      usedSlots
      maxControllableVillages
      cpProducedForNextSlot
      cpNeededForNextSlot
    }
    villageList {
      ... on VillageListGroup {
        id name color
        villages { id name distance incomingAttacksAmount incomingAttacksSymbols { gray green red yellow } x y }
      }
      ... on VillageListVillage {
        id name distance incomingAttacksAmount incomingAttacksSymbols { gray green red yellow } x y
      }
    }
    profileBan { isActive tooltip }
  }
}
```

#### Own Player (Minimal)
```graphql
{ ownPlayer { name } }
{ ownPlayer { name tribeId village { id name x y population } } }
```

#### Wallet
```graphql
{ ownPlayer { wallet { silverAmount goldAmount } } }
```

#### Ban Info
```graphql
{ ownPlayer { banInfo { type } } }
```

#### Village Details
```graphql
{ ownPlayer { village { id sortIndex name tribeId hasHarbour } } }
```

### Hero

#### Hero Status
```graphql
{ ownPlayer { hero { status { status } } } }
```

#### Adventures Count
```graphql
{ ownPlayer { hero { adventuresAmount } } }
```

#### Crafting
```graphql
{ ownPlayer { hero { crafting { forge { ...inventoryItemFields } } } } }
```

### Daily Quests

#### Quest State
```graphql
query($lastSeenAt: Int) {
  ownPlayer {
    dailyQuests {
      achievedPoints
      day
      lastSeenAt
      quests {
        id amountNeeded completedTimesToday isEnabled maxTimes
        pointsAchieved pointsPerTask
        nextContribution { villages resources }
      }
      rewards { points awardRedeemed }
    }
  }
}
```

#### Quest Points
```graphql
{ ownPlayer { dailyQuests { achievedPoints rewards { points awardRedeemed } } } }
```

### Items

#### Item Details
```graphql
query($typeId: Int!, $rarity: Rarity!) {
  item(typeId: $typeId, rarity: $rarity) {
    typeId name isConsumable slot quality rarity
    possibleAmountsToSell
    attributes { description descriptionDetails }
    isUsableIfDead
  }
}
```

#### Resource Items
```graphql
{
  lumber: item(typeId: 145, rarity: none) { typeId name ... }
}
```

### Farm Lists

#### Get Farm List
```graphql
query($id: Int!, $onlyExpanded: Boolean!) {
  abandonedFarmList(id: $id) { ...farmListFragment }
}
```

#### Farm List with Bootstrap
```graphql
query($id: Int!, $onlyExpanded: Boolean!) {
  bootstrapData { timestamp }
  ownPlayer { village { ...VillageInfo } }
  abandonedFarmList(id: $id) { ...farmListFragment }
}
```

#### Farm List with Player Context
```graphql
query($isAbandoned: Boolean!, $id: Int!) {
  ownPlayer { village { ...VillageInfo } }
}
```

### Bootstrap / Server Data

#### Bootstrap Data
```graphql
{
  bootstrapData {
    timestamp
    buildings { type validTribes }
    serverSupportedFeatures { keepVidOnConquer }
    auction { minAdventuresRequired }
  }
}
```

#### Full Bootstrap (used on page load)
```graphql
{
  bootstrapData {
    buildings { type validTribes }
    serverSupportedFeatures { keepVidOnConquer }
  }
  ownPlayer {
    name tribeId isSitter
    goldFeatures { travianPlus { isActive } goldClub }
    village { id tribeId name sortIndex population loyalty
      quickLinks { all { type buildingIsAvailable availableBuildingId }
        villageListSet { type buildingIsAvailable availableBuildingId }
        villageSet { type buildingIsAvailable availableBuildingId }
      }
    }
    isSitter
    culturalPointsOverview { usedSlots maxControllableVillages cpProducedForNextSlot cpNeededForNextSlot }
    profileBan { isActive tooltip }
    villageList {
      ... on VillageListGroup { id name color villages { id name distance incomingAttacksAmount incomingAttacksSymbols { gray green red yellow } x y } }
      ... on VillageListVillage { id name distance incomingAttacksAmount incomingAttacksSymbols { gray green red yellow } x y }
    }
  }
}
```

### Reports (Metadata Only)

```graphql
# Batched report metadata — up to ~250 aliases per query
{
    r0: report(objectId: "72738472") {
        time
        title
        defender {
            playerName
            village { id name x y }
        }
    }
    r1: report(objectId: "72638225") {
        time
        title
        defender {
            playerName
            village { id name x y }
        }
    }
}
```

**Fields:**
- `time` — Unix timestamp (seconds)
- `title` — Report subject (e.g., `"Chieftain\`s village raids Unoccupied oasis (−108|142)"`)
- `defender.playerName` — Defender name
- `defender.village` — `{id, name, x, y}` (null for oasis targets)
- `resources` — **⚠️ Always returns `null`** — resource data must come from HTML

**Limitations:**
- `reports` (plural) root query returns empty `[]` — cannot list reports
- `ownPlayer { reports { ... } }` returns empty `{}`
- Report listing must be done via HTML page scraping (`/report/all?page=N`)

> See `docs/12-reports-system.md` for full report system documentation.

### Village Alliance Lookup (Batched)

```graphql
{
    a0: village(id: 12345) { player { alliance { tag } } }
    a1: village(id: 67890) { player { alliance { tag } } }
}
```

### External URLs

```graphql
query($ids: [String!]!) {
  externalURLs {
    knowledgeBaseArticles(ids: $ids) { id url }
  }
}
```

---

## Known Types (Inferred)

### Enums
- `Rarity`: `none`, `common`, `uncommon`, `rare`, `epic`, `legendary`

### Root Query Fields
- `ownPlayer` → Player object
- `bootstrapData` → Server configuration
- `item(typeId, rarity)` → Item details
- `abandonedFarmList(id)` → Farm list data
- `externalURLs` → External link resolver

### Player Fields
- `name`, `tribeId`, `isSitter`
- `goldFeatures`, `village`, `hero`
- `wallet`, `banInfo`, `profileBan`
- `culturalPointsOverview`, `villageList`
- `dailyQuests`

### Village Fields
- `id`, `tribeId`, `name`, `sortIndex`
- `population`, `loyalty`, `x`, `y`
- `hasHarbour`, `quickLinks`
- `incomingAttacksAmount`, `incomingAttacksSymbols`

### Hero Fields
- `status { status }`, `adventuresAmount`
- `crafting { forge { ... } }`
- `freePoints`, `health`, `maxPointsPerAttribute`
- `attributes { power, offBonus, defBonus, productionPoints }`

### Item Fields (Fragment: itemFields)
- `typeId`, `name`, `isConsumable`, `slot`
- `quality`, `rarity`, `possibleAmountsToSell`
- `attributes { description, descriptionDetails }`

### InventoryItem Fields (Fragment: inventoryItemFields)
- Same as itemFields + `isUsableIfDead`

### HeroInventoryItem Fields (Fragment: heroInventoryItemFields)
- `id`, `typeId`, `amount`, `placeId`
- `quality`, `rarity`, `isConsumable`, `slot`, `name`
- `attributes { description, descriptionDetails }`
