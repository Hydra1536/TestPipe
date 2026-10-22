import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    groq_model: str = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
    # Fallback chain: each Groq model has its own free-tier quota, so when one hits its
    # daily token cap the next one takes over. GROQ_MODEL (if set) always goes first.
    groq_models: list[str] = field(
        default_factory=lambda: list(dict.fromkeys(
            [os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"]
            + [m.strip() for m in os.getenv(
                "GROQ_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]
        ))
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
        ]
    )
    headless: bool = _bool("HEADLESS", True)
    nav_timeout_ms: int = int(os.getenv("NAV_TIMEOUT_MS", "45000"))
    step_timeout_ms: int = int(os.getenv("STEP_TIMEOUT_MS", "5000"))
    # Max wait for an SPA to render something interactive after navigation. A heavy app on
    # a slow link can take 40s+ on a cold cache (assets are disk-cached after the first load).
    render_timeout_ms: int = int(os.getenv("RENDER_TIMEOUT_MS", "60000"))
    # Parallel browsers during a test run.
    run_workers: int = int(os.getenv("RUN_WORKERS", "4"))
    # Show a real browser window while running tests, so the user can watch each test.
    # When on, tests run one at a time (a single visible tab) and a little slower.
    run_headful: bool = _bool("RUN_HEADFUL", True)
    run_slow_mo_ms: int = int(os.getenv("RUN_SLOW_MO_MS", "300"))
    # Resource types aborted during test runs to speed up page loads. Add "image" for
    # more speed, at the risk of false failures on image visibility checks.
    block_resources: frozenset[str] = frozenset(
        r.strip() for r in os.getenv("BLOCK_RESOURCES", "font,media").split(",") if r.strip()
    )
    # Headless Chromium's default UA contains "HeadlessChrome", which bot filters block.
    user_agent: str = os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )

    data_dir: Path = BASE_DIR / "data"
    static_dir: Path = BASE_DIR / "data" / "static"
    screenshots_dir: Path = BASE_DIR / "data" / "static" / "screenshots"
    auth_state_file: Path = BASE_DIR / "data" / "auth_state.json"


settings = Settings()
settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
