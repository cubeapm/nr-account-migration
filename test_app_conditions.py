#!/usr/bin/env python3

import json
import sys
import os

# Add the current directory to the path so we can import the library modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import library.localstore as store

# Test data
test_condition = {
    "id": 2645055,
    "type": "apm_app_metric",
    "name": "Error percentage (High)",
    "enabled": False,
    "entities": ["135069422"],
    "metric": "error_percentage",
    "condition_scope": "application",
    "terms": [
        {
            "duration": "5",
            "operator": "above",
            "threshold": "1",
            "time_function": "all",
            "priority": "critical"
        },
        {
            "duration": "5",
            "operator": "above",
            "threshold": "0.5",
            "time_function": "all",
            "priority": "warning"
        }
    ]
}

test_entities = [
    {
        "applicationId": 135069422,
        "name": "test-app",
        "entityType": "APM_APPLICATION_ENTITY"
    }
]

def test_mapAppCondition():
    """Test the mapAppCondition function"""
    from cubeapm2 import mapAppCondition
    
    qType, query, config, thresholdMultiplier = mapAppCondition(test_condition, test_entities)
    
    print("Test Results:")
    print(f"Query Type: {qType}")
    print(f"Query: {query}")
    print(f"Config: {config}")
    print(f"Threshold Multiplier: {thresholdMultiplier}")
    
    # Verify the query contains the expected elements
    assert qType == 'ERROR_PERCENTAGE', f"Expected ERROR_PERCENTAGE, got {qType}"
    assert 'service="test-app"' in query, "Query should contain service name"
    assert 'cube_apm_calls_total' in query, "Query should contain cube_apm_calls_total"
    assert 'status_code="ERROR"' in query, "Query should contain error filter"
    
    print("✅ All tests passed!")

if __name__ == '__main__':
    test_mapAppCondition()

