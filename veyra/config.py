from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "VEYRA_", "env_file": ".env"}

    fernet_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./veyra.db"
    host: str = "127.0.0.1"
    port: int = 5678

    # Game defaults
    default_delay: float = 2.5
    respawn_wait: int = 30
    refresh_every: int = 10

    # Dev account for server-side scraping (loot DB, etc.)
    dev_email: str = ""
    dev_password: str = ""

    # Secret key to access /docs — use as /docs?key=YOUR_KEY
    docs_key: str = ""


settings = Settings()
