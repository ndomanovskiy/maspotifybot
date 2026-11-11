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

    # Last.fm
    lastfm_api_key: str = ""
    lastfm_shared_secret: str = ""

    # Voting
    vote_drop_threshold: int = 2

    # A key the bot no longer uses (OPENAI_API_KEY) may stay in .env without stopping the start
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
