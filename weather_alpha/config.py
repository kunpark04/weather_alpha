"""Configuration loader. Single source of truth for runtime knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "weather_alpha.yaml"


Mode = Literal["paper", "live"]
FillModel = Literal["ask", "mid", "bid"]
AsosSource = Literal["iem", "synoptic", "metar_substitute", "auto"]


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    model_dir: Path
    live_log: Path
    positions_snapshot: Path
    notebook_v3: Path
    logs_dir: Path
    kill_switch: Path
    scheduler_state: Path


@dataclass(frozen=True)
class ModelCfg:
    name: str
    anchor_hour_local: int
    enabled: bool = True              # False = model-free engine path (market-anchored strategies)


@dataclass(frozen=True)
class RegimeThrottle:
    hrrr_persistence_gap_max_f: float
    confidence_floor_top1: float


@dataclass(frozen=True)
class WingCfg:
    """Wing-strategy selection knobs (used when StrategyCfg.name is a wing variant)."""
    drop_lower_ask: bool = True
    require_agreement: bool = False
    anchor: str = "market"            # "model" | "market"
    assumed_win_prob: float = 0.92
    max_ask_sum: float = 0.90
    sizing_mode: str = "equal_payout"
    flat_usd: float = 2.5             # >0 = flat-$ total per trade (overrides Kelly); 0 = Kelly
    fee_aware: bool = True            # skip a fire when fees would eat the whole win


@dataclass(frozen=True)
class StrategyCfg:
    bankroll_usd: float
    kelly_fraction: float
    kl_concentration_alpha: float
    edge_floor_cents: int
    per_contract_max_pct: float
    total_exposure_max_pct: float
    regime_throttle: RegimeThrottle
    name: str = "joint_kelly"                       # engine dispatch target
    wing: WingCfg = field(default_factory=WingCfg)


@dataclass(frozen=True)
class ExecutionCfg:
    fill_model: FillModel
    slippage_cents: int
    intraday_refresh_minutes: int
    market_event_pattern: str


@dataclass(frozen=True)
class MarketCfg:
    """One tradeable market = one city's daily-high event. A process trades a list of these.

    A single-market (legacy) config synthesizes exactly one of these from the top-level
    `station`/`local_tz` + `execution.market_event_pattern`; a multi-market config lists
    them explicitly under `markets:`.
    """
    name: str            # short tag, e.g. "CHI"/"chicago" — keys per-city state + terminal lines
    event_pattern: str   # Kalshi event-ticker prefix, e.g. "KXHIGHCHI"
    station: str         # NWS settlement station, e.g. "KMDW"
    local_tz: str        # IANA tz of the city's 1 PM anchor, e.g. "America/Chicago"


@dataclass(frozen=True)
class KalshiCfg:
    api_base: str
    key_id_env: str
    private_key_path_env: str
    rest_poll_seconds: int
    request_timeout_seconds: int


@dataclass(frozen=True)
class DataCfg:
    """Live-data routing knobs. `asos_source = auto` uses Synoptic when
    SYNOPTIC_TOKEN is present, falls back to IEM otherwise."""
    asos_source: AsosSource
    synoptic_token_env: str
    metar_obs_limit: int
    taf_hours_back: int
    asos_hours_back: int
    cli_days_back: int
    hrrr_publish_lag_min: int


@dataclass(frozen=True)
class SchedulerCfg:
    anchor_grace_minutes: int
    data_refresh_minutes: int
    predict_lookback_hours: int


@dataclass(frozen=True)
class RiskCfg:
    daily_max_loss_usd: float
    per_anchor_max_trades: int
    # Drawdown circuit-breakers — fractions of the RUNNING account (logic wired in Phase 3).
    per_city_drawdown_pct: float = 0.25   # halt ONE city at 25% drawdown from its peak
    account_drawdown_pct: float = 0.50    # halt ALL cities at 50% account drawdown from peak


@dataclass(frozen=True)
class UICfg:
    refresh_hz: int
    theme: str


@dataclass(frozen=True)
class Config:
    mode: Mode
    station: str                       # legacy scalar (== markets[0].station); kept for back-compat
    local_tz: str                      # legacy scalar (== markets[0].local_tz); kept for back-compat
    markets: tuple[MarketCfg, ...]     # the cities this process trades (always >= 1)
    paths: Paths
    model: ModelCfg
    strategy: StrategyCfg
    execution: ExecutionCfg
    kalshi: KalshiCfg
    data: DataCfg
    scheduler: SchedulerCfg
    risk: RiskCfg
    ui: UICfg

    def is_live(self) -> bool:
        return self.mode == "live"

    def resolved_asos_source(self) -> str:
        """Resolve `auto` -> `synoptic` if token present, `metar_substitute` otherwise.

        `metar_substitute` is the real-time fallback when no Synoptic key is configured;
        IEM mode (24-48h lag) is only the result when the user explicitly requests it.
        """
        if self.data.asos_source != "auto":
            return self.data.asos_source
        return "synoptic" if os.environ.get(self.data.synoptic_token_env, "").strip() \
                          else "metar_substitute"

    def synoptic_token(self) -> str | None:
        token = os.environ.get(self.data.synoptic_token_env, "").strip()
        return token or None


def _abs(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def load_config(path: Path | str | None = None) -> Config:
    """Load YAML config. Resolution order: arg > $WEATHER_ALPHA_CONFIG > default."""
    if path is None:
        env = os.environ.get("WEATHER_ALPHA_CONFIG")
        path = Path(env) if env else DEFAULT_CONFIG_PATH
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    paths = Paths(
        data_dir=_abs(raw["paths"]["data_dir"]),
        model_dir=_abs(raw["paths"]["model_dir"]),
        live_log=_abs(raw["paths"]["live_log"]),
        positions_snapshot=_abs(raw["paths"]["positions_snapshot"]),
        notebook_v3=_abs(raw["paths"]["notebook_v3"]),
        logs_dir=_abs(raw["paths"]["logs_dir"]),
        kill_switch=_abs(raw["paths"]["kill_switch"]),
        scheduler_state=_abs(raw["paths"].get("scheduler_state", "data/scheduler_state.json")),
    )
    model = ModelCfg(**raw["model"])
    throttle = RegimeThrottle(**raw["strategy"]["regime_throttle"])
    wing = WingCfg(**raw["strategy"].get("wing", {}))
    strategy = StrategyCfg(
        bankroll_usd=float(raw["strategy"]["bankroll_usd"]),
        kelly_fraction=float(raw["strategy"]["kelly_fraction"]),
        kl_concentration_alpha=float(raw["strategy"]["kl_concentration_alpha"]),
        edge_floor_cents=int(raw["strategy"]["edge_floor_cents"]),
        per_contract_max_pct=float(raw["strategy"]["per_contract_max_pct"]),
        total_exposure_max_pct=float(raw["strategy"]["total_exposure_max_pct"]),
        regime_throttle=throttle,
        name=raw["strategy"].get("name", "joint_kelly"),
        wing=wing,
    )
    # --- markets: multi-market list, with single-market back-compat ----------------
    exec_raw = raw["execution"]
    raw_markets = raw.get("markets")
    if raw_markets:
        markets = tuple(
            MarketCfg(
                name=str(m["name"]),
                event_pattern=str(m["event_pattern"]),
                station=str(m["station"]),
                local_tz=str(m["local_tz"]),
            )
            for m in raw_markets
        )
        # legacy scalars stay populated (== first market) for any not-yet-migrated reader
        legacy_station = raw.get("station") or markets[0].station
        legacy_tz = raw.get("local_tz") or markets[0].local_tz
        event_pattern = exec_raw.get("market_event_pattern") or markets[0].event_pattern
    else:
        # legacy single-market config: synthesize exactly one market from the top-level scalars
        legacy_station = raw["station"]
        legacy_tz = raw["local_tz"]
        event_pattern = exec_raw["market_event_pattern"]
        markets = (
            MarketCfg(
                name=str(raw.get("market_name", legacy_station)),
                event_pattern=event_pattern,
                station=legacy_station,
                local_tz=legacy_tz,
            ),
        )

    execution = ExecutionCfg(
        fill_model=exec_raw["fill_model"],
        slippage_cents=int(exec_raw["slippage_cents"]),
        intraday_refresh_minutes=int(exec_raw["intraday_refresh_minutes"]),
        market_event_pattern=event_pattern,
    )
    kalshi = KalshiCfg(**raw["kalshi"])
    data = DataCfg(**raw["data"])
    scheduler = SchedulerCfg(**raw["scheduler"])
    risk = RiskCfg(**raw["risk"])
    ui = UICfg(**raw["ui"])

    cfg = Config(
        mode=raw["mode"],
        station=legacy_station,
        local_tz=legacy_tz,
        markets=markets,
        paths=paths,
        model=model,
        strategy=strategy,
        execution=execution,
        kalshi=kalshi,
        data=data,
        scheduler=scheduler,
        risk=risk,
        ui=ui,
    )

    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {cfg.mode!r}")
    _validate_markets(cfg.markets)
    if cfg.is_live():
        _validate_live_credentials(cfg)

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate_live_credentials(cfg: Config) -> None:
    """Hard failure if LIVE mode is selected without Kalshi creds present."""
    key_id = os.environ.get(cfg.kalshi.key_id_env, "").strip()
    key_path_str = os.environ.get(cfg.kalshi.private_key_path_env, "").strip()
    if not key_id or not key_path_str:
        raise RuntimeError(
            f"LIVE mode requires ${cfg.kalshi.key_id_env} and "
            f"${cfg.kalshi.private_key_path_env} to be set in the environment. "
            f"Use mode: paper or set them in .env."
        )
    if not Path(key_path_str).exists():
        raise FileNotFoundError(f"Kalshi private key not found at {key_path_str}")


def _validate_markets(markets: tuple[MarketCfg, ...]) -> None:
    """Fail fast on a malformed market list so the engine can safely trade ANY set of
    US-tz cities. Names + event patterns must be unique (they key per-city state and the
    trades themselves), and every local_tz must be a real IANA zone — a typo there would
    silently mis-time a trade, so we reject it at load."""
    if not markets:
        raise ValueError("config defines no markets")
    names = [m.name for m in markets]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate market names (they key per-city state): {names}")
    patterns = [m.event_pattern for m in markets]
    if len(set(patterns)) != len(patterns):
        raise ValueError(f"duplicate market event_patterns: {patterns}")
    for m in markets:
        try:
            ZoneInfo(m.local_tz)
        except Exception as e:  # surface any tz-db failure as a clear config error
            raise ValueError(
                f"market {m.name!r}: invalid IANA timezone {m.local_tz!r} "
                f"(expected e.g. 'America/Chicago', 'America/New_York', 'America/Phoenix')"
            ) from e
