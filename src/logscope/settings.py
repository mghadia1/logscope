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

    # Events older than this are deleted (whole segments, in the background)
    # and refused at ingest. 0 keeps everything.
    retention_hours: float = 0
    # An hour with this many segments is merged into one.
    merge_threshold: int = 8
    # How often retention and merging run.
    maintenance_interval_seconds: float = 30.0

    # Live tail: a comment is sent this often, so an idle connection stays
    # open and a closed one is noticed; a subscriber holds at most this many
    # unsent batches before dropping (and saying how many events it dropped).
    tail_heartbeat_seconds: float = 15.0
    tail_queue_batches: int = 1000

    # Ingest limits. A request over the first two is refused whole (413); a
    # line over the third is rejected on its own.
    max_body_bytes: int = 8 * 1024 * 1024
    max_batch_events: int = 10_000
    max_line_bytes: int = 64 * 1024
