import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "toolchain_fetch", Path(__file__).resolve().parents[1] / "tools" / "toolchain" / "fetch.py"
)
fetch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetch)

SUPPORTED = [{"system": "Darwin", "machine": "arm64"}, {"system": "Linux", "machine": "x86_64"}]


def test_a_supported_platform_passes() -> None:
    fetch.check_platform(SUPPORTED, "Linux", "x86_64")


@pytest.mark.parametrize(("system", "machine"), [("Darwin", "x86_64"), ("Windows", "AMD64")])
def test_an_unsupported_platform_is_refused_with_a_pointer_to_the_readme(
    system: str, machine: str
) -> None:
    with pytest.raises(fetch.FetchError, match="README.md"):
        fetch.check_platform(SUPPORTED, system, machine)


def test_bytes_that_do_not_match_the_pin_are_refused() -> None:
    pinned = fetch.sha256_digest(b"expected")
    with pytest.raises(fetch.FetchError, match="was pinned"):
        fetch.verify(b"tampered", pinned, "wheel")


def _manifest(*titles: str) -> dict:
    return {
        "layers": [
            {
                "mediaType": fetch.WHEEL_TYPE,
                "digest": f"sha256:{i}",
                "annotations": {fetch.TITLE: t},
            }
            for i, t in enumerate(titles)
        ]
        + [{"mediaType": "application/json", "digest": "sha256:x", "annotations": {}}]
    }


def test_only_the_expected_wheels_are_selected() -> None:
    layers = fetch.wheel_layers(
        _manifest("a-1-py3-none-any.whl", "b-1-py3-none-any.whl"), ["b-1-py3-none-any.whl"]
    )
    assert list(layers) == ["b-1-py3-none-any.whl"]


def test_a_missing_wheel_is_refused() -> None:
    with pytest.raises(fetch.FetchError, match="no layer"):
        fetch.wheel_layers(_manifest("a-1-py3-none-any.whl"), ["b-1-py3-none-any.whl"])


def test_a_wheel_title_that_is_a_path_is_refused() -> None:
    with pytest.raises(fetch.FetchError, match="not a file name"):
        fetch.wheel_layers(_manifest("../escape.whl"), ["../escape.whl"])
