"""
Cognetra 3.0 — Server Connection Health Probe
"""
import logging
import requests
from typing import Tuple

logger = logging.getLogger("cognetra.connection")


def check_server_health(server_url: str, timeout: int = 10) -> Tuple[bool, str]:
    """
    Probe the server /health endpoint.
    Returns (is_healthy: bool, message: str)
    """
    try:
        url = f"{server_url.rstrip('/')}/health"
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok":
                return True, f"Server OK: {data.get('server', 'cognetra')} v{data.get('version', '?')}"
        return False, f"Unexpected response: HTTP {resp.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused — is the server running?"
    except requests.exceptions.Timeout:
        return False, f"Server health check timed out after {timeout}s"
    except Exception as e:
        return False, f"Health check error: {e}"
