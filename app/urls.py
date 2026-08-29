import re
from urllib.parse import unquote, urlsplit

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,199}$")


def parse_profile_url(value: str) -> tuple[str, str]:
    """Return the public identifier and a canonical URL."""
    try:
        url = urlsplit(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid LinkedIn profile URL") from exc

    try:
        host = (url.hostname or "").lower().rstrip(".")
        port = url.port
    except ValueError as exc:
        raise ValueError("invalid LinkedIn profile URL") from exc
    if (
        url.scheme != "https"
        or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
        or url.username
        or url.password
        or port
    ):
        raise ValueError("use an HTTPS linkedin.com profile URL")

    parts = [unquote(part) for part in url.path.split("/") if part]
    if (
        len(parts) != 2
        or parts[0].lower() != "in"
        or not _IDENTIFIER.fullmatch(parts[1])
    ):
        raise ValueError("URL must match https://www.linkedin.com/in/<identifier>")

    identifier = parts[1]
    return identifier, f"https://www.linkedin.com/in/{identifier}/"
