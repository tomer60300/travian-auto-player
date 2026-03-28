"""Tests for HTML and report parsers."""

import pytest
from datetime import datetime

from travian_api.parsers.html_parser import (
    parse_buildings_from_dorf,
    parse_resources_from_page,
    parse_construction_queue,
    find_village_info
)
from travian_api.parsers.report_parser import (
    parse_report_list,
    parse_individual_report,
    _parse_timestamp,
    _parse_relative_timestamp
)
from travian_api.utils.checksum import (
    extract_checksum,
    extract_hidden_fields,
    parse_resources_from_script,
    clean_unicode_text
)
from travian_api.exceptions import ParseError, ChecksumError
from travian_api.constants import BuildingType


class TestHTMLParsers:
    """Test HTML parsing functions."""
    
    def test_parse_buildings_dorf1(self):
        """Test parsing resource field buildings."""
        # Mock dorf1.php HTML content
        html_content = '''
        <html>
        <body>
            <a href="/build.php?id=1" data-aid="1" data-gid="1" class="level5">
                <img src="wood.gif" alt="Woodcutter">
            </a>
            <a href="/build.php?id=2" data-aid="2" data-gid="2" class="level3">
                <img src="clay.gif" alt="Clay Pit">
            </a>
            <a href="/build.php?id=18" data-aid="18" data-gid="4" class="level1">
                <img src="crop.gif" alt="Cropland">
            </a>
        </body>
        </html>
        '''
        
        buildings = parse_buildings_from_dorf(html_content, is_dorf1=True)
        
        assert len(buildings) == 3
        assert 1 in buildings
        assert buildings[1].building_type == BuildingType.WOODCUTTER
        assert buildings[1].level == 5
        assert buildings[2].building_type == BuildingType.CLAY_PIT
        assert buildings[2].level == 3
    
    def test_parse_buildings_dorf2(self):
        """Test parsing village buildings."""
        html_content = '''
        <html>
        <body>
            <a href="/build.php?id=19" data-aid="19" data-gid="15" class="level10">
                <img src="main.gif" alt="Main Building">
            </a>
            <a href="/build.php?id=20" data-aid="20" data-gid="16" class="level1">
                <img src="rally.gif" alt="Rally Point">
            </a>
        </body>
        </html>
        '''
        
        buildings = parse_buildings_from_dorf(html_content, is_dorf1=False)
        
        assert len(buildings) == 2
        assert 19 in buildings
        assert buildings[19].building_type == BuildingType.MAIN_BUILDING
        assert buildings[19].level == 10
        assert buildings[20].building_type == BuildingType.RALLY_POINT
        assert buildings[20].level == 1
    
    def test_parse_resources_from_script(self):
        """Test parsing resources from JavaScript."""
        html_content = '''
        <html>
        <head>
            <script>
                var resources = {"1": 1500, "2": 2300, "3": 800, "4": 1200};
                var other_var = 123;
            </script>
        </head>
        </html>
        '''
        
        resources_data = parse_resources_from_script(html_content)
        
        assert resources_data is not None
        assert resources_data["1"] == 1500  # Wood
        assert resources_data["2"] == 2300  # Clay
        assert resources_data["3"] == 800   # Iron
        assert resources_data["4"] == 1200  # Crop
    
    def test_parse_resources_from_page(self):
        """Test complete resource parsing."""
        html_content = '''
        <html>
        <head>
            <script>
                var resources = {"1": 1500, "2": 2300, "3": 800, "4": 1200};
                var warehouse_capacity = 8000;
                var granary_capacity = 10000;
            </script>
        </head>
        </html>
        '''
        
        resources = parse_resources_from_page(html_content)
        
        assert resources.wood == 1500
        assert resources.clay == 2300
        assert resources.iron == 800
        assert resources.crop == 1200
        assert resources.warehouse_capacity == 8000
        assert resources.granary_capacity == 10000
    
    def test_find_village_info(self):
        """Test extracting village information."""
        html_content = '''
        <html>
        <head>
            <title>My Village - Travian</title>
            <script>
                var villageId = "12345";
            </script>
        </head>
        <body>
            <div class="villageName">My Test Village</div>
        </body>
        </html>
        '''
        
        village_id, village_name = find_village_info(html_content)
        
        assert village_id == "12345"
        assert village_name in ["My Village", "My Test Village"]  # Either could be picked
    
    def test_parse_construction_queue_empty(self):
        """Test parsing empty construction queue."""
        html_content = '''
        <html>
        <body>
            <ul class="buildingList">
                <!-- No construction items -->
            </ul>
        </body>
        </html>
        '''
        
        queue = parse_construction_queue(html_content)
        
        assert len(queue.items) == 0
        assert queue.max_parallel == 1


class TestChecksumUtils:
    """Test checksum and form utilities."""
    
    def test_extract_checksum_valid(self):
        """Test extracting valid checksum."""
        html_content = '''
        <html>
        <body>
            <form action="/dorf1.php?id=1&amp;gid=1&amp;action=build&amp;checksum=abc123">
                <input type="submit" value="Build">
            </form>
        </body>
        </html>
        '''
        
        checksum = extract_checksum(html_content)
        assert checksum == "abc123"
    
    def test_extract_checksum_not_found(self):
        """Test checksum extraction when not found."""
        html_content = '<html><body>No checksum here</body></html>'
        
        with pytest.raises(ChecksumError):
            extract_checksum(html_content)
    
    def test_extract_checksum_empty_content(self):
        """Test checksum extraction with empty content."""
        with pytest.raises(ChecksumError):
            extract_checksum("")
    
    def test_extract_hidden_fields(self):
        """Test extracting hidden form fields."""
        html_content = '''
        <html>
        <body>
            <form>
                <input type="hidden" name="checksum" value="abc123">
                <input type="hidden" name="villageId" value="12345">
                <input type="hidden" name="eventType" value="2">
                <input type="text" name="visible" value="skip">
            </form>
        </body>
        </html>
        '''
        
        hidden_fields = extract_hidden_fields(html_content)
        
        assert "checksum" in hidden_fields
        assert hidden_fields["checksum"] == "abc123"
        assert hidden_fields["villageId"] == "12345"
        assert hidden_fields["eventType"] == "2"
        assert "visible" not in hidden_fields  # Should skip non-hidden fields
    
    def test_clean_unicode_text(self):
        """Test cleaning Unicode directional markers."""
        dirty_text = "\u202DHello\u202CWorld\u202D123\u202C"
        clean_text = clean_unicode_text(dirty_text)
        
        assert clean_text == "HelloWorld123"
        
        # Test with whitespace cleanup
        dirty_text = "  \u202D  Hello   World  \u202C  "
        clean_text = clean_unicode_text(dirty_text)
        
        assert clean_text == "Hello World"


class TestReportParsers:
    """Test report parsing functions."""
    
    def test_parse_timestamp_formats(self):
        """Test parsing various timestamp formats."""
        # German format
        timestamp = _parse_timestamp("25.12.2023 14:30:45")
        assert timestamp.day == 25
        assert timestamp.month == 12
        assert timestamp.year == 2023
        assert timestamp.hour == 14
        
        # US format
        timestamp = _parse_timestamp("12/25/2023 02:30:45")
        assert timestamp.month == 12
        assert timestamp.day == 25
        
        # ISO format
        timestamp = _parse_timestamp("2023-12-25 14:30:45")
        assert timestamp.year == 2023
        assert timestamp.month == 12
        assert timestamp.day == 25
    
    def test_parse_relative_timestamp(self):
        """Test parsing relative timestamps."""
        now = datetime.utcnow()
        
        # Hours ago
        timestamp = _parse_relative_timestamp("2 hours ago")
        time_diff = (now - timestamp).total_seconds()
        assert abs(time_diff - 7200) < 60  # Within 1 minute tolerance
        
        # Minutes ago
        timestamp = _parse_relative_timestamp("30 minutes ago")
        time_diff = (now - timestamp).total_seconds()
        assert abs(time_diff - 1800) < 60
        
        # Days ago
        timestamp = _parse_relative_timestamp("1 day ago")
        time_diff = (now - timestamp).total_seconds()
        assert abs(time_diff - 86400) < 60
    
    def test_parse_report_list_empty(self):
        """Test parsing empty report list."""
        html_content = '''
        <html>
        <body>
            <table class="reports">
                <!-- No report rows -->
            </table>
        </body>
        </html>
        '''
        
        report_list = parse_report_list(html_content, page=1)
        
        assert len(report_list.reports) == 0
        assert report_list.page == 1
        assert report_list.per_page == 30
        assert not report_list.has_more
    
    def test_parse_report_list_with_reports(self):
        """Test parsing report list with actual reports."""
        html_content = '''
        <html>
        <body>
            <table class="reports">
                <tr class="report iReport1">
                    <td><input name="ids[]" value="report123" type="checkbox"></td>
                    <td class="title">Scout report from (10|20)</td>
                    <td class="time">25.12.2023 14:30</td>
                </tr>
                <tr class="report iReport4 read">
                    <td><input name="ids[]" value="report456" type="checkbox"></td>
                    <td class="title">Battle report - Attack on (30|40)</td>
                    <td class="time">24.12.2023 12:15</td>
                </tr>
            </table>
        </body>
        </html>
        '''
        
        report_list = parse_report_list(html_content, page=1)
        
        assert len(report_list.reports) == 2
        
        # First report (scout)
        report1 = report_list.reports[0]
        assert report1.id == "report123"
        assert report1.type.value == "scout"
        assert "Scout report" in report1.title
        
        # Second report (attack)
        report2 = report_list.reports[1]
        assert report2.id == "report456"
        assert report2.type.value == "attack"
        assert report2.status.value == "read"  # Has 'read' class


class TestParsingErrorHandling:
    """Test error handling in parsers."""
    
    def test_parse_malformed_html(self):
        """Test handling of malformed HTML."""
        malformed_html = "<html><body><div>Unclosed div<body></html>"
        
        # Should not raise exception, parsers should be robust
        try:
            buildings = parse_buildings_from_dorf(malformed_html)
            assert isinstance(buildings, dict)
        except ParseError:
            # ParseError is acceptable for malformed content
            pass
    
    def test_parse_empty_html(self):
        """Test handling of empty HTML."""
        empty_html = ""
        
        with pytest.raises(ParseError):
            parse_buildings_from_dorf(empty_html)
    
    def test_parse_html_no_data(self):
        """Test parsing HTML with no relevant data."""
        html_no_data = "<html><body><p>No buildings here</p></body></html>"
        
        buildings = parse_buildings_from_dorf(html_no_data)
        assert len(buildings) == 0  # Should return empty dict, not fail
    
    def test_checksum_extraction_edge_cases(self):
        """Test checksum extraction edge cases."""
        # Multiple checksums - should return first
        html_multiple = '''
        <html>
        <body>
            <a href="?checksum=abc123">Link 1</a>
            <a href="?checksum=def456">Link 2</a>
        </body>
        </html>
        '''
        
        checksum = extract_checksum(html_multiple)
        assert checksum == "abc123"  # Should return first match
        
        # Checksum in different case
        html_case = '<html><body><a href="?CHECKSUM=ABC123">Link</a></body></html>'
        
        checksum = extract_checksum(html_case)
        assert checksum == "abc123"  # Should normalize to lowercase
    
    def test_unicode_text_edge_cases(self):
        """Test Unicode text cleaning edge cases."""
        # Empty string
        assert clean_unicode_text("") == ""
        
        # None input
        assert clean_unicode_text(None) == None
        
        # Only Unicode markers
        assert clean_unicode_text("\u202D\u202C") == ""
        
        # Mixed content
        mixed = "Normal text \u202D with markers \u202C and\u200E more\u200F text"
        cleaned = clean_unicode_text(mixed)
        assert cleaned == "Normal text with markers and more text"