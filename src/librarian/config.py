"""Configuration: TOML file, overridden by LIBRARIAN_* env vars, overridden by CLI flags."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    _toml = None


@dataclass
class Config:
    sources: Path = Path(r"D:\Readings\Sources")
    data: Path = Path(r"D:\Readings\librarian")
    legacy: Optional[Path] = Path(r"D:\Readings\indexes\lra")
    manifest: Optional[Path] = Path(r"F:\repos\lra-source-profiles\data\source-manifest.yaml")
    publish: Optional[Path] = None

    @property
    def db_path(self) -> Path:
        return self.data / "librarian.sqlite"

    @property
    def objects(self) -> Path:
        return self.data / "objects"

    @property
    def reports(self) -> Path:
        return self.data / "reports"


_LINE = re.compile(r"""^\s*([A-Za-z_]\w*)\s*=\s*(?:'([^']*)'|"([^"]*)")\s*(?:#.*)?$""")


def _read_toml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if _toml is not None:
        return _toml.loads(text)
    # Minimal fallback: flat key = 'string' pairs only.
    out = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def load(config_file: Optional[Path] = None, **overrides) -> Config:
    cfg = Config()
    candidates = [config_file] if config_file else [Path("config/local.toml")]
    for c in candidates:
        if c and c.is_file():
            for k, v in _read_toml(c).items():
                if hasattr(cfg, k):
                    setattr(cfg, k, Path(v) if v else None)
    for f in fields(cfg):
        env = os.environ.get(f"LIBRARIAN_{f.name.upper()}")
        if env is not None:
            setattr(cfg, f.name, Path(env) if env else None)
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, Path(v))
    return cfg
