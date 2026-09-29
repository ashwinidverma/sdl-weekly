# SDL-MOF Code Walkthrough

A file-by-file guide for a researcher who wants to actually understand
and modify this codebase, not just run it. Organized **bottom-up**, in
dependency order: each layer only depends on the ones above it in this
document. If you understand a file, everything below it in this doc
will make more sense.

```
Dashboard / API  ─────────┐
                            │  (read results, trigger campaigns)
Orchestrator  ◄─────────────┘
    │  builds & wires
    ▼
Optimizer (ParMOO)  ──calls──►  Driver (HAL)  ──calls──►  Simulator
    │                                │
    ▼                                ▼
Config (YAML)                 Data layer (SQLite)
```

The single most important idea in this codebase: **the optimizer never
talks to the simulator directly.** It talks to a `SynthesisDriver`
interface. Three different implementations of that interface exist
(direct-call virtual, MQTT-streamed virtual, and a stub for real
hardware) and they are interchangeable without touching the optimizer,
orchestrator, dashboard, or API. Almost every design decision below
exists to protect that property. Keep it intact when you edit things.

---

## 1. The simulator — `sdl_core/simulator/mof5_synthesis.py`

**Role:** the only "fake" part of the system. Stands in for a real
reactor + PXRD + balance until hardware exists.

**Software engineering concepts:**
- **Pure functions.** `run_synthesis(params, rng)` has no hidden state —
  same inputs (including the same `rng` state) always give the same
  output. This is what makes it unit-testable (see `test_simulator.py`)
  and reproducible across a whole campaign via a single seeded
  `numpy.random.Generator`.
- **Single source of truth for the design space.** `PARAM_BOUNDS` (a
  dict) and `RESPONSE_NAMES` (a tuple) are defined *once* here and
  imported everywhere else that needs them (`HAL`, `config.py`
  validation). If you only edited the YAML campaign config but not this
  dict, `CampaignConfig.validate_against_driver()` would catch the
  mismatch and fail loudly instead of silently misbehaving.
- **Separation of "true" response from "measured" response.** Three
  private functions (`_yield_response`, `_crystallinity_response`,
  `_particle_size_response`) compute noise-free, deterministic values.
  `run_synthesis` then adds measurement noise and occasional "failed
  batch" behavior on top. This separation is what let me unit-test "do
  good conditions score higher than bad conditions" without noise
  fighting the test (I average over 100 seeded runs in
  `tests/test_simulator.py::_mean_result`).

**How to edit it:**
- **To change the response surface** (e.g. you have real literature data
  and want a more accurate optimum): edit the three `_..._response`
  functions. Each is just algebra over the 5 params — no other file
  needs to change as long as you don't change the function *signatures*
  or `PARAM_BOUNDS`/`RESPONSE_NAMES`.
- **To add a new design variable** (e.g. `stirring_rpm`): add it to
  `PARAM_BOUNDS`, use it inside the `_..._response` functions, *and* add
  a matching entry to `campaigns/mof5_default.yaml`'s
  `design_variables` list — `CampaignConfig.validate_against_driver`
  will refuse to run otherwise (on purpose — it's cheaper to fail at
  startup than mid-campaign).
- **To add a new measured response** (e.g. `defect_density`): add it to
  `RESPONSE_NAMES`, write a `_defect_density_response` function, add it
  to the `return` dict in `run_synthesis`, and add a matching
  `objectives` entry in the YAML. `ParMOOEngine` reads `driver.response_names`
  generically, so it doesn't need code changes — just the config.

---

## 2. Hardware Abstraction Layer (HAL) — `sdl_core/hal/`

This is the **Strategy pattern**: one interface, multiple
interchangeable implementations, selected at campaign-construction time.

### 2.1 `base.py` — the interface

```python
class SynthesisDriver(ABC):
    param_bounds: Dict[str, tuple]
    response_names: tuple

    @abstractmethod
    def run_experiment(self, params: Dict[str, float]) -> Dict[str, float]:
        ...
```

This is Python's version of an abstract base class (`abc.ABC` +
`@abstractmethod`) — it can't be instantiated directly, and any subclass
*must* implement `run_experiment` or Python will refuse to construct it.
`validate_params` is a concrete helper method (not abstract) shared by
all subclasses, since every driver needs the same "did I get all the
params I expect" check.

**How to edit it:** you almost never should. This is the contract every
other piece of code (optimizer, orchestrator, dashboard) is written
against. If you add a method here, you have to add it to *all three*
driver implementations below.

### 2.2 `virtual_driver.py` — `VirtualSynthesisDriver`

**Role:** wraps the simulator behind the `SynthesisDriver` interface,
with a direct synchronous function call. This is the one used by
`run_campaign.py` and the dashboard by default.

**Key pattern — the `on_result` callback:**
```python
def __init__(self, seed=None, on_result=None, simulated_latency_s=0.0):
    ...
def run_experiment(self, params):
    ...
    if self._on_result is not None:
        try:
            self._on_result(dict(params), dict(result), wall_time_s)
        except Exception:
            pass  # logging must never take down the experiment loop
```
This is **dependency injection**: instead of `VirtualSynthesisDriver`
hard-coding "write to a database" (which would make it untestable
without a real DB, and useless for the dashboard/API which want
different logging), it accepts *any* callable and calls it after every
experiment. `CampaignRunner` (orchestrator) passes in a function that
writes to SQLite; `examples/run_streaming_campaign.py` does the same for
its own campaign; the pytest tests in `test_hal.py` pass in a plain
list-appending lambda to check the callback fires. The `try/except`
around the callback is deliberate: a bug in logging should never abort
a real (or virtual) experiment mid-flight.

**How to edit it:** if you want different demo behavior (e.g. simulate
occasional hardware errors, add artificial delay to make the dashboard
"Run new campaign" button feel more realistic) this is the file —
`simulated_latency_s` already exists for exactly that second use case.

### 2.3 `real_driver_stub.py` — `RealSynthesisDriver`

**Role:** not implemented. `run_experiment` raises `NotImplementedError`
with a message pointing back at the virtual driver. Its purpose is
purely **documentation-as-code** — the constructor signature
(`liquid_handler_url`, `reactor_endpoint`, `pxrd_watch_dir`) and the
module docstring describe exactly what needs to be built and with what
libraries (Opentrons/PyLabRobot, OPC-UA, pymatgen's XRD tools) once
hardware exists.

**How to edit it:** this is the file you'll actually fill in on
hardware day. Implement `run_experiment` to dispense reagents, run the
reactor, and parse characterization output into the same
`{"yield_pct": ..., "crystallinity": ..., "particle_size_nm": ...}`
shape the simulator returns — that shape match is what keeps everything
upstream (optimizer, orchestrator, dashboard) working unmodified.

### 2.4 `streaming/event_bus.py` — `EventBus`

**Role:** thin wrapper around an MQTT client (`paho-mqtt`), giving a
minimal `publish(topic, dict)` / `subscribe(topic, handler)` API.

**Key pattern — Observer/pub-sub, and a dataclass for messages:**
```python
@dataclass
class Message:
    topic: str
    payload: dict
```
`@dataclass` auto-generates `__init__`, `__repr__`, `__eq__` for a
plain data holder — no need to hand-write boilerplate for a class that's
just "these two fields, bundled." Every subscriber's `handler` gets
called with a `Message` whenever a matching topic gets a publish,
decoupling *who sends* from *who receives* completely — they only agree
on a topic string and a JSON shape.

**How to edit it:** if you outgrow Mosquitto (e.g. need Kafka's
horizontal scaling for multiple labs/sites, or want to actually connect
to Argonne's real MDML instance via `mdml_client`), this is the *only*
file that should need to change — `LabWorker` and `StreamingSynthesisDriver`
both depend only on the `publish`/`subscribe` interface, not on MQTT
specifics.

### 2.5 `streaming/lab_worker.py` — `LabWorker`

**Role:** the "lab" side of the pub-sub pair. Subscribes to
`sdl/mof5/experiment_request`, runs the simulator (today) or would
trigger real instruments (later), publishes to
`sdl/mof5/experiment_result`.

```python
def _handle_request(self, msg: Message) -> None:
    correlation_id = msg.payload["correlation_id"]
    params = msg.payload["params"]
    ...
    result = run_synthesis(params, rng=self.rng)
    self.bus.publish(RESULT_TOPIC, {"correlation_id": correlation_id, "result": result})
```

**Key pattern — correlation IDs.** MQTT topics are broadcast, not
request/response — anyone subscribed to `experiment_result` gets *every*
result, not just the one they asked for. The `correlation_id` (a UUID
generated per-request) is how the client matches "this result" back to
"the request I made" — a very standard pattern in any async messaging
system (also used in AMQP, gRPC streaming, etc.).

**How to edit it:** this is **the file you rewrite on hardware day.**
Replace the body of `_handle_request` — after popping `params` off the
message — with real liquid-handler/reactor/PXRD calls, keeping the same
`self.bus.publish(RESULT_TOPIC, {"correlation_id": ..., "result": {...}})`
shape at the end. Nothing else in the codebase needs to know.

### 2.6 `streaming_driver.py` — `StreamingSynthesisDriver`

**Role:** the client-side half. Implements the *same*
`SynthesisDriver` interface as `VirtualSynthesisDriver`, but
`run_experiment` publishes a request and **blocks on a
`threading.Event`** until the matching result arrives (or times out).

```python
def run_experiment(self, params):
    correlation_id = str(uuid.uuid4())
    event = threading.Event()
    self._pending[correlation_id] = event
    self.bus.publish(REQUEST_TOPIC, {"correlation_id": correlation_id, "params": params})
    got_result = event.wait(timeout=self.timeout_s)
    ...
```

**Key pattern — turning async pub/sub into a synchronous call.**
`ParMOOEngine` (and everything else upstream) is written as plain,
synchronous Python — it calls `driver.run_experiment(params)` and
expects a return value, full stop. `threading.Event` is what lets
`StreamingSynthesisDriver` present that same synchronous face to
callers while the actual work happens asynchronously over MQTT in a
background thread (`EventBus`'s `loop_start()`). `_handle_result` (the
subscriber callback, running on that background thread) just does
`self._results[correlation_id] = ...; event.set()`, which wakes up the
waiting `run_experiment` call on the *calling* thread. This is a classic
concurrency primitive — worth understanding if you extend this, since
getting the locking wrong here is where subtle async bugs hide (though
with one event per correlation ID, there's no shared mutable state
between concurrent requests to race on).

**How to edit it:** raise `timeout_s` if real experiments will take
longer than the default 30s. If you want to run many experiments
*concurrently* (e.g. multiple reactors in parallel), this driver already
supports it — each `run_experiment` call gets its own `correlation_id`
and `Event`, so concurrent calls from multiple threads won't collide.

---

## 3. Configuration — `sdl_core/config.py` + `campaigns/*.yaml`

**Role:** the human-editable "what to run" layer, validated with
**Pydantic** so mistakes fail fast and loudly instead of silently.

```python
class DesignVariable(BaseModel):
    name: str
    lb: float
    ub: float

    @field_validator("ub")
    @classmethod
    def _ub_gt_lb(cls, ub, info):
        ...
```

**Key pattern — schema validation at the boundary.** Pydantic's
`BaseModel` turns a plain YAML dict into typed, validated Python objects
the moment it's loaded (`CampaignConfig.from_yaml`), not later when
something downstream crashes confusingly. The `@field_validator` on
`ub` catches "upper bound below lower bound" typos immediately.
`validate_against_driver()` is a second validation pass, cross-checking
the *design variable names* in the YAML against whatever driver you
actually pass in — this is what catches the "you renamed a parameter in
the simulator but forgot the YAML" mistake mentioned above.

**How to edit it:** to add a new *kind* of setting (e.g. a per-campaign
random-batch-size override), add a field to `CampaignConfig` with a
default value — existing YAML files keep working unmodified since
Pydantic fills in defaults for anything not specified.

**`campaigns/mof5_default.yaml`** is pure data, no code — this is
deliberately the file a non-programmer researcher can edit alone to
change the design space, objectives, or optimization budget without
touching any Python.

---

## 4. Data layer — `sdl_core/data/`

**Role:** persist every experiment (params + measured result) so the
dashboard/API can show it later, and so nothing is lost if a campaign
crashes partway through.

### 4.1 `models.py` — SQLAlchemy ORM models

```python
class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(Integer)
    params_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    ...
    @property
    def params(self) -> dict:
        return json.loads(self.params_json)
```

**Key pattern — ORM (Object-Relational Mapping) + schema flexibility
via JSON columns.** `Mapped[...]` / `mapped_column(...)` is SQLAlchemy
2.0's typed declarative style — each class attribute maps to a table
column, and SQLAlchemy generates the SQL. Storing `params`/`result` as
JSON text (rather than one column per parameter) is a deliberate
trade-off: it means the database schema **never needs to change** when
you add a design variable or a new MOF with a totally different
parameter set, at the cost of not being able to write SQL `WHERE`
clauses directly against individual parameters. The `@property` methods
give you back a normal Python dict when reading (`row.params["temperature"]`)
without needing raw `json.loads` calls scattered everywhere.

### 4.2 `db.py` — engine/session helpers

Two small functions: `get_engine` (creates the SQLite file + tables if
they don't exist) and `get_session` (gives you a SQLAlchemy `Session` to
query/add rows with). This file exists purely so nobody else has to
remember SQLAlchemy's engine/session boilerplate — every other file just
calls `get_session(db_path)`.

**How to edit it:** to move from SQLite to Postgres for a
multi-instrument, multi-user deployment, this is the *only* file that
changes — swap the connection string in `create_engine(...)`
(`f"postgresql://user:pass@host/db"` instead of `f"sqlite:///{db_path}"`);
`models.py` and everything that calls `get_session` stays identical.

---

## 5. Optimizer — `sdl_core/optimizer/parmoo_engine.py`

**Role:** wraps ParMOO (the actual multi-objective Bayesian optimization
library) so it can be driven by a `CampaignConfig` + any
`SynthesisDriver`, without knowing anything MOF-specific.

**Key pattern — closures for dynamic function generation:**
```python
def make_obj(idx=idx, sign=sign):
    def obj_func(x, sx):
        return sign * sx[SIM_NAME][idx]
    return obj_func
moop.addObjective({"name": obj.name, "obj_func": make_obj()})
```
This loop runs once per objective in the config (could be 1, could be
5). Each `obj_func` needs to "remember" *which* response index and
*which* sign it's responsible for — but if you wrote `obj_func`
directly inside the `for` loop without `make_obj`, every closure would
share the *same* loop variable `idx`/`sign` by reference, and by the
time ParMOO calls them later, they'd all see the *final* loop value (a
classic Python closure-in-a-loop bug). `make_obj(idx=idx, sign=sign)`
captures the current value as a default argument, which *is* bound at
function-definition time — this is the standard fix.

**Key pattern — sign convention normalization.** ParMOO always
*minimizes*. Our config lets objectives be declared `max` or `min` in
plain English (`sense: max` for yield). `sign = -1.0 if sense == "max" else 1.0`
converts once, here, so nothing else in the codebase has to think about
minimize-vs-maximize — except `to_natural_units()`, which flips the sign
back specifically for CSV/dashboard display, since "-79% yield" is a
confusing thing to show a chemist.

**The `sim_func` closure — this is the actual HAL integration point:**
```python
def sim_func(x):
    params = {name: float(x[name]) for name in driver.param_bounds}
    result = driver.run_experiment(params)
    return [result[name] for name in driver.response_names]
```
This function is what ParMOO calls internally every time it wants to
evaluate a candidate design point. It's a two-line adapter: unpack
ParMOO's `x` object into our plain dict, call `driver.run_experiment`
(virtual, streaming, or eventually real — `ParMOOEngine` doesn't know or
care which), repack the result dict into the ordered list ParMOO
expects. **This closure is the entire integration surface between
"our HAL abstraction" and "ParMOO's API expectations."** If you ever
swap optimizer libraries (say, to BoTorch/Ax), this is the shape you'd
need to reproduce.

**How to edit it:** to change optimizer *behavior* (not the design
space) — e.g. use a different surrogate model or acquisition strategy —
this is the file: swap `GaussRBF`/`GlobalSurrogate_PS`/`RandomConstraint`
for other ParMOO components in `_build_moop`. See
[ParMOO's docs](https://parmoo.readthedocs.io) for the full menu of
surrogates/optimizers/acquisitions.

---

## 6. Orchestrator — `sdl_core/orchestrator/campaign.py`

**Role:** the "main loop controller" — the only file that knows about
*all* the other layers at once (config, driver, optimizer, database,
CSV export). Everything else is deliberately narrower.

**Key pattern — composition root.** `CampaignRunner.__init__` is where
concrete choices actually get made: which driver class to instantiate,
what to log where. This is intentional — it's much easier to reason
about a system when there's *one* place that wires concrete
implementations together, and every other file only depends on
abstractions (`SynthesisDriver`, `CampaignConfig`). This is sometimes
called a "composition root" in dependency-injection terminology.

```python
def _build_driver(self):
    if self.config.hal_mode == "virtual":
        return VirtualSynthesisDriver(seed=..., on_result=self._log_experiment)
    raise NotImplementedError(...)
```
Note this only handles `"virtual"` — the streaming path
(`examples/run_streaming_campaign.py`) currently wires
`StreamingSynthesisDriver` by hand rather than through
`CampaignRunner`. If you want `hal_mode: streaming` to work through the
normal `CampaignRunner`/dashboard/API path, this is exactly where you'd
add a branch (see the cheat-sheet at the end of this doc).

**How to edit it:** to change what gets logged per experiment, edit
`_log_experiment`. To change what "export results" means (e.g. also
write a JSON summary, or push to a different sink), edit
`export_results`.

---

## 7. Entry points

### 7.1 `run_campaign.py` (CLI, direct-call HAL)

A thin `argparse` wrapper: load config → `CampaignRunner(cfg).run()` →
print summary → optionally plot. This is the file a researcher runs
day-to-day from the terminal. **How to edit it:** add new `--flags` here
for anything you want to override from the command line without editing
YAML (mirrors how `--iterations` already overrides `cfg.iterations`).

### 7.2 `examples/run_streaming_campaign.py` (CLI, MQTT HAL)

Same idea, but wires `LabWorker` + `StreamingSynthesisDriver` by hand
(since `CampaignRunner` doesn't yet know about `hal_mode: streaming` —
see above) and does its own DB logging inline via a closure
(`log_result`). In a real deployment, the `LabWorker` half of this
script runs as its own separate long-running process
(`python -m sdl_core.hal.streaming.lab_worker`) on a machine near the
instruments — this file runs both halves together purely for
convenience so you can try the whole thing with one command.

---

## 8. Interfaces — dashboard & API

### 8.1 `dashboard/app.py` (Streamlit)

**Key pattern — Streamlit's rerun-on-interaction model.** Unlike a
typical web app, there's no explicit event-handler wiring — Streamlit
reruns this *entire script top-to-bottom* every time the user touches a
widget (a slider, a button). `st.session_state` is the one piece of
state that survives across reruns (used here to remember which
campaign's results to show after you click "Run new campaign"). If
you're used to Flask/Django, this file will look unusually
straight-line — that's normal for Streamlit, not a code smell.

**How to edit it:** add new `st.metric`/`st.dataframe`/`px.scatter_3d`
calls anywhere after `pf_df = pd.read_csv(pf_path)` to show more views
of the same results — you have the full Pareto-front and
full-history dataframes already loaded at that point.

### 8.2 `api/main.py` (FastAPI)

**Key pattern — Pydantic request/response models double as API
documentation.** `CampaignRequest`/`CampaignResponse` aren't just type
hints — FastAPI uses them to generate an interactive OpenAPI docs page
automatically (visit `/docs` once the server is running) and to
validate incoming JSON before your function body even runs.

**How to edit it:** add a new `@app.get("/...")` or `@app.post("/...")`
function anywhere — FastAPI picks up new routes automatically, no
central registry to update. Note the docstring's warning: campaigns run
*synchronously inside the request* right now, which is fine for a quick
virtual campaign but would block for hours on real hardware — that's
flagged as a known thing to fix (background task/queue) before this API
talks to real instruments.

---

## 9. Validation — benchmark & tests

### 9.1 `benchmarks/compare_vs_random.py`

Not a unit test — a standalone script that runs *two* strategies
(random sampling vs. ParMOO-guided) against the *same* virtual driver
and plots best-score-so-far over the campaign. This is how we got
concrete evidence ("64.2 vs 57.7, lower variance") that the optimizer is
doing real work, not just running without crashing. **How to edit it:**
change the `score()` function if you want the benchmark to reflect a
different notion of "good" (it's currently a simple scalarization for
plotting purposes only — the real campaign still optimizes all three
objectives natively via ParMOO).

### 9.2 `tests/*.py` (pytest)

- `test_simulator.py` — tests the *pure function* layer in isolation,
  no driver/optimizer involved. Fast, no external services needed.
- `test_hal.py` — tests `VirtualSynthesisDriver`, including the
  "broken callback doesn't crash the experiment" resilience property.
- `test_optimizer.py` — tests `ParMOOEngine` end-to-end against the
  virtual driver, including that config-vs-driver mismatches raise
  `ValueError` as designed.
- `test_streaming_hal.py` — tests the MQTT path, with a `pytest.fixture`
  (`lab_worker`) that starts/tears down a real `LabWorker` per test, and
  `pytestmark = pytest.mark.skipif(...)` so the whole file auto-skips
  cleanly if no MQTT broker is running (instead of failing confusingly).

**How to edit tests:** each test file mirrors the file it tests
one-to-one — if you add a function to `mof5_synthesis.py`, the natural
place for its test is `test_simulator.py`. Run everything with
`pytest tests/ -v` from the project root; run one file with
`pytest tests/test_simulator.py -v`.

---

## 10. Deployment — `docker/Dockerfile`, `requirements.txt`

Standard `pip install -r requirements.txt` inside a `python:3.12-slim`
image, defaulting to launching the dashboard, with a comment showing how
to override the command to run the API or CLI instead. **How to edit
it:** add new packages to `requirements.txt` (with a `>=` version
floor, not a pin, so you don't fight dependency resolution) — no
Dockerfile change needed unless you need a new system-level package
(e.g. `apt-get install -y mosquitto` if you want the streaming HAL
usable inside the container too — it's not there yet).

---

## Cheat-sheet: "I want to change X"

| You want to... | Edit this file (and only this file, usually) |
|---|---|
| Change synthesis chemistry trends (yield/crystallinity/size formulas) | `sdl_core/simulator/mof5_synthesis.py` |
| Change the optimization budget / iterations / acquisitions | `campaigns/mof5_default.yaml` (or `--iterations` CLI flag) |
| Add a new design variable | `mof5_synthesis.py` (`PARAM_BOUNDS` + response functions) **and** `campaigns/*.yaml` |
| Add a new objective/response | `mof5_synthesis.py` (`RESPONSE_NAMES` + return dict) **and** `campaigns/*.yaml` |
| Add a whole new MOF target | new `sdl_core/simulator/<mof>_synthesis.py` + new `campaigns/<mof>_default.yaml` — everything else reused |
| Change optimizer algorithm/settings | `sdl_core/optimizer/parmoo_engine.py::_build_moop` |
| Change what's logged to the database | `sdl_core/orchestrator/campaign.py::_log_experiment` |
| Move from SQLite to Postgres | `sdl_core/data/db.py` only |
| Add a dashboard view/chart | `dashboard/app.py` (append after `pf_df` is loaded) |
| Add an API endpoint | `api/main.py` (add a new `@app.get`/`@app.post`) |
| Make `hal_mode: streaming` work via `CampaignRunner`/dashboard | `sdl_core/orchestrator/campaign.py::_build_driver` — add an `elif self.config.hal_mode == "streaming":` branch |
| Swap MQTT for Kafka / real MDML | `sdl_core/hal/streaming/event_bus.py` only |
| Implement real hardware | `sdl_core/hal/streaming/lab_worker.py::_handle_request` (streaming path) or `sdl_core/hal/real_driver_stub.py::run_experiment` (direct-call path) |

## A note on safe editing

The property to protect above all others: **the optimizer and
everything above it (`ParMOOEngine`, `CampaignRunner`, dashboard, API)
must only ever depend on `SynthesisDriver.run_experiment`'s input/output
*shape*** — a dict of param names → floats in, a dict of response names
→ floats out. As long as every driver you write honors that shape,
you can freely swap simulator fidelity, messaging transport, or real
hardware underneath without breaking anything upstream. Whenever you're
about to add a new "kind" of thing to this codebase, ask "does this
belong behind the existing `SynthesisDriver` interface, or does it need
to change the interface itself?" — the former is almost always the
right answer, and it's what keeps `pytest tests/ -v` green while you
work.
