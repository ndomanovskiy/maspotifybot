from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    telegram_admin_id: int

    # Spotify
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str = "http://localhost:8888/callback"

    # Database
    database_url: str = "postgresql://maspotify:maspotify@db:5432/maspotify"

    # AI
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Voting
    vote_drop_threshold: int = 2

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
