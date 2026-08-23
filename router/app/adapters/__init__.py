from .wordpress import WordPressAdapter

ADAPTERS = {
    "wordpress": WordPressAdapter(),
}


def get_adapter(name: str):
    if name not in ADAPTERS:
        raise KeyError(f"no adapter registered for {name!r}")
    return ADAPTERS[name]
