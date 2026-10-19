"""MinerU backends and output normalization."""

from .cloud import MineruCloudBackend
from .local import MineruLocalBackend
from .self_host import MineruSelfHostBackend

__all__ = ["MineruCloudBackend", "MineruLocalBackend", "MineruSelfHostBackend"]
