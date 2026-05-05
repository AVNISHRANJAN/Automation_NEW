"""
utils/url_normalizer.py — URL normalization and deduplication utilities.
"""

from urllib.parse import urlparse, urlunparse, urljoin


def normalize(url: str) -> str:
    """Strip fragments, normalize trailing slashes, lowercase scheme+host."""
    try:
        p = urlparse(url.strip())
        normalized = urlunparse((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            p.params,
            p.query,
            "",          # strip fragment
        ))
        return normalized
    except Exception:
        return url.strip()


def is_same_domain(url: str, base_url: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base_url).netloc
    except Exception:
        return False


def is_valid_http(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def resolve(base: str, href: str) -> str:
    """Resolve a potentially relative href against a base URL."""
    try:
        return urljoin(base, href)
    except Exception:
        return href
