"""Runtime configuration, read from ``LOGSCOPE_*`` environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOGSCOPE_")

    # The write-ahead log, segments and manifest live here. One process at a
    # time: a lock file refuses a second one.
    data_dir: Path = Path("var/data")
    # The built React UI (ui/dist). The API serves it when the directory
    # exists; in development Vite serves it instead.
    ui_dir: Path = Path("ui/dist")

    # Flush the memtable to segments at this many events, or when its oldest
    # event has waited this long, whichever comes first.
    flush_events: int = 50_000
    flush_interval_seconds: float = 60.0

    # Ingest limits. A request over the first two is refused whole (413); a
    # line over the third is rejected on its own.
    max_body_bytes: int = 8 * 1024 * 1024
    max_batch_events: int = 10_000
    max_line_bytes: int = 64 * 1024
