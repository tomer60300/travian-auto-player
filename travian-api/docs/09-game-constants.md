# Game Constants

Sourced from `/js/Constants.js` and `/js/Variables.js`

## Server Configuration

| Setting | Value |
|---------|-------|
| Speed | 1x |
| Map Size | 401×401 (-200 to +200) |
| Season | Winter |
| World Edge Travel | Enabled |
| Gold-to-Silver Rate | 100 |
| Silver-to-Gold Rate | 200 |
| Farm List Limit | 100 |
| Max Farm Units | 9,999,999 |
| Production Boost | 25% |
| Trade Offer Ratio Limit | 1 |
| Trade Population Threshold | 200 |
| Max Village Name Length | 20 |
| Quick Links (Village List) | 4 |
| Quick Links (Village) | 5 |
| Village List Max Groups | 20 |

## Tribes

| ID | Name | Playable (this server) |
|----|------|----------------------|
| 1 | Romans | ✅ |
| 2 | Teutons | ✅ |
| 3 | Gauls | ✅ |
| 4 | Nature | ❌ (NPC) |
| 5 | Natars | ❌ (NPC) |
| 6 | Egyptians | ❌ (not enabled) |
| 7 | Huns | ❌ (not enabled) |
| 8 | Spartans | ❌ (not enabled) |
| 9 | Vikings | ❌ (not enabled) |

## Resources

| ID | Name | Stock Bar ID |
|----|------|-------------|
| 1 | Lumber | l1 |
| 2 | Clay | l2 |
| 3 | Iron | l3 |
| 4 | Crop | l4 |

## Hero Status Codes

| Code | Status | Description |
|------|--------|-------------|
| 100 | IN_VILLAGE | Hero is in a village |
| 101 | DEAD | Hero is dead |
| 102 | CAPTURED | Hero is captured by enemy |
| 103 | ON_SUPPLY | Hero is defending another village/oasis |
| 3 | ON_THE_WAY_TO_ATTACK | Moving to attack |
| 4 | ON_THE_WAY_TO_RAID | Moving to raid |
| 5 | ON_THE_WAY_TO_SUPPLY | Moving to reinforce |
| 9 | ON_THE_RETURN_PATH | Returning home |
| 40 | ON_ESCAPE | Escaping |
| 50 | ON_ADVENTURE | On an adventure |

## Hero Attributes

| Attribute | Effect |
|-----------|--------|
| power | +attack strength (base 100 + bonus) |
| offBonus | +X% offense bonus |
| defBonus | +X% defense bonus |
| productionPoints | +X resource production per hour |

## Village Types

| ID | Type |
|----|------|
| 0 | Normal village |
| 1 | Capital |
| 2 | Occupied oasis |
| 3 | Free oasis |

## Building IDs

| ID | Building |
|----|----------|
| 13 | Blacksmith |
| 14 | (Tournament Square) |
| 15 | (Main Building) |
| 16 | Rally Point |
| 17 | Marketplace |
| 19 | Barracks |
| 20 | Stable |
| 21 | Workshop |
| 22 | (Academy) |
| 23 | (Cranny) |
| 24 | Town Hall |
| 25 | Residence |
| 26 | Palace |
| 27 | (Treasury) |
| 29 | Great Barracks |
| 30 | Great Stable |
| 31 | (City Wall - Romans) |
| 32 | (Earth Wall - Teutons) |
| 33 | (Palisade - Gauls) |
| 34 | (Stonemason) |
| 35 | (Brewery - Teutons) |
| 36 | (Trapper - Gauls) |
| 37 | Hero's Mansion |
| 38 | (Great Warehouse) |
| 39 | (Great Granary) |
| 44 | Command Center |
| 46 | Hospital |
| 48 | Asclepeion |

## Troop IDs

Troops are numbered 1-50+, grouped by tribe:
- **Group 0 (Romans + Teutons + Gauls):** IDs 1-30
- **Group 1 (additional):** IDs 31-50

Special troop number types:
| Type | Key |
|------|-----|
| Catapult | t8 |
| Hero | t11 |
| Trade Ship | t901 |

## Event Types

| Code | Type |
|------|------|
| 3 | Attack |
| 4 | Raid |
| 72 | Forward/redirect |

## Loyalty

| Setting | Value |
|---------|-------|
| Medium loyalty value | 100 |

## Item Rarities

| Rarity | Shape in UI |
|--------|------------|
| common | Circle |
| uncommon | Triangle |
| rare | Diamond |
| epic | Pentagon |
| legendary | Gem/hexagon |

## Feature Flags (This Server)

```json
{
  "multi_language": true,
  "territory": false,
  "cities": false,
  "boostedStart": false,
  "travelOverTheWorldEdge": true,
  "tribesVikings": false,
  "auctionsV2": true,
  "craftingItems": false,
  "legendaryItems": false,
  "videoFeatureDailyQuest": true,
  "videoFeatureAdventures": true,
  "videoFeatureSmithy": true,
  "videoFeatureAcademy": true,
  "videoFeatureConstruction": true,
  "videoFeatureProductionBoost": true,
  "videoFeatureConstructionQueue": false
}
```

## Accusation/Report Reasons

| Key | Translation Key |
|-----|----------------|
| multiAccount | admin.c3_sperrgrund_multi_und_pushing |
| passwordSharing | admin.c3_sperrgrund_multiaccount |
| botScript | admin.c3_sperrgrund_scripts_und_bots |
| other | admin.c3_sperrgrund_sonstiges |
| inappropriateContent | admin.inappropriateContent |
| privateFarm | admin.banReasonPrivateFarm |

## Player Genders

- `male`, `female`, `diverse`, `NotSpecified`

## Shop Tabs

- `buyGold`, `advantages` (pros), `vouchers`, `specialOffers`

## Transfer Types

- `GoldToSilver`, `SilverToGold`

## Troops Speedup

- Distance threshold: 20 tiles (troops speed up for short distances)
