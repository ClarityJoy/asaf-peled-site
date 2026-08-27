"""Loading config/*.yaml and turning it into source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .pacing import Pacer
from .sources.base import Source
from .sources.jobspy_source import JobSpySource

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ConfigError(Exception):
    pass


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


@dataclass
class Config:
    profile: dict[str, Any]
    queries: dict[str, Any]
    sources: dict[str, Any]
    config_dir: Path

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Config":
        directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        return cls(
            profile=_load_yaml(directory / "profile.yaml"),
            queries=_load_yaml(directory / "queries.yaml"),
            sources=_load_yaml(directory / "sources.yaml"),
            config_dir=directory,
        )

    # -- derived views -----------------------------------------------------

    @property
    def locations_by_label(self) -> dict[str, dict[str, Any]]:
        jobs = self.queries.get("jobs") or {}
        return {loc["label"]: loc for loc in jobs.get("locations", [])}

    def make_pacer(self, sleeper=None) -> Pacer:
        pacing = self.sources.get("pacing") or {}
        kwargs: dict[str, Any] = {
            "min_delay": float(pacing.get("min_delay_seconds", 12)),
            "max_delay": float(pacing.get("max_delay_seconds", 35)),
            "max_scrape_calls": int(pacing.get("max_scrape_calls", 40)),
        }
        if sleeper is not None:
            kwargs["sleeper"] = sleeper
        return Pacer(**kwargs)

    def build_job_sources(self, only: list[str] | None = None) -> list[Source]:
        jobs = self.queries.get("jobs") or {}
        all_queries: list[str] = jobs.get("queries", [])
        if not all_queries:
            raise ConfigError("queries.yaml defines no jobs.queries")
        by_label = self.locations_by_label
        hours_old = int(jobs.get("hours_old", 72))
        default_results = int(jobs.get("results_wanted", 25))

        built: list[Source] = []
        for entry in self.sources.get("sources", []):
            name = entry.get("name")
            if only and name not in only:
                continue
            labels = entry.get("locations") or list(by_label)
            missing = [label for label in labels if label not in by_label]
            if missing:
                raise ConfigError(
                    f"source '{name}' references unknown location label(s): "
                    f"{', '.join(missing)} - define them under jobs.locations"
                )
            built.append(
                JobSpySource(
                    name=name,
                    site=entry.get("site", name),
                    queries=all_queries[: int(entry.get("max_queries", len(all_queries)))],
                    locations=[by_label[label] for label in labels],
                    results_wanted=int(entry.get("results_wanted", default_results)),
                    hours_old=hours_old,
                    fetch_description=bool(entry.get("fetch_description", False)),
                    enabled=bool(entry.get("enabled", True)),
                )
            )
        if only:
            found = {source.name for source in built}
            unknown = [name for name in only if name not in found]
            if unknown:
                raise ConfigError(
                    f"unknown source(s): {', '.join(unknown)} - "
                    f"sources.yaml defines: "
                    f"{', '.join(e.get('name','?') for e in self.sources.get('sources', []))}"
                )
        return built
