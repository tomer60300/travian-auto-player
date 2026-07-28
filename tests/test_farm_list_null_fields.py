"""Regression: Travian returns null distance/ownerVillage; model must tolerate it."""

from travian_api.models.farm_list import FarmList


def test_farm_list_tolerates_null_distance_and_owner_village():
    payload = {
        "id": 1,
        "name": "Natar-1-Clubs",
        "ownerVillage": None,
        "slots": [
            {"id": 10, "distance": None, "target": {"x": 26, "y": 81}},
        ],
    }

    fl = FarmList.model_validate(payload)

    assert fl.owner_village.id == 0
    assert fl.slots[0].distance == 0.0
