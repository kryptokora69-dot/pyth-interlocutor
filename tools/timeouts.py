"""Hard timeouts via daemon threads (a hung call can't block Ctrl+C or interpreter exit)."""
import threading


class HardTimeout(Exception):
    pass


def run_with_timeout(fn, timeout, *args, **kwargs):
    box = {}

    def work():
        try:
            box["v"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa
            box["e"] = e
    t = threading.Thread(target=work, daemon=True, name=f"timeout:{getattr(fn, '__name__', 'fn')}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise HardTimeout(f"{getattr(fn, '__name__', 'call')} exceeded {timeout}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")
