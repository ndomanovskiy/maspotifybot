import asyncio
import logging
from aiohttp import web
import tekore as tk

from app.config import settings

log = logging.getLogger(__name__)

_spotify: tk.Spotify | None = None
_token: tk.Token | None = None
_credentials: tk.Credentials | None = None


def _get_credentials() -> tk.Credentials:
    global _credentials
    if _credentials is None:
        _credentials = tk.Credentials(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=settings.spotify_redirect_uri,
        )
    return _credentials


async def start_oauth() -> str:
    """Return the OAuth URL for the user to visit."""
    creds = _get_credentials()
    scopes = tk.Scope(
        tk.scope.user_read_currently_playing,
        tk.scope.user_read_playback_state,
        tk.scope.user_modify_playback_state,
        tk.scope.playlist_modify_public,
        tk.scope.playlist_modify_private,
        tk.scope.playlist_read_collaborative,
    )
    url = creds.user_authorisation_url(scope=scopes)
    return url


async def exchange_code(code: str) -> tk.Token:
    """Exchange authorization code for token."""
    global _token, _spotify
    creds = _get_credentials()
    _token = creds.request_user_token(code)
    _spotify = tk.Spotify(_token.access_token, asynchronous=True)
    log.info("Spotify authenticated successfully")
    return _token


async def get_spotify() -> tk.Spotify:
    """Get authenticated Spotify client, refreshing token if needed."""
    global _token, _spotify
    if _token is None or _spotify is None:
        raise RuntimeError("Spotify not authenticated. Use /auth to connect.")

    if _token.is_expiring:
        creds = _get_credentials()
        _token = creds.refresh(_token)
        _spotify = tk.Spotify(_token.access_token, asynchronous=True)
        log.info("Spotify token refreshed")

    return _spotify


async def save_token_to_db(pool, token: tk.Token):
    """Persist refresh token to database."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO spotify_tokens (refresh_token, access_token, expires_at)
            VALUES ($1, $2, NOW() + INTERVAL '1 hour')
            ON CONFLICT (id) DO UPDATE
            SET refresh_token = $1, access_token = $2, expires_at = NOW() + INTERVAL '1 hour'
            """,
            str(token.refresh_token),
            str(token.access_token),
        )


async def load_token_from_db(pool) -> tk.Token | None:
    """Load saved refresh token from database."""
    global _token, _spotify
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT refresh_token FROM spotify_tokens ORDER BY id DESC LIMIT 1")
    if row is None:
        return None

    creds = _get_credentials()
    try:
        _token = creds.refresh_user_token(row["refresh_token"])
        _spotify = tk.Spotify(_token.access_token, asynchronous=True)
        log.info("Spotify token loaded from DB and refreshed")
        return _token
    except Exception as e:
        log.error(f"Failed to refresh saved token: {e}")
        return None


async def run_oauth_callback_server(on_code):
    """Run a temporary HTTP server to catch the OAuth callback."""
    code_future = asyncio.get_event_loop().create_future()

    async def handle_callback(request):
        code = request.query.get("code")
        if code:
            if not code_future.done():
                code_future.set_result(code)
            return web.Response(text="Auth successful! You can close this tab.")
        return web.Response(text="No code received", status=400)

    app = web.Application()
    app.router.add_get("/callback", handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8888)
    await site.start()
    log.info("OAuth callback server started on http://localhost:8888/callback")

    try:
        code = await asyncio.wait_for(code_future, timeout=300)
        await on_code(code)
    except asyncio.TimeoutError:
        log.warning("OAuth timeout — no callback received in 5 minutes")
    finally:
        await runner.cleanup()
