"""Container ASGI factory; reads only explicitly injected process environment."""

from .api import create_api
from .config import load_settings
from .environment import apply_environment, read_environment


def create():
    return create_api(apply_environment(load_settings(), read_environment()))
