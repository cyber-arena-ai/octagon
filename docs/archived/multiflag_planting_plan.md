# Multiflag Planting — Plan

Status: **planned, not implemented.** Captures the finalized design agreed for
enabling multiple flags per service. Scope of the first cut is **framework
plumbing only** — no existing challenge adopts multiflag; future challenges will.

## Goal

Support the real A/D "flag IDs" model: each round a service may hold **N distinct
flags at once**, each in a named **store**, each **scored independently**. Today
the model is strictly one flag per `(team, service)` (`FlagStore.planted[(team,
service)] = flag`; re-plant replaces + invalidates the prior). This lifts that to
N named stores while keeping single-flag challenges behaving exactly as today.

Non-goals (v1): no multiflag behavior in any of the 22 existing challenges; no
store-level retry; no per-store score/expiry/checker semantics (the data model
leaves room for them — see "Future").

## Data model

Flag becomes an object (not a primitive), so the API can grow without parallel
arrays. The **store name is the dict key** — the single source of truth — so
`Flag` carries no `id`:

```python
DEFAULT_STORE = "default"
_STORE_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

@dataclass(frozen=True)
class Flag:
    value: str                                    # the flag string
    metadata: Mapping[str, Any] = field(default_factory=dict)  # future: score/expiry/checker
```

- `frozen=True` prevents rebinding the field; `Mapping[str, Any]` signals the
  contents are read-only to type checkers. Runtime immutability (`MappingProxyType`
  in `__post_init__`) is a future hardening; v1 uses the `Mapping` annotation only.

## Challenge / FlagHandler contract

One unified planting entry point keyed by store name — no legacy fork:

```python
class Challenge:
    def flag_stores(self) -> list[str]:
        return [DEFAULT_STORE]                     # single store by default; override for multiflag

class FlagHandler(ABC):
    @abstractmethod
    def plant(self, target, flags: Mapping[str, Flag]) -> Mapping[str, str]: ...   # {store: handle}
    @abstractmethod
    def retrieve(self, target, handle: str) -> str | None: ...   # unchanged; handle is now per-store

class SingleFlagHandler(FlagHandler):
    """Compat base for single-store challenges — they implement plant_one only."""
    def plant(self, target, flags):
        if set(flags) != {DEFAULT_STORE}:
            raise ValueError(f"{type(self).__name__} only supports store {DEFAULT_STORE!r}")
        return {DEFAULT_STORE: self.plant_one(target, flags[DEFAULT_STORE].value)}
    @abstractmethod
    def plant_one(self, target, value: str) -> str: ...
```

A multiflag challenge overrides `flag_stores()` (named stores) and implements
`plant(target, flags)` directly, indexing by name (`flags["document"].value`) —
never by position.

### `plant()` return + atomicity (v1)

- Retry granularity stays **`(team, service)`**, so the return is the simple
  `Mapping[str, str]` (store → handle).
- **Handles are per-store locators.** `retrieve(target, handle)` deliberately
  takes no store arg — the framework stores each store's handle on its
  `FlagRecord` and passes it back, so the handler doesn't need the store name at
  retrieve time. This implies a constraint on `plant`: **each store's returned
  handle must independently locate *that store's* flag.** The string values may
  coincide, but a handler must not return one handle shared across two stores that
  means different things — `retrieve(handle)` must be unambiguous per store. (To
  be stated in the `plant` docstring + authoring SOP.)
- Contract: **all-or-nothing** — return handles for every requested store, or
  raise. Silent partial returns are not allowed. The framework enforces
  `set(handles) == set(flags)`; a mismatch is treated as a plant failure (existing
  retry / `FLAG_PLANT_FAILED` path).
- **"Atomic" here is framework-*acceptance* atomicity, not remote transactional
  atomicity.** The attempt is *accepted* only if handles come back for every
  store. But a handler may have already written stores 1–2 to the live service
  before failing on store 3, and the framework **cannot roll back those remote
  side effects**. So the handler owns them:
  - **Retry must be idempotent** — a `(team, service)` retry re-invokes `plant()`
    for *all* stores, so re-planting a store must converge to one correct state
    (overwrite, not append).
  - **Overwriting a store must not leave duplicate / stale records** on the
    service (e.g. a second row/file the checker or attacker could latch onto).
  - **Partial side effects are the challenge's responsibility** to clean up or
    make harmless; the framework only decides accept-vs-retry from the return.
- **Future form** (when store-level retry lands, to express partial failure):
  `@dataclass PlantResult(handle: str | None, error: str | None = None)` →
  `Mapping[str, PlantResult]`.

## Authoring — what a challenge provides

`SingleFlagHandler` **is a subclass of** `FlagHandler`: it pre-implements the
dict-based `plant` for the single-store case and exposes a simpler `plant_one`,
so single-flag authors never touch the mapping. The framework always calls
`plant(dict[str, Flag])`; for a `SingleFlagHandler` that inherited `plant` routes
to `plant_one`.

**Single-flag challenge** (the default; all 22 existing) — subclass
`SingleFlagHandler`, implement `plant_one` + `retrieve`; don't override
`Challenge.flag_stores()` (defaults to `["default"]`), don't touch the dict:

```python
class FooFlagHandler(SingleFlagHandler):
    def plant_one(self, target, value: str) -> str: ...        # write the one flag, return handle
    def retrieve(self, target, handle: str) -> str | None: ...
```

**Multi-flag challenge** (future) — subclass `FlagHandler`, implement
`plant(target, flags)` (index by store name, return `{store: handle}`) +
`retrieve`; declare the named stores on the `Challenge`:

```python
class Challenge:
    def flag_stores(self) -> list[str]:
        return ["document", "profile"]

class BarFlagHandler(FlagHandler):
    def plant(self, target, flags: Mapping[str, Flag]) -> Mapping[str, str]:
        return {"document": write_doc(target, flags["document"].value),
                "profile":  write_profile(target, flags["profile"].value)}
    def retrieve(self, target, handle: str) -> str | None: ...
```

| | single-flag | multi-flag |
|---|---|---|
| base class | `SingleFlagHandler` | `FlagHandler` |
| implement | `plant_one(target, value)` + `retrieve` | `plant(target, flags)` + `retrieve` |
| `flag_stores()` | not overridden (→ `["default"]`) | override with store names |
| touches the mapping? | no | yes, by name (`flags["document"]`) |

Both share one framework lifecycle / events / storage. Each returned handle must
independently locate its own store's flag (see the `plant` contract above).

> This table + the two snippets are the content that lands in
> `docs/CHALLENGE_AUTHORING_SOP.md` **as part of the orche PR** (when the API
> ships). It is deliberately **not** added to the live SOP now — that doc
> describes the current contract, and documenting a not-yet-existing API there
> would mislead authors.

## Framework changes

- **Validation (bring-up, fail-fast):** for every deployed challenge, check
  `flag_stores()` is non-empty, names are unique, and each matches `_STORE_RE`
  (no spaces/slashes — log- and serialization-safe). Else `ValueError` at startup.
- **FlagServer.plant_flags / rotate — plant-then-swap ordering (no valid-flag
  gap).** Per `(team, service)`, in this exact order:
  1. Generate the new group: `flags = {s: Flag(value=new_flag()) for s in
     challenge.flag_stores()}`.
  2. `handler.plant(target, flags)` (with the existing `(team, service)` retry).
  3. Validate `set(handles) == set(flags)`.
  4. **Only on success:** commit the new group into `FlagStore` **and invalidate
     the old group in the same atomic swap** (under the store lock). The swap
     covers `set(old_stores) ∪ set(new_stores)`: new-group stores are
     replaced/activated (values enter `by_flag`); **any store in the old group but
     absent from the new group is force-invalidated** (record dropped, value out
     of `by_flag`). This closes the **orphan-store leak** — if a challenge's
     `flag_stores()` shrinks/changes between deployments (e.g.
     `["document","profile"]` → `["default"]`), the dropped store's old flag would
     otherwise stay valid forever. The swap asserts no record survives for
     `(team, service)` outside the new group.
  5. Emit **one `FLAG_PLANTED` per store** (`payload.store = <name>` — logs read
     `store=document`, never `store=2`).

  The old group is invalidated **only after** the new group is confirmed planted
  (step 4). If planting fails (a store never returns a handle after retries), the
  store is **left untouched** — the old flags stay valid/submittable and the round
  goes to `FLAG_PLANT_FAILED`. Never invalidate-then-plant: that opens a window
  where the service has no valid flag.
- **FlagStore:** stores a full **record** per store, not a bare string — Flag is
  an object at plant time and must stay one in the store (rotation, retrieve,
  debugging, and future metadata all need it):

  ```python
  @dataclass
  class FlagRecord:
      flag: Flag          # value + metadata (the object, not a string)
      handle: str         # plant locator, for retrieve()
      round_id: int       # round this record was planted
      active: bool        # currently live / submittable

  planted: dict[tuple[str, str, str], FlagRecord]   # (team, service, store) -> record
  by_flag: dict[str, tuple[str, str, str]]          # flag value -> (team, service, store), O(1) submit
  submissions: dict[str, set[str]]                  # unchanged: keyed on the flag value
  ```

  - `submit()` / `submissions` stay keyed on the flag **value**, so **per-flag
    independent scoring** falls out unchanged.
  - **Rotation (v1 = hard invalidation):** replace the record for each
    `(team, service, store)` and drop the old value from `by_flag` (old flag →
    `UNKNOWN` on submit), matching today. `active` mirrors `by_flag` membership in
    v1; it (plus `round_id`) is the hook that makes future **soft-retire /
    overlapping-round validity** a data change, not a schema change.
  - **`handle` now lives on the record**, so `FlagServer._handles` (the "reserved
    for future retrieve" dict) is removed — `retrieve()` reads `record.handle`.
- **Fail-loud rule B:** `expected_flag_count` sums over the **deployed
  `(team, service)` instances**, not distinct challenge *types* (the same
  challenge is deployed to multiple teams, and a team may later hold multiple
  instances of one challenge type):

  ```python
  expected = sum(len(ch.flag_stores()) for (_team, _service), ch in self._challenge_by_owner.items())
  ```

  (was `len(self._challenge_by_owner)`.) `count_planted_flags` already counts
  `FLAG_PLANTED` events, one per flag now, so "planted < expected → FAIL" holds.

## Migration (decision: unified contract)

All handlers move to the single `plant(target, flags: Mapping[str, Flag])`
contract; `SingleFlagHandler` is the compatibility abstraction. The 22 existing
handlers migrate mechanically: subclass `SingleFlagHandler` and rename
`plant(self, target, flag)` → `plant_one(self, target, value)`. Behavior
unchanged (single `"default"` store). Rationale: one plant entry point, no legacy
branch; single- and multi-flag challenges share one lifecycle / event / storage
model; adding score/expiry/checker/store-level-retry later never touches the
handler API again.

## Testing

No real challenge enables multiflag in v1, so the multiflag path would otherwise
never execute. Add a **synthetic test fixture** — a two-store challenge/handler
`flag_stores() -> ["document", "profile"]` — and assert the whole path end-to-end:

- two **distinct** flags are generated (one per store);
- the handler receives a **name-indexed** `Mapping[str, Flag]` (`flags["document"]`);
- a **missing handle key** in the return fails the plant (`set(handles) != set(flags)`);
- **two `FLAG_PLANTED`** events fire, each with the correct `payload.store`;
- **both flags submit + score independently** (and neither is `OWN`/`DUPLICATE`
  for the other);
- after **rotation**, both old flags become `UNKNOWN`, both new ones valid;
- **orphan-store cleanup:** a fixture whose `flag_stores()` shrinks between
  rotations (e.g. `["document","profile"]` → `["document"]`) — the dropped
  store's old flag becomes `UNKNOWN`, no forever-valid orphan survives;
- **idempotent-retry convergence** (the core invariant the atomicity section
  demands): a fixture handler configurable to *fail the first attempt after a
  partial write* (return a handle for only one store) — the framework rejects it
  (`set(handles) != set(flags)`), retries the whole `(team, service)`, the second
  attempt succeeds, and the final state has both flags valid with **no duplicate /
  stale records** from the partial first write;
- **fail-loud `expected_flag_count` == 2** for that one deployed instance;
- **bring-up validation** rejects illegal `flag_stores()` at startup with
  `ValueError`: empty list, duplicate names, and a name failing `_STORE_RE`
  (e.g. `"has space"` / `"a/b"`);
- (regression) a `SingleFlagHandler` challenge still plants exactly one
  `"default"` store and behaves as today.

## Rollout

**Dependency direction:** orche (parent) embeds `challenge-warehouse` as a
submodule; the API (`Flag` / `FlagHandler` / `SingleFlagHandler` / `Challenge`)
lives in **orche**, and the 22 handlers in **warehouse** `import` it — so
**warehouse depends on the orche API**, and the **submodule pointer is bumped in
orche (the parent), never in the warehouse PR**. The API + the handlers only
agree at one combined commit, so CI must run there, in this order:

1. **orche branch** — land the new API surface + framework + the synthetic
   two-store fixture test. This is the stable API warehouse builds against.
2. **warehouse branch** — migrate the 22 handlers to `SingleFlagHandler` against
   that API.
3. **Integration CI** — point the orche branch's `challenge-warehouse` submodule
   at the *warehouse branch* commit and run orche's **full** suite on that
   combined state (new API + migrated handlers). This is the only state where
   API and handlers are consistent; it must be green.
4. **Merge warehouse first** into `challenge-warehouse` main. (orche main still
   pins the *old* warehouse commit here, so orche main keeps working untouched —
   no circular break.)
5. **Re-point** the orche branch's submodule to the **merged warehouse main**
   commit (not the branch commit); confirm orche CI green on that final combined
   commit.
6. **Merge orche last** — orche main now atomically has the new API **and** pins
   the migrated warehouse.

**orche PR contents:** `Flag`, `Challenge.flag_stores`, `FlagHandler` +
`SingleFlagHandler`, bring-up validation, `FlagStore`/`FlagRecord` (per-store
keys), `FlagServer` (generate/plant/validate/plant-then-swap/events), fail-loud
`expected_flag_count`, the synthetic multiflag test, and `docs/UML/flag.md` +
`docs/CHALLENGE_AUTHORING_SOP.md` updates + the submodule bump (step 5).

**warehouse PR contents:** migrate the 22 handlers to `SingleFlagHandler`
(`plant` → `plant_one`); no behavior change (all single `"default"` store).

Never merge the orche API to main while its submodule still pins un-migrated
handlers (orche CI would go red), nor rely on a warehouse commit until an orche
commit pins it.

## Future

- `PlantResult` per-store returns + store-level retry.
- Per-store `score` / `expires` / `checker` via `Flag.metadata` (or promoted
  fields) — framework carries them through without a handler-API change.
- `MappingProxyType` for runtime-immutable `metadata`.
