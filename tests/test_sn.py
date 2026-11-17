import pytest

from ota_agent.errors import ValidationError
from ota_agent.sn import canonical, change


def test_remove_middle_splits_range_and_keeps_leading_zeroes():
    assert change(["00772633000001-00772633000005"], ["00772633000003"], remove=True) == [
        "00772633000001-00772633000002",
        "00772633000004-00772633000005",
    ]


def test_add_is_idempotent_and_coalesces_overlaps():
    assert change(["00772633000001-00772633000005"], ["00772633000003", "00772633000004-00772633000008"]) == [
        "00772633000001-00772633000008"
    ]


def test_special_sn_is_preserved_as_string():
    assert change([], ["801625490773333"]) == ["801625490773333"]
    assert change(["801625490773333"], ["801625490773334"], remove=True) == ["801625490773333"]


@pytest.mark.parametrize(
    "value",
    [
        "ABC",
        "00772633000005-00772633000001",
        "00772633000001-00772634000002",
        "X" * 23,
        801625490773333,
        "801625490773333-801625490773334",
        " 801625490773333",
    ],
)
def test_invalid_sn_is_rejected(value):
    with pytest.raises(ValidationError):
        canonical([value])


def test_large_range_does_not_expand():
    assert change(["ABCDEFGH000000-ABCDEFGH999999"], ["ABCDEFGH500000"], remove=True) == [
        "ABCDEFGH000000-ABCDEFGH499999",
        "ABCDEFGH500001-ABCDEFGH999999",
    ]
