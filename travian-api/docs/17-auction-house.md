# Auction House API

The auction house allows players to buy and sell hero items using **silver** currency. It uses both REST and GraphQL APIs.

> **Prerequisite:** Auction features require completing at least **10 adventures** (`bootstrapData.auction.minAdventuresRequired`). Without this, GraphQL auction queries return empty objects, though the REST API (`hero/auction/data`) may still return listing data.

---

## Wallet — Check Currency

### GraphQL

```graphql
{ ownPlayer { wallet { silverAmount goldAmount } } }
```

**Response:**
```json
{
    "data": {
        "ownPlayer": {
            "wallet": {
                "silverAmount": 2815,
                "goldAmount": 130
            }
        }
    }
}
```

### Silver ↔ Gold Exchange

```
POST /api/v1/silver-to-gold
```

**Request:**
```json
{
    "action": "silverExchange",
    "coins": 200
}
```

**Response:** `{ "silver": newSilverAmount, "gold": newGoldAmount }`

**Exchange Rates** (from `Travian.Variables`):
- Gold → Silver: `rateGoldToSilver` (typically 100 silver per 1 gold)
- Silver → Gold: `rateSilverToGold` (typically 200 silver per 1 gold)

---

## Browse Available Auctions

### GraphQL — Buy Listings (Paginated)

```graphql
query(
    $after: String
    $first: Int!
    $sortBy: AuctionsSortBy
    $sortOrder: SortOrder
    $filter: AuctionFilter
) {
    bootstrapData { timestamp }
    ownPlayer {
        auctions {
            buy(
                after: $after
                first: $first
                sortBy: $sortBy
                sortOrder: $sortOrder
                filter: $filter
            ) {
                pageInfo { endCursor hasNextPage }
                totalCount
                edges {
                    node {
                        amount
                        bidsAmount
                        finishedAt
                        highestBidder {
                            id
                            name
                            relation
                            alliance { name }
                        }
                        identifier
                        item {
                            typeId
                            name
                            slot
                            isConsumable
                            quality
                            rarity
                            attributes {
                                description
                                descriptionDetails
                            }
                        }
                        maxBid
                        price
                    }
                }
            }
        }
    }
}
```

**Variables:**
```json
{
    "first": 10,
    "after": null,
    "sortBy": null,
    "sortOrder": null,
    "filter": {}
}
```

**Enums:**
- `AuctionsSortBy`: values TBD (not `PRICE` — introspection disabled)
- `SortOrder`: `ASC`, `DESC`
- `AuctionFilter`: `{ itemSlots: ["helmet", "body", ...] }` or `{}`

**Verified Response (live):**
```json
{
    "data": {
        "ownPlayer": {
            "auctions": {
                "buy": {
                    "pageInfo": {
                        "endCursor": "eyJpZCI6Mzc3NDAuLi4=",
                        "hasNextPage": true
                    },
                    "totalCount": 901,
                    "edges": [{
                        "node": {
                            "amount": 10,
                            "bidsAmount": 8,
                            "finishedAt": 1774368446,
                            "highestBidder": {
                                "id": 7924,
                                "name": "OneTrueMorty",
                                "relation": null,
                                "alliance": {"name": "Helms Deep"}
                            },
                            "identifier": "5DU45HwEqI2FzS7y...",
                            "item": {
                                "typeId": 114,
                                "name": "Cage",
                                "slot": "bag",
                                "isConsumable": true,
                                "quality": 0,
                                "rarity": "none",
                                "attributes": [{
                                    "description": "Tames animals.",
                                    "descriptionDetails": "Equip and raid an oasis..."
                                }]
                            },
                            "maxBid": null,
                            "price": 2001
                        }
                    }]
                }
            }
        }
    }
}
```

**Key fields per auction:**

| Field | Type | Description |
|-------|------|-------------|
| `identifier` | String | Unique auction ID (used for bidding) |
| `price` | Int | Current highest bid (in silver) |
| `maxBid` | Int/null | Your max auto-bid (null if you haven't bid) |
| `bidsAmount` | Int | Total number of bids |
| `amount` | Int | Quantity of items in this auction |
| `finishedAt` | Int | Unix timestamp when auction ends |
| `highestBidder` | Object | Current winning bidder info |
| `item` | Object | Item details (typeId, name, slot, rarity, etc.) |

### REST — Buy Listings (Legacy)

```
POST /api/v1/hero/auction/data
```

**Request:**
```json
{
    "dataType": "buy",
    "buyPage": 1,
    "buyFilter": ""
}
```

**Response** includes `common` (wallet, filters), `buy` (auction listings), `sell`, `bids` sections. Each auction entry has:

```json
{
    "id": 37854,
    "uid": 21513,
    "item_type_id": 114,
    "amount": 5,
    "rarity": "none",
    "status": "running",
    "time_start": 1774284400,
    "time_end": 1774368247,
    "price": 2010,
    "bids": 5,
    "uid_bidder": 14979,
    "identifier": "midEKVRKUJq...",
    "maxBid": "",
    "title": "Cage||Tames animals...",
    "category": "cage",
    "tier": 0,
    "name_bidder": "NightWolf",
    "allianceId": 288,
    "allianceTag": "H2R3",
    "currentBidder": false,
    "obfuscatedId": "FUwhLxFwLDMCNwseIXFg",
    "minBid": 2011
}
```

### REST — Other Data Types

```json
{"dataType": "buy", "buyPage": 1, "buyFilter": ""}
{"dataType": "sell", "finishedAuctionsPage": 1}
{"dataType": "bids", "bidsPage": 1, "bidsFilter": ""}
```

---

## Browse Available Item Types

### GraphQL — Item Catalog

```graphql
query($filter: AuctionItemsFilter) {
    ownPlayer {
        auctions {
            items(filter: $filter) {
                edges {
                    node {
                        typeId
                        quality
                        rarity
                        slot
                        auctionsCount
                    }
                }
            }
        }
    }
}
```

**Variables:**
```json
{"filter": {}}                           // All items
{"filter": {"itemSlots": ["helmet"]}}     // Filter by slot
```

**Verified Response (live, partial):**
```json
{
    "edges": [
        {"node": {"typeId": 114, "quality": 0, "rarity": "none", "slot": "bag", "auctionsCount": 213}},
        {"node": {"typeId": 106, "quality": 0, "rarity": "none", "slot": "bag", "auctionsCount": 245}},
        {"node": {"typeId": 79, "quality": 1, "rarity": "common", "slot": "leftHand", "auctionsCount": 35}},
        {"node": {"typeId": 1, "quality": 1, "rarity": "common", "slot": "helmet", "auctionsCount": 5}}
    ]
}
```

**Item Slots:** `helmet`, `body`, `shoes`, `rightHand`, `leftHand`, `horse`, `bag`, `inventory`

**Item Rarity:** `none`, `common`, `uncommon`, `rare`, `epic`, `legendary`

**Item Quality:** 0 (none), 1 (quality I), 2 (quality II), 3 (quality III)

### GraphQL — Detailed Item Listings

```graphql
query(
    $after: String
    $first: Int!
    $sortBy: AuctionItemsSortBy
    $sortOrder: SortOrder
    $filter: AuctionItemsFilter
) {
    bootstrapData { timestamp }
    ownPlayer {
        auctions {
            items(
                after: $after
                first: $first
                sortBy: $sortBy
                sortOrder: $sortOrder
                filter: $filter
            ) {
                pageInfo { endCursor hasNextPage }
                totalCount
                edges {
                    cursor
                    node {
                        auctionsCount
                        itemsCount
                        nextFinishAt
                        nextPrice
                        typeId
                        slot
                        isConsumable
                        quality
                        rarity
                        attributes { description descriptionDetails }
                    }
                }
            }
        }
    }
}
```

---

## Price History

### GraphQL — Auction Item Stats

```graphql
query($itemTypeId: Int!, $rarity: Rarity!) {
    ownPlayer {
        auctions {
            auctionItemStats(itemTypeId: $itemTypeId, rarity: $rarity) {
                averagePrice
                highestPrice
                lowestPrice
                priceHistory
                salesHistory
            }
            auctionItem(typeId: $itemTypeId, rarity: $rarity) {
                typeId
                name
                isConsumable
                slot
                quality
                rarity
                possibleAmountsToSell
                attributes { description descriptionDetails }
            }
        }
    }
}
```

**Variables:**
```json
{"itemTypeId": 114, "rarity": "none"}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `averagePrice` | Float | Average sale price (last 24h) |
| `highestPrice` | Int | Highest sale price (last 24h) |
| `lowestPrice` | Int | Lowest sale price (last 24h) |
| `priceHistory` | [Float] | 24-element array — average price per hour (index 0 = 23h ago, index 23 = now) |
| `salesHistory` | [Int] | 24-element array — number of sales per hour |

> **Note:** Returns empty `{}` if the player hasn't completed the minimum required adventures (10). The chart in the UI displays `priceHistory` as a line and `salesHistory` as bars over a 24-hour window.

### Pin a Specific Auction

The price history query can also include a pinned auction:

```graphql
query($itemTypeId: Int!, $rarity: Rarity!, $isAuctionPinned: Boolean!, $identifier: String!) {
    ownPlayer {
        auctions {
            auctionItemStats(itemTypeId: $itemTypeId, rarity: $rarity) {
                averagePrice highestPrice lowestPrice priceHistory salesHistory
            }
            auction(identifier: $identifier) @include(if: $isAuctionPinned) {
                amount bidsAmount finishedAt
                highestBidder { id name relation alliance { name } }
                identifier maxBid price
                item { typeId name slot isConsumable quality rarity attributes { description descriptionDetails } }
            }
        }
    }
}
```

---

## Place a Bid

### REST API

```
POST /api/v1/hero/auction/bid
```

**Request (simple — from item type page):**
```json
{
    "action": "auction",
    "maxBid": 500,
    "identifier": "5DU45HwEqI2FzS7y98U18cIV0nI13T8deubeLv324SeEumrw1YKIuMeSeCAAACjr",
    "showResponseData": false
}
```

**Request (with full page refresh — from buy tab):**
```json
{
    "action": "auction",
    "maxBid": 500,
    "identifier": "5DU45HwEqI2Fz...",
    "buyFilter": "",
    "buyPage": 1,
    "bidsFilter": "",
    "bidsPage": 1,
    "showResponseData": true
}
```

**Parameters:**

| Field | Type | Description |
|-------|------|-------------|
| `action` | String | Always `"auction"` |
| `maxBid` | Int | Maximum silver amount to bid (auto-bid system) |
| `identifier` | String | Auction identifier (64-char string) |
| `showResponseData` | Boolean | If true, response includes updated `common`, `buy`, `bids` data |
| `buyFilter` | String | Current buy tab filter (optional) |
| `buyPage` | Int | Current buy tab page (optional) |
| `bidsFilter` | String | Current bids tab filter (optional) |
| `bidsPage` | Int | Current bids tab page (optional) |

**Success Response:** Depends on `showResponseData`:
- `false`: Empty success (just confirms bid placed)
- `true`: Returns updated `{ common: {...}, buy: {...}, bids: {...} }` with refreshed data

**Error Responses:**

| Error | Description |
|-------|-------------|
| `hero.youWouldOutbid` | You'd outbid yourself (already highest bidder) |

**Auto-bid system:** `maxBid` is the maximum you're willing to pay. The server automatically bids the minimum needed to win, up to your max. If someone outbids you (below your max), the server auto-raises.

### Bid Help

```
GET /api/v1/hero/auction/bid-help
```

Returns contextual help data for the auction UI. Called on first row expand.

---

## Sell an Item

### REST API

```
POST /api/v1/hero/auction/sell-item
```

**Request:**
```json
{
    "id": 12345,
    "amount": 1,
    "action": "auction"
}
```

**Parameters:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | Int | Inventory item ID |
| `amount` | Int | Quantity to sell (for stackable items) |
| `action` | String | Always `"auction"` |

For stackable items, a `batchSize` can be specified via form data.

---

## My Bids — Track Active Bids

### GraphQL

```graphql
query(
    $after: String
    $first: Int!
    $sortBy: AuctionsBidsSortBy
    $sortOrder: SortOrder
    $filter: BidsAuctionsFilter
) {
    bootstrapData { timestamp }
    ownPlayer {
        auctions {
            bids {
                auctions(
                    after: $after
                    first: $first
                    sortBy: $sortBy
                    sortOrder: $sortOrder
                    filter: $filter
                ) {
                    pageInfo { endCursor hasNextPage }
                    totalCount
                    edges {
                        node {
                            amount
                            bidsAmount
                            finishedAt
                            highestBidder { id name relation alliance { name } }
                            identifier
                            maxBid
                            price
                            item {
                                typeId name slot isConsumable quality rarity
                                attributes { description descriptionDetails }
                            }
                        }
                    }
                }
            }
        }
    }
}
```

---

## My Sell Listings

### GraphQL — Running Auctions

```graphql
query(
    $sortBy: SellAuctionsSortBy
    $sortOrder: SortOrder
    $filter: SellAuctionsFilter
) {
    bootstrapData { timestamp }
    ownPlayer {
        auctions {
            sell {
                auctions(sortBy: $sortBy, sortOrder: $sortOrder, filter: $filter) {
                    pageInfo { endCursor hasNextPage }
                    totalCount
                    edges {
                        cursor
                        node {
                            amount
                            startedAt
                            identifier
                            item { typeId slot isConsumable quality rarity attributes { description descriptionDetails } }
                        }
                    }
                }
            }
        }
    }
}
```

**Filter:** `{ statuses: ["RUNNING"] }`

### GraphQL — Finished Auctions

Same query with `filter: { statuses: ["FINISHED"] }` to see completed sales.

---

## Cancel an Auction

### REST API

```
DELETE /api/v1/hero/auction
```

**Request (cancel own running auction):**
```json
{
    "identifiers": ["5DU45HwEqI2Fz..."],
    "status": "running"
}
```

**Request (cancel outbid notification):**
```json
{
    "identifiers": ["5DU45HwEqI2Fz..."],
    "status": "runningOutbid",
    "bidsFilter": "",
    "bidsPage": 1
}
```

After cancelling a running sell auction, there's a 5-minute cooldown (`startedAt` shown in UI with 300-second countdown).

---

## Adventure Requirement Check

```graphql
{
    bootstrapData {
        auction { minAdventuresRequired }
    }
    ownPlayer {
        hero { adventuresAmount }
    }
}
```

**Verified Response:**
```json
{
    "bootstrapData": {"auction": {"minAdventuresRequired": 10}},
    "ownPlayer": {"hero": {"adventuresAmount": 0}}
}
```

If `adventuresAmount < minAdventuresRequired`, auction GraphQL queries return empty objects. REST `hero/auction/data` may still return data.

---

## JavaScript API

### React Component

```javascript
Travian.React.Auctions.render({
    activeTabKey: "buy",        // "buy" | "sell" | "bids"
    favouriteTabKey: null,
    tabBarName: "auctions",
    knowledgeBaseLink: "https://support.travian.com/.../auctions"
}, ["items", "auctions", "hero", "crafting"]);
```

### Constants

```javascript
Travian.Constants.ACTION.auction     // "auction"
Travian.Constants.ACTION.silverExchange  // "silverExchange"
Travian.Variables.rateGoldToSilver   // e.g., 100
Travian.Variables.rateSilverToGold   // e.g., 200
```

---

## Programmatic Usage

### List All Buy Auctions (JavaScript)

```javascript
Travian.graphQL({
    query: 'query($first:Int!$filter:AuctionFilter){ownPlayer{auctions{buy(first:$first filter:$filter){totalCount edges{node{identifier price amount finishedAt item{typeId name rarity}highestBidder{name}}}}}}}',
    variables: { first: 50, filter: {} }
}, function(data) {
    var auctions = data.data.ownPlayer.auctions.buy.edges;
    auctions.forEach(function(e) {
        var a = e.node;
        console.log(a.item.name + ' x' + a.amount + ' @ ' + a.price + ' silver, ends ' + new Date(a.finishedAt * 1000));
    });
});
```

### Place a Bid (JavaScript)

```javascript
Travian.api("hero/auction/bid", {
    data: {
        action: "auction",
        maxBid: 500,
        identifier: "5DU45HwEqI2Fz...",
        showResponseData: false
    },
    success: function() { console.log("Bid placed!"); },
    error: function(err) { console.log("Error:", err.message); }
});
```

### Check Wallet (JavaScript)

```javascript
Travian.graphQL({
    query: '{ ownPlayer { wallet { silverAmount goldAmount } } }'
}, function(data) {
    var w = data.data.ownPlayer.wallet;
    console.log("Silver:", w.silverAmount, "Gold:", w.goldAmount);
});
```
