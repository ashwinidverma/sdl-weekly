#!/usr/bin/env python3
"""
CLI entrypoint: run a full SDL campaign from a YAML config.

Usage:
    python run_campaign.py --config campaigns/mof5_default.yaml
    python run_campaign.py --config campaigns/mof5_default.yaml --iterations 5 --plot
"""
from __future__ import annotations

import argparse
import os

from sdl_core.config import CampaignConfig
from sdl_core.orchestrator.campaign import CampaignRunner


def main():
    parser = argparse.ArgumentParser(description="Run an SDL synthesis campaign.")
    parser.add_argument("--config", required=True, help="Path to campaign YAML config.")
    parser.add_argument("--iterations", type=int, default=None,
                         help="Override iterations from config.")
    parser.add_argument("--plot", action="store_true",
                         help="Save a Pareto-front plot (matplotlib) to the results dir.")
    args = parser.parse_args()

    cfg = CampaignConfig.from_yaml(args.config)
    if args.iterations is not None:
        cfg.iterations = args.iterations

    print(f"Running campaign '{cfg.name}' for target {cfg.target_mof} "
          f"({cfg.hal_mode} HAL, {cfg.iterations} iterations x "
          f"{cfg.acquisitions_per_iter} acquisitions/iter)...")

    runner = CampaignRunner(cfg)
    results = runner.run()

    print()
    print(results.summary())

    if args.plot:
        _save_plot(results, cfg)


def _save_plot(results, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = results.pareto_front
    obj_names = [o.name for o in cfg.objectives]
    fig, ax = plt.subplots(figsize=(7, 6))
    sc = ax.scatter(df[obj_names[0]], df[obj_names[1]],
                     c=df[obj_names[2]], cmap="viridis_r", s=70, edgecolor="k")
    ax.set_xlabel(obj_names[0])
    ax.set_ylabel(obj_names[1])
    cbar = fig.colorbar(sc)
    cbar.set_label(obj_names[2])
    ax.set_title(f"Pareto front: {cfg.name}")
    fig.tight_layout()
    out_path = os.path.join(cfg.results_dir, f"{cfg.name}_pareto_front.png")
    fig.savefig(out_path, dpi=150)
    print(f"Pareto-front plot saved to: {out_path}")


if __name__ == "__main__":
    main()
