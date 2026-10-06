"""Runtime configuration, read from ``LOGSCOPE_*`` environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOGSCOPE_")

    # Where the log and its segments live. Unused until Milestone 1.
    data_dir: Path = Path("var/data")
    # The built React UI (ui/dist). The API serves it when the directory
    # exists; in development Vite serves it instead.
    ui_dir: Path = Path("ui/dist")
