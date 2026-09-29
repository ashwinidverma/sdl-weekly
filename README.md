# SDL-MOF: Self-Driving Lab Software Stack for Autonomous One-Pot MOF Synthesis

**Status:** Software-complete, validated entirely in simulation. No physical
hardware is connected yet — every "experiment" below is executed by a
digital-twin simulator standing behind the same interface a real liquid
handler / reactor / PXRD will use later.

Built for Thermax's materials science group as the software foundation for
a future physical self-driving lab (SDL) targeting autonomous, closed-loop
optimization of **one-pot MOF synthesis**.

## Why simulate first?

Without hardware, the goal is to de-risk *everything that isn't the robot*:
the optimizer, the data model, the orchestration loop, and the dashboard.
The one piece that's necessarily fake right now is the "run an experiment"
step — so that's the one piece isolated behind a clean interface
(`sdl_core/hal/base.py`). Swap `VirtualSynthesisDriver` for a real driver
implementing the same interface, and nothing else in the stack changes.

## Architecture

```
Dashboard (Streamlit)  /  API (FastAPI)
              |
        Orchestrator (sdl_core/orchestrator)
      logs every experiment to SQLite as it runs
              |
   Optimizer Engine (ParMOO, multi-objective BO)
      proposes design points -> calls driver.run_experiment()
              |
   Hardware Abstraction Layer (sdl_core/hal)
    +-- VirtualSynthesisDriver  (ACTIVE: digital twin, sdl_core/simulator)
    +-- RealSynthesisDriver     (STUB: documented, not yet implemented)
```

Target system: **MOF-5** (Zn₄O(BDC)₃), one-pot solvothermal synthesis.
Design space: Zn:linker ratio, total concentration, temperature, time,
modulator equivalents. Objectives (3, optimized jointly as a genuine
multi-objective / Pareto problem, not collapsed to one score): **yield**,
**crystallinity**, **particle size**.

## Results so far (virtual campaigns)

- A 150-virtual-experiment campaign found a **30-point Pareto front**
  spanning realistic yield/crystallinity/particle-size trade-offs
  (`results/mof5_one_pot_v1_pareto_front.png`).
- Benchmarked the ParMOO-guided loop against random search under an
  identical experiment budget (`benchmarks/compare_vs_random.py`): the
  guided loop reached a **~11% higher best score with far lower run-to-run
  variance** at the same number of experiments
  (`results/benchmark_vs_random.png`) — i.e. the optimization loop is
  doing real work, not just running.

## Quickstart

```bash
pip install -r requirements.txt

# Run a campaign from the command line
python run_campaign.py --config campaigns/mof5_default.yaml --plot

# Run the interactive dashboard
streamlit run dashboard/app.py

# Run the API
uvicorn api.main:app --reload

# Run the test suite
pytest tests/ -v

# Run the "does the optimizer beat random search?" benchmark
python benchmarks/compare_vs_random.py
```

### Docker

```bash
docker build -f docker/Dockerfile -t sdl-mof .
docker run -p 8501:8501 sdl-mof                 # dashboard
docker run -p 8000:8000 sdl-mof uvicorn api.main:app --host 0.0.0.0 --port 8000
```

## 3D visualization (virtual lab bench)

A self-contained, browser-based 3D replay of any campaign — dispensing,
heating, characterization, and a live Pareto-front plot, animated from
real logged experiment data. No installs, no CDN, works offline. See
`visualization/README.md` for the quickstart. Screenshot-worthy demo
for stakeholders; not a CAD-accurate digital twin (see that README for
the path to one, via NVIDIA Omniverse, once real hardware exists).

## Repository layout

```
sdl-mof/
├── sdl_core/
│   ├── simulator/mof5_synthesis.py    # digital twin (the only "fake" part)
│   ├── hal/                            # base.py, virtual_driver.py, real_driver_stub.py
│   │   └── streaming/                  # MQTT event-bus HAL (MDML-style pattern)
│   ├── optimizer/parmoo_engine.py      # ParMOO multi-objective BO wrapper
│   ├── orchestrator/campaign.py        # closed-loop controller + DB logging
│   ├── data/                           # SQLAlchemy models + session helpers
│   └── config.py                       # Pydantic campaign config (YAML-driven)
├── campaigns/mof5_default.yaml         # design space + objectives + budget
├── run_campaign.py                     # CLI entrypoint (direct-call HAL)
├── examples/run_streaming_campaign.py  # CLI entrypoint (MQTT event-bus HAL)
├── dashboard/app.py                    # Streamlit live campaign dashboard
├── api/main.py                          # FastAPI campaign control API
├── benchmarks/compare_vs_random.py     # proves the optimizer adds value
├── tests/                               # pytest, 12 tests, all passing
├── docker/Dockerfile
└── requirements.txt
```

## Open-source stack

| Purpose | Library |
|---|---|
| Multi-objective Bayesian optimization | [ParMOO](https://parmoo.readthedocs.io) (Sandia National Labs) |
| Event streaming / pub-sub (real-hardware readiness) | Mosquitto (MQTT) + paho-mqtt — same architectural role as Argonne's MDML/Kafka |
| Config validation | Pydantic + PyYAML |
| Data / provenance | SQLAlchemy (SQLite; swap connection string for Postgres later) |
| Dashboard | Streamlit + Plotly |
| API | FastAPI + uvicorn |
| Numerics / plotting | NumPy, pandas, matplotlib |
| Testing | pytest |

## Extending to other MOFs

The design pattern is: one simulator module per target chemistry
(`sdl_core/simulator/<mof>_synthesis.py`, exposing `PARAM_BOUNDS`,
`RESPONSE_NAMES`, `run_synthesis`), one matching campaign YAML, and
everything else (HAL, optimizer, orchestrator, dashboard, API) is reused
unchanged. To add e.g. ZIF-8: write `zif8_synthesis.py`, add
`campaigns/zif8_default.yaml`, point a `VirtualSynthesisDriver` subclass or
factory at it.

## Event-streaming architecture (MDML-style, for real-hardware readiness)

`sdl_core/hal/streaming_driver.py` + `sdl_core/hal/streaming/` implement an
alternative HAL path that decouples the optimizer from the experiment
executor over a pub/sub message bus (MQTT/Mosquitto today) instead of a
direct function call:

```
ParMOOEngine --publish--> [MQTT: sdl/mof5/experiment_request] --> LabWorker
ParMOOEngine <--subscribe-- [MQTT: sdl/mof5/experiment_result] <-- LabWorker
```

This mirrors the pattern Argonne National Laboratory uses in their
ParMOO + MDML autonomous flow-chemistry lab (MDML runs the same
request/result/correlation-ID pattern on Apache Kafka; we use MQTT as a
lighter self-hostable equivalent for single-lab scale). It matters once
real instruments are involved: a reactor, PXRD, etc. run on their own
timescales, may live on separate machines, and don't return a clean
value the instant they're asked — pub/sub decouples "ask for an
experiment" from "the lab reports back whenever it's actually done."

`LabWorker` today just wraps the same virtual simulator behind the bus
instead of a function call; when real hardware exists, only
`LabWorker._handle_request` needs to change to call real instruments —
`StreamingSynthesisDriver`, `ParMOOEngine`, and everything upstream stay
identical, since they only depend on `SynthesisDriver.run_experiment`.

Try it:
```bash
apt-get install -y mosquitto && mosquitto -d -p 1883   # one-time setup
python examples/run_streaming_campaign.py
```

## Path to physical hardware

`sdl_core/hal/real_driver_stub.py` documents exactly what to implement when
the liquid handler / reactor / PXRD are installed: dispense via a robot API
(Opentrons HTTP API / PyLabRobot), trigger the reactor (OPC-UA or vendor
API), and parse characterization output (PXRD pattern matching via
pymatgen's `XRDCalculator`, particle sizing via DLS/SEM image analysis)
into the same response dict the simulator returns today. Because the
optimizer and orchestrator only ever call `driver.run_experiment(params)`,
this is the *only* code that needs to change.

## Roadmap

- [x] Phase 0-1: architecture, schema, HAL interface
- [x] Phase 2: virtual synthesis simulator (MOF-5)
- [x] Phase 3: multi-objective BO integration (ParMOO)
- [x] Phase 4: orchestrator + experiment logging
- [x] Phase 5: dashboard (Streamlit) + API (FastAPI)
- [x] Phase 6: benchmark vs. random-search baseline
- [ ] Phase 7: physical hardware integration (`real_driver_stub.py` →
      real driver, once liquid handler / reactor / PXRD are available)
- [ ] Stretch: additional MOF simulator modules (ZIF-8, HKUST-1, UiO-66);
      higher-fidelity simulator tiers (kinetics-based or literature-data
      surrogate); Prefect-based orchestration for fault tolerance once
      real hardware runs unattended for long campaigns.
