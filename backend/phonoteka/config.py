"""Explicit deployment configuration. Music writes are disabled by default."""

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit


def boolean(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    work_root: Path
    source_root: Path
    publication_root: Path
    storage_guard_dir: Path
    app_url: str = "https://localhost"
    owner_username: str = "owner"
    owner_password_file: Path | None = None
    static_dir: Path | None = None
    read_only: bool = True
    storage_verified: bool = False
    instance_id: str = ""
    allow_insecure_localhost: bool = False
    file_workers: int = 2
    io_threads: int = 2
    session_hours: int = 12
    provider_daily_limit: int = 2500
    external_metadata: bool = False
    external_fingerprints: bool = False
    theaudiodb_enabled: bool = False
    theaudiodb_artwork: bool = False
    discogs_enabled: bool = False
    discogs_token_file: Path | None = None
    musicbrainz_host: str = "musicbrainz.org"
    inference_url: str = ""
    inference_secret_file: Path | None = None

    def __post_init__(self):
        if self.musicbrainz_host not in {"musicbrainz.org", "beta.musicbrainz.org"}:
            raise ValueError("MUSICBRAINZ_HOST must be an official MusicBrainz endpoint")

    @classmethod
    def from_env(cls) -> "Settings":
        def path(name, default):
            return Path(os.environ.get(name, default))
        return cls(
            state_dir=path("STATE_DIR", "/state"),
            work_root=path("WORK_ROOT", "/work"),
            source_root=path("SOURCE_ROOT", "/original"),
            publication_root=path("PUBLICATION_ROOT", "/publication"),
            storage_guard_dir=path("STORAGE_GUARD_DIR", "/storage-guard"),
            app_url=os.environ.get("APP_URL", "https://localhost").rstrip("/"),
            owner_username=os.environ.get("OWNER_USERNAME", "owner"),
            owner_password_file=path("OWNER_PASSWORD_FILE", "/run/secrets/owner_password"),
            static_dir=path("STATIC_DIR", "/app/frontend/dist"),
            read_only=boolean("READ_ONLY", True),
            storage_verified=boolean("STORAGE_VERIFIED"),
            instance_id=os.environ.get("INSTANCE_ID", ""),
            allow_insecure_localhost=boolean("ALLOW_INSECURE_LOCALHOST"),
            file_workers=max(1, min(int(os.environ.get("FILE_WORKERS", "2")), 8)),
            inference_url=os.environ.get("INFERENCE_URL", "").rstrip("/"),
            inference_secret_file=path("INFERENCE_SECRET_FILE", "/run/secrets/inference_key"),
            external_metadata=boolean("EXTERNAL_METADATA", False),
            external_fingerprints=boolean("EXTERNAL_FINGERPRINTS", False),
            theaudiodb_enabled=boolean('THEAUDIODB_ENABLED',False),
            theaudiodb_artwork=boolean('THEAUDIODB_ARTWORK',False),
            discogs_enabled=boolean('DISCOGS_ENABLED',False),
            discogs_token_file=Path(os.environ['DISCOGS_TOKEN_FILE']) if os.environ.get('DISCOGS_TOKEN_FILE') else None,
            musicbrainz_host=os.environ.get("MUSICBRAINZ_HOST", "musicbrainz.org"),
            provider_daily_limit=max(1,min(int(os.environ.get("PROVIDER_DAILY_LIMIT","2500")),10000)),
            io_threads=max(1, min(int(os.environ.get("IO_THREADS", "2")), 8)),
        )

    @property
    def roots(self) -> dict:
        return {"work": self.work_root, "source": self.source_root,
                "publication": self.publication_root}

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.app_url).scheme == "https"

    @property
    def writable(self) -> bool:
        return not self.read_only and self.storage_verified and bool(self.instance_id)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "phonoteka.sqlite3"

    def validate(self) -> None:
        url = urlsplit(self.app_url)
        local = url.hostname in {"localhost", "127.0.0.1", "::1", "testserver"}
        if url.scheme != "https" and not (
            url.scheme == "http" and self.allow_insecure_localhost and local
        ):
            raise ValueError("APP_URL requires HTTPS; HTTP is allowed only for explicit localhost development")
        if not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError("APP_URL must be an origin without credentials, path or query")
        if not 1 <= len(self.owner_username) <= 80:
            raise ValueError("Invalid OWNER_USERNAME")
        roots = [self.work_root.resolve(), self.source_root.resolve(), self.publication_root.resolve(), self.state_dir.resolve()]
        for i, first in enumerate(roots):
            for second in roots[i + 1:]:
                if first == second or first in second.parents or second in first.parents:
                    raise ValueError("Original, work, publication and state roots must be disjoint")
