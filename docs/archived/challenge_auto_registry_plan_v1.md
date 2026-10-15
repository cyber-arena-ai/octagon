# Challenge Auto-Registry + Config-Layered Health Interval — Plan v1

Status: **planned, not yet shipped** (authored 2026-06-13).
Live design once shipped: [docs/UML/challenges.md](../UML/challenges.md) ·
[docs/UML/health.md](../UML/health.md).

## Problem

Adding a challenge today requires editing **two framework files outside the
challenge folder**:

1. [`challenges/registry.py`](../../challenges/registry.py) — up to 4 hand-maintained
   entries per challenge: an import, `_CHALLENGE_FACTORIES`, a `_<x>_build()`
   wrapper + `_IMAGE_BUILDERS`, and optionally `_<x>_initial_start()` +
   `_INITIAL_START` and `_HEALTH_INTERVAL_SECS`.
2. `config/game*.yaml` — the `challenges:` entry + the team prod node `image:`.

Per [CHALLENGE_AUTHORING_SOP.md](../CHALLENGE_AUTHORING_SOP.md) §"Submit", a
contributor ships only `challenges/<snake>/` and a **maintainer** wires the
registry. That maintainer step is the friction this plan removes.

Evidence it bites: `challenges/{birthdaygram,cartography,chat_app,fittyfit,
jokes,marscasino,treasury}/` are complete, self-contained challenge packages
(each exports a `*Challenge` class + `build.py`) yet are referenced **nowhere**
outside their own folders — fully authored, unregistered, inert.

Most registry content is boilerplate:
- The 7 `_<x>_build()` wrappers are byte-identical modulo the package name
  (`from challenges.<pkg>.build import build_image; return build_image()`).
- 6 of the 7 `_<x>_initial_start()` hooks are byte-identical
  (`exec_in(target.host, "/arena/restart.sh")`); only `notes` differs (no hook).

## Goal

Adding a challenge requires touching **only**:
- `challenges/<snake>/` (the self-contained package), and
- `config/game*.yaml` (which the maintainer accepts editing).

Zero edits to `registry.py` or any other framework `.py`.

Plus: make the per-challenge health-poll interval **layered** — challenge ships
an intrinsic default, `game.yaml` may override per match.

## Design

### A. Discovery replaces the factory dict

`registry.py` scans `challenges/*/`, imports each package, and registers the
exported `Challenge` subclass by its `name`.

Two constraints shape the mechanism:

1. **Name must be readable without instantiating.** Factories are zero-arg and
   lazy today because `IcoChallenge.vulbox` shells out to `git rev-parse` at
   construction (see registry.py L36-38). Discovery must learn the name from the
   *class*, not an instance. → Promote `name` from an abstract **property** to a
   class-level attribute (`ClassVar[str]`) on the `Challenge` ABC. `cls.name`
   then reads without `cls()`. (Folder-name convention is **not** usable:
   `mlflow_artifact_lfi/` → `"mlflow-lfi"`, no mechanical mapping.)

2. **Importing a package is safe; instantiating is not.** Discovery imports each
   `challenges.<pkg>` module (defines the class — cheap, no git) and stores
   `name -> class`. `build_challenge(name)` still does `cls()` lazily, preserving
   today's laziness.

**Subclass-resolution convention (`_challenge_subclass_in`).** A challenge
package's `__init__.py` must export **exactly one concrete `Challenge`
subclass**. Resolution rule:
- **0 concrete subclasses** → not a challenge package; skip silently (covers
  `interface/`, `__pycache__/`, helper-only dirs).
- **1** → that's the challenge.
- **>1** → hard error naming the package and the colliding classes (don't guess
  which one is "the" challenge).

"Concrete" = a `Challenge` subclass that isn't `Challenge` itself and isn't
abstract (`inspect.isabstract` is False) — so mixins like
`RestartScriptInitialStart` and the ABC never count.

**Name validation, all at discovery time (fail fast, never last-writer-wins):**
- `cls.name` missing / not a `str` / empty-or-whitespace → hard error naming the
  class and module.
- `cls.name` not kebab-case (matches `^[a-z0-9]+(-[a-z0-9]+)*$`) → hard error —
  the value is used as a `game.yaml` key, event `service` field, and image-tag
  fragment, so the shape is load-bearing.
- duplicate `name` across two packages → hard error naming both modules.

Config-vs-registry mismatch is caught at lookup, not discovery: `build_challenge`
raises with a clear message + the sorted `known_challenge_names()` list when a
`game.yaml` references a name no package declares (preserves today's behavior).

```python
# registry.py — sketch, replaces all 4 dicts
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

def _discover() -> dict[str, type[Challenge]]:
    found: dict[str, type[Challenge]] = {}
    for info in pkgutil.iter_modules(challenges.__path__):
        if info.name in {"interface"}:
            continue
        mod = importlib.import_module(f"challenges.{info.name}")
        cls = _challenge_subclass_in(mod)        # 0 -> None (skip); >1 -> raises
        if cls is None:
            continue
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{cls.__module__}.{cls.__qualname__}: "
                             f"`name` must be a non-empty str, got {name!r}")
        if not _KEBAB.match(name):
            raise ValueError(f"{cls.__module__}: challenge name {name!r} "
                             f"is not kebab-case")
        if name in found:
            raise ValueError(f"duplicate challenge name {name!r}: "
                             f"{found[name].__module__} vs {cls.__module__}")
        found[name] = cls
    return found
```

### B. Per-challenge behavior moves onto the ABC

The three side-dicts collapse into the `Challenge` contract, so each challenge
carries its own behavior inside its folder:

| Today (registry dict)        | After (on `Challenge`)                                   |
|------------------------------|----------------------------------------------------------|
| `_IMAGE_BUILDERS[name]`      | convention: `challenges.<pkg>.build.build_image()` — discovery already knows `<pkg>` via `cls.__module__`; no wrapper needed. |
| `_INITIAL_START[name]`       | `initial_start(self, target, exec_in) -> None`, default no-op. |
| `_HEALTH_INTERVAL_SECS[name]`| `health_interval_secs(self) -> float \| None` property, default `None`. |

To kill the 6 identical hooks, ship a mixin in the interface. Named
`RestartScriptInitialStart` (not `...Sh...`) so it doesn't bind to a shell
detail — the contract is "kick the restart entrypoint", with the script path a
class-level constant a challenge could override:

```python
# challenges/interface/challenge.py (or a small helpers module)
class RestartScriptInitialStart:
    """initial_start that kicks the restart entrypoint (spawn == restart)."""
    restart_script: str = "/arena/restart.sh"
    def initial_start(self, target, exec_in) -> None:
        rc, out = exec_in(target.host, self.restart_script)
        if rc != 0:
            log.warning("%s initial_start: %s rc=%d out=%r",
                        self.name, self.restart_script, rc,
                        out[-200:] if out else "")
```

`ico/mlflow/aiohttp/gradio/pyload/marimo` inherit it; `notes` keeps the default
no-op. The 6 hooks become 6 base-class mentions inside each challenge folder.

`build_images(cfg)` becomes: for each `chcfg`, look up the class, derive its
package from `cls.__module__`, import `<pkg>.build`, call `build_image()`. The
`<pkg>.build.build_image()` convention is **required, not implicit** — a missing
`build.py` or `build_image` must raise an error that names the offending
challenge package (`challenge 'foo' (challenges.foo): expected build.py with
build_image()`), never a bare `ImportError`/`AttributeError`. Document the
convention in the SOP (below).

`registry.initial_start(...)` is deleted; callers invoke
`challenge.initial_start(target, exec_in)` directly.

### C. Layered health interval (merged from the health-interval discussion)

The interval has dual nature: it's an **intrinsic** checker-cost property (ico
talks to the live binary each tick; mlflow writes sqlite each tick) **and** an
operational knob an organizer may want to tune per match. So: layer it.

Precedence (highest first):

```
game.yaml  challenges[].health_interval_secs   (organizer override, optional)
  └─ challenge.health_interval_secs             (intrinsic default, in the folder; None = unset)
       └─ DEFAULT_INTERVAL_SECS (= 10, health/poller.py)   (framework fallback)
```

- `ChallengeConfig` ([gamemgr/config.py](../../gamemgr/config.py) L21-26) gains
  `health_interval_secs: float | None = None`; the loader reads
  `c.get("health_interval_secs")` and **validates at config-load time**: when
  present it must coerce to a **positive float** — `0`, negative, or
  non-numeric strings raise a clear error naming the challenge entry
  (`challenges[2] 'mlflow-lfi': health_interval_secs must be a positive number,
  got -5`). Don't defer this to runtime.
- `Challenge` gains the `health_interval_secs` property (default `None`);
  `ico`/`mlflow` override to `30.0` inside their folders. A challenge default
  that is non-`None` is also validated positive at discovery time (same rule),
  so a bad intrinsic default fails fast rather than silently poisoning the loop.
- `build_targets` ([health/poller.py](../../health/poller.py) L138-172) computes
  the interval inline:
  `chcfg.health_interval_secs ?? challenge.health_interval_secs ?? DEFAULT_INTERVAL_SECS`.
  The `interval_lookup` parameter and `registry.health_interval_secs` are removed.

Net: `_HEALTH_INTERVAL_SECS` is deleted (auto-registry goal), and organizers gain
a per-match override (health-interval goal) — both in one change.

## File-by-file changes

Framework:

1. [`challenges/interface/challenge.py`](../../challenges/interface/challenge.py)
   - `name`: abstract `@property` → required `ClassVar[str]` class attribute.
   - add `initial_start(self, target, exec_in) -> None` default no-op.
   - add `health_interval_secs(self) -> float | None` property default `None`.
   - add `RestartScriptInitialStart` mixin (here or a sibling helpers module).
2. [`challenges/registry.py`](../../challenges/registry.py)
   - rewrite: `_discover()` + `build_challenge` / `known_challenge_names` /
     `build_images`. Delete all 4 dicts, the 7 `_<x>_build()` wrappers, the 6
     `_<x>_initial_start()` hooks, `initial_start()`, and `health_interval_secs()`.
3. [`gamemgr/config.py`](../../gamemgr/config.py) — `ChallengeConfig` +
   optional `health_interval_secs`; parse it.
4. [`gamemgr/game_mgr.py`](../../gamemgr/game_mgr.py) — drop
   `initial_start, health_interval_secs` from the registry import; call
   `challenge.initial_start(target, exec_in)` at deploy (L196-221 area); drop the
   `interval_lookup=` arg from the `build_targets(...)` call (L359-365).
5. [`health/poller.py`](../../health/poller.py) — `build_targets` computes the
   layered interval inline; remove the `interval_lookup` parameter.

Per challenge (one-time migration — INSIDE each folder, not part of the
"add a new challenge" cost going forward):

6. Every `challenges/<name>/challenge.py`: `name` property → class attribute.
7. `ico/mlflow/aiohttp/gradio/pyload/marimo`: inherit `RestartScriptInitialStart`.
8. `ico`, `mlflow`: add `health_interval_secs = 30.0` (override property).

Tests:

9. [`tests/test_challenge_registry.py`](../../tests/test_challenge_registry.py) —
   rewrite around discovery: finds every package; `_challenge_subclass_in`
   returns the sole concrete subclass (0 → skip, >1 → raises); bad `name`
   (missing / non-str / empty / non-kebab) raises; duplicate `name` raises;
   `build_challenge` is lazy; `build_images` resolves builders by convention and
   a package missing `build.py`/`build_image` raises a package-named error.
   Use a tmp fixtures package (not the live `challenges/`) so the negative cases
   don't depend on real challenges.
10. [`tests/test_health_poller.py`](../../tests/test_health_poller.py) — interval
    precedence (yaml > challenge default > framework default).
11. Config-layer tests (in `tests/test_config.py` or alongside the loader):
    - `game.yaml` referencing an unknown challenge name → clear error listing
      `known_challenge_names()`.
    - `health_interval_secs` override flows through to the `PollTarget`.
    - `health_interval_secs <= 0` / non-numeric in `game.yaml` → config-load error.
    - a previously-unregistered package (e.g. `treasury`) becomes a build target
      by adding **only** its `game.yaml` entry (the headline guarantee).

Docs (per CLAUDE.md update rule):

12. [`docs/UML/challenges.md`](../UML/challenges.md) — replace the "four entries in
    registry.py" section (L260-290) with the discovery model; update the
    `registry.py` responsibility blurb (L220-221).
13. [`docs/CHALLENGE_AUTHORING_SOP.md`](../CHALLENGE_AUTHORING_SOP.md) — delete
    the "Maintainer adds to registry.py" step (L116-124); the new contract is
    "drop the folder + add the `game.yaml` entry". Spell out the now-load-bearing
    conventions:
    - `name` is a **kebab-case `ClassVar[str]`** on the challenge class.
    - `initial_start` / `RestartScriptInitialStart` (inherit for the
      spawn==restart pattern; default is no-op).
    - `health_interval_secs` — challenge default (positive float) + optional
      `game.yaml` override; precedence and validation.
    - **`build.py` exposing `build_image()` is required** (discovery calls it by
      convention).
    - **`__init__.py` exports exactly one concrete `Challenge` subclass.**
    - **Package import must be side-effect-light** — no git/docker/network/
      filesystem work at import time (discovery imports every package on
      startup). Heavy work belongs in `build_image()` or behind the lazy
      `vulbox`/probe properties, not at module top level.
14. [`CLAUDE.md`](../../CLAUDE.md) — add this plan to the Archived index.

## Migration / sequencing

Land in one PR (the ABC change to `name` is breaking for every challenge, so
they must move together):

1. ABC change + `RestartShInitialStart` mixin.
2. Migrate all existing challenges (`name` attr, mixin, interval overrides).
3. Rewrite `registry.py` to discovery.
4. Config + poller layering.
5. Tests green, then docs.

Verify: `pytest tests/test_challenge_registry.py tests/test_health_poller.py`
plus the existing `game.yaml` smoke run still deploys ico + mlflow with a 30s
poll, and an unregistered folder (e.g. `treasury`) becomes deployable by adding
**only** a `game.yaml` entry.

## Trade-offs / decisions

- **Class attribute vs decorator for `name`.** Chose a `ClassVar` attribute over
  `@register("name")` self-registration: no import-order dependence, the name
  lives next to the class, and discovery stays a single explicit scan. Decorator
  is the fallback if we later want challenges shipped as out-of-tree packages.
- **entry_points / pluggy** rejected — challenges live in-tree; packaging
  machinery is unjustified weight.
- **`VulboxImage.build_context`** is currently unconsumed; this plan keeps the
  `build.py:build_image()` convention rather than wiring `build_context`. Folding
  build into the ABC via `build_context` is a possible follow-up, out of scope.
- **Discovery cost**: importing every challenge package on startup is cheap
  (class definition only; no git/docker). If it ever matters, gate discovery to
  names referenced by the loaded `game.yaml`.
- **Import failure = skip, not fatal (refined during implementation).** The plan
  treats malformed *names* as hard errors, but a package that won't *import*
  (missing optional third-party dep, syntax error) is recorded and skipped with a
  logged warning — otherwise one challenge's `import requests` would brick a
  stdlib-only deploy (e.g. notes), regressing the old "registry only imported the
  registered few" property. The failure resurfaces loudly: `build_challenge` /
  `build_images` for a skipped name raises listing the known names *and* the
  skipped packages with their import errors. (In-tree example: several `faust-*`
  scaffolds import `requests`/`nclib`/`pwn`, which aren't in `requirements.txt`;
  they're discovered only where those deps are present.) Contract violations in a
  *successfully imported* package (bad/duplicate name, two concrete subclasses)
  stay hard errors.
