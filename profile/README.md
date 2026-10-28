<div align="center">

<img src="https://raw.githubusercontent.com/DepLoom/.github/main/assets/deploom-mark.svg" alt="DepLoom woven dependency graph logo" width="152" />

# DepLoom

**Evidence-aware dependency upgrades for coding agents.**

*Plan the change. Understand the constraints. Verify the result.*

</div>

DepLoom helps agents and developers make explainable decisions about JavaScript and TypeScript dependency updates: which versions can move together, what must stay pinned, where peer requirements conflict, and which security or compatibility facts are still unknown.

The goal is **a useful, inspectable upgrade blueprint**, not another opaque instruction to run `npm update`.

### The pieces

| Project | What it does |
| --- | --- |
| [**Core**](https://github.com/DepLoom/core) | Headless Python planning engine, bounded target selection, peer-compatibility constraints and explicit evidence/unknowns. |
| [**MCP**](https://github.com/DepLoom/mcp) | Agent-facing MCP server, compact Upgrade Blueprints, source and policy provenance, and an optional versioned upgrade Skill. |
| [**Original DepLoom**](https://github.com/AlexanderLevenskikh/deploom) | Historical Desktop and monorepo implementation; retained as a reference while headless components evolve. |

### What matters in a blueprint

- **User authority stays explicit.** `pinned` and `advisory` targets have different rules; selected dependencies and approved major lines remain constraints.
- **Health and freshness are not the same goal.** Meeting a policy does not imply that the latest compatible versions were chosen.
- **UNKNOWN stays UNKNOWN.** Missing exact-target security data is not a zero-vulnerability result, and a known policy violation is not just incomplete evidence.
- **Plans are not proofs.** Static planning cannot certify npm installability, a resolved dependency tree, runtime behavior or an application's security posture. Those checks must be run against final project inputs.

### Current research

DepLoom is an evolving engineering project. Early diagnostic pilots confirmed several planning and evidence contracts, but **have not established that agent + MCP is faster or produces better real migrations than a capable agent on its own**. A small React Router migration comparison found the same functional result with and without the full Skill, while the Skill produced a more structured acceptance record.

The next evaluation isolates **agent + MCP (without the Skill) vs. a bare agent** on a harder, controlled dependency-upgrade task, with equal execution checks and independent review. Skill improvements will be evaluated separately.

### Explore

Start at [DepLoom MCP](https://github.com/DepLoom/mcp) to see how a coding agent consumes a Blueprint. For the decision engine and its lightweight tests, see [DepLoom Core](https://github.com/DepLoom/core).

> DepLoom plans dependency changes. Successful installation, quality gates, policy closure and real-world runtime correctness still require independent evidence.
