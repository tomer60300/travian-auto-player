"""Utility functions for extracting checksums and form data."""

import re
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup

from ..exceptions import ChecksumError, ParseError
from ..logging_config import get_logger

logger = get_logger(__name__)


def extract_checksum(html_content: str) -> str:
    """
    Extract 6-character hex checksum from HTML content.
    
    Args:
        html_content: HTML page content
        
    Returns:
        Extracted checksum
        
    Raises:
        ChecksumError: If checksum cannot be found
    """
    if not html_content:
        raise ChecksumError("HTML content is empty")
    
    # Pattern matches checksum=XXXXXX where X is hex digit
    pattern = r'checksum=([a-f0-9]{6})'
    
    match = re.search(pattern, html_content, re.IGNORECASE)
    if not match:
        logger.error(f"Failed to find checksum in HTML content (length: {len(html_content)})")
        raise ChecksumError("Checksum not found in HTML content")
    
    checksum = match.group(1).lower()
    logger.debug(f"Extracted checksum: {checksum}")
    return checksum


def find_action_url(html_content: str, form_selector: str = "form") -> Optional[str]:
    """
    Find form action URL from HTML content.
    
    Args:
        html_content: HTML page content
        form_selector: CSS selector for the form
        
    Returns:
        Form action URL or None if not found
    """
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        form = soup.select_one(form_selector)
        
        if form and form.get('action'):
            action = form['action']
            logger.debug(f"Found form action: {action}")
            return action
            
    except Exception as e:
        logger.warning(f"Error parsing form action: {e}")
    
    return None


def extract_hidden_fields(html_content: str, form_selector: str = "form") -> Dict[str, str]:
    """
    Extract all hidden input fields from a form.
    
    Args:
        html_content: HTML page content
        form_selector: CSS selector for the form
        
    Returns:
        Dictionary of field names to values
        
    Raises:
        ParseError: If HTML cannot be parsed
    """
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        form = soup.select_one(form_selector)
        
        if not form:
            raise ParseError(f"Form not found with selector: {form_selector}")
        
        hidden_fields = {}
        
        # Find all hidden input fields
        hidden_inputs = form.find_all('input', type='hidden')
        
        for input_field in hidden_inputs:
            name = input_field.get('name')
            value = input_field.get('value', '')
            
            if name:
                hidden_fields[name] = value
                logger.debug(f"Found hidden field: {name} = {value}")
        
        logger.debug(f"Extracted {len(hidden_fields)} hidden fields")
        return hidden_fields
        
    except Exception as e:
        logger.error(f"Error extracting hidden fields: {e}")
        raise ParseError(f"Failed to extract hidden fields: {e}")


def extract_onclick_checksum(html_content: str, button_selector: str = "button[onclick*='checksum']") -> Optional[str]:
    """
    Extract checksum from button onclick attribute.
    
    Some Travian pages inject checksums via JavaScript onclick handlers.
    
    Args:
        html_content: HTML page content
        button_selector: CSS selector for button with checksum
        
    Returns:
        Extracted checksum or None if not found
    """
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        button = soup.select_one(button_selector)
        
        if button and button.get('onclick'):
            onclick = button['onclick']
            
            # Look for checksum in onclick handler
            match = re.search(r'checksum[\'"\s]*[:=]\s*[\'"]([a-f0-9]{6})[\'"]', onclick, re.IGNORECASE)
            if match:
                checksum = match.group(1).lower()
                logger.debug(f"Extracted checksum from onclick: {checksum}")
                return checksum
        
    except Exception as e:
        logger.warning(f"Error extracting onclick checksum: {e}")
    
    return None


def parse_resources_from_script(html_content: str) -> Optional[Dict[str, int]]:
    """
    Parse resources from inline JavaScript.
    
    Travian pages often contain resource data in format:
    var resources = {"1": 1234, "2": 5678, "3": 9012, "4": 3456};
    
    Args:
        html_content: HTML page content
        
    Returns:
        Dictionary mapping resource IDs to amounts, or None if not found
    """
    try:
        # Look for resources variable
        pattern = r'var\s+resources\s*=\s*(\{[^}]+\});'
        match = re.search(pattern, html_content, re.IGNORECASE)
        
        if not match:
            return None
            
        resources_str = match.group(1)
        logger.debug(f"Found resources string: {resources_str}")
        
        # Simple parsing - convert to proper JSON format
        resources_str = resources_str.replace("'", '"')  # Single to double quotes
        
        import json
        resources_data = json.loads(resources_str)
        
        # Convert string keys to integers and values to integers
        result = {}
        for key, value in resources_data.items():
            try:
                resource_id = str(int(key))  # Keep as string but validate it's numeric
                resource_amount = int(value)
                result[resource_id] = resource_amount
            except (ValueError, TypeError):
                logger.warning(f"Invalid resource data: {key} = {value}")
                continue
        
        logger.debug(f"Parsed {len(result)} resource values")
        return result
        
    except Exception as e:
        logger.warning(f"Error parsing resources from script: {e}")
        return None


def clean_unicode_text(text: str) -> str:
    """
    Clean Unicode directional markers and other artifacts from text.
    
    Travian reports contain Unicode markers like U+202D (LTR override) and 
    U+202C (pop directional formatting) that need to be removed.
    
    Args:
        text: Text to clean
        
    Returns:
        Cleaned text
    """
    if not text:
        return text
    
    # Remove directional formatting characters
    cleaned = text.replace('\u202D', '').replace('\u202C', '')
    
    # Remove other common artifacts
    cleaned = cleaned.replace('\u200E', '')  # LTR mark
    cleaned = cleaned.replace('\u200F', '')  # RTL mark
    cleaned = cleaned.replace('\u2060', '')  # Word joiner
    
    # Clean up extra whitespace
    cleaned = ' '.join(cleaned.split())
    
    return cleaned