import re
from importlib.metadata import metadata, requires
from pathlib import Path


def test_metadata_matches_spec() -> None:
    meta = metadata("headgate")
    assert meta["License-Expression"] == "MIT"
    assert meta["Requires-Python"] == ">=3.14"
    assert "Khaled Elsayed" in meta["Author-email"]
    reqs = [re.sub(r"\[[^\]]*\]", "", r.lower()).replace(" ", "") for r in requires("headgate") or []]
    for needed in ("httpx2>=2.13.1", "msgspec", "hishel", "aiolimiter"):
        assert any(r.startswith(needed) for r in reqs), needed
    for banned in ("httpx>=", "httpx[", "cryptography", "pyjwt", "aioftp"):
        assert not any(r.startswith(banned) for r in reqs), banned
    assert any("extra=='curl'" in r and r.startswith("curl-cffi") for r in reqs)
    assert any("extra=='pydantic'" in r and r.startswith("pydantic") for r in reqs)


def test_the_package_is_marked_typed() -> None:
    import headgate

    assert (Path(headgate.__file__).parent / "py.typed").is_file()
