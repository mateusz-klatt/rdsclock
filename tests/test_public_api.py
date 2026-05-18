"""Top-level package exports define the supported public API."""

import inspect

import rdsclock


def test_public_api_exports_supported_names():
    assert rdsclock.__all__ == [
        "__version__",
        "decode_file",
        "decode_iq",
        "DecodeResult",
        "ClockTime",
        "StationInfo",
        "TimeConsensus",
        "SubSecondEstimate",
    ]

    assert isinstance(rdsclock.__version__, str)
    assert inspect.isfunction(rdsclock.decode_file)
    assert inspect.isfunction(rdsclock.decode_iq)
    assert inspect.isclass(rdsclock.DecodeResult)
    assert inspect.isclass(rdsclock.ClockTime)
    assert inspect.isclass(rdsclock.StationInfo)
    assert inspect.isclass(rdsclock.TimeConsensus)
    assert inspect.isclass(rdsclock.SubSecondEstimate)
