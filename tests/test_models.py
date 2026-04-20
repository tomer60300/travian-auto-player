"""Tests for Pydantic models."""

import pytest
from datetime import datetime
from pydantic import ValidationError

from travian_api.models.auth import LoginRequest, LoginResponse, AuthStatus, JWTCache
from travian_api.models.common import Coordinates, VillageInfo, ResourceAmount, TroopCount
from travian_api.models.buildings import Building, Resources, QueueItem
from travian_api.models.military import TargetInfo, TroopComposition, ScoutRequest
from travian_api.constants import BuildingType, TribeType


class TestAuthModels:
    """Test authentication models."""
    
    def test_login_request_valid(self):
        """Test valid login request."""
        request = LoginRequest(name="test@example.com", password="password123")
        assert request.name == "test@example.com"
        assert request.password == "password123"
        assert request.w == "1920:1080"
        assert request.mobile_optimizations is False
    
    def test_login_request_empty_fields(self):
        """Test login request with empty fields."""
        with pytest.raises(ValidationError):
            LoginRequest(name="", password="password123")
        
        with pytest.raises(ValidationError):
            LoginRequest(name="test@example.com", password="")
    
    def test_login_response(self):
        """Test login response parsing."""
        response = LoginResponse(redirectTo="/api/v1/auth?code=abc123")
        assert response.redirect_to == "/api/v1/auth?code=abc123"
    
    def test_auth_status(self):
        """Test auth status model."""
        status = AuthStatus(
            is_authenticated=True,
            jwt_token="token123",
            username="test@example.com"
        )
        assert status.is_authenticated is True
        assert not status.is_expired  # No expiry time set
    
    def test_jwt_cache_stale(self):
        """Test JWT cache staleness detection."""
        cache = JWTCache(
            token="token123",
            username="test@example.com",
            server_url="https://server.com"
        )
        
        # Should not be stale immediately
        assert not cache.is_stale(max_age_hours=24)
        
        # Should be stale with 0 max age
        assert cache.is_stale(max_age_hours=0)


class TestCommonModels:
    """Test common data models."""
    
    def test_coordinates_valid(self):
        """Test valid coordinates."""
        coords = Coordinates(x=100, y=-50)
        assert coords.x == 100
        assert coords.y == -50
        assert str(coords) == "(100, -50)"
    
    def test_coordinates_invalid(self):
        """Test invalid coordinates."""
        with pytest.raises(ValidationError):
            Coordinates(x=500, y=0)  # Outside valid range
        
        with pytest.raises(ValidationError):
            Coordinates(x=0, y=-500)  # Outside valid range
    
    def test_coordinates_distance(self):
        """Test coordinate distance calculation."""
        coord1 = Coordinates(x=0, y=0)
        coord2 = Coordinates(x=3, y=4)
        assert coord1.distance_to(coord2) == 5.0  # 3-4-5 triangle
    
    def test_village_info(self):
        """Test village info model."""
        village = VillageInfo(
            id="12345",
            name="Test Village",
            coordinates=Coordinates(x=10, y=20),
            population=500
        )
        assert village.id == "12345"
        assert village.name == "Test Village"
        assert village.population == 500
    
    def test_resource_amount_operations(self):
        """Test resource amount operations."""
        res1 = ResourceAmount(wood=100, clay=200, iron=300, crop=400)
        res2 = ResourceAmount(wood=50, clay=75, iron=25, crop=100)
        
        # Addition
        total = res1 + res2
        assert total.wood == 150
        assert total.clay == 275
        assert total.total() == 1250
        
        # Subtraction
        diff = res1 - res2
        assert diff.wood == 50
        assert diff.clay == 125
        
        # Can afford check
        assert res1.can_afford(res2)
        assert not res2.can_afford(res1)
    
    def test_troop_count(self):
        """Test troop count model."""
        troops = TroopCount(t1=10, t2=5, t3=2)
        assert troops.total() == 17
        assert not troops.is_empty()
        
        empty_troops = TroopCount()
        assert empty_troops.total() == 0
        assert empty_troops.is_empty()


class TestBuildingModels:
    """Test building models."""
    
    def test_building_info(self):
        """Test building model."""
        building = Building(
            slot_id=15,
            gid=BuildingType.MAIN_BUILDING,
            name="Main Building",
            level=5,
        )

        assert building.slot_id == 15
        assert building.gid == BuildingType.MAIN_BUILDING
        assert building.level == 5
        assert building.name == "Main Building"
    
    def test_building_invalid_slot(self):
        """Test building with invalid slot."""
        with pytest.raises(ValidationError):
            Building(
                slot_id=50,  # Invalid slot (must be 1-40)
                gid=BuildingType.MAIN_BUILDING,
                name="Main Building",
                level=1,
            )

    def test_resources_model(self):
        """Test resources model."""
        resources = Resources(
            lumber=1000,
            clay=1500,
            iron=800,
            crop=2000,
        )

        assert resources.lumber == 1000
        assert resources.clay == 1500
        assert resources.iron == 800
        assert resources.crop == 2000


class TestMilitaryModels:
    """Test military models."""
    
    def test_target_info(self):
        """Test target info model."""
        target = TargetInfo(x=10, y=20, village_id=123, village_name="Target Village")

        assert target.x == 10
        assert target.y == 20
        assert target.village_id == 123
        assert target.village_name == "Target Village"

    def test_troop_composition(self):
        """Test troop composition model."""
        composition = TroopComposition()
        assert composition.total() == 0

        composition = TroopComposition(t1=10, t4=5)
        assert composition.total() == 15
        assert composition.t1 == 10
        assert composition.t4 == 5

    def test_scout_request(self):
        """Test scout request model."""
        target = TargetInfo(x=50, y=100, village_id=456)
        request = ScoutRequest(target=target, scouts=3)

        assert request.target == target
        assert request.scouts == 3

    def test_scout_request_invalid_count(self):
        """Test scout request with invalid count."""
        target = TargetInfo(x=50, y=100, village_id=456)

        with pytest.raises(ValidationError):
            ScoutRequest(target=target, scouts=0)


class TestValidationEdgeCases:
    """Test edge cases and validation."""
    
    def test_empty_strings(self):
        """Test handling of empty strings."""
        with pytest.raises(ValidationError):
            VillageInfo(id="", name="Test", coordinates=Coordinates(x=0, y=0))
    
    def test_negative_values(self):
        """Test handling of negative values where not allowed."""
        with pytest.raises(ValidationError):
            ResourceAmount(wood=-100, clay=200, iron=300, crop=400)
    
    def test_extreme_coordinates(self):
        """Test extreme coordinate values."""
        # Valid boundary values
        Coordinates(x=-400, y=400)
        Coordinates(x=400, y=-400)
        
        # Invalid boundary values
        with pytest.raises(ValidationError):
            Coordinates(x=-401, y=0)
        
        with pytest.raises(ValidationError):
            Coordinates(x=0, y=401)
    
    def test_building_level_limits(self):
        """Test building level validation."""
        # Valid levels
        Building(slot_id=1, gid=BuildingType.WOODCUTTER, name="Woodcutter", level=0)
        Building(slot_id=1, gid=BuildingType.WOODCUTTER, name="Woodcutter", level=20)

        # Invalid level
        with pytest.raises(ValidationError):
            Building(slot_id=1, gid=BuildingType.WOODCUTTER, name="Woodcutter", level=-1)