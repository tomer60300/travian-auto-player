# Travian: Legends — Mechanics Reference for Code Review
### `travian-auto-player` · Europe 2 · x1 · 3 tribes · Teuton · ~26 villages · ≈ day 161

*Server start 26 Mar 2026 15:00 UTC. This is Travian: **Legends** (T4). Travian **Kingdoms** (T5) mechanics differ materially and are discarded throughout — several plausible-looking sources conflate them.*

---

## How to use this document

It is written for someone who can read code but has never played the game, and who is about to review a tool that plays it. The organising principle is **the tool's own subsystems**, not the game manual, so each part lands on the modules a reviewer will actually open.

The document has **three layers**, and they answer different questions:

| Layer | Parts | Question it answers |
|---|---|---|
| **Rules** | 0–V | Does the code match what the game does? |
| **Techniques** | VI | Is the code's model as *wide* as the game, or narrower than what players actually do? |
| **Behaviour** | VII | Would a good player have made this decision? |

The rules layer catches wrong numbers. The techniques layer catches a subtler failure: a model that is internally correct but expresses less than the game allows, so the tool escalates or declares infeasible where a player would simply solve the problem. The behaviour layer catches the failure that no amount of mechanical correctness prevents — an action that is legal, well-computed, and still the wrong move.

Every mechanic carries two annotations:

- **What to look for** — the specific class of logic error to hunt for.
- **Direction of danger** — whether getting it wrong is *quiet* (silently wrong output, no error) or *loud* (visible failure), and whether erring high or low is worse. A constant that is wrong in the wasteful direction is a cost problem; wrong in the over-committing direction it starves armies.

Confidence tags: **VERIFIED** (official source plus independent corroboration) · **MEASURED** (confirmed against this account or this codebase's own captures) · **PARTIAL** (official documentation covers some of it) · **COMMUNITY** (no official documentation) · **DISPUTED** · **UNVERIFIED**.

Where a number could plausibly differ on a modified server, that is stated along with how to check it in-game. Inferences are labelled as inferences.

---

